"""FastAPI application -- the Mac companion daemon.

Runs on the Mac, holds every secret, and exposes one authenticated HTTP + WebSocket
surface for the iPad. Nothing that touches a repository ever leaves this process.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .auth import CSRF_COOKIE, SESSION_COOKIE, AuthService
from .config import Config, load_config
from .db import Database
from .events import EventBus
from .logging_setup import configure_logging
from .providers.claude_code import ClaudeCodeProvider
from .providers.codex_cli import CodexCLIProvider
from .providers.codex_desktop import CodexDesktopProvider
from .registry import SessionRegistry
from .services.forgejo import ForgejoClient
from .services.git_service import GitService
from .services.pty_service import PtyService
from .services.worktrees import WorktreeManager
from .api import codex as codex_routes
from .api import diagnostics as diagnostics_routes
from .api import forgejo_routes
from .api import repos as repo_routes
from .api import sessions as session_routes
from .api import terminal as terminal_routes
from .api import ws as ws_routes
from .api.deps import AppState, state

log = logging.getLogger("aicontrol")

WEB_DIST = Path(__file__).resolve().parents[2] / "web" / "dist"


def build_state(config: Optional[Config] = None) -> AppState:
    config = config or load_config()
    db = Database(config.db_path)
    bus = EventBus()
    auth = AuthService(config.auth_token, config.session_secret,
                       allowed_origins=config.allowed_origins)
    git = GitService()
    pty = PtyService()
    worktrees = WorktreeManager(config, db)

    codex_desktop = CodexDesktopProvider(git)
    codex_cli = CodexCLIProvider(git)
    claude_code = ClaudeCodeProvider(git)

    registry = SessionRegistry([codex_desktop, codex_cli, claude_code], db, bus,
                               interval=config.reconcile_interval)

    forgejo = None
    if config.forgejo.configured:
        forgejo = ForgejoClient(config.forgejo.url, config.forgejo.token)

    return AppState(config=config, db=db, bus=bus, auth=auth, git=git, pty=pty,
                    worktrees=worktrees, registry=registry,
                    codex_desktop=codex_desktop, codex_cli=codex_cli,
                    claude_code=claude_code, forgejo=forgejo)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app_state: AppState = app.state.app_state
    app_state.config.worktree_root.mkdir(parents=True, exist_ok=True)
    await app_state.registry.start()
    log.info("AI Control started on %s:%s with %d allowlisted repositories",
             app_state.bound_host or app_state.config.host, app_state.config.port,
             len(app_state.config.repositories))
    try:
        yield
    finally:
        await app_state.registry.stop()
        app_state.pty.shutdown()
        if app_state.forgejo:
            await app_state.forgejo.close()


def create_app(config: Optional[Config] = None) -> FastAPI:
    configure_logging()
    app_state = build_state(config)

    app = FastAPI(title="AI Control", version="0.1.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.app_state = app_state

    if app_state.config.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=app_state.config.allowed_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["content-type", "x-aicontrol-csrf"],
        )

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; connect-src 'self' ws: wss:; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'; base-uri 'none'")
        return response

    # ------------------------------------------------------------------- auth

    @app.post("/api/auth/login")
    async def login(request: Request, response: Response,
                    token: str = Body(..., embed=True),
                    app_st: AppState = Depends(state)) -> dict[str, Any]:
        if not app_st.auth.login_limiter.check(app_st.auth.client_key(request), "login"):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                                "too many attempts, try again later")
        if not app_st.auth.verify_password(token):
            app_st.db.audit("login_failed",
                            detail={"client": app_st.auth.client_key(request)})
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")

        session_value = app_st.auth.issue_session()
        csrf = app_st.auth.issue_csrf()
        secure = request.url.scheme == "https"
        response.set_cookie(SESSION_COOKIE, session_value, httponly=True,
                            samesite="strict", secure=secure, max_age=60 * 60 * 24 * 30,
                            path="/")
        # Readable by JS on purpose: this is the double-submit half of CSRF protection.
        response.set_cookie(CSRF_COOKIE, csrf, httponly=False, samesite="strict",
                            secure=secure, max_age=60 * 60 * 24 * 30, path="/")
        app_st.db.audit("login", detail={"client": app_st.auth.client_key(request)})
        return {"ok": True, "csrfToken": csrf}

    @app.post("/api/auth/logout")
    async def logout(response: Response) -> dict[str, Any]:
        response.delete_cookie(SESSION_COOKIE, path="/")
        response.delete_cookie(CSRF_COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/auth/me")
    async def me(request: Request, app_st: AppState = Depends(state)) -> dict[str, Any]:
        authenticated = app_st.auth.verify_session(request.cookies.get(SESSION_COOKIE))
        return {"authenticated": authenticated,
                "csrfToken": request.cookies.get(CSRF_COOKIE) if authenticated else None,
                "config": app_st.config.to_dict() if authenticated else None}

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        # Unauthenticated on purpose so a reachability check needs no credentials. It
        # reveals nothing beyond the fact that the service is up.
        return {"ok": True, "service": "ai-control"}

    for router in (session_routes.router, repo_routes.router,
                   forgejo_routes.router, codex_routes.router,
                   terminal_routes.router, diagnostics_routes.router,
                   ws_routes.router):
        app.include_router(router)

    # -------------------------------------------------- static PWA (built frontend)

    if WEB_DIST.is_dir():
        assets = WEB_DIST / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{full_path:path}")
        async def spa(full_path: str) -> Response:
            if full_path.startswith("api/"):
                return JSONResponse({"detail": "not found"}, status_code=404)
            candidate = WEB_DIST / full_path
            if full_path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(WEB_DIST / "index.html")

    return app


def main() -> None:
    import uvicorn

    config = load_config()
    host = config.resolve_host()
    app = create_app(config)
    app.state.app_state.bound_host = host
    if host != config.host:
        log.info("resolved %s to the tailnet address %s", config.host, host)
    uvicorn.run(app, host=host, port=config.port, log_config=None)


if __name__ == "__main__":
    main()
