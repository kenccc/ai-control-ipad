"""Shared machinery for the two Codex-backed providers.

Codex Desktop and Codex CLI write to the same rollout store, so discovery, transcript
parsing and git resolution are identical; what differs is which `originator` values a
provider claims and what it is allowed to do to a session. Keeping the split here --
rather than in two copies -- is what makes it impossible for a CLI session to leak
into the Codex App list.
"""

from __future__ import annotations

import asyncio
import logging
import time
import unicodedata
from pathlib import Path
from typing import Any, Optional

from ..codex.appserver import CodexAppServer, daemon_status, resolve_codex_binary
from ..codex.globalstate import CodexGlobalState
from ..codex.session_index import CodexSessionIndex
from ..codex import rollout as rollout_mod
from ..models import (
    AgentSession, Capabilities, DiffStats, GitState, Provider, SessionEvent, Source, Status,
)
from ..services.git_service import GitService
from .base import AgentProvider, CapabilityError, ProviderError

log = logging.getLogger("aicontrol.providers.codex")

#: Codex records titles containing invisible joiners; strip them so the UI is clean.
_INVISIBLE = {"Cf", "Cc"}


def clean_title(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    stripped = "".join(ch for ch in text if unicodedata.category(ch) not in _INVISIBLE)
    stripped = " ".join(stripped.split())
    return stripped[:90] or None


class CodexRolloutProvider(AgentProvider):
    """Discovery and read access over the Codex rollout store."""

    #: Which originator classifications this provider owns.
    claims: frozenset[Source] = frozenset()

    def __init__(self, git: GitService, *, max_sessions: int = 400,
                 app_server: Optional[CodexAppServer] = None) -> None:
        self.git = git
        self.max_sessions = max_sessions
        self.global_state = CodexGlobalState()
        self.session_index = CodexSessionIndex()
        self._heads: dict[str, rollout_mod.RolloutHead] = {}
        self._sessions: dict[str, AgentSession] = {}
        self._app_server = app_server
        self._app_server_lock = asyncio.Lock()
        self._daemon_cache: Optional[tuple[float, dict[str, Any]]] = None

    # ----------------------------------------------------------------- app-server

    async def app_server(self) -> CodexAppServer:
        async with self._app_server_lock:
            if self._app_server is None:
                self._app_server = CodexAppServer()
            if not self._app_server.running:
                await self._app_server.start()
            return self._app_server

    def daemon_info(self, *, ttl: float = 20.0) -> dict[str, Any]:
        now = time.time()
        if self._daemon_cache and now - self._daemon_cache[0] < ttl:
            return self._daemon_cache[1]
        info = daemon_status()
        self._daemon_cache = (now, info)
        return info

    @property
    def shared_daemon_available(self) -> bool:
        """Whether a shared app-server hosts live threads from other Codex clients.

        This is the single condition that decides whether write control can reach a
        session that is currently open elsewhere. It is observed, never assumed.
        """
        return bool(self.daemon_info().get("running"))

    # ------------------------------------------------------------------ discovery

    async def discover_sessions(self) -> list[AgentSession]:
        return await self._scan()

    async def _scan(self) -> list[AgentSession]:
        loop = asyncio.get_running_loop()
        heads = await loop.run_in_executor(None, self._read_heads)
        assignments = self.global_state.thread_assignments()
        hints = self.global_state.workspace_root_hints()
        titles = self.global_state.thread_titles()
        projects = {p.id: p for p in self.global_state.projects()}

        sessions: list[AgentSession] = []
        for head in heads:
            state = await loop.run_in_executor(
                None, lambda h=head: rollout_mod.parse_rollout(h.path, collect_events=False))
            session = self._build_session(head, state, assignments, hints, titles, projects)
            await self._attach_git(session)
            sessions.append(session)
            self._heads[head.thread_id] = head
            self._sessions[head.thread_id] = session
        return sessions

    def _read_heads(self) -> list[rollout_mod.RolloutHead]:
        heads: list[rollout_mod.RolloutHead] = []
        for path in rollout_mod.iter_rollouts():
            head = rollout_mod.read_head(path)
            if head is None or head.source not in self.claims:
                continue
            heads.append(head)
            if len(heads) >= self.max_sessions:
                break
        return heads

    def _build_session(self, head: rollout_mod.RolloutHead,
                       state: rollout_mod.RolloutState,
                       assignments: dict, hints: dict, titles: dict,
                       projects: dict) -> AgentSession:
        cwd = state.cwd or head.cwd
        assignment = assignments.get(head.thread_id)
        if assignment and assignment.cwd and not cwd:
            cwd = assignment.cwd
        worktree = hints.get(head.thread_id) or cwd

        project = projects.get(assignment.project_id) if assignment else None
        repository = project.name if project else (Path(cwd).name if cwd else None)

        title = clean_title(
            self.session_index.title(head.thread_id)
            or titles.get(head.thread_id)
            or state.title
        )

        return AgentSession(
            id=f"{self.provider_id}:{head.thread_id}",
            source=head.source,
            provider=Provider.OPENAI_CODEX,
            external_session_id=head.thread_id,
            title=title,
            repository=repository,
            working_directory=cwd,
            worktree=worktree if worktree != cwd else worktree,
            branch=head.git_branch,
            status=state.status,
            current_action=state.current_action,
            created_at=head.created_at,
            last_activity=state.last_activity or head.mtime,
            model=state.model,
            capabilities=self._capabilities_for(state),
            metadata={
                "originator": head.originator,
                "sourceField": head.source_field,
                "cliVersion": head.cli_version,
                "rolloutPath": str(head.path),
                "activeTurnId": state.active_turn_id,
                "codexProjectId": assignment.project_id if assignment else None,
                "codexProjectName": project.name if project else None,
                "editedFileCount": len(state.changed_files),
                "approvalPolicy": state.approval_policy,
                "sandboxPolicy": state.sandbox_policy,
                "workspaceRoots": state.workspace_roots,
                "sessionSha": head.git_sha,
                "originUrl": head.git_origin_url,
            },
        )

    async def _attach_git(self, session: AgentSession) -> None:
        cwd = session.working_directory
        if not cwd or not Path(cwd).is_dir():
            return
        state = await self.git.status(cwd)
        if state:
            session.git_status = state
            session.metadata["currentBranch"] = state.branch
            if session.branch is None:
                session.branch = state.branch
            origin = state.origin_url or session.metadata.get("originUrl")
            if origin and not session.repository:
                session.repository = origin.rstrip("/").rstrip(".git").split("/")[-1]
            session.diff_stats = await self.git.diff_stats(cwd)

    # --------------------------------------------------------------- capabilities

    def _capabilities_for(self, state: rollout_mod.RolloutState) -> Capabilities:
        raise NotImplementedError

    async def get_session(self, session_id: str) -> Optional[AgentSession]:
        thread_id = self._thread_id(session_id)
        cached = self._sessions.get(thread_id)
        if cached is None:
            await self._scan()
            cached = self._sessions.get(thread_id)
        return cached

    async def get_capabilities(self, session_id: str) -> Capabilities:
        session = await self.get_session(session_id)
        if session is None:
            raise ProviderError(f"unknown session {session_id}")
        return session.capabilities

    async def get_conversation(self, session_id: str, *, limit: int = 500) -> list[SessionEvent]:
        thread_id = self._thread_id(session_id)
        head = self._heads.get(thread_id)
        if head is None:
            await self._scan()
            head = self._heads.get(thread_id)
        if head is None:
            raise ProviderError(f"unknown session {session_id}")
        loop = asyncio.get_running_loop()
        state = await loop.run_in_executor(
            None, lambda: rollout_mod.parse_rollout(head.path, collect_events=True))
        return state.events[-limit:]

    def _thread_id(self, session_id: str) -> str:
        return session_id.split(":", 1)[1] if ":" in session_id else session_id

    async def health(self) -> dict[str, Any]:
        binary = resolve_codex_binary()
        return {
            "providerId": self.provider_id,
            "ok": binary is not None,
            "binary": str(binary.path) if binary else None,
            "version": binary.version if binary else None,
            "desktopBundled": binary.is_desktop_bundled if binary else False,
            "sessionsDir": str(rollout_mod.SESSIONS_DIR),
            "sessionsDirPresent": rollout_mod.SESSIONS_DIR.is_dir(),
            "globalStateAvailable": self.global_state.available,
            "sharedDaemon": self.daemon_info(),
        }
