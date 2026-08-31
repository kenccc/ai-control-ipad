"""End-to-end tests through the real HTTP app.

The centrepiece is `test_codex_desktop_session_created_outside_ai_control_appears`:
the product's non-negotiable requirement, exercised the way the brief specifies it --
a Codex Desktop session that AI Control did not create, discovered, listed, opened,
and shown with its repository, branch, transcript and diff.
"""

import json
import subprocess
import time

import httpx
import pytest

from aicontrol.app import create_app
from aicontrol.config import Config, RepoConfig
from conftest import event, write_rollout

pytestmark = pytest.mark.asyncio

TOKEN = "test-token"


@pytest.fixture
async def client(codex_home, tmp_path, monkeypatch):
    repo = tmp_path / "inventory"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "api.py").write_text("def handler():\n    return 1\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "-qb", "feature/428-import"], cwd=repo, check=True)
    # An edit and a new file, as an agent would leave behind.
    (repo / "api.py").write_text("def handler():\n    return 2\n")
    (repo / "importer.py").write_text("def load(rows):\n    return rows\n")

    from aicontrol.codex import globalstate
    monkeypatch.setattr(globalstate, "GLOBAL_STATE_PATH",
                        codex_home / ".codex-global-state.json")
    config = Config(
        repositories={"inventory": RepoConfig("inventory", repo)},
        auth_token=TOKEN, session_secret="secret",
        worktree_root=tmp_path / "worktrees", reconcile_interval=0.5,
        db_path=tmp_path / "db.sqlite",
    )
    app = create_app(config)
    state = app.state.app_state
    for provider in (state.codex_desktop, state.codex_cli):
        provider.global_state._path = codex_home / ".codex-global-state.json"
        provider.session_index._path = codex_home / "session_index.jsonl"
    state.claude_code.max_sessions = 0

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as http:
        async with app.router.lifespan_context(app):
            yield http, state, repo


async def _login(http):
    response = await http.post("/api/auth/login", json={"token": TOKEN})
    assert response.status_code == 200
    return {"x-aicontrol-csrf": response.json()["csrfToken"]}


async def _wait_for_reconcile(state, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        await state.registry.reconcile()
        if state.registry.sessions():
            return
    raise AssertionError("no sessions discovered")


async def test_codex_desktop_session_created_outside_ai_control_appears(client, codex_home):
    """THE mandatory test: a Codex Desktop task AI Control never launched."""
    http, state, repo = client

    # Codex Desktop's own state: a project, and a thread assigned to it.
    thread_id = "01a04f19-6956-7ac0-a5f2-cbc44df1cea0"
    (codex_home / ".codex-global-state.json").write_text(json.dumps({
        "local-projects": {"local-1": {"id": "local-1", "name": "inventory",
                                       "rootPaths": [str(repo)]}},
        "project-order": ["local-1"],
        "thread-project-assignments": {
            thread_id: {"projectKind": "local", "projectId": "local-1",
                        "cwd": str(repo)}},
    }))
    (codex_home / "session_index.jsonl").write_text(json.dumps(
        {"id": thread_id, "thread_name": "Inventory CSV import",
         "updated_at": "2026-08-30T10:00:00Z"}) + "\n")

    # ...and the rollout the desktop app wrote while it worked.
    write_rollout(codex_home, thread_id, originator="Codex Desktop",
                  cwd=str(repo), branch="feature/428-import", events=[
                      event("task_started", turn_id="t1"),
                      event("user_message", message="Add a CSV importer"),
                      event("agent_reasoning", text="PRIVATE MODEL REASONING"),
                      event("exec_command_begin", command=["pytest", "-q"]),
                      event("patch_apply_end", changes={f"{repo}/importer.py": {}},
                            success=True),
                      event("agent_message", message="Added the importer."),
                      event("task_complete", turn_id="t1"),
                  ])

    headers = await _login(http)
    await _wait_for_reconcile(state)

    # 6. It is detected automatically.
    listing = (await http.get("/api/sessions")).json()["sessions"]
    desktop = [s for s in listing if s["source"] == "codex_desktop"]
    assert len(desktop) == 1, "the Codex Desktop session was not discovered"
    session = desktop[0]

    # 8. Metadata is the app's own, not guessed.
    assert session["sourceLabel"] == "Codex App"
    assert session["title"] == "Inventory CSV import"
    assert session["metadata"]["originator"] == "Codex Desktop"
    assert session["metadata"]["codexProjectName"] == "inventory"
    assert session["model"] is None or isinstance(session["model"], str)

    # 9. Repository and worktree resolve to the real directory.
    assert session["workingDirectory"] == str(repo)
    assert session["branch"] == "feature/428-import"
    assert session["repository"] == "inventory"

    # 10. The diff the desktop session produced is visible.
    changes = (await http.get(f"/api/sessions/{session['id']}/changes")).json()
    paths = {f["path"] for f in changes["files"]}
    assert {"api.py", "importer.py"} <= paths
    assert changes["stats"]["files_changed"] >= 2

    diff = (await http.get(f"/api/sessions/{session['id']}/diff",
                           params={"file": "api.py"})).json()["diff"]
    assert "return 2" in diff

    # 7. The conversation opens -- without any model reasoning in it.
    events = (await http.get(f"/api/sessions/{session['id']}/events")).json()["events"]
    blob = json.dumps(events)
    assert "PRIVATE MODEL REASONING" not in blob
    assert "Added the importer." in blob
    kinds = {e["kind"] for e in events}
    assert "user_message" in kinds and "command" in kinds and "file_edit" in kinds

    # 11. Interaction is offered only where it is real. No turn is in flight, so this
    #     thread can be continued; interrupting it cannot be, and is not offered.
    caps = session["capabilities"]
    assert caps["read_conversation"] and caps["diff"] and caps["stream_events"]
    assert caps["send_message"] is True
    assert caps["interrupt"] is False
    assert caps["terminal"] is False

    # And a Codex Desktop project is exposed as the app itself records it.
    projects = (await http.get("/api/codex/projects")).json()["projects"]
    assert [p["name"] for p in projects] == ["inventory"]
    assert len(projects[0]["sessions"]) == 1

    assert headers  # login produced a CSRF token


async def test_live_desktop_session_is_read_only_and_says_why(client, codex_home):
    http, state, repo = client
    write_rollout(codex_home, "01a04f19-0000-7ac0-a5f2-000000000001",
                  originator="Codex Desktop", cwd=str(repo),
                  events=[event("task_started", turn_id="t1")])
    headers = await _login(http)
    await _wait_for_reconcile(state)

    session = [s for s in (await http.get("/api/sessions")).json()["sessions"]
               if s["source"] == "codex_desktop"][0]
    assert session["capabilities"]["send_message"] is False
    assert "desktop app" in session["capabilities"]["write_blocked_reason"]

    # The attempt is refused with the reason, not with a generic server error.
    response = await http.post(f"/api/sessions/{session['id']}/messages",
                               json={"message": "continue"}, headers=headers)
    assert response.status_code == 409
    assert "desktop app" in response.json()["detail"]


async def test_sources_are_listed_separately(client, codex_home):
    http, state, repo = client
    write_rollout(codex_home, "01a04f19-0000-7ac0-a5f2-000000000010",
                  originator="Codex Desktop", cwd=str(repo))
    write_rollout(codex_home, "01a04f19-0000-7ac0-a5f2-000000000011",
                  originator="codex-tui", cwd=str(repo))
    await _login(http)
    await _wait_for_reconcile(state)

    listing = (await http.get("/api/sessions")).json()["sessions"]
    labels = {s["externalSessionId"]: s["sourceLabel"] for s in listing}
    assert labels["01a04f19-0000-7ac0-a5f2-000000000010"] == "Codex App"
    assert labels["01a04f19-0000-7ac0-a5f2-000000000011"] == "Codex CLI"

    filtered = (await http.get("/api/sessions",
                               params={"source": "codex_desktop"})).json()["sessions"]
    assert {s["sourceLabel"] for s in filtered} == {"Codex App"}


async def test_associations_survive_a_backend_restart(client, codex_home, tmp_path):
    """A restart must not lose which issue a session belongs to."""
    http, state, repo = client
    thread_id = "01a04f19-0000-7ac0-a5f2-000000000020"
    write_rollout(codex_home, thread_id, originator="Codex Desktop", cwd=str(repo))
    headers = await _login(http)
    await _wait_for_reconcile(state)

    session = [s for s in (await http.get("/api/sessions")).json()["sessions"]
               if s["externalSessionId"] == thread_id][0]
    response = await http.post(f"/api/sessions/{session['id']}/issue",
                               json={"issue": 428}, headers=headers)
    assert response.status_code == 200

    # A brand-new registry over the same database, as a restart would produce.
    from aicontrol.events import EventBus
    from aicontrol.providers.codex_desktop import CodexDesktopProvider
    from aicontrol.registry import SessionRegistry
    from aicontrol.services.git_service import GitService

    provider = CodexDesktopProvider(GitService())
    provider.global_state._path = codex_home / ".codex-global-state.json"
    provider.session_index._path = codex_home / "session_index.jsonl"
    restored = await SessionRegistry([provider], state.db, EventBus()).reconcile()
    assert [s.forgejo_issue for s in restored if s.external_session_id == thread_id] == [428]


async def test_agents_are_confined_to_the_allowlist(client):
    http, _, _ = client
    headers = await _login(http)
    response = await http.post("/api/terminals", json={"cwd": "/etc"}, headers=headers)
    assert response.status_code == 403
    assert "allowlist" in response.json()["detail"]


async def test_review_comments_become_one_prompt(client, codex_home):
    http, state, repo = client
    write_rollout(codex_home, "01a04f19-0000-7ac0-a5f2-000000000030",
                  originator="codex-tui", cwd=str(repo))
    headers = await _login(http)
    await _wait_for_reconcile(state)
    session = [s for s in (await http.get("/api/sessions")).json()["sessions"]
               if s["source"] == "codex_cli"][0]

    for path, line, body in [("api.py", 127, "Use select_related() here."),
                             ("importer.py", 84, "This should be transactional.")]:
        response = await http.post(f"/api/sessions/{session['id']}/review",
                                   json={"file_path": path, "line": line, "body": body},
                                   headers=headers)
        assert response.status_code == 200

    comments = (await http.get(f"/api/sessions/{session['id']}/review")).json()["comments"]
    assert len(comments) == 2

    from aicontrol.services.review import build_feedback_prompt
    prompt = build_feedback_prompt(comments)
    assert "api.py:127" in prompt and "select_related" in prompt
    assert "importer.py:84" in prompt


async def test_attachment_proxy_validates_and_forwards(client, monkeypatch):
    """Forgejo attachments need the server-side token, so they are proxied, not linked."""
    http, state, _ = client
    headers = await _login(http)

    class FakeForgejo:
        def __init__(self):
            self.requested = []

        async def close(self):
            """The app closes its Forgejo client on shutdown."""

        async def fetch_attachment(self, attachment_id, *, max_bytes):
            self.requested.append(attachment_id)
            return b"\x89PNG\r\n\x1a\nfake", "image/png"

    state.forgejo = FakeForgejo()
    uuid = "0aea0000-7b27-46fe-b692-958aa9bbec00"

    response = await http.get(f"/api/forgejo/attachment/{uuid}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/png")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.content.startswith(b"\x89PNG")
    assert state.forgejo.requested == [uuid]

    # Only well-formed ids reach the upstream request, so the URL we build cannot be
    # steered elsewhere.
    for bad in ["../../etc/passwd", "not-a-uuid", "0aea0000", uuid + "/x"]:
        bad_response = await http.get(f"/api/forgejo/attachment/{bad}")
        assert bad_response.status_code in (400, 404), bad
    assert state.forgejo.requested == [uuid]
    assert headers


async def test_attachment_proxy_refuses_non_images(client):
    """Serving attacker-supplied HTML from our own origin would be a scripting hole."""
    http, state, _ = client
    await _login(http)

    class HtmlForgejo:
        async def close(self):
            """The app closes its Forgejo client on shutdown."""

        async def fetch_attachment(self, attachment_id, *, max_bytes):
            return b"<script>alert(1)</script>", "text/html"

    state.forgejo = HtmlForgejo()
    response = await http.get(
        "/api/forgejo/attachment/0aea0000-7b27-46fe-b692-958aa9bbec00")
    assert response.status_code == 415
    assert "not an image" in response.json()["detail"]


async def test_attachment_proxy_requires_authentication(client):
    http, state, _ = client

    class AnyForgejo:
        async def close(self):
            """The app closes its Forgejo client on shutdown."""

        async def fetch_attachment(self, attachment_id, *, max_bytes):
            raise AssertionError("must not be reached without a session")

    state.forgejo = AnyForgejo()
    response = await http.get(
        "/api/forgejo/attachment/0aea0000-7b27-46fe-b692-958aa9bbec00")
    assert response.status_code == 401


async def test_bypass_permissions_is_explicit_and_audited(client, monkeypatch):
    """Unattended mode must be chosen per session, and never be the default."""
    http, state, repo = client
    headers = await _login(http)

    captured: dict = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        from aicontrol.models import (AgentSession, Capabilities, Provider, Source,
                                      Status)
        return AgentSession(id="codex_cli:new", source=Source.CODEX_CLI,
                            provider=Provider.OPENAI_CODEX, external_session_id="new",
                            working_directory=kwargs["cwd"], status=Status.RUNNING,
                            capabilities=Capabilities(read_sessions=True))

    monkeypatch.setattr(state.codex_cli, "create_session", fake_create)

    # Default: approvals stay on.
    await http.post("/api/sessions", headers=headers, json={
        "provider": "codex_cli", "repository": "inventory", "prompt": "hello"})
    assert captured["approval_policy"] is None
    assert captured["sandbox"] is None

    captured.clear()
    response = await http.post("/api/sessions", headers=headers, json={
        "provider": "codex_cli", "repository": "inventory", "prompt": "go",
        "bypass_permissions": True})
    assert response.status_code == 200
    # Both halves, or the agent stalls on a sandbox veto the approvals would have gated.
    assert captured["approval_policy"] == "never"
    assert captured["sandbox"] == "danger-full-access"

    actions = [e["action"] for e in state.db.audit_entries(20)]
    assert "permissions_bypassed" in actions


async def test_bypass_permissions_maps_to_claude_mode(client, monkeypatch):
    http, state, _ = client
    headers = await _login(http)
    captured: dict = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        from aicontrol.models import (AgentSession, Capabilities, Provider, Source,
                                      Status)
        return AgentSession(id="claude_code:new", source=Source.CLAUDE_CODE,
                            provider=Provider.ANTHROPIC_CLAUDE,
                            external_session_id="new", status=Status.RUNNING,
                            capabilities=Capabilities(read_sessions=True))

    monkeypatch.setattr(state.claude_code, "create_session", fake_create)
    await http.post("/api/sessions", headers=headers, json={
        "provider": "claude_code", "repository": "inventory", "prompt": "go",
        "bypass_permissions": True})
    assert captured["permission_mode"] == "bypassPermissions"
