"""Git worktree management, one worktree per independently-writing agent.

Two agents editing the same tree at once corrupt each other's work, so the manager
refuses to hand the same path to a second writer unless the caller explicitly opts in.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..config import Config
from ..db import Database

log = logging.getLogger("aicontrol.worktrees")

_SAFE = re.compile(r"[^a-zA-Z0-9._-]+")


def slugify(value: str, *, max_len: int = 48) -> str:
    return _SAFE.sub("-", value).strip("-")[:max_len] or "agent"


class WorktreeError(RuntimeError):
    pass


@dataclass
class Worktree:
    path: Path
    repository: str
    branch: str
    base_commit: Optional[str]


class WorktreeManager:
    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db

    def root_for(self, repository: str) -> Path:
        return self.config.worktree_root / slugify(repository)

    async def _git(self, cwd: Path, *args: str) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(cwd), *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")

    def active_writer(self, path: str | Path) -> Optional[str]:
        """The session currently registered as writing in `path`, if any."""
        target = str(Path(path).resolve())
        for row in self.db.worktrees():
            if Path(row["path"]).resolve() == Path(target) and row["session_id"]:
                return row["session_id"]
        return None

    async def create(self, repository: str, *, label: str,
                     branch: Optional[str] = None,
                     base: Optional[str] = None,
                     session_id: Optional[str] = None,
                     issue: Optional[int] = None) -> Worktree:
        repo = self.config.repositories.get(repository)
        if repo is None:
            raise WorktreeError(f"{repository} is not in the repository allowlist")
        if not repo.path.is_dir():
            raise WorktreeError(f"{repo.path} does not exist")

        branch_name = branch or f"ai/{slugify(label)}"
        target = self.root_for(repository) / slugify(label)
        if target.exists():
            raise WorktreeError(f"worktree {target} already exists")
        target.parent.mkdir(parents=True, exist_ok=True)

        code, out, _ = await self._git(repo.path, "rev-parse", base or "HEAD")
        base_commit = out.strip() if code == 0 else None

        args = ["worktree", "add"]
        code, branches, _ = await self._git(repo.path, "branch", "--list", branch_name)
        if branches.strip():
            args += [str(target), branch_name]
        else:
            args += ["-b", branch_name, str(target), base or "HEAD"]

        code, _, err = await self._git(repo.path, *args)
        if code != 0:
            raise WorktreeError(err.strip() or "git worktree add failed")

        self.db.add_worktree(str(target), repository, branch_name, base_commit,
                             session_id, issue)
        return Worktree(path=target, repository=repository, branch=branch_name,
                        base_commit=base_commit)

    async def remove(self, path: str | Path, *, force: bool = False) -> None:
        target = Path(path)
        row = next((r for r in self.db.worktrees()
                    if Path(r["path"]) == target), None)
        if row is None:
            raise WorktreeError(f"{target} is not a tracked worktree")
        repo = self.config.repositories.get(row["repository"])
        if repo is None:
            raise WorktreeError(f"{row['repository']} is not in the allowlist")
        args = ["worktree", "remove", str(target)]
        if force:
            args.insert(2, "--force")
        code, _, err = await self._git(repo.path, *args)
        if code != 0 and not force:
            raise WorktreeError(err.strip() or "git worktree remove failed")
        self.db.remove_worktree(str(target))

    async def list_git_worktrees(self, repository: str) -> list[dict[str, str]]:
        repo = self.config.repositories.get(repository)
        if repo is None:
            return []
        code, out, _ = await self._git(repo.path, "worktree", "list", "--porcelain")
        if code != 0:
            return []
        entries: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for line in out.splitlines():
            if not line.strip():
                if current:
                    entries.append(current)
                    current = {}
                continue
            key, _, value = line.partition(" ")
            current[key] = value
        if current:
            entries.append(current)
        return entries
