"""SQLite persistence.

Holds the mapping between external agent sessions and everything AI Control knows
about them, so a backend restart never loses the association between a Codex thread,
its repository, its worktree and its Forgejo issue.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id                  TEXT PRIMARY KEY,
    source              TEXT NOT NULL,
    provider            TEXT NOT NULL,
    external_session_id TEXT NOT NULL,
    title               TEXT,
    repository          TEXT,
    working_directory   TEXT,
    branch              TEXT,
    worktree            TEXT,
    forgejo_issue       INTEGER,
    status              TEXT,
    current_action      TEXT,
    created_at          REAL,
    last_activity       REAL,
    capabilities        TEXT,
    metadata            TEXT,
    archived            INTEGER NOT NULL DEFAULT 0,
    user_label          TEXT,
    first_seen          REAL NOT NULL,
    updated_at          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_external ON sessions(external_session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_issue    ON sessions(forgejo_issue);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp  REAL NOT NULL,
    actor      TEXT,
    action     TEXT NOT NULL,
    session_id TEXT,
    repository TEXT,
    detail     TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(timestamp DESC);

CREATE TABLE IF NOT EXISTS review_comments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    file_path   TEXT NOT NULL,
    line        INTEGER,
    body        TEXT NOT NULL,
    created_at  REAL NOT NULL,
    sent_at     REAL
);
CREATE INDEX IF NOT EXISTS idx_review_session ON review_comments(session_id);

CREATE TABLE IF NOT EXISTS worktrees (
    path        TEXT PRIMARY KEY,
    repository  TEXT NOT NULL,
    branch      TEXT,
    base_commit TEXT,
    session_id  TEXT,
    issue       INTEGER,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS activity (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp  REAL NOT NULL,
    session_id TEXT,
    kind       TEXT NOT NULL,
    text       TEXT,
    detail     TEXT
);
CREATE INDEX IF NOT EXISTS idx_activity_ts ON activity(timestamp DESC);
"""


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._local = threading.local()
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._path, check_same_thread=False, timeout=15)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    # ------------------------------------------------------------------- sessions

    def upsert_session(self, session: Any) -> None:
        now = time.time()
        data = session.to_dict()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (id, source, provider, external_session_id, title,
                    repository, working_directory, branch, worktree, forgejo_issue,
                    status, current_action, created_at, last_activity, capabilities,
                    metadata, archived, first_seen, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    repository=excluded.repository,
                    working_directory=excluded.working_directory,
                    branch=excluded.branch,
                    worktree=excluded.worktree,
                    status=excluded.status,
                    current_action=excluded.current_action,
                    last_activity=excluded.last_activity,
                    capabilities=excluded.capabilities,
                    metadata=excluded.metadata,
                    updated_at=excluded.updated_at
                """,
                (data["id"], data["source"], data["provider"], data["externalSessionId"],
                 data["title"], data["repository"], data["workingDirectory"],
                 data["branch"], data["worktree"], data["forgejoIssue"], data["status"],
                 data["currentAction"], data["createdAt"], data["lastActivity"],
                 json.dumps(data["capabilities"]), json.dumps(data["metadata"]),
                 int(data["archived"]), now, now),
            )

    def set_session_issue(self, session_id: str, issue: Optional[int]) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE sessions SET forgejo_issue=?, updated_at=? WHERE id=?",
                         (issue, time.time(), session_id))

    def set_session_label(self, session_id: str, label: Optional[str]) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE sessions SET user_label=?, updated_at=? WHERE id=?",
                         (label, time.time(), session_id))

    def set_archived(self, session_id: str, archived: bool) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE sessions SET archived=?, updated_at=? WHERE id=?",
                         (int(archived), time.time(), session_id))

    def stored_sessions(self) -> dict[str, sqlite3.Row]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM sessions").fetchall()
        return {row["id"]: row for row in rows}

    # ---------------------------------------------------------------------- audit

    def audit(self, action: str, *, actor: Optional[str] = None,
              session_id: Optional[str] = None, repository: Optional[str] = None,
              detail: Optional[dict[str, Any]] = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO audit_log (timestamp, actor, action, session_id, repository, detail)"
                " VALUES (?,?,?,?,?,?)",
                (time.time(), actor, action, session_id, repository,
                 json.dumps(detail) if detail else None))

    def audit_entries(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
        return [{**dict(r), "detail": json.loads(r["detail"]) if r["detail"] else None}
                for r in rows]

    # ------------------------------------------------------------------- activity

    def add_activity(self, session_id: Optional[str], kind: str, text: Optional[str],
                     detail: Optional[dict[str, Any]] = None,
                     timestamp: Optional[float] = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO activity (timestamp, session_id, kind, text, detail)"
                " VALUES (?,?,?,?,?)",
                (timestamp or time.time(), session_id, kind, text,
                 json.dumps(detail) if detail else None))

    def activity(self, limit: int = 100,
                 session_id: Optional[str] = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM activity"
        params: list[Any] = []
        if session_id:
            query += " WHERE session_id=?"
            params.append(session_id)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [{**dict(r), "detail": json.loads(r["detail"]) if r["detail"] else None}
                for r in rows]

    # ------------------------------------------------------------ review comments

    def add_review_comment(self, session_id: str, file_path: str,
                           line: Optional[int], body: str) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO review_comments (session_id, file_path, line, body, created_at)"
                " VALUES (?,?,?,?,?)",
                (session_id, file_path, line, body, time.time()))
            return int(cur.lastrowid)

    def review_comments(self, session_id: str, *,
                        unsent_only: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM review_comments WHERE session_id=?"
        if unsent_only:
            query += " AND sent_at IS NULL"
        query += " ORDER BY file_path, line"
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(query, (session_id,)).fetchall()]

    def delete_review_comment(self, comment_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM review_comments WHERE id=?", (comment_id,))

    def mark_comments_sent(self, ids: Iterable[int]) -> None:
        now = time.time()
        with self.connect() as conn:
            conn.executemany("UPDATE review_comments SET sent_at=? WHERE id=?",
                             [(now, i) for i in ids])

    # ------------------------------------------------------------------ worktrees

    def add_worktree(self, path: str, repository: str, branch: Optional[str],
                     base_commit: Optional[str], session_id: Optional[str],
                     issue: Optional[int]) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO worktrees"
                " (path, repository, branch, base_commit, session_id, issue, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (path, repository, branch, base_commit, session_id, issue, time.time()))

    def worktrees(self, repository: Optional[str] = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM worktrees"
        params: list[Any] = []
        if repository:
            query += " WHERE repository=?"
            params.append(repository)
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(query, params).fetchall()]

    def remove_worktree(self, path: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM worktrees WHERE path=?", (path,))
