"""Async client for the official `codex app-server` JSON-RPC protocol.

The protocol is versioned and the binary ships its own schema
(`codex app-server generate-json-schema`), so this is a supported surface rather than
a reverse-engineered one. Two Codex cores are typically installed and they expose
different method sets -- the one bundled inside Codex Desktop is usually newer -- so
the binary is resolved deliberately (see `resolve_codex_binary`) instead of trusting
`$PATH`.

Server->client requests matter as much as client->server calls: approvals arrive as
`applyPatchApproval` / `execCommandApproval` / `permissionsRequestApproval` requests
that we must answer, which is how remote permission handling works.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from .rollout import CODEX_HOME

log = logging.getLogger("aicontrol.codex.appserver")

DESKTOP_APP_BINARY = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
CONFIG_PATH = CODEX_HOME / "config.toml"

ApprovalHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
NotificationHandler = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass
class CodexBinary:
    path: Path
    version: str
    is_desktop_bundled: bool


def _version_of(path: Path) -> Optional[str]:
    try:
        out = subprocess.run([str(path), "--version"], capture_output=True, text=True,
                             timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    match = re.search(r"(\d+\.\d+\.\d+[\w.\-]*)", out.stdout)
    return match.group(1) if match else out.stdout.strip() or None


def _configured_desktop_binary() -> Optional[Path]:
    """Read CODEX_CLI_PATH out of config.toml, which Codex Desktop maintains itself."""
    try:
        text = CONFIG_PATH.read_text()
    except OSError:
        return None
    match = re.search(r'CODEX_CLI_PATH\s*=\s*"([^"]+)"', text)
    if not match:
        return None
    candidate = Path(match.group(1))
    return candidate if candidate.exists() else None


def discover_codex_binaries() -> list[CodexBinary]:
    """Every Codex core we can find, newest-capable first."""
    seen: set[Path] = set()
    found: list[CodexBinary] = []
    candidates: list[tuple[Path, bool]] = []

    configured = _configured_desktop_binary()
    if configured:
        candidates.append((configured, True))
    if DESKTOP_APP_BINARY.exists():
        candidates.append((DESKTOP_APP_BINARY, True))
    standalone = CODEX_HOME / "packages" / "standalone" / "current" / "codex"
    if standalone.exists():
        candidates.append((standalone, False))
    on_path = shutil.which("codex")
    if on_path:
        candidates.append((Path(on_path), False))

    for path, bundled in candidates:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        version = _version_of(path)
        if version:
            found.append(CodexBinary(path=path, version=version, is_desktop_bundled=bundled))

    # Prefer the desktop-bundled core: it is the one that wrote the desktop sessions,
    # and it exposes a superset of the methods the $PATH build does.
    found.sort(key=lambda b: (not b.is_desktop_bundled, b.version), reverse=False)
    return found


def resolve_codex_binary() -> Optional[CodexBinary]:
    binaries = discover_codex_binaries()
    return binaries[0] if binaries else None


class AppServerError(RuntimeError):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data


class CodexAppServer:
    """One `codex app-server` process, spoken to over stdio JSON-RPC.

    Also usable against an already-running daemon via `app-server proxy --sock`, which
    is how AI Control reaches threads that are live in another process when the shared
    managed daemon is installed.
    """

    def __init__(self, binary: Optional[CodexBinary] = None, *,
                 socket_path: Optional[Path] = None,
                 client_name: str = "ai-control",
                 client_version: str = "0.1.0") -> None:
        self.binary = binary or resolve_codex_binary()
        self.socket_path = socket_path
        self.client_name = client_name
        self.client_version = client_version

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task[None]] = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_id = 0
        self._lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self.server_info: dict[str, Any] = {}
        self.approval_handler: Optional[ApprovalHandler] = None
        self.notification_handler: Optional[NotificationHandler] = None
        self._closing = False

    # ------------------------------------------------------------------ lifecycle

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self) -> None:
        async with self._lock:
            if self.running:
                return
            if not self.binary:
                raise RuntimeError("no codex binary found")
            argv = [str(self.binary.path), "app-server"]
            if self.socket_path:
                argv += ["proxy", "--sock", str(self.socket_path)]
            env = dict(os.environ)
            env.setdefault("CODEX_HOME", str(CODEX_HOME))
            self._proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=env,
            )
            self._closing = False
            self._reader_task = asyncio.create_task(self._read_loop())
            self.server_info = await self.request("initialize", {
                "clientInfo": {
                    "name": self.client_name,
                    "title": "AI Control",
                    "version": self.client_version,
                }
            }, timeout=45)

    async def close(self) -> None:
        self._closing = True
        if self._reader_task:
            self._reader_task.cancel()
            self._reader_task = None
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._proc.kill()
        self._proc = None
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("app-server closed"))
        self._pending.clear()

    # -------------------------------------------------------------------- plumbing

    async def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                await self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("app-server read loop failed")
        finally:
            if not self._closing:
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(ConnectionError("app-server exited"))
                self._pending.clear()

    async def _dispatch(self, message: dict[str, Any]) -> None:
        msg_id = message.get("id")
        if "method" in message and msg_id is not None:
            await self._handle_server_request(msg_id, message["method"],
                                              message.get("params") or {})
            return
        if "method" in message:
            handler = self.notification_handler
            if handler:
                try:
                    await handler(message["method"], message.get("params") or {})
                except Exception:
                    log.exception("notification handler failed")
            return
        fut = self._pending.pop(msg_id, None)
        if fut is None or fut.done():
            return
        if "error" in message:
            err = message["error"] or {}
            fut.set_exception(AppServerError(err.get("code", -1),
                                             err.get("message", "unknown"),
                                             err.get("data")))
        else:
            fut.set_result(message.get("result"))

    async def _handle_server_request(self, msg_id: Any, method: str,
                                     params: dict[str, Any]) -> None:
        """Answer a server->client request -- above all, approval prompts."""
        handler = self.approval_handler
        if handler is None:
            # Refusing is the safe default: never auto-approve on a user's machine.
            await self._send({"jsonrpc": "2.0", "id": msg_id,
                              "result": {"decision": "denied"}})
            return
        try:
            result = await handler(method, params)
        except Exception:
            log.exception("approval handler failed for %s", method)
            result = {"decision": "denied"}
        await self._send({"jsonrpc": "2.0", "id": msg_id, "result": result})

    async def _send(self, payload: dict[str, Any]) -> None:
        if not self._proc or not self._proc.stdin:
            raise ConnectionError("app-server not running")
        data = (json.dumps(payload) + "\n").encode()
        async with self._write_lock:
            self._proc.stdin.write(data)
            await self._proc.stdin.drain()

    async def request(self, method: str, params: Optional[dict[str, Any]] = None, *,
                      timeout: float = 60.0) -> Any:
        if not self.running:
            await self.start()
        self._next_id += 1
        req_id = self._next_id
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            payload["params"] = params
        await self._send(payload)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise

    # ----------------------------------------------------------------- convenience

    async def list_threads(self, *, limit: int = 100, cursor: Optional[str] = None,
                           archived: Optional[bool] = None) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if archived is not None:
            params["archived"] = archived
        return await self.request("thread/list", params)

    async def read_thread(self, thread_id: str, *, include_turns: bool = False) -> dict[str, Any]:
        return await self.request("thread/read", {"threadId": thread_id,
                                                  "includeTurns": include_turns})

    async def loaded_threads(self) -> dict[str, Any]:
        return await self.request("thread/loaded/list", {})

    async def resume_thread(self, thread_id: str, *, path: Optional[str] = None,
                            cwd: Optional[str] = None,
                            approval_policy: Optional[str] = None) -> dict[str, Any]:
        # `path` is deliberately accepted and ignored: thread/resume rejects it unless
        # the client negotiated the experimentalApi capability, and thread_id alone is
        # the documented preferred form. Verified to continue the same rollout file.
        params: dict[str, Any] = {"threadId": thread_id}
        if cwd:
            params["cwd"] = cwd
        if approval_policy:
            params["approvalPolicy"] = approval_policy
        return await self.request("thread/resume", params)

    async def start_thread(self, *, cwd: str, model: Optional[str] = None,
                           approval_policy: Optional[str] = None,
                           sandbox: Optional[str] = None) -> dict[str, Any]:
        params: dict[str, Any] = {"cwd": cwd}
        if model:
            params["model"] = model
        if approval_policy:
            params["approvalPolicy"] = approval_policy
        if sandbox:
            # SandboxMode: read-only | workspace-write | danger-full-access
            params["sandbox"] = sandbox
        return await self.request("thread/start", params)

    async def start_turn(self, thread_id: str, text: str, *,
                         cwd: Optional[str] = None,
                         model: Optional[str] = None,
                         approval_policy: Optional[str] = None) -> dict[str, Any]:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": text}],
        }
        if cwd:
            params["cwd"] = cwd
        if model:
            params["model"] = model
        if approval_policy:
            params["approvalPolicy"] = approval_policy
        return await self.request("turn/start", params)

    async def steer_turn(self, thread_id: str, turn_id: str, text: str) -> dict[str, Any]:
        # expectedTurnId is a precondition: the call fails rather than steering a turn
        # other than the one we observed.
        return await self.request("turn/steer", {
            "threadId": thread_id,
            "expectedTurnId": turn_id,
            "input": [{"type": "text", "text": text}],
        })

    async def interrupt_turn(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        return await self.request("turn/interrupt", {"threadId": thread_id,
                                                     "turnId": turn_id})

    async def list_models(self) -> Any:
        return await self.request("model/list", {})

    async def archive_thread(self, thread_id: str) -> Any:
        return await self.request("thread/archive", {"threadId": thread_id})


def daemon_status(binary: Optional[CodexBinary] = None) -> dict[str, Any]:
    """Probe the shared managed daemon without starting anything.

    Its presence is what decides whether write control into a *live* desktop thread is
    possible, so this feeds the capability advertisement and /diagnostics directly.
    """
    binary = binary or resolve_codex_binary()
    control_sock = CODEX_HOME / "app-server-control" / "app-server-control.sock"
    standalone = CODEX_HOME / "packages" / "standalone" / "current" / "codex"
    result: dict[str, Any] = {
        "controlSocket": str(control_sock),
        "controlSocketPresent": control_sock.exists(),
        "standaloneInstallPresent": standalone.exists(),
        "running": False,
        "versions": None,
        "error": None,
    }
    if not binary:
        result["error"] = "no codex binary found"
        return result
    try:
        proc = subprocess.run([str(binary.path), "app-server", "daemon", "version"],
                              capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        result["error"] = str(exc)
        return result
    if proc.returncode == 0:
        result["running"] = True
        try:
            result["versions"] = json.loads(proc.stdout)
        except json.JSONDecodeError:
            result["versions"] = {"raw": proc.stdout.strip()}
    else:
        result["error"] = (proc.stderr or proc.stdout).strip().splitlines()[0] if (
            proc.stderr or proc.stdout).strip() else "daemon not running"
    return result
