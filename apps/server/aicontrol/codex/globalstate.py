"""Reader for `~/.codex/.codex-global-state.json` -- the Codex Desktop app's own state.

Only the Electron desktop app writes this file, which makes it two useful things at
once: the source of the real Projects sidebar (so project associations are the app's
own, not invented from directory names), and a corroborating signal that a thread is
known to the desktop app.

This module is strictly read-only. AI Control never writes Codex's state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ..models import CodexProject
from .rollout import CODEX_HOME

GLOBAL_STATE_PATH = CODEX_HOME / ".codex-global-state.json"


@dataclass
class ThreadAssignment:
    project_id: Optional[str]
    project_kind: Optional[str]
    cwd: Optional[str]


class CodexGlobalState:
    """Cached view of the desktop app's state file, reloaded when its mtime moves."""

    def __init__(self, path: Path = GLOBAL_STATE_PATH) -> None:
        self._path = path
        self._mtime: float = -1.0
        self._data: dict[str, Any] = {}

    def _reload_if_stale(self) -> None:
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            self._data = {}
            self._mtime = -1.0
            return
        if mtime == self._mtime:
            return
        try:
            # The app rewrites this file atomically, but a read can still land
            # mid-swap; a failed parse just keeps the previous snapshot.
            self._data = json.loads(self._path.read_text())
            self._mtime = mtime
        except (OSError, json.JSONDecodeError):
            return

    @property
    def available(self) -> bool:
        self._reload_if_stale()
        return bool(self._data)

    def projects(self) -> list[CodexProject]:
        self._reload_if_stale()
        raw = self._data.get("local-projects") or {}
        order: list[str] = self._data.get("project-order") or []
        projects: dict[str, CodexProject] = {}
        for pid, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            projects[pid] = CodexProject(
                id=pid,
                name=entry.get("name") or pid,
                root_paths=list(entry.get("rootPaths") or []),
                kind="local",
                created_at=_ms(entry.get("createdAt")),
                updated_at=_ms(entry.get("updatedAt")),
            )
        ranked = sorted(
            projects.values(),
            key=lambda p: order.index(p.id) if p.id in order else len(order),
        )
        return ranked

    def thread_assignments(self) -> dict[str, ThreadAssignment]:
        self._reload_if_stale()
        raw = self._data.get("thread-project-assignments") or {}
        out: dict[str, ThreadAssignment] = {}
        for tid, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            out[tid] = ThreadAssignment(
                project_id=entry.get("projectId"),
                project_kind=entry.get("projectKind"),
                cwd=entry.get("cwd"),
            )
        return out

    def workspace_root_hints(self) -> dict[str, str]:
        self._reload_if_stale()
        raw = self._data.get("thread-workspace-root-hints") or {}
        return {k: v for k, v in raw.items() if isinstance(v, str)}

    def thread_titles(self) -> dict[str, str]:
        self._reload_if_stale()
        raw = self._data.get("thread-titles") or {}
        return {k: v for k, v in raw.items() if isinstance(v, str)}

    def selected_project_id(self) -> Optional[str]:
        self._reload_if_stale()
        sel = self._data.get("selected-project") or {}
        return sel.get("projectId") if isinstance(sel, dict) else None

    def mobile_paired(self) -> bool:
        self._reload_if_stale()
        return bool(self._data.get("codex-mobile-has-connected-device"))


def _ms(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return value / 1000.0 if value > 1e11 else float(value)
    return None
