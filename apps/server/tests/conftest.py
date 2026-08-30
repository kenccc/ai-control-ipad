import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    """A synthetic ~/.codex so tests never touch the real session store."""
    home = tmp_path / "codex"
    (home / "sessions" / "2026" / "08" / "30").mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(home))

    from aicontrol.codex import rollout
    monkeypatch.setattr(rollout, "CODEX_HOME", home)
    monkeypatch.setattr(rollout, "SESSIONS_DIR", home / "sessions")
    rollout.reset_listing_cache()
    return home


def write_rollout(codex_home, thread_id, *, originator="Codex Desktop",
                  cwd="/tmp/repo", events=None, branch="main", day="30"):
    path = (codex_home / "sessions" / "2026" / "08" / day /
            f"rollout-2026-08-{day}T10-00-00-{thread_id}.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    lines = [{
        "timestamp": now_iso(-60),
        "type": "session_meta",
        "payload": {"session_id": thread_id, "id": thread_id, "cwd": cwd,
                    "originator": originator, "source": "vscode",
                    "cli_version": "0.147.0", "model_provider": "openai",
                    "timestamp": now_iso(-60),
                    "git": {"commit_hash": "abc123", "branch": branch,
                            "repository_url": "https://git.example/o/r"}},
    }]
    lines += events or []
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    os.utime(path, (now, now))
    return path


def now_iso(offset=0.0):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(time.time() + offset,
                                  tz=timezone.utc).isoformat().replace("+00:00", "Z")


def event(payload_type, **payload):
    """An event stamped *now*, so staleness-based status behaves as it does live."""
    return {"timestamp": now_iso(), "type": "event_msg",
            "payload": {"type": payload_type, **payload}}
