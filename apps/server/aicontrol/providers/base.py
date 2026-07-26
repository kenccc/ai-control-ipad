"""The provider contract every agent source implements."""

from __future__ import annotations

import abc
from typing import Any, AsyncIterator, Optional

from ..models import AgentSession, Capabilities, SessionEvent


class ProviderError(RuntimeError):
    """A provider could not complete an operation."""


class CapabilityError(ProviderError):
    """The operation is not supported for this session.

    Raised rather than faked. The API turns this into a 409 carrying the provider's
    own explanation, which the UI shows verbatim instead of inventing a message.
    """


class AgentProvider(abc.ABC):
    provider_id: str

    @abc.abstractmethod
    async def discover_sessions(self) -> list[AgentSession]:
        """All sessions this provider can see, including ones it did not create."""

    @abc.abstractmethod
    async def get_session(self, session_id: str) -> Optional[AgentSession]:
        ...

    @abc.abstractmethod
    async def get_capabilities(self, session_id: str) -> Capabilities:
        ...

    async def get_conversation(self, session_id: str, *, limit: int = 500) -> list[SessionEvent]:
        raise CapabilityError("Reading the conversation is not supported for this session.")

    async def create_session(self, **options: Any) -> AgentSession:
        raise CapabilityError("Creating a session is not supported by this provider.")

    async def send_message(self, session_id: str, message: str) -> None:
        raise CapabilityError("Sending a message is not supported for this session.")

    async def interrupt(self, session_id: str) -> None:
        raise CapabilityError("Interrupting is not supported for this session.")

    async def resume(self, session_id: str) -> None:
        raise CapabilityError("Resuming is not supported for this session.")

    async def terminate(self, session_id: str) -> None:
        raise CapabilityError("Terminating is not supported for this session.")

    async def stream_events(self, session_id: str) -> AsyncIterator[SessionEvent]:
        raise CapabilityError("Streaming is not supported for this session.")
        yield  # pragma: no cover - makes this an async generator

    async def health(self) -> dict[str, Any]:
        return {"providerId": self.provider_id, "ok": True}
