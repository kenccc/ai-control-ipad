"""Diagnostics, activity feed, audit log and search."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request

from ..codex.appserver import daemon_status, discover_codex_binaries
from ..models import Source
from ..services import tailscale as tailscale_service
from .deps import AppState, state

router = APIRouter(prefix="/api", tags=["diagnostics"])


@router.get("/diagnostics")
async def diagnostics(request: Request,
                      app: AppState = Depends(state)) -> dict[str, Any]:
    app.auth.require_session(request)
    health = await app.registry.health()
    codex_health = health["providers"].get("codex_desktop", {})
    daemon = daemon_status()

    desktop_caps = {
        "discoverSessions": True,
        "readConversation": True,
        "readGitChanges": True,
        # Idle threads can always be continued via thread/resume on the same thread id.
        "continueIdleSession": True,
        # Live threads need a shared app-server that hosts them.
        "continueLiveSession": bool(daemon.get("running")),
        "interrupt": bool(daemon.get("running")),
        "approveActions": bool(daemon.get("running")),
        "startNewSessionInApp": False,
    }

    return {
        "server": {"ok": True, "wsSubscribers": app.bus.subscriber_count,
                   "lastReconcile": app.registry.last_reconcile,
                   "reconcileError": app.registry.last_error},
        "codexBinaries": [{"path": str(b.path), "version": b.version,
                           "desktopBundled": b.is_desktop_bundled}
                          for b in discover_codex_binaries()],
        "codexDesktop": {**codex_health, "capabilities": desktop_caps},
        "codexCli": health["providers"].get("codex_cli", {}),
        "claudeCode": health["providers"].get("claude_code", {}),
        "sharedDaemon": daemon,
        "forgejo": await _forgejo_status(app),
        "tailscale": tailscale_service.status(),
        "git": {"detected": shutil.which("git") is not None},
        "sessionCounts": health["sessionCounts"],
        "activeSessions": health["activeSessions"],
        "repositories": [r.to_dict() for r in app.config.repositories.values()],
        "terminals": len(app.pty.list()),
    }


async def _forgejo_status(app: AppState) -> dict[str, Any]:
    if app.forgejo is None:
        return {"configured": False, "connected": False,
                "hint": "Set forgejo.url in config.yaml and store a token with "
                        "./scripts/set-secret.sh forgejo-token"}
    try:
        user = await app.forgejo.whoami()
        return {"configured": True, "connected": True,
                "url": app.config.forgejo.url, "user": user.get("login")}
    except Exception as exc:
        return {"configured": True, "connected": False, "error": str(exc)[:200]}


@router.get("/activity")
async def activity(request: Request, limit: int = Query(100, le=500),
                   session_id: Optional[str] = Query(None),
                   app: AppState = Depends(state)) -> dict[str, Any]:
    app.auth.require_session(request)
    return {"activity": app.db.activity(limit=limit, session_id=session_id)}


@router.get("/audit")
async def audit(request: Request, limit: int = Query(200, le=1000),
                app: AppState = Depends(state)) -> dict[str, Any]:
    app.auth.require_session(request)
    return {"entries": app.db.audit_entries(limit)}


@router.get("/search")
async def search(request: Request, q: str = Query(..., min_length=1),
                 app: AppState = Depends(state)) -> dict[str, Any]:
    """Global search across sessions, repositories, Codex projects and Forgejo."""
    app.auth.require_session(request)
    needle = q.lower()

    sessions = [s.to_dict() for s in app.registry.sessions()
                if needle in (s.title or "").lower()
                or needle in (s.repository or "").lower()
                or needle in (s.branch or "").lower()][:20]

    repositories = [r.to_dict() for r in app.config.repositories.values()
                    if needle in r.name.lower()][:10]

    projects = [p.to_dict() for p in app.codex_desktop.get_projects()
                if needle in p.name.lower()][:10]

    issues: list[dict[str, Any]] = []
    if app.forgejo:
        for repo in app.config.repositories.values():
            if not repo.forgejo:
                continue
            try:
                owner, name = repo.forgejo.split("/", 1)
                found = await app.forgejo.issues(owner, name, limit=20)
                issues += [i for i in found
                           if needle in (i.get("title") or "").lower()
                           or needle in str(i.get("number"))][:10]
            except Exception:
                continue
            if len(issues) >= 10:
                break

    return {"sessions": sessions, "repositories": repositories,
            "codexProjects": projects, "issues": issues[:10]}


@router.get("/usage")
async def usage(request: Request, refresh: bool = Query(False),
                app: AppState = Depends(state)) -> dict[str, Any]:
    """Plan and rate-limit status for both agent providers.

    Codex reports real windows; Claude Code reports what it actually exposes, with a
    note saying why there is no percentage. Neither number is invented.
    """
    app.auth.require_session(request)
    return await app.usage.read(force=refresh)
