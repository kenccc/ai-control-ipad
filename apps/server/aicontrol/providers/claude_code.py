"""ClaudeCodeProvider -- Claude Code CLI sessions.

Claude Code persists every session to `~/.claude/projects/<path-slug>/<uuid>.jsonl`,
so sessions started outside AI Control are discoverable on exactly the same footing as
ones we launch. Continuation uses `claude --resume <session-id>`, which continues the
same session rather than starting a new one.

`thinking` content blocks are extended thinking and are dropped at the parser -- they
must never reach the API, the same rule applied to Codex's `agent_reasoning`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..models import (
    AgentSession, Capabilities, EventKind, Provider, SessionEvent, Source, Status,
)
from ..services.git_service import GitService
from .base import AgentProvider, CapabilityError, ProviderError

log = logging.getLogger("aicontrol.providers.claude_code")

CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
PROJECTS_DIR = CLAUDE_HOME / "projects"

#: A session whose file has not been written to for this long is not running.
IDLE_AFTER_SECONDS = 90.0


@dataclass
class ClaudeSessionFile:
    session_id: str
    path: Path
    cwd: Optional[str] = None
    git_branch: Optional[str] = None
    title: Optional[str] = None
    model: Optional[str] = None
    permission_mode: Optional[str] = None
    version: Optional[str] = None
    first_ts: Optional[float] = None
    last_ts: Optional[float] = None
    mtime: float = 0.0
    last_role: Optional[str] = None
    pending_tool: Optional[str] = None
    events: list[SessionEvent] = field(default_factory=list)


def _ts(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return value / 1000.0 if value > 1e11 else float(value)
    if isinstance(value, str):
        try:
            from datetime import datetime
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _describe_tool(name: str, tool_input: dict[str, Any]) -> tuple[EventKind, str]:
    if name == "Bash":
        cmd = str(tool_input.get("command", "")).strip()
        return EventKind.COMMAND, " ".join(cmd.split())[:120]
    if name in {"Edit", "Write", "NotebookEdit"}:
        target = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        return EventKind.FILE_EDIT, os.path.basename(str(target))
    if name in {"Read", "Grep", "Glob"}:
        target = (tool_input.get("file_path") or tool_input.get("pattern")
                  or tool_input.get("path") or "")
        return EventKind.TOOL, f"{name} {str(target)[:80]}".strip()
    return EventKind.TOOL, name


#: Enough of the end of a session file to determine current state. Claude Code
#: transcripts routinely reach megabytes, so status polling reads only the tail.
STATUS_TAIL_BYTES = 256 * 1024


def parse_session_file(path: Path, *, collect_events: bool = True,
                       max_events: int = 2000,
                       tail_bytes: Optional[int] = None) -> Optional[ClaudeSessionFile]:
    try:
        stat = path.stat()
    except OSError:
        return None
    info = ClaudeSessionFile(session_id=path.stem, path=path, mtime=stat.st_mtime)

    try:
        fh = path.open("r", errors="ignore")
        if tail_bytes and stat.st_size > tail_bytes:
            fh.seek(stat.st_size - tail_bytes)
            fh.readline()      # discard the partial line
    except OSError:
        return None

    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            rtype = record.get("type")
            if rtype == "ai-title":
                info.title = record.get("aiTitle") or info.title
                continue
            if rtype == "permission-mode":
                info.permission_mode = record.get("permissionMode") or info.permission_mode
                continue
            if rtype not in {"user", "assistant"}:
                continue

            info.cwd = record.get("cwd") or info.cwd
            info.git_branch = record.get("gitBranch") or info.git_branch
            info.version = record.get("version") or info.version
            ts = _ts(record.get("timestamp"))
            if ts:
                info.first_ts = info.first_ts or ts
                info.last_ts = ts
            info.last_role = rtype

            message = record.get("message") or {}
            model = message.get("model")
            if model and model != "<synthetic>":
                info.model = model
            content = message.get("content")

            if isinstance(content, str):
                if collect_events and len(info.events) < max_events:
                    info.events.append(SessionEvent(
                        kind=EventKind.USER_MESSAGE if rtype == "user" else EventKind.AGENT_MESSAGE,
                        timestamp=ts or 0.0, text=content))
                continue

            if not isinstance(content, list):
                continue

            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "thinking" or btype == "redacted_thinking":
                    # Extended thinking is not user-facing output. Dropped here so it
                    # cannot reach the API or the browser.
                    continue
                if btype == "text" and collect_events and len(info.events) < max_events:
                    info.events.append(SessionEvent(
                        kind=EventKind.USER_MESSAGE if rtype == "user" else EventKind.AGENT_MESSAGE,
                        timestamp=ts or 0.0, text=block.get("text")))
                elif btype == "tool_use":
                    name = block.get("name") or "tool"
                    kind, text = _describe_tool(name, block.get("input") or {})
                    info.pending_tool = text or name
                    if collect_events and len(info.events) < max_events:
                        info.events.append(SessionEvent(
                            kind=kind, timestamp=ts or 0.0, text=text,
                            detail={"tool": name}))
                elif btype == "tool_result":
                    info.pending_tool = None
                    if block.get("is_error") and collect_events and len(info.events) < max_events:
                        info.events.append(SessionEvent(
                            kind=EventKind.ERROR, timestamp=ts or 0.0,
                            text="tool call failed"))
    return info


def _title_from_head(path: Path, *, max_lines: int = 400) -> Optional[str]:
    """Recover a title with a short bounded read from the start of the file.

    A tail-only status read never sees the `ai-title` record. Not every session has one
    -- around one in seven does not -- so the first user message is the fallback, which
    beats showing an untitled row on the dashboard.
    """
    first_prompt: Optional[str] = None
    try:
        with path.open("r", errors="ignore") as fh:
            for index, line in enumerate(fh):
                if index >= max_lines:
                    break
                if '"ai-title"' in line:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if record.get("type") == "ai-title" and record.get("aiTitle"):
                        return record["aiTitle"]
                elif first_prompt is None and '"user"' in line:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if record.get("type") != "user" or record.get("isSidechain"):
                        continue
                    content = (record.get("message") or {}).get("content")
                    text = None
                    if isinstance(content, str):
                        text = content
                    elif isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                text = block.get("text")
                                break
                    if text:
                        cleaned = " ".join(text.split())
                        # Slash commands and tool noise make poor titles.
                        if cleaned and not cleaned.startswith(("<", "/")):
                            first_prompt = cleaned[:80]
    except OSError:
        return None
    return first_prompt


#: Resolved slugs, so the filesystem walk below runs once per project directory.
_SLUG_CACHE: dict[str, Optional[str]] = {}


def _resolve_slug(parts: list[str], base: Path) -> Optional[Path]:
    """Match slug components against real directories, greedily joining as needed.

    The slug is lossy: Claude Code replaces `/`, `_` and `.` all with `-`, so
    `-Users-ken-SpecialGuard-DEV` could be `/Users/ken/SpecialGuard/DEV` or
    `/Users/ken/SpecialGuard_DEV`. One component of the slug can therefore correspond
    to several components of the real path, which is resolved by trying the longest
    join first and backtracking.
    """
    if not parts:
        return base
    try:
        entries = [e for e in base.iterdir() if e.is_dir()]
    except OSError:
        return None

    def normalise(name: str) -> str:
        # A leading dot becomes a leading dash, which the slug's empty-component
        # filtering has already dropped -- so strip it here too.
        return name.replace("_", "-").replace(".", "-").replace(" ", "-").lstrip("-")

    # Try consuming as many slug components as possible, longest first, so
    # `SpecialGuard_DEV` wins over a bare `SpecialGuard`.
    for take in range(len(parts), 0, -1):
        target = "-".join(parts[:take])
        for entry in entries:
            if normalise(entry.name) == target:
                resolved = _resolve_slug(parts[take:], entry)
                if resolved is not None:
                    return resolved
    return None


def _slug_to_path(slug: str) -> Optional[str]:
    """`-Users-ken-SpecialGuard-DEV` -> `/Users/ken/SpecialGuard_DEV`, or None.

    Only a fallback: the `cwd` recorded inside the session file is authoritative and is
    what is used in practice. When the slug cannot be fully resolved this returns None
    rather than a partial path -- a confidently wrong working directory would attach a
    session to the wrong repository, which is worse than admitting we do not know.
    """
    if slug in _SLUG_CACHE:
        return _SLUG_CACHE[slug]
    parts = [p for p in slug.split("-") if p]
    resolved = _resolve_slug(parts, Path("/")) if parts else None
    result = str(resolved) if resolved is not None else None
    _SLUG_CACHE[slug] = result
    return result


class ClaudeCodeProvider(AgentProvider):
    provider_id = "claude_code"

    def __init__(self, git: GitService, *, max_sessions: int = 400) -> None:
        self.git = git
        self.max_sessions = max_sessions
        self._files: dict[str, ClaudeSessionFile] = {}
        self._sessions: dict[str, AgentSession] = {}
        self._parse_cache: dict[tuple[str, float], ClaudeSessionFile] = {}
        #: session id -> running `claude` process we own
        self._processes: dict[str, asyncio.subprocess.Process] = {}

    @staticmethod
    def binary() -> Optional[str]:
        return shutil.which("claude") or (
            str(Path.home() / ".local" / "bin" / "claude")
            if (Path.home() / ".local" / "bin" / "claude").exists() else None)

    # ------------------------------------------------------------------ discovery

    async def discover_sessions(self) -> list[AgentSession]:
        loop = asyncio.get_running_loop()
        paths = await loop.run_in_executor(None, self._list_paths)
        sessions: list[AgentSession] = []
        live_keys: set[tuple[str, float]] = set()
        for path in paths:
            try:
                key = (str(path), path.stat().st_mtime)
            except OSError:
                continue
            live_keys.add(key)
            info = self._parse_cache.get(key)
            if info is None:
                info = await loop.run_in_executor(
                    None, lambda p=path: parse_session_file(
                        p, collect_events=False, tail_bytes=STATUS_TAIL_BYTES))
                if info is not None:
                    self._parse_cache[key] = info
            if info is None or info.last_ts is None:
                continue
            session = self._build(info)
            await self._attach_git(session)
            self._files[info.session_id] = info
            self._sessions[info.session_id] = session
            sessions.append(session)
        for stale in set(self._parse_cache) - live_keys:
            del self._parse_cache[stale]
        return sessions

    def _list_paths(self) -> list[Path]:
        if not PROJECTS_DIR.is_dir():
            return []
        entries: list[tuple[float, Path]] = []
        for path in PROJECTS_DIR.glob("*/*.jsonl"):
            try:
                entries.append((path.stat().st_mtime, path))
            except OSError:
                continue
        entries.sort(key=lambda e: e[0], reverse=True)
        return [p for _, p in entries[: self.max_sessions]]

    def _status_for(self, info: ClaudeSessionFile) -> tuple[Status, Optional[str]]:
        age = time.time() - (info.last_ts or 0)
        owned = info.session_id in self._processes
        if age > IDLE_AFTER_SECONDS:
            if info.last_role == "user":
                # Last thing written was our prompt and nothing came back.
                return (Status.DISCONNECTED, None) if owned else (Status.IDLE, None)
            return Status.IDLE, "Waiting for your input"
        if info.pending_tool:
            lowered = info.pending_tool.lower()
            if lowered.startswith(("pytest", "npm test", "jest", "vitest")) or "test" in lowered[:20]:
                return Status.EXECUTING, f"Running {info.pending_tool.split()[0]}"
            return Status.EXECUTING, f"Running {info.pending_tool}"[:80]
        if info.last_role == "user":
            return Status.RUNNING, "Working"
        return Status.RUNNING, "Responding"

    def _build(self, info: ClaudeSessionFile) -> AgentSession:
        status, action = self._status_for(info)
        if info.title is None:
            info.title = _title_from_head(info.path)
        cwd = info.cwd or _slug_to_path(info.path.parent.name)
        return AgentSession(
            id=f"{self.provider_id}:{info.session_id}",
            source=Source.CLAUDE_CODE,
            provider=Provider.ANTHROPIC_CLAUDE,
            external_session_id=info.session_id,
            title=info.title,
            repository=Path(cwd).name if cwd else None,
            working_directory=cwd,
            branch=info.git_branch,
            worktree=cwd,
            status=status,
            current_action=action,
            created_at=info.first_ts,
            last_activity=info.last_ts,
            model=info.model,
            capabilities=Capabilities(
                read_sessions=True, read_conversation=True, stream_events=True,
                send_message=True, resume=True, terminal=True, diff=True,
                approvals=True, archive=True,
                interrupt=info.session_id in self._processes,
                terminate=info.session_id in self._processes,
            ),
            metadata={
                "sessionFile": str(info.path),
                "permissionMode": info.permission_mode,
                "claudeVersion": info.version,
                "ownedByAiControl": info.session_id in self._processes,
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
            session.diff_stats = await self.git.diff_stats(cwd)

    # ------------------------------------------------------------------- accessors

    def _sid(self, session_id: str) -> str:
        return session_id.split(":", 1)[1] if ":" in session_id else session_id

    async def get_session(self, session_id: str) -> Optional[AgentSession]:
        sid = self._sid(session_id)
        if sid not in self._sessions:
            await self.discover_sessions()
        return self._sessions.get(sid)

    async def get_capabilities(self, session_id: str) -> Capabilities:
        session = await self.get_session(session_id)
        if session is None:
            raise ProviderError(f"unknown session {session_id}")
        return session.capabilities

    async def get_conversation(self, session_id: str, *, limit: int = 500) -> list[SessionEvent]:
        sid = self._sid(session_id)
        info = self._files.get(sid)
        if info is None:
            await self.discover_sessions()
            info = self._files.get(sid)
        if info is None:
            raise ProviderError(f"unknown session {session_id}")
        loop = asyncio.get_running_loop()
        parsed = await loop.run_in_executor(
            None, lambda: parse_session_file(info.path, collect_events=True))
        return (parsed.events[-limit:] if parsed else [])

    # ----------------------------------------------------------------------- write

    async def _spawn(self, argv: list[str], cwd: str) -> asyncio.subprocess.Process:
        binary = self.binary()
        if not binary:
            raise ProviderError("claude binary not found on this Mac")
        return await asyncio.create_subprocess_exec(
            binary, *argv, cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )

    #: Modes `claude --permission-mode` accepts. "default" is our own name for
    #: "leave it alone", so it is not passed through.
    PERMISSION_MODES = frozenset({
        "acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan",
    })

    async def create_session(self, *, cwd: str, prompt: str,
                             model: Optional[str] = None,
                             permission_mode: str = "default",
                             **_: Any) -> AgentSession:
        argv = ["--print", "--output-format", "stream-json", "--verbose"]
        if permission_mode in self.PERMISSION_MODES:
            argv += ["--permission-mode", permission_mode]
        elif permission_mode not in ("default", "", None):
            raise ProviderError(f"unknown permission mode {permission_mode!r}")
        if model:
            argv += ["--model", model]
        argv.append(prompt)
        proc = await self._spawn(argv, cwd)

        # The session id arrives in the first stream-json event; we need it to bind the
        # process to a discoverable session.
        session_id = await self._read_session_id(proc)
        if session_id:
            self._processes[session_id] = proc
        asyncio.create_task(self._reap(session_id, proc))

        await self.discover_sessions()
        if session_id and session_id in self._sessions:
            return self._sessions[session_id]
        return AgentSession(
            id=f"{self.provider_id}:{session_id or 'pending'}",
            source=Source.CLAUDE_CODE, provider=Provider.ANTHROPIC_CLAUDE,
            external_session_id=session_id or "pending", working_directory=cwd,
            repository=Path(cwd).name, status=Status.RUNNING, model=model,
            capabilities=Capabilities(read_sessions=True, read_conversation=True,
                                      stream_events=True, send_message=True,
                                      resume=True, interrupt=True, terminate=True,
                                      terminal=True, diff=True),
        )

    async def _read_session_id(self, proc: asyncio.subprocess.Process,
                               timeout: float = 45.0) -> Optional[str]:
        if proc.stdout is None:
            return None
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=5)
            except asyncio.TimeoutError:
                continue
            if not line:
                return None
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid = event.get("session_id") or event.get("sessionId")
            if sid:
                return sid
        return None

    async def _reap(self, session_id: Optional[str],
                    proc: asyncio.subprocess.Process) -> None:
        try:
            if proc.stdout:
                async for _ in proc.stdout:  # drain so the pipe never blocks the child
                    pass
            await proc.wait()
        finally:
            if session_id:
                self._processes.pop(session_id, None)

    async def send_message(self, session_id: str, message: str) -> None:
        session = await self.get_session(session_id)
        if session is None:
            raise ProviderError(f"unknown session {session_id}")
        cwd = session.working_directory
        if not cwd:
            raise ProviderError("session has no working directory")
        sid = self._sid(session_id)
        # --resume continues this exact session rather than starting a new one.
        argv = ["--resume", sid, "--print", "--output-format", "stream-json",
                "--verbose", message]
        proc = await self._spawn(argv, cwd)
        self._processes[sid] = proc
        asyncio.create_task(self._reap(sid, proc))

    async def resume(self, session_id: str) -> None:
        await self.send_message(session_id, "Continue.")

    async def interrupt(self, session_id: str) -> None:
        sid = self._sid(session_id)
        proc = self._processes.get(sid)
        if proc is None or proc.returncode is not None:
            raise CapabilityError(
                "This Claude Code session is not running under AI Control, so it "
                "cannot be interrupted from here.")
        proc.terminate()

    async def terminate(self, session_id: str) -> None:
        sid = self._sid(session_id)
        proc = self._processes.pop(sid, None)
        if proc is None or proc.returncode is not None:
            raise CapabilityError("This session is not running under AI Control.")
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()

    async def health(self) -> dict[str, Any]:
        binary = self.binary()
        version = None
        if binary:
            try:
                proc = await asyncio.create_subprocess_exec(
                    binary, "--version", stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL)
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
                version = out.decode().strip() or None
            except (OSError, asyncio.TimeoutError):
                version = None
        return {
            "providerId": self.provider_id,
            "ok": binary is not None,
            "binary": binary,
            "version": version,
            "projectsDir": str(PROJECTS_DIR),
            "projectsDirPresent": PROJECTS_DIR.is_dir(),
            "ownedProcesses": len(self._processes),
        }
