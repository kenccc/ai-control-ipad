"""Reader for the Codex rollout store (`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`).

Every Codex client -- the desktop app, the CLI, the VS Code extension -- appends to
this store as it works. Reading it gives a live, read-only view of a session no matter
which process owns it, which is why it is AI Control's floor for Codex Desktop status
rather than an RPC subscription (see docs/integration-research.md section 4).

Two things this module is careful about:

* `agent_reasoning` payloads are dropped. They are model reasoning, not user-facing
  output, and must never reach the API.
* The originating client is read from `session_meta.originator`, never from `source`.
  Codex Desktop reports `source: "vscode"`, identically to the VS Code extension.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

from ..models import EventKind, SessionEvent, Source, Status

CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
SESSIONS_DIR = CODEX_HOME / "sessions"

_ROLLOUT_RE = re.compile(
    r"^rollout-(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-(?P<id>[0-9a-f-]{36})\.jsonl$"
)

#: Maps the `originator` string a Codex client stamps into `session_meta` onto our
#: source taxonomy. Matched as a lowercase prefix, because builds vary the suffix
#: (`Codex Desktop`, `codex_work_desktop`). Anything unmatched stays CODEX_UNKNOWN --
#: we would rather show "Codex" than mislabel a session's origin.
_ORIGINATOR_PREFIXES: tuple[tuple[str, Source], ...] = (
    ("codex desktop", Source.CODEX_DESKTOP),
    ("codex_desktop", Source.CODEX_DESKTOP),
    ("codex_work_desktop", Source.CODEX_DESKTOP),
    ("codex-desktop", Source.CODEX_DESKTOP),
    ("codex-tui", Source.CODEX_CLI),
    ("codex_cli", Source.CODEX_CLI),
    ("codex-cli", Source.CODEX_CLI),
    ("codex_exec", Source.CODEX_CLI),
    ("codex-exec", Source.CODEX_CLI),
)


def classify_originator(originator: Optional[str]) -> Source:
    if not originator:
        return Source.CODEX_UNKNOWN
    low = originator.strip().lower()
    for prefix, source in _ORIGINATOR_PREFIXES:
        if low.startswith(prefix):
            return source
    return Source.CODEX_UNKNOWN


@dataclass
class RolloutHead:
    """The cheap half of a rollout file: its first `session_meta` line."""

    thread_id: str
    path: Path
    originator: Optional[str]
    source_field: Optional[str]
    cwd: Optional[str]
    cli_version: Optional[str]
    model_provider: Optional[str]
    created_at: Optional[float]
    mtime: float
    #: Git state as it was when the session started -- the branch the agent actually
    #: worked on, which is not necessarily the repo's current branch.
    git_branch: Optional[str] = None
    git_sha: Optional[str] = None
    git_origin_url: Optional[str] = None

    @property
    def source(self) -> Source:
        return classify_originator(self.originator)


@dataclass
class RolloutState:
    """Derived live state for one session, from a full pass over its rollout."""

    status: Status = Status.UNKNOWN
    current_action: Optional[str] = None
    last_activity: Optional[float] = None
    active_turn_id: Optional[str] = None
    model: Optional[str] = None
    cwd: Optional[str] = None
    title: Optional[str] = None
    approval_policy: Optional[str] = None
    sandbox_policy: Optional[str] = None
    workspace_roots: list[str] = field(default_factory=list)
    changed_files: set[str] = field(default_factory=set)
    events: list[SessionEvent] = field(default_factory=list)


def _parse_ts(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        # Codex writes both seconds and milliseconds depending on the field.
        return value / 1000.0 if value > 1e11 else float(value)
    if isinstance(value, str):
        try:
            from datetime import datetime

            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def read_head(path: Path) -> Optional[RolloutHead]:
    """Read only the first line of a rollout. One `head -1`, no full parse."""
    try:
        with path.open("r", errors="ignore") as fh:
            first = fh.readline()
        if not first.strip():
            return None
        record = json.loads(first)
    except (OSError, json.JSONDecodeError):
        return None

    if record.get("type") != "session_meta":
        return None
    payload = record.get("payload") or {}
    match = _ROLLOUT_RE.match(path.name)
    thread_id = payload.get("session_id") or payload.get("id") or (match.group("id") if match else None)
    if not thread_id:
        return None

    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0

    git = payload.get("git") or {}
    return RolloutHead(
        thread_id=thread_id,
        path=path,
        originator=payload.get("originator"),
        source_field=payload.get("source"),
        cwd=payload.get("cwd"),
        cli_version=payload.get("cli_version") or payload.get("cliVersion"),
        model_provider=payload.get("model_provider"),
        created_at=_parse_ts(payload.get("timestamp")) or _parse_ts(record.get("timestamp")),
        mtime=mtime,
        git_branch=git.get("branch"),
        git_sha=git.get("commit_hash"),
        git_origin_url=git.get("repository_url"),
    )


#: Walking the whole session store costs thousands of stat calls, and the historical
#: part of it never changes, so the deep walk is cached. The *recent* day directories
#: are always re-listed, because a brand-new session must appear on the iPad within one
#: reconcile -- caching those would delay the product's core promise by the whole TTL.
_LISTING_TTL = 30.0
_RECENT_DAYS = 2
_listing_cache: "tuple[float, list[tuple[float, Path]]] | None" = None


def reset_listing_cache() -> None:
    """Drop the cached deep walk. Used by tests and whenever CODEX_HOME changes."""
    global _listing_cache
    _listing_cache = None


def _recent_day_dirs(now: float) -> list[Path]:
    from datetime import datetime, timedelta
    dirs = []
    for offset in range(_RECENT_DAYS + 1):
        day = datetime.fromtimestamp(now) - timedelta(days=offset)
        candidate = SESSIONS_DIR / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
        if candidate.is_dir():
            dirs.append(candidate)
    return dirs


def _stat_rollouts(paths) -> list[tuple[float, Path]]:
    out: list[tuple[float, Path]] = []
    for path in paths:
        try:
            out.append((path.stat().st_mtime, path))
        except OSError:
            continue
    return out


def read_model(path: Path, *, max_lines: int = 200) -> Optional[str]:
    """Find the model from the first `turn_context`, which sits near the file's head."""
    try:
        with path.open("r", errors="ignore") as fh:
            for index, line in enumerate(fh):
                if index >= max_lines:
                    break
                if '"turn_context"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") == "turn_context":
                    model = (record.get("payload") or {}).get("model")
                    if model:
                        return model
    except OSError:
        return None
    return None


def iter_rollouts(since: Optional[float] = None) -> Iterator[Path]:
    """Yield rollout paths, newest first, optionally only those modified since `since`."""
    global _listing_cache
    if not SESSIONS_DIR.is_dir():
        return

    now = time.time()
    cached = _listing_cache
    if cached and now - cached[0] < _LISTING_TTL:
        entries = list(cached[1])
        # Always re-list the last few days, so a session started seconds ago is found
        # on the very next reconcile instead of waiting out the cache.
        known = {path for _, path in entries}
        for day_dir in _recent_day_dirs(now):
            for mtime, path in _stat_rollouts(day_dir.glob("rollout-*.jsonl")):
                if path not in known:
                    entries.append((mtime, path))
    else:
        entries = _stat_rollouts(SESSIONS_DIR.rglob("rollout-*.jsonl"))
        _listing_cache = (now, entries)

    entries.sort(key=lambda item: item[0], reverse=True)
    for mtime, path in entries:
        if since is not None and mtime < since:
            continue
        yield path


def _shorten_command(cmd: str, cwd: Optional[str]) -> str:
    cmd = " ".join(cmd.split())
    if cwd:
        cmd = cmd.replace(cwd + "/", "").replace(cwd, ".")
    return cmd if len(cmd) <= 80 else cmd[:77] + "..."


def _extract_exec(payload: dict[str, Any]) -> Optional[tuple[str, Optional[str]]]:
    """Pull a human-readable command out of a tool-call payload.

    Codex encodes shell calls as a JS snippet in `input`, e.g.
    `const r = await tools.exec_command({cmd:"pytest -q", workdir:"/repo", ...})`.
    We lift the `cmd` and `workdir` out of it rather than showing the wrapper.
    """
    raw = payload.get("input")
    if not isinstance(raw, str):
        return None
    cmd_match = re.search(r'"?cmd"?\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
    if not cmd_match:
        return None
    try:
        cmd = json.loads('"' + cmd_match.group(1) + '"')
    except json.JSONDecodeError:
        cmd = cmd_match.group(1)
    wd_match = re.search(r'"?workdir"?\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
    workdir = wd_match.group(1) if wd_match else None
    return cmd, workdir


def _describe_command(cmd: str) -> str:
    """Turn a shell command into the short phrase the dashboard shows."""
    head = cmd.strip().split()
    if not head:
        return "Running command"
    first = os.path.basename(head[0])
    if first in {"pytest", "tox", "jest", "vitest", "go"} or "test" in cmd[:40]:
        return f"Running {first}"
    if first in {"docker", "docker-compose"}:
        return "Executing docker compose" if "compose" in cmd else "Executing docker"
    if first in {"git"}:
        return f"Running git {head[1]}" if len(head) > 1 else "Running git"
    if first in {"npm", "pnpm", "yarn", "bun"} and len(head) > 1:
        return f"Running {first} {head[1]}"
    return f"Running {first}"


#: Payload types that carry model reasoning. Never surfaced.
_REASONING_TYPES = {"agent_reasoning", "agent_reasoning_delta", "reasoning",
                    "agent_reasoning_section_break", "agent_reasoning_raw_content"}

#: How long after the last write a thread with an open turn is still considered live.
STALE_TURN_SECONDS = 180.0


#: How much of the end of a rollout is enough to determine current state. Status
#: depends only on recent events, so a status scan reads the tail rather than the whole
#: file -- the difference between a dashboard that refreshes in milliseconds and one
#: that re-reads hundreds of megabytes every two seconds.
STATUS_TAIL_BYTES = 256 * 1024


def _open_from_tail(path: Path, tail_bytes: int):
    """Open a rollout positioned at a line boundary near its end."""
    fh = path.open("r", errors="ignore")
    try:
        size = path.stat().st_size
    except OSError:
        return fh, False
    if size <= tail_bytes:
        return fh, False
    fh.seek(size - tail_bytes)
    fh.readline()          # discard the partial line we landed in the middle of
    return fh, True


def parse_rollout(path: Path, *, collect_events: bool = True,
                  max_events: int = 2000,
                  tail_bytes: Optional[int] = None) -> RolloutState:
    """Pass over a rollout, producing status, current action and transcript.

    `tail_bytes` limits the read to the end of the file. Use it for status polling;
    leave it None when the full transcript is needed.
    """
    state = RolloutState()
    open_turn: Optional[str] = None
    last_ts: Optional[float] = None
    pending_action: Optional[str] = None
    last_assistant: Optional[str] = None
    aborted = False
    errored = False

    truncated = False
    try:
        if tail_bytes:
            fh, truncated = _open_from_tail(path, tail_bytes)
        else:
            fh = path.open("r", errors="ignore")
    except OSError:
        return state

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
            payload = record.get("payload") or {}
            ptype = payload.get("type")
            ts = _parse_ts(record.get("timestamp")) or last_ts or 0.0
            last_ts = ts

            if rtype == "session_meta":
                state.cwd = payload.get("cwd") or state.cwd
                continue

            if rtype == "turn_context":
                state.model = payload.get("model") or state.model
                state.cwd = payload.get("cwd") or state.cwd
                state.approval_policy = payload.get("approval_policy") or state.approval_policy
                sandbox = payload.get("sandbox_policy")
                if isinstance(sandbox, dict):
                    state.sandbox_policy = sandbox.get("type") or state.sandbox_policy
                roots = payload.get("workspace_roots")
                if isinstance(roots, list):
                    state.workspace_roots = [r for r in roots if isinstance(r, str)]
                continue

            if ptype in _REASONING_TYPES:
                continue

            if rtype != "event_msg":
                # response_item duplicates event_msg for messages; we only mine it for
                # tool calls, which do not always have an event_msg counterpart.
                if ptype == "custom_tool_call":
                    extracted = _extract_exec(payload)
                    if extracted:
                        cmd, workdir = extracted
                        pending_action = _describe_command(cmd)
                        if collect_events and len(state.events) < max_events:
                            state.events.append(SessionEvent(
                                kind=EventKind.COMMAND, timestamp=ts,
                                text=_shorten_command(cmd, workdir or state.cwd),
                                turn_id=open_turn,
                                detail={"command": cmd, "workdir": workdir},
                            ))
                continue

            if ptype == "task_started":
                open_turn = payload.get("turn_id") or open_turn
                pending_action = None
                aborted = False
                if collect_events and len(state.events) < max_events:
                    state.events.append(SessionEvent(
                        kind=EventKind.TURN_START, timestamp=ts, turn_id=open_turn))

            elif ptype == "task_complete":
                open_turn = None
                pending_action = None
                last_assistant = payload.get("last_agent_message") or last_assistant
                if collect_events and len(state.events) < max_events:
                    state.events.append(SessionEvent(
                        kind=EventKind.TURN_END, timestamp=ts,
                        turn_id=payload.get("turn_id")))

            elif ptype == "turn_aborted":
                open_turn = None
                pending_action = None
                aborted = True
                if collect_events and len(state.events) < max_events:
                    state.events.append(SessionEvent(
                        kind=EventKind.TURN_ABORTED, timestamp=ts,
                        text=payload.get("reason"), turn_id=payload.get("turn_id")))

            elif ptype == "user_message":
                text = payload.get("message")
                if state.title is None and text:
                    state.title = " ".join(text.split())[:80]
                if collect_events and len(state.events) < max_events:
                    state.events.append(SessionEvent(
                        kind=EventKind.USER_MESSAGE, timestamp=ts, text=text,
                        turn_id=open_turn))

            elif ptype == "agent_message":
                last_assistant = payload.get("message") or last_assistant
                if collect_events and len(state.events) < max_events:
                    state.events.append(SessionEvent(
                        kind=EventKind.AGENT_MESSAGE, timestamp=ts,
                        text=payload.get("message"), turn_id=open_turn,
                        detail={"phase": payload.get("phase")}))

            elif ptype in {"patch_apply_begin", "patch_apply_end"}:
                changes = payload.get("changes") or {}
                files = list(changes.keys()) if isinstance(changes, dict) else []
                for f in files:
                    state.changed_files.add(f)
                if files:
                    rel = os.path.relpath(files[0], state.cwd) if state.cwd else files[0]
                    pending_action = f"Editing {rel}"
                if ptype == "patch_apply_end" and collect_events and len(state.events) < max_events:
                    state.events.append(SessionEvent(
                        kind=EventKind.FILE_EDIT, timestamp=ts,
                        text=", ".join(os.path.basename(f) for f in files[:4]) or "patch applied",
                        turn_id=open_turn,
                        detail={"files": files, "success": payload.get("success", True)}))

            elif ptype in {"exec_command_begin", "exec_command_end"}:
                command = payload.get("command")
                if isinstance(command, list):
                    command = " ".join(command)
                if command:
                    if ptype == "exec_command_begin":
                        pending_action = _describe_command(command)
                    if collect_events and len(state.events) < max_events:
                        state.events.append(SessionEvent(
                            kind=EventKind.COMMAND, timestamp=ts,
                            text=_shorten_command(command, state.cwd), turn_id=open_turn,
                            detail={"command": command,
                                    "exitCode": payload.get("exit_code")}))

            elif ptype in {"mcp_tool_call_begin", "mcp_tool_call_end"}:
                inv = payload.get("invocation") or {}
                tool = inv.get("tool") or payload.get("tool") or "tool"
                if ptype == "mcp_tool_call_begin":
                    pending_action = f"Calling {tool}"
                elif collect_events and len(state.events) < max_events:
                    state.events.append(SessionEvent(
                        kind=EventKind.TOOL, timestamp=ts, text=str(tool),
                        turn_id=open_turn, detail={"server": inv.get("server")}))

            elif ptype in {"exec_approval_request", "apply_patch_approval_request",
                           "permissions_request"}:
                if collect_events and len(state.events) < max_events:
                    state.events.append(SessionEvent(
                        kind=EventKind.PERMISSION_REQUEST, timestamp=ts,
                        text=payload.get("reason") or "Permission requested",
                        turn_id=open_turn, detail=payload))
                pending_action = "Waiting for permission"

            elif ptype in {"error", "stream_error"}:
                errored = True
                if collect_events and len(state.events) < max_events:
                    state.events.append(SessionEvent(
                        kind=EventKind.ERROR, timestamp=ts,
                        text=payload.get("message"), turn_id=open_turn))

    if truncated and (state.cwd is None or state.model is None):
        head = read_head(path)
        if head:
            state.cwd = state.cwd or head.cwd
        if state.model is None:
            state.model = read_model(path)
    if truncated and last_ts is None:
        # The tail contained no timestamped record at all; fall back to the file's own
        # mtime rather than reporting "unknown".
        try:
            last_ts = path.stat().st_mtime
        except OSError:
            last_ts = None

    state.last_activity = last_ts
    state.active_turn_id = open_turn

    stale = last_ts is not None and (time.time() - last_ts) > STALE_TURN_SECONDS

    if open_turn and not stale:
        state.status = Status.EXECUTING if pending_action else Status.RUNNING
        if pending_action and pending_action.startswith("Editing"):
            state.status = Status.EDITING
        if pending_action == "Waiting for permission":
            state.status = Status.WAITING_FOR_PERMISSION
        state.current_action = pending_action or "Working"
    elif open_turn and stale:
        # A turn was opened and nothing has been written for a while. The owning
        # process may have exited; we report the honest answer rather than a guess.
        state.status = Status.DISCONNECTED
        state.current_action = None
    elif errored:
        state.status = Status.FAILED
    elif aborted:
        state.status = Status.INTERRUPTED
    elif last_ts is None:
        state.status = Status.UNKNOWN
    else:
        state.status = Status.IDLE
        state.current_action = "Waiting for your input"

    if state.title is None and last_assistant:
        state.title = " ".join(last_assistant.split())[:80]
    return state
