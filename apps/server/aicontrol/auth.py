"""Authentication, origin validation and rate limiting.

This service can run commands on a development Mac, so authentication is mandatory
and there is no unauthenticated mode. It is designed to sit behind Tailscale, but it
does not rely on the network being private: every request is checked.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

from fastapi import HTTPException, Request, status

log = logging.getLogger("aicontrol.auth")

SESSION_COOKIE = "aicontrol_session"
CSRF_COOKIE = "aicontrol_csrf"
CSRF_HEADER = "x-aicontrol-csrf"
SESSION_TTL = 60 * 60 * 24 * 30      # 30 days: an iPad home-screen app should stay in


@dataclass
class RateLimiter:
    """Fixed-window limiter, keyed per client and bucket."""

    limit: int = 60
    window: float = 60.0
    _hits: dict[tuple[str, str], list[float]] = field(default_factory=dict)

    def check(self, key: str, bucket: str = "default") -> bool:
        now = time.time()
        entry = self._hits.setdefault((key, bucket), [])
        cutoff = now - self.window
        entry[:] = [t for t in entry if t > cutoff]
        if len(entry) >= self.limit:
            return False
        entry.append(now)
        return True


class AuthService:
    def __init__(self, token: Optional[str], session_secret: Optional[str], *,
                 allowed_origins: Optional[list[str]] = None) -> None:
        if not token:
            raise RuntimeError(
                "No auth token configured. Run ./setup.sh, which stores one in the "
                "macOS Keychain. AI Control refuses to start unauthenticated.")
        self._token = token
        self._secret = (session_secret or secrets.token_hex(32)).encode()
        self.allowed_origins = allowed_origins or []
        self.login_limiter = RateLimiter(limit=10, window=300)
        self.write_limiter = RateLimiter(limit=120, window=60)

    # -------------------------------------------------------------------- tokens

    def verify_password(self, candidate: str) -> bool:
        return hmac.compare_digest(candidate.strip(), self._token)

    def issue_session(self) -> str:
        issued = str(int(time.time()))
        nonce = secrets.token_hex(8)
        payload = f"{issued}.{nonce}"
        signature = hmac.new(self._secret, payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{signature}"

    def verify_session(self, cookie: Optional[str]) -> bool:
        if not cookie or cookie.count(".") != 2:
            return False
        issued, nonce, signature = cookie.split(".")
        expected = hmac.new(self._secret, f"{issued}.{nonce}".encode(),
                            hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return False
        try:
            return (time.time() - int(issued)) < SESSION_TTL
        except ValueError:
            return False

    @staticmethod
    def issue_csrf() -> str:
        return secrets.token_urlsafe(24)

    # ------------------------------------------------------------------- requests

    @staticmethod
    def client_key(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def check_origin(self, origin: Optional[str], host: Optional[str]) -> bool:
        """Strict origin validation, applied to WebSocket upgrades as well.

        A missing Origin is accepted only for non-browser clients; browsers always
        send one on cross-origin requests and on WebSocket handshakes.
        """
        if origin is None:
            return True
        if self.allowed_origins and origin in self.allowed_origins:
            return True
        parsed = urlparse(origin)
        if not parsed.hostname:
            return False
        if host and parsed.netloc == host:
            return True
        if parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
            return True
        # Tailscale addresses: the 100.64.0.0/10 CGNAT range and *.ts.net names.
        if parsed.hostname.endswith(".ts.net"):
            return True
        try:
            address = ipaddress.ip_address(parsed.hostname)
            return address in ipaddress.ip_network("100.64.0.0/10")
        except ValueError:
            return False

    def require_session(self, request: Request) -> None:
        if not self.verify_session(request.cookies.get(SESSION_COOKIE)):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")
        if not self.check_origin(request.headers.get("origin"),
                                 request.headers.get("host")):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "origin not allowed")

    def require_write(self, request: Request) -> None:
        """Session + CSRF double-submit + rate limit, for anything that mutates."""
        self.require_session(request)
        cookie = request.cookies.get(CSRF_COOKIE)
        header = request.headers.get(CSRF_HEADER)
        if not cookie or not header or not hmac.compare_digest(cookie, header):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF token missing or invalid")
        if not self.write_limiter.check(self.client_key(request), "write"):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate limit exceeded")
