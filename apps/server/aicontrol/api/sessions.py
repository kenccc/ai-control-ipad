"""Session routes: list, inspect, continue, interrupt, review."""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from ..models import Source, Status
from ..providers.base import CapabilityError, ProviderError
from ..services.review import (
    build_feedback_prompt, build_review_prompt, findings_to_prompt, parse_review_output,
)
from .deps import AppState, state

log = logging.getLogger("aicontrol.api.sessions")
router = APIRouter(prefix="/api", tags=["sessions"])


class MessageBody(BaseModel):
    message: str = Field(min_length=1, max_length=32_000)


class CreateSessionBody(BaseModel):
    provider: str                       # codex_cli | claude_code | codex_desktop
    repository: str
    prompt: str = Field(min_length=1, max_length=32_000)
    model: Optional[str] = None
    branch_mode: str = "current"        # current | new_branch | new_worktree
    branch: Optional[str] = None
    issue: Optional[int] = None
    approval_policy: Optional[str] = None
    permission_mode: Optional[str] = None
    sandbox: Optional[str] = None
    #: Run the agent without approval prompts, using each tool's own documented
    #: unattended mode -- Codex `approvalPolicy: never` with a full-access sandbox,
    #: Claude Code `--permission-mode bypassPermissions`. Never a default: it must be
    #: chosen per session, and every use is audited.
    bypass_permissions: bool = False


class ReviewCommentBody(BaseModel):
    file_path: str
    line: Optional[int] = None
    body: str = Field(min_length=1, max_length=8_000)


class CrossReviewBody(BaseModel):
    reviewer: str                       # claude_code | codex_cli


def _capability_error(exc: CapabilityError) -> HTTPException:
    # 409, not 400: the request is well-formed, the operation simply is not available
    # for this session. The UI shows the provider's own explanation verbatim.
    return HTTPException(status.HTTP_409_CONFLICT, str(exc))


@router.get("/sessions")
async def list_sessions(
    request: Request,
    source: Optional[str] = Query(None),
    repository: Optional[str] = Query(None),
    active_only: bool = Query(False),
    include_archived: bool = Query(False),
    app: AppState = Depends(state),
) -> dict[str, Any]:
    app.auth.require_session(request)
    sessions = app.registry.sessions(include_archived=include_archived)
    if source:
        wanted = {s.strip() for s in source.split(",")}
        sessions = [s for s in sessions if s.source.value in wanted]
    if repository:
        sessions = [s for s in sessions if s.repository == repository]
    if active_only:
        sessions = [s for s in sessions if s.status.is_active]
    return {
        "sessions": [s.to_dict() for s in sessions],
        "lastReconcile": app.registry.last_reconcile,
    }


@router.get("/sessions/{session_id:path}/events")
async def session_events(session_id: str, request: Request,
                         limit: int = Query(400, le=5000),
                         app: AppState = Depends(state)) -> dict[str, Any]:
    app.auth.require_session(request)
    session, provider = _require(app, session_id)
    if not session.capabilities.read_conversation:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "Reading this session's conversation is not available.")
    try:
        events = await provider.get_conversation(session_id, limit=limit)
    except CapabilityError as exc:
        raise _capability_error(exc) from exc
    return {"events": [e.to_dict() for e in events]}


@router.get("/sessions/{session_id:path}/changes")
async def session_changes(session_id: str, request: Request,
                          app: AppState = Depends(state)) -> dict[str, Any]:
    app.auth.require_session(request)
    session, _ = _require(app, session_id)
    cwd = session.working_directory
    if not cwd:
        return {"files": [], "stats": None, "reason": "session has no working directory"}
    files = await app.git.changed_files(cwd)
    stats = await app.git.diff_stats(cwd)
    return {"files": [f.to_dict() for f in files], "stats": stats.to_dict(),
            "workingDirectory": cwd}


@router.get("/sessions/{session_id:path}/diff")
async def session_diff(session_id: str, request: Request,
                       file: str = Query(...),
                       context: int = Query(3, ge=0, le=50),
                       app: AppState = Depends(state)) -> dict[str, Any]:
    app.auth.require_session(request)
    session, _ = _require(app, session_id)
    if not session.working_directory:
        raise HTTPException(status.HTTP_409_CONFLICT, "session has no working directory")
    diff = await app.git.file_diff(session.working_directory, file, context=context)
    return {"file": file, "diff": diff}


@router.post("/sessions/{session_id:path}/messages")
async def send_message(session_id: str, request: Request, body: MessageBody,
                       app: AppState = Depends(state)) -> dict[str, Any]:
    app.auth.require_write(request)
    session, provider = _require(app, session_id)
    try:
        await provider.send_message(session_id, body.message)
    except CapabilityError as exc:
        raise _capability_error(exc) from exc
    except ProviderError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    app.db.audit("message_sent", session_id=session_id, repository=session.repository,
                 detail={"length": len(body.message), "source": session.source.value})
    app.db.add_activity(session_id, "message", "Message sent")
    app.bus.publish("session.message", sessionId=session_id)
    return {"ok": True}


@router.post("/sessions/{session_id:path}/interrupt")
async def interrupt(session_id: str, request: Request,
                    app: AppState = Depends(state)) -> dict[str, Any]:
    app.auth.require_write(request)
    session, provider = _require(app, session_id)
    try:
        await provider.interrupt(session_id)
    except CapabilityError as exc:
        raise _capability_error(exc) from exc
    app.db.audit("agent_interrupted", session_id=session_id,
                 repository=session.repository)
    return {"ok": True}


@router.post("/sessions/{session_id:path}/resume")
async def resume(session_id: str, request: Request,
                 app: AppState = Depends(state)) -> dict[str, Any]:
    app.auth.require_write(request)
    session, provider = _require(app, session_id)
    try:
        await provider.resume(session_id)
    except CapabilityError as exc:
        raise _capability_error(exc) from exc
    app.db.audit("agent_resumed", session_id=session_id, repository=session.repository)
    return {"ok": True}


@router.post("/sessions/{session_id:path}/terminate")
async def terminate(session_id: str, request: Request,
                    app: AppState = Depends(state)) -> dict[str, Any]:
    app.auth.require_write(request)
    session, provider = _require(app, session_id)
    try:
        await provider.terminate(session_id)
    except CapabilityError as exc:
        raise _capability_error(exc) from exc
    app.db.audit("agent_terminated", session_id=session_id,
                 repository=session.repository)
    return {"ok": True}


@router.post("/sessions/{session_id:path}/archive")
async def archive(session_id: str, request: Request, archived: bool = Body(True, embed=True),
                  app: AppState = Depends(state)) -> dict[str, Any]:
    app.auth.require_write(request)
    _require(app, session_id)
    app.db.set_archived(session_id, archived)
    return {"ok": True, "archived": archived}


@router.post("/sessions/{session_id:path}/issue")
async def link_issue(session_id: str, request: Request,
                     issue: Optional[int] = Body(None, embed=True),
                     app: AppState = Depends(state)) -> dict[str, Any]:
    app.auth.require_write(request)
    session, _ = _require(app, session_id)
    app.db.set_session_issue(session_id, issue)
    session.forgejo_issue = issue
    return {"ok": True, "issue": issue}


# ------------------------------------------------------------------------- review

@router.get("/sessions/{session_id:path}/review")
async def list_review_comments(session_id: str, request: Request,
                               app: AppState = Depends(state)) -> dict[str, Any]:
    app.auth.require_session(request)
    return {"comments": app.db.review_comments(session_id)}


@router.post("/sessions/{session_id:path}/review")
async def add_review_comment(session_id: str, request: Request,
                             body: ReviewCommentBody,
                             app: AppState = Depends(state)) -> dict[str, Any]:
    app.auth.require_write(request)
    _require(app, session_id)
    comment_id = app.db.add_review_comment(session_id, body.file_path, body.line,
                                           body.body)
    return {"ok": True, "id": comment_id}


@router.delete("/sessions/{session_id:path}/review/{comment_id}")
async def delete_review_comment(session_id: str, comment_id: int, request: Request,
                                app: AppState = Depends(state)) -> dict[str, Any]:
    app.auth.require_write(request)
    app.db.delete_review_comment(comment_id)
    return {"ok": True}


@router.post("/sessions/{session_id:path}/review/send")
async def send_review(session_id: str, request: Request,
                      app: AppState = Depends(state)) -> dict[str, Any]:
    """Send the collected inline comments back to the session that made the changes."""
    app.auth.require_write(request)
    session, provider = _require(app, session_id)
    comments = app.db.review_comments(session_id, unsent_only=True)
    if not comments:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no unsent review comments")
    prompt = build_feedback_prompt(comments)
    try:
        await provider.send_message(session_id, prompt)
    except CapabilityError as exc:
        raise _capability_error(exc) from exc
    app.db.mark_comments_sent([c["id"] for c in comments])
    app.db.audit("review_sent", session_id=session_id, repository=session.repository,
                 detail={"comments": len(comments)})
    return {"ok": True, "sent": len(comments)}


@router.post("/sessions/{session_id:path}/cross-review")
async def cross_review(session_id: str, request: Request, body: CrossReviewBody,
                       app: AppState = Depends(state)) -> dict[str, Any]:
    """Launch the *other* agent to review this session's diff, read-only."""
    app.auth.require_write(request)
    session, _ = _require(app, session_id)
    cwd = session.working_directory
    if not cwd:
        raise HTTPException(status.HTTP_409_CONFLICT, "session has no working directory")
    app.require_allowed_path(cwd)

    reviewer = {"claude_code": app.claude_code, "codex_cli": app.codex_cli}.get(body.reviewer)
    if reviewer is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "reviewer must be claude_code or codex_cli")

    files = await app.git.changed_files(cwd)
    if not files:
        raise HTTPException(status.HTTP_409_CONFLICT, "there are no changes to review")
    diff_parts = []
    for change in files[:60]:
        diff_parts.append(await app.git.file_diff(cwd, change.path))
    prompt = build_review_prompt(diff="\n".join(diff_parts),
                                 repository=session.repository or cwd,
                                 branch=session.branch)

    review_session = await reviewer.create_session(
        cwd=cwd, prompt=prompt,
        **({"permission_mode": "plan"} if body.reviewer == "claude_code"
           else {"approval_policy": "never"}))
    review_session.metadata["reviewOf"] = session_id
    app.db.audit("cross_review_started", session_id=session_id,
                 repository=session.repository,
                 detail={"reviewer": body.reviewer, "reviewSession": review_session.id})
    return {"ok": True, "reviewSessionId": review_session.id}


@router.post("/sessions/{session_id:path}/review/forward")
async def forward_review(session_id: str, request: Request,
                         target_session_id: str = Body(..., embed=True),
                         app: AppState = Depends(state)) -> dict[str, Any]:
    """Send a completed review's findings to the agent that wrote the code."""
    app.auth.require_write(request)
    review_session, review_provider = _require(app, session_id)
    target, target_provider = _require(app, target_session_id)

    events = await review_provider.get_conversation(session_id, limit=200)
    text = "\n".join(e.text or "" for e in events if e.kind.value == "agent_message")
    findings = parse_review_output(text)
    if not findings:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "no structured findings were produced by the review")
    prompt = findings_to_prompt(findings, reviewer=review_session.source.label)
    try:
        await target_provider.send_message(target_session_id, prompt)
    except CapabilityError as exc:
        raise _capability_error(exc) from exc
    app.db.audit("review_forwarded", session_id=target_session_id,
                 detail={"from": session_id, "findings": len(findings)})
    return {"ok": True, "findings": [f.to_dict() for f in findings]}


@router.get("/sessions/{session_id:path}/review/findings")
async def review_findings(session_id: str, request: Request,
                          app: AppState = Depends(state)) -> dict[str, Any]:
    app.auth.require_session(request)
    _, provider = _require(app, session_id)
    events = await provider.get_conversation(session_id, limit=200)
    text = "\n".join(e.text or "" for e in events if e.kind.value == "agent_message")
    return {"findings": [f.to_dict() for f in parse_review_output(text)]}


# --------------------------------------------------------------------- new session

@router.post("/sessions")
async def create_session(request: Request, body: CreateSessionBody,
                         app: AppState = Depends(state)) -> dict[str, Any]:
    app.auth.require_write(request)
    repo = app.repo_or_404(body.repository)

    provider = {"codex_cli": app.codex_cli, "claude_code": app.claude_code,
                "codex_desktop": app.codex_desktop}.get(body.provider)
    if provider is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown provider")

    cwd = str(repo.path)
    worktree_path: Optional[str] = None
    if body.branch_mode == "new_worktree":
        from ..services.worktrees import WorktreeError
        label = body.branch or f"{body.provider}-{body.issue or 'task'}"
        try:
            worktree = await app.worktrees.create(
                body.repository, label=label, branch=body.branch, issue=body.issue)
        except WorktreeError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        cwd = str(worktree.path)
        worktree_path = cwd
    elif body.branch_mode == "new_branch" and body.branch:
        import asyncio
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", cwd, "checkout", "-b", body.branch,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                err.decode(errors="replace").strip())

    app.require_allowed_path(cwd)

    # Refuse to put a second writing agent into a tree another agent already holds.
    if body.branch_mode != "new_worktree":
        holder = app.worktrees.active_writer(cwd)
        if holder:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{holder} is already writing in {cwd}. Use a new worktree, or stop "
                "that agent first.")

    prompt = body.prompt
    if body.issue and repo.forgejo and app.forgejo:
        from ..services.forgejo import build_issue_context
        owner, name = app.split_slug(repo.forgejo)
        try:
            issue = await app.forgejo.issue(owner, name, body.issue)
            comments = await app.forgejo.issue_comments(owner, name, body.issue)
            prompt = build_issue_context(issue, comments,
                                         repo_slug=repo.forgejo) + "\n\n" + prompt
        except Exception as exc:
            log.warning("could not attach issue context: %s", exc)

    kwargs: dict[str, Any] = {"cwd": cwd, "prompt": prompt, "model": body.model}
    if body.provider == "claude_code":
        kwargs["permission_mode"] = (
            "bypassPermissions" if body.bypass_permissions
            else (body.permission_mode or "default"))
    else:
        # Codex needs both halves: approvals off, and a sandbox that does not veto what
        # the approvals would have gated. Setting only one produces an agent that still
        # stalls, which is worse than not offering the mode.
        kwargs["approval_policy"] = (
            "never" if body.bypass_permissions else body.approval_policy)
        kwargs["sandbox"] = (
            "danger-full-access" if body.bypass_permissions else body.sandbox)

    if body.bypass_permissions:
        log.warning("starting %s in %s without approval prompts",
                    body.provider, body.repository)

    try:
        session = await provider.create_session(**kwargs)
    except CapabilityError as exc:
        raise _capability_error(exc) from exc
    except ProviderError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    if body.issue:
        app.db.set_session_issue(session.id, body.issue)
        session.forgejo_issue = body.issue
    if worktree_path:
        app.db.add_worktree(worktree_path, body.repository, body.branch, None,
                            session.id, body.issue)
    app.db.audit("session_created", session_id=session.id, repository=body.repository,
                 detail={"provider": body.provider, "issue": body.issue,
                         "branchMode": body.branch_mode,
                         "bypassPermissions": body.bypass_permissions})
    if body.bypass_permissions:
        # Recorded separately so it is greppable in the audit log on its own.
        app.db.audit("permissions_bypassed", session_id=session.id,
                     repository=body.repository,
                     detail={"provider": body.provider, "cwd": cwd})
        app.db.add_activity(session.id, "bypass",
                            "Started without approval prompts")
    app.bus.publish("session.created", sessionId=session.id,
                    session=session.to_dict())
    return {"ok": True, "session": session.to_dict()}


def _require(app: AppState, session_id: str):
    try:
        return app.registry.require(session_id)
    except ProviderError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
