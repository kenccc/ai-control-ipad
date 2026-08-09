"""PTY sessions bridged to WebSockets for the terminal tab.

Every terminal is bound to an allowlisted directory and is audited on open. Output is
streamed as it arrives -- scrollback lives in the browser's xterm buffer, so a reconnect
never replays the whole history over the socket.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import pty
import signal
import struct
import termios
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger("aicontrol.pty")

DEFAULT_SHELL = os.environ.get("SHELL", "/bin/zsh")
#: Ring buffer kept server-side purely so a reconnect can redraw the visible screen.
SCROLLBACK_BYTES = 64 * 1024


@dataclass
class PtySession:
    id: str
    fd: int
    pid: int
    cwd: str
    cols: int = 120
    rows: int = 32
    created_at: float = field(default_factory=time.time)
    buffer: bytearray = field(default_factory=bytearray)
    subscribers: set[asyncio.Queue[bytes]] = field(default_factory=set)
    closed: bool = False


class PtyService:
    def __init__(self) -> None:
        self._sessions: dict[str, PtySession] = {}

    def get(self, pty_id: str) -> Optional[PtySession]:
        return self._sessions.get(pty_id)

    def list(self) -> list[dict]:
        return [{"id": s.id, "cwd": s.cwd, "pid": s.pid, "createdAt": s.created_at,
                 "cols": s.cols, "rows": s.rows, "closed": s.closed}
                for s in self._sessions.values()]

    def spawn(self, cwd: str, *, command: Optional[list[str]] = None,
              cols: int = 120, rows: int = 32,
              env: Optional[dict[str, str]] = None) -> PtySession:
        if not Path(cwd).is_dir():
            raise ValueError(f"{cwd} is not a directory")
        argv = command or [DEFAULT_SHELL, "-l"]

        pid, fd = pty.fork()
        if pid == 0:  # child
            try:
                os.chdir(cwd)
                child_env = dict(os.environ)
                child_env.update(env or {})
                child_env["TERM"] = child_env.get("TERM", "xterm-256color")
                # Signals the parent process ignores must be restored for the shell.
                signal.signal(signal.SIGINT, signal.SIG_DFL)
                os.execvpe(argv[0], argv, child_env)
            except Exception:
                os._exit(1)

        session = PtySession(id=uuid.uuid4().hex[:12], fd=fd, pid=pid, cwd=cwd,
                             cols=cols, rows=rows)
        self._sessions[session.id] = session
        self.resize(session.id, cols, rows)
        os.set_blocking(fd, False)
        asyncio.get_running_loop().add_reader(fd, self._on_readable, session)
        return session

    def _on_readable(self, session: PtySession) -> None:
        try:
            data = os.read(session.fd, 65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self.close(session.id)
            return
        if not data:
            self.close(session.id)
            return

        session.buffer.extend(data)
        if len(session.buffer) > SCROLLBACK_BYTES:
            del session.buffer[: len(session.buffer) - SCROLLBACK_BYTES]

        for queue in list(session.subscribers):
            try:
                queue.put_nowait(data)
            except asyncio.QueueFull:
                log.debug("pty subscriber saturated, dropping chunk")

    def write(self, pty_id: str, data: bytes) -> None:
        session = self._sessions.get(pty_id)
        if session is None or session.closed:
            raise ValueError("terminal is not open")
        os.write(session.fd, data)

    def resize(self, pty_id: str, cols: int, rows: int) -> None:
        session = self._sessions.get(pty_id)
        if session is None or session.closed:
            return
        session.cols, session.rows = cols, rows
        try:
            fcntl.ioctl(session.fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
        except OSError as exc:
            log.debug("resize failed: %s", exc)

    def subscribe(self, pty_id: str) -> tuple[asyncio.Queue[bytes], bytes]:
        session = self._sessions.get(pty_id)
        if session is None:
            raise ValueError("terminal is not open")
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=256)
        session.subscribers.add(queue)
        return queue, bytes(session.buffer)

    def unsubscribe(self, pty_id: str, queue: asyncio.Queue[bytes]) -> None:
        session = self._sessions.get(pty_id)
        if session:
            session.subscribers.discard(queue)

    def close(self, pty_id: str) -> None:
        session = self._sessions.get(pty_id)
        if session is None or session.closed:
            return
        session.closed = True
        try:
            asyncio.get_running_loop().remove_reader(session.fd)
        except (ValueError, RuntimeError):
            pass
        try:
            os.close(session.fd)
        except OSError:
            pass
        try:
            os.kill(session.pid, signal.SIGHUP)
        except (OSError, ProcessLookupError):
            pass
        for queue in list(session.subscribers):
            try:
                queue.put_nowait(b"")
            except asyncio.QueueFull:
                pass

    def shutdown(self) -> None:
        for pty_id in list(self._sessions):
            self.close(pty_id)
        self._sessions.clear()
