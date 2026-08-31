"""Configuration and secret handling.

Secrets live in the macOS Keychain, never in the YAML config and never in git. The
config file carries only non-sensitive settings plus the repository allowlist, which
is the boundary every agent and git operation is checked against.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

log = logging.getLogger("aicontrol.config")

APP_DIR = Path(os.environ.get("AICONTROL_HOME", Path.home() / ".ai-control"))
CONFIG_PATH = Path(os.environ.get("AICONTROL_CONFIG", APP_DIR / "config.yaml"))
DB_PATH = APP_DIR / "aicontrol.db"
WORKTREE_ROOT = APP_DIR / "worktrees"
KEYCHAIN_SERVICE = "ai-control"


class KeychainError(RuntimeError):
    pass


def keychain_get(account: str, *, service: str = KEYCHAIN_SERVICE) -> Optional[str]:
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("keychain read failed for %s: %s", account, exc)
        return None
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def keychain_set(account: str, secret: str, *, service: str = KEYCHAIN_SERVICE) -> None:
    proc = subprocess.run(
        ["security", "add-generic-password", "-s", service, "-a", account,
         "-w", secret, "-U"],
        capture_output=True, text=True, timeout=15)
    if proc.returncode != 0:
        raise KeychainError(proc.stderr.strip() or "keychain write failed")


def _secret(env_var: str, keychain_account: str) -> Optional[str]:
    """Env var wins (useful for tests and CI); Keychain is the real storage."""
    return os.environ.get(env_var) or keychain_get(keychain_account)


@dataclass
class RepoConfig:
    name: str
    path: Path
    forgejo: Optional[str] = None       # "owner/repo"

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "path": str(self.path), "forgejo": self.forgejo,
                "exists": self.path.is_dir()}


@dataclass
class ForgejoConfig:
    url: Optional[str] = None
    #: Read from the Keychain at access time; never serialised to the frontend.
    token: Optional[str] = field(default=None, repr=False)

    @property
    def configured(self) -> bool:
        return bool(self.url and self.token)


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8787
    repositories: dict[str, RepoConfig] = field(default_factory=dict)
    forgejo: ForgejoConfig = field(default_factory=ForgejoConfig)
    auth_token: Optional[str] = field(default=None, repr=False)
    session_secret: Optional[str] = field(default=None, repr=False)
    allowed_origins: list[str] = field(default_factory=list)
    reconcile_interval: float = 2.0
    #: Opt-in: talk to the shared Codex app-server daemon for live desktop write control.
    codex_shared_daemon: bool = False
    worktree_root: Path = WORKTREE_ROOT
    db_path: Path = DB_PATH

    def resolve_host(self) -> str:
        """Turn `host: tailscale` into this machine's tailnet address.

        Binding the Tailscale interface specifically means the service is reachable
        from your iPad and not from whatever café network the Mac is also on. If
        Tailscale never comes up we refuse to start rather than silently falling back
        to 0.0.0.0, which would be a security downgrade nobody asked for.
        """
        if self.host != "tailscale":
            return self.host
        from .services.tailscale import wait_for_ipv4
        address = wait_for_ipv4()
        if address is None:
            raise RuntimeError(
                "config sets host: tailscale, but Tailscale did not come up. "
                "Run `tailscale status` to check it, or set an explicit host in "
                "~/.ai-control/config.yaml.")
        return address

    def repo_for_path(self, path: str | Path) -> Optional[RepoConfig]:
        """Which allowlisted repository contains `path`, if any."""
        try:
            resolved = Path(path).resolve()
        except OSError:
            return None
        best: Optional[RepoConfig] = None
        for repo in self.repositories.values():
            try:
                root = repo.path.resolve()
            except OSError:
                continue
            if resolved == root or root in resolved.parents:
                if best is None or len(str(root)) > len(str(best.path.resolve())):
                    best = repo
        return best

    def is_allowed(self, path: str | Path) -> bool:
        if not self.repositories:
            # An empty allowlist means nothing is allowed. Failing closed is the only
            # safe default for something that can run commands on a dev machine.
            return False
        if self.repo_for_path(path) is not None:
            return True
        try:
            resolved = Path(path).resolve()
        except OSError:
            return False
        root = self.worktree_root.resolve()
        return resolved == root or root in resolved.parents

    def to_dict(self) -> dict[str, Any]:
        """Safe to send to the browser: contains no secrets."""
        return {
            "host": self.host,
            "port": self.port,
            "repositories": [r.to_dict() for r in self.repositories.values()],
            "forgejo": {"url": self.forgejo.url, "configured": self.forgejo.configured},
            "reconcileInterval": self.reconcile_interval,
            "codexSharedDaemon": self.codex_shared_daemon,
            "worktreeRoot": str(self.worktree_root),
            "databasePath": str(self.db_path),
        }


def load_config(path: Path = CONFIG_PATH) -> Config:
    raw: dict[str, Any] = {}
    if path.is_file():
        try:
            raw = yaml.safe_load(path.read_text()) or {}
        except (OSError, yaml.YAMLError) as exc:
            log.error("could not read config at %s: %s", path, exc)

    cfg = Config()
    cfg.host = raw.get("host", cfg.host)
    cfg.port = int(raw.get("port", cfg.port))
    cfg.reconcile_interval = float(raw.get("reconcileInterval", cfg.reconcile_interval))
    cfg.codex_shared_daemon = bool(raw.get("codexSharedDaemon", False))
    cfg.allowed_origins = list(raw.get("allowedOrigins") or [])
    if raw.get("worktreeRoot"):
        cfg.worktree_root = Path(raw["worktreeRoot"]).expanduser()
    if raw.get("databasePath"):
        cfg.db_path = Path(raw["databasePath"]).expanduser()

    for name, entry in (raw.get("repositories") or {}).items():
        if isinstance(entry, str):
            entry = {"path": entry}
        if not isinstance(entry, dict) or not entry.get("path"):
            continue
        cfg.repositories[name] = RepoConfig(
            name=name,
            path=Path(entry["path"]).expanduser(),
            forgejo=entry.get("forgejo"),
        )

    forgejo_raw = raw.get("forgejo") or {}
    cfg.forgejo = ForgejoConfig(
        url=(forgejo_raw.get("url") or os.environ.get("AICONTROL_FORGEJO_URL")),
        token=_secret("AICONTROL_FORGEJO_TOKEN", "forgejo-token"),
    )

    cfg.auth_token = _secret("AICONTROL_AUTH_TOKEN", "auth-token")
    cfg.session_secret = _secret("AICONTROL_SESSION_SECRET", "session-secret")
    return cfg
