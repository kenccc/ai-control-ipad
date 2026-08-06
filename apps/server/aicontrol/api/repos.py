"""Repository routes -- status, changes, diffs, branches, commits, worktrees."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status

from ..services.worktrees import WorktreeError
from .deps import AppState, state

router = APIRouter(prefix="/api/repos", tags=["repos"])


@router.get("")
async def list_repos(request: Request, app: AppState = Depends(state)) -> dict[str, Any]:
    app.auth.require_session(request)
    out = []
    for repo in app.config.repositories.values():
        entry = repo.to_dict()
        if repo.path.is_dir():
            git_state = await app.git.status(repo.path)
            entry["git"] = git_state.to_dict() if git_state else None
        sessions = [s for s in app.registry.sessions() if s.repository == repo.name]
        entry["sessions"] = len(sessions)
        entry["activeSessions"] = sum(1 for s in sessions if s.status.is_active)
        out.append(entry)
    return {"repositories": out}


@router.get("/{name}")
async def repo_detail(name: str, request: Request,
                      app: AppState = Depends(state)) -> dict[str, Any]:
    app.auth.require_session(request)
    repo = app.repo_or_404(name)
    git_state = await app.git.status(repo.path, use_cache=False)
    return {
        **repo.to_dict(),
        "git": git_state.to_dict() if git_state else None,
        "worktrees": app.db.worktrees(name),
        "sessions": [s.to_dict() for s in app.registry.sessions()
                     if s.repository == name],
    }


@router.get("/{name}/changes")
async def repo_changes(name: str, request: Request,
                       app: AppState = Depends(state)) -> dict[str, Any]:
    app.auth.require_session(request)
    repo = app.repo_or_404(name)
    files = await app.git.changed_files(repo.path)
    stats = await app.git.diff_stats(repo.path)
    return {"files": [f.to_dict() for f in files], "stats": stats.to_dict()}


@router.get("/{name}/diff")
async def repo_diff(name: str, request: Request, file: str = Query(...),
                    context: int = Query(3, ge=0, le=50),
                    app: AppState = Depends(state)) -> dict[str, Any]:
    app.auth.require_session(request)
    repo = app.repo_or_404(name)
    return {"file": file,
            "diff": await app.git.file_diff(repo.path, file, context=context)}


@router.get("/{name}/commits")
async def repo_commits(name: str, request: Request, limit: int = Query(30, le=200),
                       app: AppState = Depends(state)) -> dict[str, Any]:
    app.auth.require_session(request)
    repo = app.repo_or_404(name)
    return {"commits": await app.git.log(repo.path, limit)}


@router.get("/{name}/branches")
async def repo_branches(name: str, request: Request,
                        app: AppState = Depends(state)) -> dict[str, Any]:
    app.auth.require_session(request)
    repo = app.repo_or_404(name)
    return {"branches": await app.git.branches(repo.path)}


@router.post("/{name}/worktrees")
async def create_worktree(name: str, request: Request,
                          label: str = Body(...), branch: Optional[str] = Body(None),
                          base: Optional[str] = Body(None),
                          app: AppState = Depends(state)) -> dict[str, Any]:
    app.auth.require_write(request)
    app.repo_or_404(name)
    try:
        worktree = await app.worktrees.create(name, label=label, branch=branch, base=base)
    except WorktreeError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    app.db.audit("worktree_created", repository=name,
                 detail={"path": str(worktree.path), "branch": worktree.branch})
    return {"ok": True, "worktree": {"path": str(worktree.path),
                                     "branch": worktree.branch,
                                     "baseCommit": worktree.base_commit}}


@router.delete("/{name}/worktrees")
async def remove_worktree(name: str, request: Request, path: str = Query(...),
                          force: bool = Query(False),
                          app: AppState = Depends(state)) -> dict[str, Any]:
    app.auth.require_write(request)
    app.repo_or_404(name)
    try:
        await app.worktrees.remove(path, force=force)
    except WorktreeError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    app.db.audit("worktree_removed", repository=name, detail={"path": path})
    return {"ok": True}
