"""The main event WebSocket: session status, git changes, discovery, permissions."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..auth import SESSION_COOKIE
from .deps import AppState

log = logging.getLogger("aicontrol.api.ws")
router = APIRouter()

HEARTBEAT_SECONDS = 20.0


@router.websocket("/api/stream")
async def stream(websocket: WebSocket) -> None:
    app: AppState = websocket.app.state.app_state
    if not app.auth.check_origin(websocket.headers.get("origin"),
                                 websocket.headers.get("host")):
        await websocket.close(code=4403)
        return
    if not app.auth.verify_session(websocket.cookies.get(SESSION_COOKIE)):
        await websocket.close(code=4401)
        return

    await websocket.accept()
    queue = app.bus.subscribe()
    try:
        await websocket.send_json({
            "type": "hello",
            "sessions": [s.to_dict() for s in app.registry.sessions()],
            "lastReconcile": app.registry.last_reconcile,
        })
        while True:
            try:
                message = await asyncio.wait_for(queue.get(),
                                                 timeout=HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                # Keeps the connection alive through the iPad sleeping the radio, and
                # gives the client something to measure latency against.
                await websocket.send_json({"type": "ping"})
                continue
            await websocket.send_json(message)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        app.bus.unsubscribe(queue)
