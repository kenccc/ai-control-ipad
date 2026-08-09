"""Terminal routes: PTY lifecycle over REST, byte stream over WebSocket."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from fastapi import (APIRouter, Body, Depends, HTTPException, Request, WebSocket,
                     WebSocketDisconnect, status)

from .deps import AppState, state

log = logging.getLogger("aicontrol.api.terminal")
router = APIRouter(prefix="/api/terminals", tags=["terminal"])


@router.get("")
async def list_terminals(request: Request,
                         app: AppState = Depends(state)) -> dict[str, Any]:
    app.auth.require_session(request)
    return {"terminals": app.pty.list()}


@router.post("")
async def open_terminal(request: Request, cwd: Optional[str] = Body(None),
                        repository: Optional[str] = Body(None),
                        session_id: Optional[str] = Body(None),
                        cols: int = Body(120), rows: int = Body(32),
                        app: AppState = Depends(state)) -> dict[str, Any]:
    app.auth.require_write(request)

    target = cwd
    if session_id and not target:
        session = app.registry.get(session_id)
        if session is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown session")
        if not session.capabilities.terminal:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "This session does not have a terminal. It runs in a process AI "
                "Control does not own.")
        target = session.working_directory
    if repository and not target:
        target = str(app.repo_or_404(repository).path)
    if not target:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "cwd, repository or session_id required")

    app.require_allowed_path(target)
    try:
        pty_session = app.pty.spawn(target, cols=cols, rows=rows)
    except (ValueError, OSError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    app.db.audit("terminal_opened", session_id=session_id, repository=repository,
                 detail={"cwd": target, "ptyId": pty_session.id})
    return {"ok": True, "terminal": {"id": pty_session.id, "cwd": pty_session.cwd,
                                     "cols": cols, "rows": rows}}


@router.delete("/{pty_id}")
async def close_terminal(pty_id: str, request: Request,
                         app: AppState = Depends(state)) -> dict[str, Any]:
    app.auth.require_write(request)
    app.pty.close(pty_id)
    return {"ok": True}


@router.websocket("/{pty_id}/stream")
async def terminal_stream(websocket: WebSocket, pty_id: str) -> None:
    app: AppState = websocket.app.state.app_state
    # WebSocket upgrades bypass CORS, so origin and session are checked by hand here.
    if not app.auth.check_origin(websocket.headers.get("origin"),
                                 websocket.headers.get("host")):
        await websocket.close(code=4403)
        return
    from ..auth import SESSION_COOKIE
    if not app.auth.verify_session(websocket.cookies.get(SESSION_COOKIE)):
        await websocket.close(code=4401)
        return

    try:
        queue, backlog = app.pty.subscribe(pty_id)
    except ValueError:
        await websocket.close(code=4404)
        return

    await websocket.accept()
    if backlog:
        # Only the retained tail, so a reconnect redraws the screen without replaying
        # the entire history over the wire.
        await websocket.send_bytes(backlog)

    async def pump() -> None:
        while True:
            chunk = await queue.get()
            if chunk == b"":
                await websocket.close(code=1000)
                return
            await websocket.send_bytes(chunk)

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            if (data := message.get("bytes")) is not None:
                app.pty.write(pty_id, data)
            elif (text := message.get("text")) is not None:
                if text.startswith("\x00resize:"):
                    try:
                        cols, rows = text[8:].split(",")
                        app.pty.resize(pty_id, int(cols), int(rows))
                    except ValueError:
                        pass
                else:
                    app.pty.write(pty_id, text.encode())
    except (WebSocketDisconnect, RuntimeError, ValueError):
        pass
    finally:
        pump_task.cancel()
        app.pty.unsubscribe(pty_id, queue)
