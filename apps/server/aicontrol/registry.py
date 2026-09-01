"""Session orchestrator: periodic reconciliation, deduplication, event emission.

Sessions are discovered, never assumed to have been created here -- the whole point of
the product is that a Codex Desktop task you started by hand shows up on the iPad. So
reconciliation polls every provider on an interval, diffs against the previous
snapshot, and emits the deltas.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from .db import Database
from .events import EventBus
from .models import AgentSession, Source, Status
from .providers.base import AgentProvider, CapabilityError, ProviderError

log = logging.getLogger("aicontrol.registry")


class SessionRegistry:
    def __init__(self, providers: list[AgentProvider], db: Database, bus: EventBus, *,
                 interval: float = 2.0) -> None:
        self.providers = {p.provider_id: p for p in providers}
        self.db = db
        self.bus = bus
        self.interval = interval
        self._sessions: dict[str, AgentSession] = {}
        self._task: Optional[asyncio.Task[None]] = None
        self._lock = asyncio.Lock()
        self.last_reconcile: Optional[float] = None
        self.last_error: Optional[str] = None

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                log.exception("reconcile failed")
            await asyncio.sleep(self.interval)

    # --------------------------------------------------------------- reconciliation

    async def reconcile(self) -> list[AgentSession]:
        async with self._lock:
            discovered: list[AgentSession] = []
            for provider in self.providers.values():
                try:
                    discovered.extend(await provider.discover_sessions())
                except Exception as exc:
                    # Several exception types stringify to "" (asyncio.TimeoutError
                    # among them), so include the class and a traceback -- a bare
                    # "discovery failed: " tells you nothing at 3am.
                    log.warning("provider %s discovery failed: %s: %s",
                                provider.provider_id, type(exc).__name__, exc,
                                exc_info=True)

            merged = self._deduplicate(discovered)
            previous = self._sessions
            self._sessions = {s.id: s for s in merged}
            self.last_reconcile = time.time()
            self.last_error = None

            self._apply_persisted_fields()
            self._emit_deltas(previous, self._sessions)

            for session in merged:
                self.db.upsert_session(session)
            return merged

    def _deduplicate(self, sessions: list[AgentSession]) -> list[AgentSession]:
        """Collapse sessions that several providers expose as the same underlying task.

        Matching is on stable identity only -- the external session id, or an explicit
        import mapping. Titles are never used: two agents given the same instruction
        produce near-identical titles and are still two separate sessions.
        """
        by_external: dict[tuple[str, str], AgentSession] = {}
        ordered: list[AgentSession] = []

        for session in sessions:
            key = (session.provider.value, session.external_session_id)
            existing = by_external.get(key)
            if existing is None:
                by_external[key] = session
                ordered.append(session)
                continue
            winner, loser = self._prefer(existing, session)
            if winner is not existing:
                ordered[ordered.index(existing)] = winner
                by_external[key] = winner
            winner.metadata.setdefault("mergedFrom", []).append({
                "provider": loser.id.split(":", 1)[0],
                "source": loser.source.value,
            })

        ordered.sort(key=lambda s: (not s.status.is_active, -(s.last_activity or 0)))
        return ordered

    @staticmethod
    def _prefer(a: AgentSession, b: AgentSession) -> tuple[AgentSession, AgentSession]:
        """Between two views of one session, keep the one that can do more."""
        def score(s: AgentSession) -> tuple[int, float]:
            caps = s.capabilities
            return (sum([caps.send_message, caps.interrupt, caps.steer, caps.terminal,
                         caps.approvals]), s.last_activity or 0)
        return (a, b) if score(a) >= score(b) else (b, a)

    def _apply_persisted_fields(self) -> None:
        """Re-attach things the user set that providers cannot know about."""
        stored = self.db.stored_sessions()
        for session_id, session in self._sessions.items():
            row = stored.get(session_id)
            if row is None:
                continue
            if row["forgejo_issue"] is not None:
                session.forgejo_issue = row["forgejo_issue"]
            if row["user_label"]:
                session.metadata["userLabel"] = row["user_label"]
            if row["archived"]:
                session.archived = True

    def _emit_deltas(self, before: dict[str, AgentSession],
                     after: dict[str, AgentSession]) -> None:
        for session_id, session in after.items():
            old = before.get(session_id)
            if old is None:
                # A session nobody told us about -- this is the external-discovery path.
                self.bus.publish("session.discovered", sessionId=session_id,
                                 session=session.to_dict())
                self.db.add_activity(session_id, "discovered",
                                     f"{session.source.label}: {session.title or 'session'}")
                continue
            if old.status != session.status or old.current_action != session.current_action:
                self.bus.publish("session.status", sessionId=session_id,
                                 status=session.status.value,
                                 action=session.current_action,
                                 isActive=session.status.is_active)
                if old.status != session.status:
                    self.db.add_activity(session_id, "status",
                                         f"{session.source.label}: {session.status.value}")
                if session.status is Status.COMPLETED:
                    self.bus.publish("session.completed", sessionId=session_id)
                elif session.status is Status.FAILED:
                    self.bus.publish("session.failed", sessionId=session_id)
                elif session.status is Status.WAITING_FOR_PERMISSION:
                    self.bus.publish("session.permission", sessionId=session_id,
                                     action=session.current_action)
            if _git_changed(old, session):
                self.bus.publish("session.git_changed", sessionId=session_id,
                                 diffStats=session.diff_stats.to_dict()
                                 if session.diff_stats else None)

        for session_id in before.keys() - after.keys():
            self.bus.publish("session.removed", sessionId=session_id)

    # ------------------------------------------------------------------- accessors

    def sessions(self, *, include_archived: bool = False) -> list[AgentSession]:
        return [s for s in self._sessions.values()
                if include_archived or not s.archived]

    def get(self, session_id: str) -> Optional[AgentSession]:
        return self._sessions.get(session_id)

    def provider_for(self, session_id: str) -> Optional[AgentProvider]:
        session = self._sessions.get(session_id)
        if session is None:
            return None
        return self.providers.get(session_id.split(":", 1)[0])

    def require(self, session_id: str) -> tuple[AgentSession, AgentProvider]:
        session = self._sessions.get(session_id)
        if session is None:
            raise ProviderError(f"unknown session {session_id}")
        provider = self.providers.get(session_id.split(":", 1)[0])
        if provider is None:
            raise ProviderError(f"no provider for {session_id}")
        return session, provider

    async def health(self) -> dict[str, Any]:
        providers = {}
        for pid, provider in self.providers.items():
            try:
                providers[pid] = await provider.health()
            except Exception as exc:
                providers[pid] = {"providerId": pid, "ok": False, "error": str(exc)}
        counts: dict[str, int] = {}
        for session in self._sessions.values():
            counts[session.source.value] = counts.get(session.source.value, 0) + 1
        return {
            "providers": providers,
            "sessionCounts": counts,
            "activeSessions": sum(1 for s in self._sessions.values() if s.status.is_active),
            "lastReconcile": self.last_reconcile,
            "lastError": self.last_error,
        }


def _git_changed(old: AgentSession, new: AgentSession) -> bool:
    a = old.diff_stats.to_dict() if old.diff_stats else None
    b = new.diff_stats.to_dict() if new.diff_stats else None
    return a != b
