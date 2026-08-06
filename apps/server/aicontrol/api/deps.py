"""Shared application state, wired once at startup and reachable from every route."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException, Request, status

from ..auth import AuthService
from ..config import Config
from ..db import Database
from ..events import EventBus
from ..providers.claude_code import ClaudeCodeProvider
from ..providers.codex_cli import CodexCLIProvider
from ..providers.codex_desktop import CodexDesktopProvider
from ..registry import SessionRegistry
from ..services.forgejo import ForgejoClient
from ..services.git_service import GitService
from ..services.pty_service import PtyService
from ..services.worktrees import WorktreeManager


@dataclass
class AppState:
    config: Config
    db: Database
    bus: EventBus
    auth: AuthService
    git: GitService
    pty: PtyService
    worktrees: WorktreeManager
    registry: SessionRegistry
    codex_desktop: CodexDesktopProvider
    codex_cli: CodexCLIProvider
    claude_code: ClaudeCodeProvider
    forgejo: Optional[ForgejoClient] = None

    def repo_or_404(self, name: str):
        repo = self.config.repositories.get(name)
        if repo is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                f"{name} is not in the repository allowlist")
        return repo

    def require_allowed_path(self, path: str) -> None:
        """Every agent launch, terminal and git write funnels through this check."""
        if not self.config.is_allowed(path):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"{path} is outside the configured repository allowlist")

    def forgejo_or_503(self) -> ForgejoClient:
        if self.forgejo is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Forgejo is not configured. Add its URL to config.yaml and store a "
                "token with ./scripts/set-secret.sh forgejo-token.")
        return self.forgejo

    def split_slug(self, slug: str) -> tuple[str, str]:
        if "/" not in slug:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "repository must be in owner/name form")
        owner, name = slug.split("/", 1)
        return owner, name


def state(request: Request) -> AppState:
    return request.app.state.app_state
