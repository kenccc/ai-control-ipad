"""Forgejo routes. The API token stays server-side; nothing here leaks it."""

from __future__ import annotations

import re
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Path as PathParam, Query, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ..services.forgejo import ForgejoError
from .deps import AppState, state

#: Attachment ids are UUIDs. Validated before use so nothing else can reach the URL we
#: build, and so path traversal is impossible by construction.
_ATTACHMENT_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

#: Only images are proxied. Serving attacker-controlled HTML or SVG from our own origin
#: would be a same-origin scripting hole, so those are refused rather than sanitised.
_PROXYABLE_TYPES = frozenset({
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/avif", "image/bmp",
})

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

router = APIRouter(prefix="/api", tags=["forgejo"])


class CommentBody(BaseModel):
    body: str = Field(min_length=1, max_length=64_000)


def _repo_slug(app: AppState, repository: Optional[str]) -> str:
    if repository and "/" in repository:
        return repository
    if repository:
        repo = app.repo_or_404(repository)
        if not repo.forgejo:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                f"{repository} has no forgejo mapping in config.yaml")
        return repo.forgejo
    configured = [r.forgejo for r in app.config.repositories.values() if r.forgejo]
    if not configured:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "no repository has a forgejo mapping")
    return configured[0]


@router.get("/issues")
async def list_issues(request: Request, repository: Optional[str] = Query(None),
                      state_filter: str = Query("open", alias="state"),
                      limit: int = Query(50, le=100),
                      app: AppState = Depends(state)) -> dict[str, Any]:
    app.auth.require_session(request)
    client = app.forgejo_or_503()
    owner, name = app.split_slug(_repo_slug(app, repository))
    try:
        issues = await client.issues(owner, name, state=state_filter, limit=limit)
    except ForgejoError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    by_issue: dict[int, list[dict]] = {}
    for session in app.registry.sessions():
        if session.forgejo_issue:
            by_issue.setdefault(session.forgejo_issue, []).append(session.to_dict())
    return {"issues": [{**i, "agents": by_issue.get(i.get("number"), [])}
                       for i in issues],
            "repository": f"{owner}/{name}"}


@router.get("/issues/{index}")
async def issue_detail(index: int, request: Request,
                       repository: Optional[str] = Query(None),
                       app: AppState = Depends(state)) -> dict[str, Any]:
    app.auth.require_session(request)
    client = app.forgejo_or_503()
    owner, name = app.split_slug(_repo_slug(app, repository))
    try:
        issue = await client.issue(owner, name, index)
        comments = await client.issue_comments(owner, name, index)
    except ForgejoError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    agents = [s.to_dict() for s in app.registry.sessions()
              if s.forgejo_issue == index]
    return {"issue": issue, "comments": comments, "agents": agents,
            "repository": f"{owner}/{name}"}


@router.post("/issues/{index}/comments")
async def comment_issue(index: int, request: Request, body: CommentBody,
                        repository: Optional[str] = Query(None),
                        app: AppState = Depends(state)) -> dict[str, Any]:
    app.auth.require_write(request)
    client = app.forgejo_or_503()
    slug = _repo_slug(app, repository)
    owner, name = app.split_slug(slug)
    try:
        comment = await client.comment_on_issue(owner, name, index, body.body)
    except ForgejoError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    app.db.audit("forgejo_comment", repository=slug,
                 detail={"issue": index, "length": len(body.body)})
    return {"ok": True, "comment": comment}


@router.get("/pulls")
async def list_pulls(request: Request, repository: Optional[str] = Query(None),
                     state_filter: str = Query("open", alias="state"),
                     app: AppState = Depends(state)) -> dict[str, Any]:
    app.auth.require_session(request)
    client = app.forgejo_or_503()
    owner, name = app.split_slug(_repo_slug(app, repository))
    try:
        return {"pulls": await client.pulls(owner, name, state=state_filter),
                "repository": f"{owner}/{name}"}
    except ForgejoError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@router.get("/pulls/{index}")
async def pull_detail(index: int, request: Request,
                      repository: Optional[str] = Query(None),
                      app: AppState = Depends(state)) -> dict[str, Any]:
    app.auth.require_session(request)
    client = app.forgejo_or_503()
    owner, name = app.split_slug(_repo_slug(app, repository))
    try:
        pull = await client.pull(owner, name, index)
        files = await client.pull_files(owner, name, index)
        commits = await client.pull_commits(owner, name, index)
    except ForgejoError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    head = ((pull.get("head") or {}).get("ref"))
    agents = [s.to_dict() for s in app.registry.sessions() if s.branch == head]
    return {"pull": pull, "files": files, "commits": commits, "agents": agents,
            "repository": f"{owner}/{name}"}


@router.get("/forgejo/repos")
async def forgejo_repos(request: Request,
                        app: AppState = Depends(state)) -> dict[str, Any]:
    app.auth.require_session(request)
    client = app.forgejo_or_503()
    try:
        return {"repositories": await client.repos()}
    except ForgejoError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@router.get("/forgejo/attachment/{attachment_id}")
async def forgejo_attachment(request: Request,
                             attachment_id: str = PathParam(...),
                             app: AppState = Depends(state)) -> Response:
    """Proxy a Forgejo issue attachment so images render in the browser.

    Forgejo serves `/attachments/<uuid>` only to authenticated callers, and the API
    token lives on this server and must never reach the browser -- so the image cannot
    be loaded directly by an <img> tag. This fetches it with the token and streams the
    bytes back, which keeps the token server-side and the image visible.
    """
    app.auth.require_session(request)
    if not _ATTACHMENT_ID.match(attachment_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid attachment id")

    client = app.forgejo_or_503()
    try:
        body, content_type = await client.fetch_attachment(
            attachment_id, max_bytes=MAX_ATTACHMENT_BYTES)
    except ForgejoError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY if exc.status >= 500 else exc.status,
            str(exc)) from exc

    if content_type not in _PROXYABLE_TYPES:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"{content_type or 'this attachment'} is not an image, so it is not "
            "proxied. Open it in Forgejo instead.")

    return Response(
        content=body,
        media_type=content_type,
        headers={
            # Attachments are immutable, so let the iPad keep them.
            "Cache-Control": "private, max-age=86400",
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff",
            # Belt and braces: even if the type check were bypassed, nothing here runs.
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )
