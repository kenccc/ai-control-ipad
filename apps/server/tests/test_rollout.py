"""Rollout parsing: source attribution, status inference, and the reasoning ban."""

import json

import pytest

from aicontrol.codex import rollout
from aicontrol.models import EventKind, Source, Status
from conftest import event, now_iso, write_rollout


@pytest.mark.parametrize("originator,expected", [
    ("Codex Desktop", Source.CODEX_DESKTOP),
    ("codex_work_desktop", Source.CODEX_DESKTOP),
    ("codex-tui", Source.CODEX_CLI),
    ("codex_exec", Source.CODEX_CLI),
    ("something-new-from-openai", Source.CODEX_UNKNOWN),
    (None, Source.CODEX_UNKNOWN),
])
def test_originator_classification(originator, expected):
    assert rollout.classify_originator(originator) is expected


def test_source_field_is_never_used_to_classify(codex_home):
    """Codex Desktop reports source='vscode'; only `originator` may decide."""
    path = write_rollout(codex_home, "aaaa-1", originator="Codex Desktop")
    head = rollout.read_head(path)
    assert head.source_field == "vscode"
    assert head.source is Source.CODEX_DESKTOP


def test_head_reads_session_git_state(codex_home):
    path = write_rollout(codex_home, "aaaa-2", branch="feature/428-import")
    head = rollout.read_head(path)
    assert head.git_branch == "feature/428-import"
    assert head.git_sha == "abc123"
    assert head.cwd == "/tmp/repo"


def test_open_turn_reports_running(codex_home):
    path = write_rollout(codex_home, "bbbb-1", events=[event("task_started", turn_id="t1")])
    state = rollout.parse_rollout(path)
    assert state.status is Status.RUNNING
    assert state.active_turn_id == "t1"


def test_completed_turn_reports_idle(codex_home):
    path = write_rollout(codex_home, "bbbb-2", events=[
        event("task_started", turn_id="t1"),
        event("agent_message", message="done"),
        event("task_complete", turn_id="t1", last_agent_message="done"),
    ])
    state = rollout.parse_rollout(path)
    assert state.status is Status.IDLE
    assert state.active_turn_id is None


def test_aborted_turn_reports_interrupted(codex_home):
    path = write_rollout(codex_home, "bbbb-3", events=[
        event("task_started", turn_id="t1"),
        event("turn_aborted", turn_id="t1", reason="user"),
    ])
    assert rollout.parse_rollout(path).status is Status.INTERRUPTED


def test_editing_status_from_patch_apply(codex_home):
    path = write_rollout(codex_home, "bbbb-4", events=[
        event("task_started", turn_id="t1"),
        event("patch_apply_end", changes={"/tmp/repo/app/api.py": {}}, success=True),
    ])
    state = rollout.parse_rollout(path)
    assert state.status is Status.EDITING
    assert state.current_action == "Editing app/api.py"
    assert "/tmp/repo/app/api.py" in state.changed_files


def test_permission_request_status(codex_home):
    path = write_rollout(codex_home, "bbbb-5", events=[
        event("task_started", turn_id="t1"),
        event("exec_approval_request", reason="run migrations"),
    ])
    assert rollout.parse_rollout(path).status is Status.WAITING_FOR_PERMISSION


def test_command_description(codex_home):
    path = write_rollout(codex_home, "bbbb-6", events=[
        event("task_started", turn_id="t1"),
        event("exec_command_begin", command=["pytest", "-q"]),
    ])
    state = rollout.parse_rollout(path)
    assert state.status is Status.EXECUTING
    assert state.current_action == "Running pytest"


def test_reasoning_is_never_surfaced(codex_home):
    """Hidden chain-of-thought must not reach the event stream."""
    path = write_rollout(codex_home, "cccc-1", events=[
        event("task_started", turn_id="t1"),
        event("agent_reasoning", text="SECRET INTERNAL REASONING"),
        {"timestamp": now_iso(), "type": "response_item",
         "payload": {"type": "reasoning", "content": "MORE SECRET REASONING"}},
        event("agent_message", message="visible answer"),
    ])
    state = rollout.parse_rollout(path)
    blob = json.dumps([e.to_dict() for e in state.events])
    assert "SECRET" not in blob
    assert "visible answer" in blob
    assert {e.kind for e in state.events} <= {
        EventKind.TURN_START, EventKind.AGENT_MESSAGE}


def test_stale_open_turn_reports_disconnected(codex_home, monkeypatch):
    path = write_rollout(codex_home, "dddd-1", events=[event("task_started", turn_id="t1")])
    monkeypatch.setattr(rollout.time, "time",
                        lambda: 9_999_999_999.0)  # far in the future
    state = rollout.parse_rollout(path)
    assert state.status is Status.DISCONNECTED
    # The turn is still recorded, so write-gating can see the thread is owned.
    assert state.active_turn_id == "t1"


def test_tail_read_matches_full_read_for_status(codex_home):
    events = [event("task_started", turn_id="t1")]
    events += [event("agent_message", message="x" * 500) for _ in range(200)]
    events.append(event("task_complete", turn_id="t1"))
    path = write_rollout(codex_home, "eeee-1", events=events)

    full = rollout.parse_rollout(path, collect_events=False)
    tail = rollout.parse_rollout(path, collect_events=False, tail_bytes=8192)
    assert tail.status is full.status is Status.IDLE
    assert tail.cwd == full.cwd
