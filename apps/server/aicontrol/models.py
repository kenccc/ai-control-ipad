"""Unified agent-session model shared by every provider.

The three agent sources are kept strictly distinct all the way to the UI. Nothing in
this module ever coerces one source into another; an unrecognised Codex originator
becomes CODEX_UNKNOWN rather than being guessed into CODEX_DESKTOP.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


class Source(str, enum.Enum):
    CODEX_DESKTOP = "codex_desktop"
    CODEX_CLI = "codex_cli"
    CODEX_UNKNOWN = "codex_unknown"
    CLAUDE_CODE = "claude_code"

    @property
    def label(self) -> str:
        return {
            Source.CODEX_DESKTOP: "Codex App",
            Source.CODEX_CLI: "Codex CLI",
            Source.CODEX_UNKNOWN: "Codex",
            Source.CLAUDE_CODE: "Claude Code",
        }[self]


class Provider(str, enum.Enum):
    OPENAI_CODEX = "openai_codex"
    ANTHROPIC_CLAUDE = "anthropic_claude"


class Status(str, enum.Enum):
    RUNNING = "running"
    THINKING = "thinking"
    EXECUTING = "executing"
    EDITING = "editing"
    WAITING = "waiting"
    WAITING_FOR_PERMISSION = "waiting_for_permission"
    IDLE = "idle"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    DISCONNECTED = "disconnected"
    UNKNOWN = "unknown"

    @property
    def is_active(self) -> bool:
        return self in _ACTIVE_STATUSES


_ACTIVE_STATUSES = {
    Status.RUNNING,
    Status.THINKING,
    Status.EXECUTING,
    Status.EDITING,
    Status.WAITING,
    Status.WAITING_FOR_PERMISSION,
}


@dataclass
class Capabilities:
    """What a provider can actually do for one specific session.

    Advertised per session, not per provider: two Codex Desktop threads can differ
    because one is live inside the desktop app and the other is idle on disk. The UI
    renders controls from this and nothing else, so a button never appears for an
    operation that would fail.
    """

    read_sessions: bool = False
    read_conversation: bool = False
    stream_events: bool = False
    send_message: bool = False
    interrupt: bool = False
    steer: bool = False
    resume: bool = False
    terminate: bool = False
    terminal: bool = False
    diff: bool = False
    approvals: bool = False
    fork: bool = False
    archive: bool = False

    #: Human-readable reason a write capability is off, shown verbatim in the UI.
    write_blocked_reason: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DiffStats:
    files_changed: int = 0
    insertions: int = 0
    deletions: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GitState:
    branch: Optional[str] = None
    sha: Optional[str] = None
    origin_url: Optional[str] = None
    modified: int = 0
    added: int = 0
    deleted: int = 0
    untracked: int = 0
    ahead: int = 0
    behind: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AgentSession:
    id: str
    source: Source
    provider: Provider
    external_session_id: str

    title: Optional[str] = None

    repository: Optional[str] = None
    working_directory: Optional[str] = None
    branch: Optional[str] = None
    worktree: Optional[str] = None

    forgejo_issue: Optional[int] = None

    status: Status = Status.UNKNOWN
    current_action: Optional[str] = None

    created_at: Optional[float] = None
    last_activity: Optional[float] = None

    model: Optional[str] = None
    archived: bool = False

    capabilities: Capabilities = field(default_factory=Capabilities)
    git_status: Optional[GitState] = None
    diff_stats: Optional[DiffStats] = None

    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source.value,
            "sourceLabel": self.source.label,
            "provider": self.provider.value,
            "externalSessionId": self.external_session_id,
            "title": self.title,
            "repository": self.repository,
            "workingDirectory": self.working_directory,
            "branch": self.branch,
            "worktree": self.worktree,
            "forgejoIssue": self.forgejo_issue,
            "status": self.status.value,
            "isActive": self.status.is_active,
            "currentAction": self.current_action,
            "createdAt": self.created_at,
            "lastActivity": self.last_activity,
            "model": self.model,
            "archived": self.archived,
            "capabilities": self.capabilities.to_dict(),
            "gitStatus": self.git_status.to_dict() if self.git_status else None,
            "diffStats": self.diff_stats.to_dict() if self.diff_stats else None,
            "metadata": self.metadata,
        }


class EventKind(str, enum.Enum):
    USER_MESSAGE = "user_message"
    AGENT_MESSAGE = "agent_message"
    COMMAND = "command"
    FILE_EDIT = "file_edit"
    TOOL = "tool"
    PERMISSION_REQUEST = "permission_request"
    ERROR = "error"
    TURN_START = "turn_start"
    TURN_END = "turn_end"
    TURN_ABORTED = "turn_aborted"
    SYSTEM = "system"


@dataclass
class SessionEvent:
    """One user-facing item in a session transcript.

    Model reasoning is never represented here. `agent_reasoning` records are dropped
    at the parser, so hidden chain-of-thought cannot reach the API or the browser.
    """

    kind: EventKind
    timestamp: float
    text: Optional[str] = None
    turn_id: Optional[str] = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "timestamp": self.timestamp,
            "text": self.text,
            "turnId": self.turn_id,
            "detail": self.detail,
        }


@dataclass
class CodexProject:
    id: str
    name: str
    root_paths: list[str]
    kind: str = "local"
    created_at: Optional[float] = None
    updated_at: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "rootPaths": self.root_paths,
            "kind": self.kind,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }
