"""Reader for `~/.codex/session_index.jsonl` -- Codex's own thread titles.

Codex names each thread itself ("Audit foto dokumentu gaps"), which is far better than
anything we could derive from the first user message, so it is the preferred title
source. The file is append-only with later lines superseding earlier ones for the same
id.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .rollout import CODEX_HOME

SESSION_INDEX_PATH = CODEX_HOME / "session_index.jsonl"


class CodexSessionIndex:
    def __init__(self, path: Path = SESSION_INDEX_PATH) -> None:
        self._path = path
        self._mtime = -1.0
        self._titles: dict[str, str] = {}

    def _reload_if_stale(self) -> None:
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            return
        if mtime == self._mtime:
            return
        titles: dict[str, str] = {}
        try:
            with self._path.open("r", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    tid, name = record.get("id"), record.get("thread_name")
                    if tid and name:
                        titles[tid] = name
        except OSError:
            return
        self._titles = titles
        self._mtime = mtime

    def title(self, thread_id: str) -> Optional[str]:
        self._reload_if_stale()
        return self._titles.get(thread_id)

    def __len__(self) -> int:
        self._reload_if_stale()
        return len(self._titles)
