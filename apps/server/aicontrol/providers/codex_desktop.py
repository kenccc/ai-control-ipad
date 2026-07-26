"""CodexDesktopProvider -- sessions belonging to the Codex desktop application.

Membership is decided by the `originator` string the desktop app writes into every
rollout it creates, corroborated by `thread-project-assignments` in the app's own
state file. It is never decided by the protocol's `source` field, which reports
`vscode` for desktop threads and so cannot distinguish them from the VS Code
extension (see docs/integration-research.md section 2).

Read access is always available. Write access is advertised only when it can actually
be performed, and the three cases are kept distinct:

* thread idle on disk -> `thread/resume` continues *the same thread id*;
* thread live inside a shared app-server we can reach -> rejoin, steer, interrupt;
* thread live inside the desktop app with no shared daemon -> writes are advertised
  as unavailable, with the reason shown to the user. We do not start a CLI
  conversation and present it as the desktop thread.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from ..codex import rollout as rollout_mod
from ..models import AgentSession, Capabilities, CodexProject, Source, Status
from .base import CapabilityError, ProviderError
from .codex_base import CodexRolloutProvider

log = logging.getLogger("aicontrol.providers.codex_desktop")

_NO_SHARED_DAEMON = (
    "This thread is currently open in the Codex desktop app. Continuing it from here "
    "needs the shared Codex app-server daemon, which is not installed. "
    "Run ./scripts/enable-codex-daemon.sh on the Mac, or reply in the desktop app."
)
_DESKTOP_ONLY_APPROVAL = "Approval required on Mac"


class CodexDesktopProvider(CodexRolloutProvider):
    provider_id = "codex_desktop"
    claims = frozenset({Source.CODEX_DESKTOP})

    def _capabilities_for(self, state: rollout_mod.RolloutState) -> Capabilities:
        caps = Capabilities(
            read_sessions=True,
            read_conversation=True,
            stream_events=True,
            diff=True,
            archive=True,
            fork=True,
            # A terminal belongs to a process we own. We do not own the desktop app's.
            terminal=False,
        )

        thread_is_live = state.status.is_active and state.active_turn_id is not None

        if not thread_is_live:
            # Idle on disk: thread/resume reattaches to this exact thread id, so a
            # follow-up genuinely continues the same conversation.
            caps.resume = True
            caps.send_message = True
            caps.interrupt = False
            caps.steer = False
            return caps

        if self.shared_daemon_available:
            caps.resume = True
            caps.send_message = True
            caps.interrupt = True
            caps.steer = True
            caps.approvals = True
            return caps

        caps.write_blocked_reason = _NO_SHARED_DAEMON
        return caps

    # ------------------------------------------------------------------ projects

    def get_projects(self) -> list[CodexProject]:
        """The desktop app's real Projects sidebar, not directory names."""
        return self.global_state.projects()

    def sessions_for_project(self, project_id: str) -> list[AgentSession]:
        return [s for s in self._sessions.values()
                if s.metadata.get("codexProjectId") == project_id]

    # --------------------------------------------------------------------- write

    async def send_message(self, session_id: str, message: str) -> None:
        session = await self.get_session(session_id)
        if session is None:
            raise ProviderError(f"unknown session {session_id}")
        caps = session.capabilities
        if not caps.send_message:
            raise CapabilityError(caps.write_blocked_reason or
                                  "Continuing this Codex Desktop session is not available.")

        server = await self.app_server()
        thread_id = session.external_session_id
        turn_id = session.metadata.get("activeTurnId")

        if caps.steer and turn_id:
            # A turn is in flight; steer it rather than queueing a new one. The
            # expectedTurnId precondition means this fails loudly if the turn moved on.
            try:
                await server.steer_turn(thread_id, turn_id, message)
                return
            except Exception as exc:
                log.warning("steer failed for %s, falling back to turn/start: %s",
                            thread_id, exc)

        await server.resume_thread(
            thread_id, path=session.metadata.get("rolloutPath"),
            cwd=session.working_directory)
        await server.start_turn(thread_id, message, cwd=session.working_directory)

    async def interrupt(self, session_id: str) -> None:
        session = await self.get_session(session_id)
        if session is None:
            raise ProviderError(f"unknown session {session_id}")
        if not session.capabilities.interrupt:
            raise CapabilityError(
                session.capabilities.write_blocked_reason or
                "Interrupting a Codex Desktop session requires the shared app-server "
                "daemon. Interrupt it in the desktop app instead.")
        turn_id = session.metadata.get("activeTurnId")
        if not turn_id:
            raise CapabilityError("No turn is currently running in this session.")
        server = await self.app_server()
        await server.interrupt_turn(session.external_session_id, turn_id)

    async def resume(self, session_id: str) -> None:
        session = await self.get_session(session_id)
        if session is None:
            raise ProviderError(f"unknown session {session_id}")
        if not session.capabilities.resume:
            raise CapabilityError("This session cannot be resumed from AI Control.")
        server = await self.app_server()
        await server.resume_thread(session.external_session_id,
                                   path=session.metadata.get("rolloutPath"),
                                   cwd=session.working_directory)

    async def create_session(self, **options: Any) -> AgentSession:
        # Codex exposes no supported way to make the *desktop application* open a new
        # thread. Saying so plainly is better than starting a CLI thread and calling
        # it a desktop session.
        raise CapabilityError(
            "Codex Desktop does not expose a supported way to start a new session in "
            "the app from outside it. Start a Codex CLI session instead, or start the "
            "task in the desktop app on your Mac.")

    async def health(self) -> dict[str, Any]:
        base = await super().health()
        base.update({
            "projects": len(self.get_projects()),
            "mobilePaired": self.global_state.mobile_paired(),
            "writeControl": "shared-daemon" if self.shared_daemon_available
                            else "resume-on-idle-only",
        })
        return base
