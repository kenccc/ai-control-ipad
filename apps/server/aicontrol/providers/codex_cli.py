"""CodexCLIProvider -- Codex CLI sessions, including ones started outside AI Control.

Externally-created CLI sessions are discovered from the same rollout store as desktop
ones and separated by `originator` (`codex-tui`, `codex_exec`, ...). Sessions AI
Control starts itself are hosted in our own app-server process, so they additionally
support interrupt, steering and approval routing.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from ..codex import rollout as rollout_mod
from ..models import AgentSession, Capabilities, Provider, Source, Status
from .base import CapabilityError, ProviderError
from .codex_base import CodexRolloutProvider

log = logging.getLogger("aicontrol.providers.codex_cli")


class CodexCLIProvider(CodexRolloutProvider):
    provider_id = "codex_cli"
    # Unrecognised originators are surfaced here as plain "Codex" rather than being
    # attributed to the desktop app.
    claims = frozenset({Source.CODEX_CLI, Source.CODEX_UNKNOWN})

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        #: Threads this process started or resumed, and therefore hosts.
        self._owned: set[str] = set()

    def _capabilities_for(self, state: rollout_mod.RolloutState) -> Capabilities:
        # A turn in flight belongs to whichever process opened it. Unless that is us,
        # the session is read-only until the turn ends.
        turn_open = state.active_turn_id is not None
        caps = Capabilities(
            read_sessions=True,
            read_conversation=True,
            stream_events=True,
            diff=True,
            archive=True,
            fork=True,
            terminal=True,
            approvals=True,
            resume=not turn_open,
            send_message=not turn_open,
            terminate=True,
            interrupt=turn_open,
            steer=turn_open,
        )
        if turn_open:
            caps.write_blocked_reason = (
                "A turn is in progress. You can interrupt it, or wait for it to finish.")
        return caps

    async def create_session(self, *, cwd: str, prompt: Optional[str] = None,
                             model: Optional[str] = None,
                             approval_policy: Optional[str] = None,
                             **_: Any) -> AgentSession:
        server = await self.app_server()
        result = await server.start_thread(cwd=cwd, model=model,
                                           approval_policy=approval_policy)
        thread = result.get("thread") or result
        thread_id = thread.get("id") or thread.get("threadId")
        if not thread_id:
            raise ProviderError("codex app-server did not return a thread id")
        self._owned.add(thread_id)
        if prompt:
            await server.start_turn(thread_id, prompt, cwd=cwd, model=model)

        await self._scan()
        session = self._sessions.get(thread_id)
        if session is None:
            session = AgentSession(
                id=f"{self.provider_id}:{thread_id}",
                source=Source.CODEX_CLI,
                provider=Provider.OPENAI_CODEX,
                external_session_id=thread_id,
                working_directory=cwd,
                status=Status.RUNNING,
                model=model,
                capabilities=self._capabilities_for(rollout_mod.RolloutState()),
            )
            self._sessions[thread_id] = session
        return session

    async def send_message(self, session_id: str, message: str) -> None:
        session = await self.get_session(session_id)
        if session is None:
            raise ProviderError(f"unknown session {session_id}")
        server = await self.app_server()
        thread_id = session.external_session_id
        turn_id = session.metadata.get("activeTurnId")

        if turn_id and thread_id not in self._owned:
            raise CapabilityError(
                "This Codex CLI session has a turn in progress in a process AI Control "
                "does not own. Wait for it to finish, or interrupt it where it runs.")

        if turn_id and thread_id in self._owned:
            try:
                await server.steer_turn(thread_id, turn_id, message)
                return
            except Exception as exc:
                log.warning("steer failed for %s: %s", thread_id, exc)

        await server.resume_thread(thread_id,
                                   path=session.metadata.get("rolloutPath"),
                                   cwd=session.working_directory)
        self._owned.add(thread_id)
        await server.start_turn(thread_id, message, cwd=session.working_directory)

    async def interrupt(self, session_id: str) -> None:
        session = await self.get_session(session_id)
        if session is None:
            raise ProviderError(f"unknown session {session_id}")
        turn_id = session.metadata.get("activeTurnId")
        if not turn_id:
            raise CapabilityError("No turn is currently running in this session.")
        server = await self.app_server()
        await server.interrupt_turn(session.external_session_id, turn_id)

    async def resume(self, session_id: str) -> None:
        session = await self.get_session(session_id)
        if session is None:
            raise ProviderError(f"unknown session {session_id}")
        server = await self.app_server()
        await server.resume_thread(session.external_session_id,
                                   path=session.metadata.get("rolloutPath"),
                                   cwd=session.working_directory)
        self._owned.add(session.external_session_id)

    async def terminate(self, session_id: str) -> None:
        session = await self.get_session(session_id)
        if session is None:
            raise ProviderError(f"unknown session {session_id}")
        server = await self.app_server()
        try:
            await server.request("thread/unsubscribe",
                                 {"threadId": session.external_session_id})
        finally:
            self._owned.discard(session.external_session_id)
