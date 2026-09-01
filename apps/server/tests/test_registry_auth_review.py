"""Reconciliation and deduplication, auth, allowlist, and review plumbing."""

import time

import pytest

from aicontrol.auth import AuthService, RateLimiter
from aicontrol.config import Config, RepoConfig
from aicontrol.db import Database
from aicontrol.events import EventBus
from aicontrol.models import (AgentSession, Capabilities, Provider, Source, Status)
from aicontrol.providers.base import AgentProvider
from aicontrol.registry import SessionRegistry
from aicontrol.services.forgejo import build_issue_context
from aicontrol.services.review import (build_feedback_prompt, findings_to_prompt,
                                       parse_review_output)




def make_session(provider_id, external_id, *, source=Source.CODEX_DESKTOP,
                 title="t", caps=None, last=100.0, status=Status.IDLE):
    return AgentSession(
        id=f"{provider_id}:{external_id}", source=source,
        provider=Provider.OPENAI_CODEX, external_session_id=external_id,
        title=title, status=status, last_activity=last,
        capabilities=caps or Capabilities(read_sessions=True))


class FakeProvider(AgentProvider):
    def __init__(self, provider_id, sessions):
        self.provider_id = provider_id
        self._sessions = sessions

    async def discover_sessions(self):
        return list(self._sessions)

    async def get_session(self, session_id):
        return next((s for s in self._sessions if s.id == session_id), None)

    async def get_capabilities(self, session_id):
        session = await self.get_session(session_id)
        return session.capabilities


@pytest.fixture
def registry(tmp_path):
    db = Database(tmp_path / "t.db")
    return db, EventBus()


@pytest.mark.asyncio
async def test_same_thread_from_two_providers_is_merged(registry):
    db, bus = registry
    read_only = make_session("a", "thread-1", caps=Capabilities(read_sessions=True))
    writable = make_session("b", "thread-1",
                            caps=Capabilities(read_sessions=True, send_message=True,
                                              interrupt=True))
    reg = SessionRegistry([FakeProvider("a", [read_only]), FakeProvider("b", [writable])],
                          db, bus)
    sessions = await reg.reconcile()
    assert len(sessions) == 1
    # The more capable view wins, so merging never loses control of a session.
    assert sessions[0].capabilities.send_message is True
    assert sessions[0].metadata["mergedFrom"]


@pytest.mark.asyncio
async def test_similar_titles_are_never_merged(registry):
    """Two agents given the same instruction are still two sessions."""
    db, bus = registry
    a = make_session("a", "thread-1", title="Add CSV import")
    b = make_session("a", "thread-2", title="Add CSV import")
    reg = SessionRegistry([FakeProvider("a", [a, b])], db, bus)
    assert len(await reg.reconcile()) == 2


@pytest.mark.asyncio
async def test_discovery_of_an_external_session_emits_an_event(registry):
    db, bus = registry
    provider = FakeProvider("a", [])
    reg = SessionRegistry([provider], db, bus)
    queue = bus.subscribe()
    await reg.reconcile()

    provider._sessions.append(make_session("a", "appeared"))
    await reg.reconcile()

    kinds = []
    while not queue.empty():
        kinds.append(queue.get_nowait()["type"])
    assert "session.discovered" in kinds


@pytest.mark.asyncio
async def test_status_change_emits_an_event(registry):
    db, bus = registry
    provider = FakeProvider("a", [make_session("a", "s1", status=Status.IDLE)])
    reg = SessionRegistry([provider], db, bus)
    await reg.reconcile()
    queue = bus.subscribe()
    # Providers build fresh session objects each scan; mutating in place would compare
    # an object against itself and mask the transition.
    updated = make_session("a", "s1", status=Status.EXECUTING)
    updated.current_action = "Running pytest"
    provider._sessions = [updated]
    await reg.reconcile()
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    status_events = [e for e in events if e["type"] == "session.status"]
    assert status_events and status_events[0]["action"] == "Running pytest"


@pytest.mark.asyncio
async def test_associations_survive_a_restart(registry, tmp_path):
    """A backend restart must not lose the issue a session belongs to."""
    db, bus = registry
    session = make_session("a", "s1")
    reg = SessionRegistry([FakeProvider("a", [session])], db, bus)
    await reg.reconcile()
    db.set_session_issue(session.id, 428)

    fresh_db = Database(tmp_path / "t.db")
    reg2 = SessionRegistry([FakeProvider("a", [make_session("a", "s1")])],
                           fresh_db, EventBus())
    restored = await reg2.reconcile()
    assert restored[0].forgejo_issue == 428


# --------------------------------------------------------------------------- auth

def test_auth_refuses_to_start_without_a_token():
    with pytest.raises(RuntimeError):
        AuthService(None, "secret")


def test_session_cookie_roundtrip():
    auth = AuthService("hunter2", "secret")
    assert auth.verify_password("hunter2")
    assert not auth.verify_password("hunter3")
    cookie = auth.issue_session()
    assert auth.verify_session(cookie)
    assert not auth.verify_session("garbage")
    assert not auth.verify_session(cookie[:-1] + "0")  # tampered signature


def test_expired_session_is_rejected(monkeypatch):
    auth = AuthService("t", "secret")
    cookie = auth.issue_session()
    far_future = time.time() + 60 * 60 * 24 * 40
    monkeypatch.setattr("aicontrol.auth.time.time", lambda: far_future)
    assert not auth.verify_session(cookie)


@pytest.mark.parametrize("origin,ok", [
    ("http://localhost:5173", True),
    ("https://macbook.tail1234.ts.net", True),
    ("http://100.101.102.103", True),
    ("https://evil.example.com", False),
    ("http://10.0.0.5", False),
])
def test_origin_validation(origin, ok):
    auth = AuthService("t", "secret")
    assert auth.check_origin(origin, "macbook:8787") is ok


def test_rate_limiter_blocks_after_the_limit():
    limiter = RateLimiter(limit=3, window=60)
    assert all(limiter.check("ip") for _ in range(3))
    assert not limiter.check("ip")
    assert limiter.check("other-ip")


# ---------------------------------------------------------------------- allowlist

def test_allowlist_fails_closed(tmp_path):
    assert Config().is_allowed(tmp_path) is False


def test_allowlist_accepts_repo_and_subpaths(tmp_path):
    repo = tmp_path / "inventory"
    (repo / "app").mkdir(parents=True)
    cfg = Config(repositories={"inventory": RepoConfig("inventory", repo)},
                 worktree_root=tmp_path / "wt")
    assert cfg.is_allowed(repo)
    assert cfg.is_allowed(repo / "app")
    assert not cfg.is_allowed(tmp_path / "elsewhere")
    assert cfg.repo_for_path(repo / "app").name == "inventory"


def test_allowlist_rejects_traversal(tmp_path):
    repo = tmp_path / "inventory"
    repo.mkdir()
    cfg = Config(repositories={"inventory": RepoConfig("inventory", repo)},
                 worktree_root=tmp_path / "wt")
    assert not cfg.is_allowed(repo / ".." / ".." / "etc")


def test_config_dict_carries_no_secrets():
    from aicontrol.config import ForgejoConfig
    cfg = Config(auth_token="SECRET_TOKEN", session_secret="SECRET_SESSION",
                 forgejo=ForgejoConfig(url="https://git.example", token="SECRET_API"))
    blob = str(cfg.to_dict())
    assert "SECRET" not in blob
    assert cfg.to_dict()["forgejo"]["configured"] is True


# ------------------------------------------------------------------------ review

def test_feedback_prompt_lists_every_comment():
    prompt = build_feedback_prompt([
        {"file_path": "inventory/api.py", "line": 127, "body": "Use select_related()."},
        {"file_path": "tests/test_inventory.py", "line": 92, "body": "Add the invalid CSV case."},
    ])
    assert "inventory/api.py:127" in prompt
    assert "Use select_related()." in prompt
    assert "tests/test_inventory.py:92" in prompt


def test_review_output_parses_into_findings():
    findings = parse_review_output(
        "## CRITICAL\n- api.py:12 -- N+1 query\n\nSUGGESTION\n- naming could be clearer\n")
    assert findings[0].severity == "CRITICAL"
    assert findings[0].file == "api.py" and findings[0].line == 12
    assert findings[1].severity == "SUGGESTION"


def test_forwarded_review_prefers_actionable_findings():
    findings = parse_review_output(
        "CRITICAL\n- a.py:1 -- boom\nSUGGESTION\n- rename things\n")
    prompt = findings_to_prompt(findings, reviewer="Claude Code")
    assert "boom" in prompt
    assert "rename things" not in prompt   # suggestions are dropped when criticals exist


# ------------------------------------------------------------------------ forgejo

def test_issue_context_is_focused():
    context = build_issue_context(
        {"number": 428, "title": "Add CSV importer", "body":
            "Import CSV files.\n\n## Acceptance criteria\n- [ ] handles invalid rows",
         "labels": [{"name": "enhancement"}], "id": 99, "url": "https://x/y"},
        [{"body": "Please use the bulk API.", "user": {"login": "dan"}}],
        repo_slug="special/inventory")
    assert "#428" in context and "Add CSV importer" in context
    assert "enhancement" in context
    assert "Please use the bulk API." in context
    assert "handles invalid rows" in context
    # Metadata an agent cannot act on stays out of the prompt.
    assert "https://x/y" not in context


# ------------------------------------------------------------------- tailscale

def test_tailscale_reports_not_installed(monkeypatch, tmp_path):
    from aicontrol.services import tailscale as ts
    monkeypatch.setattr(ts.shutil, "which", lambda _: None)
    monkeypatch.setattr(ts, "CLI_CANDIDATES", ())
    monkeypatch.setattr(ts, "APP_PATH", tmp_path / "absent.app")
    result = ts.status()
    assert result["detected"] is False and result["connected"] is False
    assert "not installed" in result["hint"]


def test_tailscale_app_without_cli_is_reported_distinctly(monkeypatch, tmp_path):
    """The app being installed does not mean the CLI exists.

    Regression guard: the bundle binary launches the GUI and never returns when run
    directly, so it must never be probed as a CLI.
    """
    from aicontrol.services import tailscale as ts
    app = tmp_path / "Tailscale.app"
    app.mkdir()
    monkeypatch.setattr(ts.shutil, "which", lambda _: None)
    monkeypatch.setattr(ts, "CLI_CANDIDATES", ())
    monkeypatch.setattr(ts, "APP_PATH", app)

    def explode(*args, **kwargs):
        raise AssertionError("no subprocess may run when the CLI is absent")
    monkeypatch.setattr(ts.subprocess, "run", explode)

    result = ts.status()
    assert result["detected"] is True
    assert result["connected"] is False
    assert result["appInstalled"] is True
    assert "Install CLI" in result["hint"] or "install-tailscale-cli" in result["hint"]


def _fake_status(monkeypatch, tmp_path, payload):
    import json as _json
    import subprocess as _subprocess
    from aicontrol.services import tailscale as ts
    monkeypatch.setattr(ts.shutil, "which", lambda _: "/usr/local/bin/tailscale")
    monkeypatch.setattr(ts, "APP_PATH", tmp_path / "Tailscale.app")
    monkeypatch.setattr(ts.subprocess, "run",
                        lambda *a, **k: _subprocess.CompletedProcess(
                            a[0], 0, _json.dumps(payload), ""))
    return ts


def test_tailscale_connected_lists_peers(monkeypatch, tmp_path):
    ts = _fake_status(monkeypatch, tmp_path, {
        "BackendState": "Running",
        "Self": {"HostName": "macbook", "DNSName": "macbook.tail1234.ts.net.",
                 "TailscaleIPs": ["100.101.102.103", "fd7a:115c::1"]},
        "Peer": {"k1": {"HostName": "ipad", "DNSName": "ipad.tail1234.ts.net.",
                        "OS": "iOS", "Online": True}},
    })
    result = ts.status()
    assert result["connected"] is True
    assert result["dnsName"] == "macbook.tail1234.ts.net"
    assert result["hint"] is None
    assert result["peers"] == [{"hostname": "ipad", "dnsName": "ipad.tail1234.ts.net",
                                "os": "iOS", "online": True}]
    # The bind address must be the IPv4, never the IPv6 that shares the list.
    assert ts.ipv4_address() == "100.101.102.103"


def test_host_tailscale_resolves_to_the_tailnet_address(monkeypatch, tmp_path):
    from aicontrol.config import Config
    _fake_status(monkeypatch, tmp_path, {
        "BackendState": "Running",
        "Self": {"TailscaleIPs": ["100.101.102.103"]},
    })
    assert Config(host="tailscale").resolve_host() == "100.101.102.103"
    # An explicit host is passed through untouched.
    assert Config(host="127.0.0.1").resolve_host() == "127.0.0.1"


def test_host_tailscale_refuses_to_start_when_tailscale_is_down(monkeypatch):
    """Falling back to 0.0.0.0 would silently widen exposure, so we fail instead."""
    from aicontrol.config import Config
    from aicontrol.services import tailscale as ts
    monkeypatch.setattr(ts, "wait_for_ipv4", lambda *a, **k: None)
    with pytest.raises(RuntimeError) as exc:
        Config(host="tailscale").resolve_host()
    assert "Tailscale did not come up" in str(exc.value)


# --------------------------------------------------------------------------- usage

def test_window_labels_read_naturally():
    from aicontrol.services.usage import _window_label
    assert _window_label(300, "x") == "5-hour"
    assert _window_label(10080, "x") == "Weekly"
    assert _window_label(1440, "x") == "Daily"
    assert _window_label(None, "Primary") == "Primary"


@pytest.mark.asyncio
async def test_codex_usage_maps_the_rate_limit_snapshot():
    from aicontrol.services.usage import UsageService

    class FakeServer:
        async def request(self, method, params=None, timeout=None):
            if method == "account/read":
                return {"account": {"type": "chatgpt", "email": "a@b.c",
                                    "planType": "plus"}}
            if method == "account/rateLimits/read":
                return {"rateLimits": {
                    "primary": {"usedPercent": 42, "windowDurationMins": 300,
                                "resetsAt": 1788257980},
                    "secondary": {"usedPercent": 7, "windowDurationMins": 10080,
                                  "resetsAt": 1788781054},
                    "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
                    "planType": "plus", "rateLimitReachedType": None},
                    "rateLimitResetCredits": {"availableCount": 1}}
            if method == "account/usage/read":
                return {"summary": {"lifetimeTokens": 123}}
            raise AssertionError(method)

    class FakeProvider:
        async def app_server(self):
            return FakeServer()

    usage = await UsageService(FakeProvider())._codex(force=True)
    assert usage.available is True
    assert usage.plan == "plus"
    assert [(w.label, w.used_percent) for w in usage.windows] == [
        ("5-hour", 42.0), ("Weekly", 7.0)]
    assert usage.windows[0].resets_at == 1788257980
    assert usage.credits["resetCreditsAvailable"] == 1
    assert usage.totals["lifetimeTokens"] == 123
    assert usage.error is None


@pytest.mark.asyncio
async def test_codex_usage_reports_the_error_instead_of_inventing_numbers():
    from aicontrol.services.usage import UsageService

    class BrokenProvider:
        async def app_server(self):
            raise ConnectionError("app-server exited")

    usage = await UsageService(BrokenProvider())._codex(force=True)
    assert usage.available is False
    assert usage.windows == []
    assert "app-server exited" in usage.error


@pytest.mark.asyncio
async def test_codex_usage_survives_a_missing_totals_endpoint():
    """Older cores lack account/usage/read; that must not lose the rate limits."""
    from aicontrol.services.usage import UsageService

    class PartialServer:
        async def request(self, method, params=None, timeout=None):
            if method == "account/read":
                return {"account": {"planType": "pro", "email": None}}
            if method == "account/rateLimits/read":
                return {"rateLimits": {"primary": {"usedPercent": 90,
                                                   "windowDurationMins": 300}}}
            raise RuntimeError("method not found")

    class PartialProvider:
        async def app_server(self):
            return PartialServer()

    usage = await UsageService(PartialProvider())._codex(force=True)
    assert usage.plan == "pro"
    assert usage.windows[0].used_percent == 90.0
    assert usage.totals is None
    assert usage.error is None


def test_claude_usage_says_plainly_that_there_is_no_live_quota(monkeypatch, tmp_path):
    """Claude Code has no quota API; the UI must not imply otherwise."""
    import json as _json
    import subprocess as _subprocess
    from aicontrol.services import usage as usage_mod

    monkeypatch.setattr(usage_mod, "STATS_CACHE", tmp_path / "stats.json")
    monkeypatch.setattr(usage_mod, "PROJECTS_DIR", tmp_path / "projects")
    (tmp_path / "stats.json").write_text(_json.dumps({
        "modelUsage": {"claude-opus-5": {"inputTokens": 10, "outputTokens": 5,
                                         "cacheReadInputTokens": 1,
                                         "cacheCreationInputTokens": 4}},
        "totalSessions": 3, "totalMessages": 40, "lastComputedDate": "2026-08-19",
        "dailyModelTokens": [{"date": "2026-08-19", "tokensByModel": {"a": 7}}],
    }))
    monkeypatch.setattr(
        "aicontrol.providers.claude_code.ClaudeCodeProvider.binary",
        staticmethod(lambda: "/usr/bin/true"))
    monkeypatch.setattr(usage_mod.subprocess, "run",
                        lambda *a, **k: _subprocess.CompletedProcess(
                            a[0], 0, _json.dumps({"loggedIn": True,
                                                  "subscriptionType": "max",
                                                  "email": "me@example.com"}), ""))

    usage = UsageServiceStub()._claude_sync()
    assert usage.plan == "max"
    assert usage.available is True
    # No fabricated windows, and an explicit reason.
    assert usage.windows == []
    assert "no live quota API" in usage.note
    assert usage.totals["lifetimeTokens"] == 20
    assert usage.totals["computedAt"] == "2026-08-19"


class UsageServiceStub:
    """UsageService without a Codex provider, for the Claude-only path."""

    def __init__(self):
        from aicontrol.services.usage import UsageService
        self._inner = UsageService(None)

    def _claude_sync(self):
        return self._inner._claude_sync()


def test_claude_last_limit_event_is_found_and_dated(monkeypatch, tmp_path):
    import json as _json
    from aicontrol.services import usage as usage_mod

    projects = tmp_path / "projects" / "-tmp-repo"
    projects.mkdir(parents=True)
    (projects / "abc.jsonl").write_text(_json.dumps({
        "type": "assistant", "timestamp": "2026-08-22T17:23:14.253Z",
        "message": {"role": "assistant", "content": [
            {"type": "text",
             "text": "You've hit your weekly limit · resets Aug 14 at 1am"}]},
    }))
    monkeypatch.setattr(usage_mod, "PROJECTS_DIR", tmp_path / "projects")

    event = usage_mod.UsageService._claude_last_limit_event()
    assert event["kind"] == "weekly"
    assert "weekly limit" in event["text"]
    assert event["timestamp"].startswith("2026-08-22")
