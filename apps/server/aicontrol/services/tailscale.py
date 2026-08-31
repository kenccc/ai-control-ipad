"""Tailscale detection and address resolution.

Two jobs: tell `/diagnostics` what state Tailscale is in, and resolve the address the
server should bind so the iPad can reach it.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("aicontrol.tailscale")

#: Where the CLI ends up. The standalone app's binary resolves its own bundle
#: identifier from its path, so a symlink to it aborts with a fatal error -- the
#: official installer writes a small `exec` wrapper instead, and so does setup.sh.
#: Running the bundle binary directly launches the GUI and never returns, so it must
#: never be probed here.
CLI_CANDIDATES = (
    "/usr/local/bin/tailscale",
    "/opt/homebrew/bin/tailscale",
    str(Path.home() / ".local" / "bin" / "tailscale"),
)
APP_PATH = Path("/Applications/Tailscale.app")


def cli_path() -> Optional[str]:
    found = shutil.which("tailscale")
    if found:
        return found
    for candidate in CLI_CANDIDATES:
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def status(*, timeout: float = 8.0) -> dict[str, Any]:
    """The three states that actually occur, each with the right next step."""
    app_installed = APP_PATH.is_dir()
    cli = cli_path()

    if not app_installed and not cli:
        return {"detected": False, "connected": False,
                "hint": "Tailscale is not installed. Install it to reach AI Control "
                        "from your iPad outside this network."}

    if cli is None:
        return {"detected": True, "connected": False, "appInstalled": True,
                "hint": "Tailscale.app is installed but its CLI is not. Run "
                        "./scripts/install-tailscale-cli.sh, or use the app's "
                        "Install CLI menu item."}

    try:
        proc = subprocess.run([cli, "status", "--json"], capture_output=True,
                              text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"detected": True, "connected": False, "appInstalled": app_installed,
                "cli": cli, "error": str(exc)}

    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout).strip()[:200]
        return {"detected": True, "connected": False, "appInstalled": app_installed,
                "cli": cli,
                "hint": message or "Tailscale is installed but not logged in. "
                                   "Run: tailscale up"}

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"detected": True, "connected": False, "cli": cli}

    self_node = data.get("Self") or {}
    running = data.get("BackendState") == "Running"
    peers = [
        {"hostname": p.get("HostName"),
         "dnsName": (p.get("DNSName") or "").rstrip("."),
         "os": p.get("OS"), "online": bool(p.get("Online"))}
        for p in (data.get("Peer") or {}).values()
    ]
    return {
        "detected": True,
        "connected": running,
        "appInstalled": app_installed,
        "cli": cli,
        "hostname": self_node.get("HostName"),
        "dnsName": (self_node.get("DNSName") or "").rstrip("."),
        "addresses": self_node.get("TailscaleIPs") or [],
        "peers": peers,
        "hint": None if running
                else "Tailscale is installed but not connected. Run: tailscale up",
    }


def ipv4_address() -> Optional[str]:
    """This machine's Tailscale IPv4, or None when Tailscale is not up."""
    state = status()
    for address in state.get("addresses") or []:
        if ":" not in address:
            return address
    return None


def wait_for_ipv4(timeout: float = 90.0, interval: float = 3.0) -> Optional[str]:
    """Wait for Tailscale to come up.

    At login the daemon often starts after our LaunchAgent, so binding immediately
    would fail on a perfectly healthy machine. Waiting is better than crash-looping.
    """
    deadline = time.monotonic() + timeout
    while True:
        address = ipv4_address()
        if address:
            return address
        if time.monotonic() >= deadline:
            return None
        log.info("waiting for Tailscale to come up before binding")
        time.sleep(interval)
