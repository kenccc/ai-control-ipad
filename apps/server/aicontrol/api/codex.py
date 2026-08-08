"""Codex Desktop projects -- the app's own sidebar, not a directory scan."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from .deps import AppState, state

router = APIRouter(prefix="/api/codex", tags=["codex"])


@router.get("/projects")
async def projects(request: Request, app: AppState = Depends(state)) -> dict[str, Any]:
    app.auth.require_session(request)
    provider = app.codex_desktop
    sessions = app.registry.sessions()
    out = []
    for project in provider.get_projects():
        members = [s.to_dict() for s in sessions
                   if s.metadata.get("codexProjectId") == project.id]
        out.append({**project.to_dict(), "sessions": members,
                    "activeSessions": sum(1 for s in members if s["isActive"])})
    return {"projects": out,
            "selectedProjectId": provider.global_state.selected_project_id(),
            "available": provider.global_state.available}
