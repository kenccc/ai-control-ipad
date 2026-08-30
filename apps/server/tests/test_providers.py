"""Provider behaviour: source separation, capability gating, honest refusals."""

import pytest

from aicontrol.models import Source, Status
from aicontrol.providers.base import CapabilityError
from aicontrol.providers.claude_code import ClaudeCodeProvider, _title_from_head
from aicontrol.providers.codex_cli import CodexCLIProvider
from aicontrol.providers.codex_desktop import CodexDesktopProvider
from aicontrol.services.git_service import GitService
from conftest import event, write_rollout

pytestmark = pytest.mark.asyncio


@pytest.fixture
def providers(codex_home, monkeypatch):
    from aicontrol.codex import globalstate, session_index
    monkeypatch.setattr(globalstate, "GLOBAL_STATE_PATH",
                        codex_home / ".codex-global-state.json")
    git = GitService()
    desktop = CodexDesktopProvider(git)
    cli = CodexCLIProvider(git)
    for provider in (desktop, cli):
        provider.global_state._path = codex_home / ".codex-global-state.json"
        provider.session_index._path = codex_home / "session_index.jsonl"
    return desktop, cli


async def test_desktop_and_cli_never_mix(providers, codex_home):
    write_rollout(codex_home, "d0000000-0000-0000-0000-000000000001",
                  originator="Codex Desktop")
    write_rollout(codex_home, "c0000000-0000-0000-0000-000000000002",
                  originator="codex-tui")
    write_rollout(codex_home, "u0000000-0000-0000-0000-000000000003",
                  originator="brand-new-client")
    desktop, cli = providers

    desktop_sessions = await desktop.discover_sessions()
    cli_sessions = await cli.discover_sessions()

    assert {s.source for s in desktop_sessions} == {Source.CODEX_DESKTOP}
    assert len(desktop_sessions) == 1
    # An unknown originator lands in the CLI provider as plain "Codex", never as an app
    # session -- guessing would misattribute the session's origin.
    assert {s.source for s in cli_sessions} == {Source.CODEX_CLI, Source.CODEX_UNKNOWN}
    assert all(s.source is not Source.CODEX_DESKTOP for s in cli_sessions)


async def test_desktop_labels(providers, codex_home):
    write_rollout(codex_home, "d0000000-0000-0000-0000-00000000000a")
    desktop, _ = providers
    session = (await desktop.discover_sessions())[0]
    assert session.source.label == "Codex App"
    assert session.provider.value == "openai_codex"
    assert session.metadata["originator"] == "Codex Desktop"


async def test_idle_desktop_thread_is_writable(providers, codex_home):
    write_rollout(codex_home, "d0000000-0000-0000-0000-00000000000b", events=[
        event("task_started", turn_id="t1"),
        event("task_complete", turn_id="t1"),
    ])
    desktop, _ = providers
    session = (await desktop.discover_sessions())[0]
    # No turn in flight: thread/resume continues this exact thread id.
    assert session.capabilities.send_message is True
    assert session.capabilities.resume is True
    assert session.capabilities.write_blocked_reason is None


async def test_live_desktop_thread_is_write_blocked(providers, codex_home):
    """A turn in flight in the desktop app must never be written into."""
    write_rollout(codex_home, "d0000000-0000-0000-0000-00000000000c",
                  events=[event("task_started", turn_id="t1")])
    desktop, _ = providers
    session = (await desktop.discover_sessions())[0]
    assert session.capabilities.send_message is False
    assert session.capabilities.interrupt is False
    assert "desktop app" in (session.capabilities.write_blocked_reason or "")
    # ...and the read side stays fully available.
    assert session.capabilities.read_conversation is True
    assert session.capabilities.diff is True


async def test_stale_open_turn_is_still_write_blocked(providers, codex_home, monkeypatch):
    """A thread thinking for minutes is quiet, not free. Two writers must not happen."""
    from aicontrol.codex import rollout
    write_rollout(codex_home, "d0000000-0000-0000-0000-00000000000d",
                  events=[event("task_started", turn_id="t1")])
    monkeypatch.setattr(rollout.time, "time", lambda: 9_999_999_999.0)
    desktop, _ = providers
    session = (await desktop.discover_sessions())[0]
    assert session.status is Status.DISCONNECTED
    assert session.capabilities.send_message is False


async def test_desktop_refuses_to_create_a_session(providers):
    desktop, _ = providers
    with pytest.raises(CapabilityError) as exc:
        await desktop.create_session(cwd="/tmp")
    # It explains rather than silently starting a CLI session and mislabelling it.
    assert "desktop" in str(exc.value).lower()


async def test_desktop_send_refuses_when_blocked(providers, codex_home):
    write_rollout(codex_home, "d0000000-0000-0000-0000-00000000000e",
                  events=[event("task_started", turn_id="t1")])
    desktop, _ = providers
    session = (await desktop.discover_sessions())[0]
    with pytest.raises(CapabilityError):
        await desktop.send_message(session.id, "continue")


async def test_desktop_projects_come_from_codex_state(providers, codex_home):
    import json
    (codex_home / ".codex-global-state.json").write_text(json.dumps({
        "local-projects": {
            "local-1": {"id": "local-1", "name": "inventory",
                        "rootPaths": ["/tmp/repo"], "createdAt": 1, "updatedAt": 2},
        },
        "project-order": ["local-1"],
        "thread-project-assignments": {
            "d0000000-0000-0000-0000-00000000000f": {
                "projectKind": "local", "projectId": "local-1", "cwd": "/tmp/repo"},
        },
    }))
    write_rollout(codex_home, "d0000000-0000-0000-0000-00000000000f")
    desktop, _ = providers
    session = (await desktop.discover_sessions())[0]
    projects = desktop.get_projects()
    assert [p.name for p in projects] == ["inventory"]
    # The project association is Codex's own, not inferred from the directory name.
    assert session.metadata["codexProjectName"] == "inventory"
    assert session.repository == "inventory"


async def test_cli_turn_in_flight_elsewhere_is_not_writable(providers, codex_home):
    write_rollout(codex_home, "c0000000-0000-0000-0000-00000000001a",
                  originator="codex-tui", events=[event("task_started", turn_id="t9")])
    _, cli = providers
    session = (await cli.discover_sessions())[0]
    assert session.capabilities.send_message is False
    assert session.capabilities.interrupt is True
    with pytest.raises(CapabilityError):
        await cli.send_message(session.id, "hello")


async def test_claude_title_falls_back_to_first_prompt(tmp_path):
    import json
    path = tmp_path / "sess.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in [
        {"type": "user", "cwd": "/tmp", "message":
            {"role": "user", "content": "Port the CSV exporter to the new branch"}},
        {"type": "assistant", "cwd": "/tmp",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}},
    ]))
    assert _title_from_head(path) == "Port the CSV exporter to the new branch"


async def test_claude_drops_thinking_blocks(tmp_path):
    import json
    from aicontrol.providers.claude_code import parse_session_file
    path = tmp_path / "sess.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in [
        {"type": "assistant", "cwd": "/tmp", "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "SECRET REASONING"},
            {"type": "redacted_thinking", "data": "ALSO SECRET"},
            {"type": "text", "text": "visible answer"},
        ]}},
    ]))
    info = parse_session_file(path)
    blob = json.dumps([e.to_dict() for e in info.events])
    assert "SECRET" not in blob
    assert "visible answer" in blob


async def test_slug_resolution_handles_underscores_and_dots(tmp_path, monkeypatch):
    """The project-directory slug is lossy; a partial match must not be reported."""
    from aicontrol.providers import claude_code as cc
    (tmp_path / "Users" / "ken" / "SpecialGuard_DEV").mkdir(parents=True)
    (tmp_path / "Users" / "ken" / ".claude-mem" / "observer-sessions").mkdir(parents=True)
    monkeypatch.setattr(cc, "_SLUG_CACHE", {})

    def resolve(slug):
        parts = [p for p in slug.split("-") if p]
        found = cc._resolve_slug(parts, tmp_path)
        return str(found) if found else None

    assert resolve("-Users-ken-SpecialGuard-DEV") == str(tmp_path / "Users/ken/SpecialGuard_DEV")
    assert resolve("-Users-ken--claude-mem-observer-sessions") == str(
        tmp_path / "Users/ken/.claude-mem/observer-sessions")
    # An unresolvable slug yields nothing rather than a confidently wrong parent path,
    # which would attach the session to the wrong repository.
    assert resolve("-Users-ken-does-not-exist") is None
