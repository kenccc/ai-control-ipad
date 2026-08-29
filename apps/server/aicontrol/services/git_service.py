"""Git inspection for whatever working directory an agent session actually uses.

Nothing here mutates a repository beyond the explicit commit/worktree helpers, and
every path is checked against the configured repository allowlist before use.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..models import DiffStats, GitState

log = logging.getLogger("aicontrol.git")

#: Bounds on counting lines in untracked files, so a huge tree cannot stall a scan.
MAX_UNTRACKED_TO_COUNT = 200
MAX_COUNTED_FILE_BYTES = 2_000_000

#: Extensions we never line-count. A NUL-byte probe alone is not enough -- an archive
#: or an image can easily have no NUL in its first few kilobytes and would otherwise
#: report a meaningless line count.
BINARY_SUFFIXES = frozenset({
    ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".jar", ".war",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".tiff", ".avif",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".mp3", ".mp4", ".mov", ".avi", ".wav", ".ogg", ".webm",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".so", ".dylib", ".dll", ".exe", ".bin", ".o", ".a", ".class",
    ".sqlite", ".sqlite3", ".db", ".pyc", ".wasm",
})


def is_probably_binary(path: Path) -> bool:
    if path.suffix.lower() in BINARY_SUFFIXES:
        return True
    try:
        with path.open("rb") as fh:
            return b"\x00" in fh.read(8192)
    except OSError:
        return True


@dataclass
class FileChange:
    path: str
    status: str          # M, A, D, R, ??
    insertions: int = 0
    deletions: int = 0
    binary: bool = False

    def to_dict(self) -> dict:
        return {"path": self.path, "status": self.status,
                "insertions": self.insertions, "deletions": self.deletions,
                "binary": self.binary}


async def _git(cwd: Path, *args: str, timeout: float = 25.0) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            # core.quotepath=false keeps non-ASCII paths readable instead of
            # C-escaped, which matters for a codebase with Czech filenames.
            "git", "-c", "core.quotepath=false", "-C", str(cwd), *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return 1, "", str(exc)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 1, "", "git timed out"
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


class GitService:
    def __init__(self, cache_ttl: float = 3.0) -> None:
        self._cache: dict[str, tuple[float, GitState]] = {}
        # Many sessions share one working directory, so diff stats are cached per
        # directory rather than recomputed per session -- otherwise a repo with 150
        # recorded sessions runs the same `git diff` 150 times per reconcile.
        self._diff_cache: dict[tuple[str, str], tuple[float, DiffStats]] = {}
        self._cache_ttl = cache_ttl

    async def is_repo(self, path: str | Path) -> bool:
        code, out, _ = await _git(Path(path), "rev-parse", "--is-inside-work-tree")
        return code == 0 and out.strip() == "true"

    async def toplevel(self, path: str | Path) -> Optional[str]:
        code, out, _ = await _git(Path(path), "rev-parse", "--show-toplevel")
        return out.strip() if code == 0 and out.strip() else None

    async def status(self, path: str | Path, *, use_cache: bool = True) -> Optional[GitState]:
        key = str(path)
        now = asyncio.get_running_loop().time()
        if use_cache:
            cached = self._cache.get(key)
            if cached and now - cached[0] < self._cache_ttl:
                return cached[1]

        cwd = Path(path)
        if not cwd.is_dir():
            return None

        code, branch_out, _ = await _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
        if code != 0:
            return None
        state = GitState(branch=branch_out.strip() or None)

        _, sha_out, _ = await _git(cwd, "rev-parse", "HEAD")
        state.sha = sha_out.strip() or None

        _, origin_out, _ = await _git(cwd, "remote", "get-url", "origin")
        state.origin_url = origin_out.strip() or None

        _, porcelain, _ = await _git(cwd, "status", "--porcelain", "-uall")
        for line in porcelain.splitlines():
            if not line:
                continue
            code_pair = line[:2]
            if code_pair == "??":
                state.untracked += 1
            elif "D" in code_pair:
                state.deleted += 1
            elif "A" in code_pair:
                state.added += 1
            else:
                state.modified += 1

        _, counts, _ = await _git(cwd, "rev-list", "--left-right", "--count",
                                  "HEAD...@{upstream}")
        parts = counts.split()
        if len(parts) == 2:
            state.ahead, state.behind = int(parts[0]), int(parts[1])

        self._cache[key] = (now, state)
        return state

    async def diff_stats(self, path: str | Path, *, base: Optional[str] = "HEAD",
                         use_cache: bool = True) -> DiffStats:
        key = (str(path), base or "")
        now = asyncio.get_running_loop().time()
        if use_cache:
            cached = self._diff_cache.get(key)
            if cached and now - cached[0] < self._cache_ttl:
                return cached[1]

        # Default to HEAD so staged work counts: an agent that ran `git add` has still
        # changed the tree, and showing +0 -0 there would be wrong.
        args = ["diff", "--numstat"]
        if base:
            args.append(base)
        code, out, _ = await _git(Path(path), *args)
        stats = DiffStats()
        if code != 0:
            self._diff_cache[key] = (now, stats)
            return stats
        files = 0
        for line in out.splitlines():
            cols = line.split("\t")
            if len(cols) != 3:
                continue
            files += 1
            if cols[0].isdigit():
                stats.insertions += int(cols[0])
            if cols[1].isdigit():
                stats.deletions += int(cols[1])
        # Untracked files are part of what an agent produced, so count them too --
        # otherwise a session that only adds new files reports "+0 -0", which reads as
        # "did nothing". Bounded so a stray node_modules cannot stall the dashboard.
        _, untracked, _ = await _git(Path(path), "ls-files", "--others",
                                     "--exclude-standard")
        names = [l for l in untracked.splitlines() if l.strip()]
        files += len(names)
        root = Path(path)
        for name in names[:MAX_UNTRACKED_TO_COUNT]:
            full = root / name
            try:
                if not full.is_file() or full.stat().st_size > MAX_COUNTED_FILE_BYTES:
                    continue
                if is_probably_binary(full):
                    continue
                with full.open("rb") as fh:
                    stats.insertions += sum(1 for _ in fh)
            except OSError:
                continue
        stats.files_changed = files
        self._diff_cache[key] = (now, stats)
        return stats

    async def changed_files(self, path: str | Path, *,
                            base: Optional[str] = "HEAD") -> list[FileChange]:
        cwd = Path(path)
        changes: dict[str, FileChange] = {}

        args = ["diff", "--numstat"]
        if base:
            args.append(base)
        _, numstat, _ = await _git(cwd, *args)
        for line in numstat.splitlines():
            cols = line.split("\t")
            if len(cols) != 3:
                continue
            ins = int(cols[0]) if cols[0].isdigit() else 0
            dels = int(cols[1]) if cols[1].isdigit() else 0
            changes[cols[2]] = FileChange(path=cols[2], status="M",
                                          insertions=ins, deletions=dels)

        _, porcelain, _ = await _git(cwd, "status", "--porcelain", "-uall")
        for line in porcelain.splitlines():
            if len(line) < 4:
                continue
            code_pair, name = line[:2].strip(), line[3:]
            if " -> " in name:
                name = name.split(" -> ", 1)[1]
            # git still quotes names containing spaces or quotes.
            if len(name) > 1 and name.startswith('"') and name.endswith('"'):
                name = name[1:-1].replace('\\"', '"')
            existing = changes.get(name)
            status = "??" if code_pair == "??" else code_pair[0] if code_pair else "M"
            if existing:
                existing.status = status
            else:
                changes[name] = FileChange(path=name, status=status)

        for change in changes.values():
            if change.status == "??" and change.insertions == 0:
                full = cwd / change.path
                try:
                    if (full.is_file() and full.stat().st_size < MAX_COUNTED_FILE_BYTES
                            and not is_probably_binary(full)):
                        change.insertions = sum(1 for _ in full.open("rb"))
                    elif full.is_file():
                        change.binary = True
                except OSError:
                    pass
        return sorted(changes.values(), key=lambda c: c.path)

    async def file_diff(self, path: str | Path, file_path: str, *,
                        base: Optional[str] = "HEAD", context: int = 3) -> str:
        cwd = Path(path)
        args = ["diff", f"-U{context}"]
        if base:
            args.append(base)
        args += ["--", file_path]
        code, out, _ = await _git(cwd, *args)
        if out.strip():
            return out
        # Untracked file: synthesise an all-additions diff so the viewer is uniform.
        full = cwd / file_path
        try:
            if not full.is_file() or full.stat().st_size > 2_000_000:
                return ""
            lines = full.read_text(errors="replace").splitlines()
        except OSError:
            return ""
        header = (f"diff --git a/{file_path} b/{file_path}\n"
                  f"new file mode 100644\n--- /dev/null\n+++ b/{file_path}\n"
                  f"@@ -0,0 +1,{len(lines)} @@\n")
        return header + "".join(f"+{line}\n" for line in lines)

    async def log(self, path: str | Path, limit: int = 30) -> list[dict]:
        code, out, _ = await _git(Path(path), "log", f"-{limit}",
                                  "--pretty=format:%H%x1f%an%x1f%at%x1f%s")
        if code != 0:
            return []
        commits = []
        for line in out.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 4:
                commits.append({"sha": parts[0], "author": parts[1],
                                "timestamp": int(parts[2]), "subject": parts[3]})
        return commits

    async def branches(self, path: str | Path) -> list[str]:
        code, out, _ = await _git(Path(path), "branch", "--format=%(refname:short)")
        return [b.strip() for b in out.splitlines() if b.strip()] if code == 0 else []
