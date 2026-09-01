"""Usage and rate-limit reporting for Codex and Claude Code.

The two providers expose very different surfaces, and the difference is reported
honestly rather than papered over:

* **Codex** has a real, supported live API -- `account/rateLimits/read` on the
  app-server returns the 5-hour and weekly windows with a used percentage and a reset
  time, plus plan, credits and reset credits.
* **Claude Code** has no local quota API. `claude auth status --json` gives the plan
  but no consumption; the local stats cache gives token counts it computes
  periodically; and a limit is only recorded after you hit one, as a synthetic message
  in the session transcript. So Claude is reported as plan + observed limit events +
  local token usage, explicitly labelled as not a live gauge.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("aicontrol.usage")

CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
STATS_CACHE = CLAUDE_HOME / "stats-cache.json"
PROJECTS_DIR = CLAUDE_HOME / "projects"

#: Rate limits move slowly and the call is a round trip to OpenAI, so it is cached.
CODEX_TTL = 60.0
#: `claude auth status` spawns a process; its answer changes very rarely.
CLAUDE_TTL = 300.0
#: How far back to look for a "you've hit your limit" message.
LIMIT_EVENT_DAYS = 14
LIMIT_EVENT_FILE_CAP = 60

_LIMIT_TEXT = re.compile(r"You've hit your ([a-z]+) limit[^\"\\]*")


@dataclass
class UsageWindow:
    label: str
    used_percent: float
    resets_at: Optional[float] = None
    window_minutes: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "usedPercent": self.used_percent,
                "resetsAt": self.resets_at, "windowMinutes": self.window_minutes}


@dataclass
class ProviderUsage:
    provider: str
    label: str
    available: bool = False
    plan: Optional[str] = None
    account: Optional[str] = None
    windows: list[UsageWindow] = field(default_factory=list)
    credits: Optional[dict[str, Any]] = None
    totals: Optional[dict[str, Any]] = None
    #: Honest explanation of what this provider does *not* report.
    note: Optional[str] = None
    error: Optional[str] = None
    last_limit_event: Optional[dict[str, Any]] = None
    fetched_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["windows"] = [w.to_dict() for w in self.windows]
        data["lastLimitEvent"] = data.pop("last_limit_event")
        data["fetchedAt"] = data.pop("fetched_at")
        return data


def _window_label(minutes: Optional[int], fallback: str) -> str:
    if not minutes:
        return fallback
    if minutes % 10080 == 0:
        weeks = minutes // 10080
        return "Weekly" if weeks == 1 else f"{weeks}-weekly"
    if minutes % 1440 == 0:
        days = minutes // 1440
        return "Daily" if days == 1 else f"{days}-day"
    if minutes % 60 == 0:
        return f"{minutes // 60}-hour"
    return f"{minutes}-minute"


class UsageService:
    def __init__(self, codex_provider: Any) -> None:
        #: Reuses the Codex provider's app-server connection rather than starting a
        #: second one; the process is expensive and already running.
        self._codex_provider = codex_provider
        self._codex_cache: Optional[tuple[float, ProviderUsage]] = None
        self._claude_cache: Optional[tuple[float, ProviderUsage]] = None
        self._lock = asyncio.Lock()

    async def read(self, *, force: bool = False) -> dict[str, Any]:
        async with self._lock:
            codex = await self._codex(force=force)
            claude = await self._claude(force=force)
        return {"providers": [codex.to_dict(), claude.to_dict()]}

    # ----------------------------------------------------------------------- codex

    async def _codex(self, *, force: bool) -> ProviderUsage:
        now = time.time()
        if not force and self._codex_cache and now - self._codex_cache[0] < CODEX_TTL:
            return self._codex_cache[1]

        usage = ProviderUsage(provider="openai_codex", label="Codex")
        try:
            server = await self._codex_provider.app_server()
            account = await server.request("account/read", {}, timeout=25)
            limits = await server.request("account/rateLimits/read", {}, timeout=25)
        except Exception as exc:
            usage.error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            self._codex_cache = (now, usage)
            return usage

        entry = (account or {}).get("account") or {}
        usage.account = entry.get("email")
        usage.plan = entry.get("planType")
        usage.available = True

        snapshot = (limits or {}).get("rateLimits") or {}
        usage.plan = snapshot.get("planType") or usage.plan
        for key, fallback in (("primary", "Primary"), ("secondary", "Secondary")):
            window = snapshot.get(key)
            if not window:
                continue
            minutes = window.get("windowDurationMins")
            usage.windows.append(UsageWindow(
                label=_window_label(minutes, fallback),
                used_percent=float(window.get("usedPercent") or 0),
                resets_at=window.get("resetsAt"),
                window_minutes=minutes,
            ))

        credits = snapshot.get("credits") or {}
        reset_credits = (limits or {}).get("rateLimitResetCredits") or {}
        usage.credits = {
            "balance": credits.get("balance"),
            "hasCredits": bool(credits.get("hasCredits")),
            "unlimited": bool(credits.get("unlimited")),
            "resetCreditsAvailable": reset_credits.get("availableCount"),
        }
        if snapshot.get("rateLimitReachedType"):
            usage.last_limit_event = {"text": snapshot["rateLimitReachedType"],
                                      "timestamp": now}

        # Lifetime totals are a separate call and only exist on newer cores, so a
        # failure here must not lose the rate limits we already read.
        try:
            summary = await server.request("account/usage/read", {}, timeout=25)
            usage.totals = (summary or {}).get("summary")
        except Exception as exc:
            log.debug("codex usage summary unavailable: %s", exc)

        self._codex_cache = (now, usage)
        return usage

    # ---------------------------------------------------------------------- claude

    async def _claude(self, *, force: bool) -> ProviderUsage:
        now = time.time()
        if not force and self._claude_cache and now - self._claude_cache[0] < CLAUDE_TTL:
            return self._claude_cache[1]

        loop = asyncio.get_running_loop()
        usage = await loop.run_in_executor(None, self._claude_sync)
        self._claude_cache = (now, usage)
        return usage

    def _claude_sync(self) -> ProviderUsage:
        from ..providers.claude_code import ClaudeCodeProvider

        usage = ProviderUsage(
            provider="anthropic_claude", label="Claude Code",
            note="Claude Code exposes no live quota API, so there is no percentage to "
                 "show. What is here: your plan, the token usage Claude Code computes "
                 "locally, and the last limit you actually hit.")

        binary = ClaudeCodeProvider.binary()
        if not binary:
            usage.error = "claude binary not found"
            return usage

        try:
            proc = subprocess.run([binary, "auth", "status", "--json"],
                                  capture_output=True, text=True, timeout=25)
            if proc.returncode == 0:
                status = json.loads(proc.stdout)
                usage.available = bool(status.get("loggedIn"))
                usage.plan = status.get("subscriptionType")
                usage.account = status.get("email")
            else:
                usage.error = (proc.stderr or proc.stdout).strip()[:200] or "auth status failed"
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            usage.error = f"{type(exc).__name__}: {exc}"[:200]

        usage.totals = self._claude_totals()
        usage.last_limit_event = self._claude_last_limit_event()
        return usage

    @staticmethod
    def _claude_totals() -> Optional[dict[str, Any]]:
        try:
            stats = json.loads(STATS_CACHE.read_text())
        except (OSError, json.JSONDecodeError):
            return None

        by_model = stats.get("modelUsage") or {}
        tokens = 0
        for entry in by_model.values():
            if not isinstance(entry, dict):
                continue
            tokens += sum(int(entry.get(k) or 0) for k in
                          ("inputTokens", "outputTokens",
                           "cacheReadInputTokens", "cacheCreationInputTokens"))

        daily = stats.get("dailyModelTokens") or []
        recent = []
        for row in daily[-14:]:
            if isinstance(row, dict):
                recent.append({"date": row.get("date"),
                               "tokens": sum(int(v or 0) for v in
                                             (row.get("tokensByModel") or {}).values())})
        return {
            "lifetimeTokens": tokens or None,
            "totalSessions": stats.get("totalSessions"),
            "totalMessages": stats.get("totalMessages"),
            "models": sorted(by_model),
            "dailyTokens": recent,
            # Claude Code recomputes this periodically, so it lags. Saying when it was
            # computed is the difference between a number and a misleading number.
            "computedAt": stats.get("lastComputedDate"),
        }

    @staticmethod
    def _claude_last_limit_event() -> Optional[dict[str, Any]]:
        """Find the most recent "you've hit your limit" message Claude Code recorded.

        This is retrospective by nature -- it only exists once a limit was actually
        reached -- so it is presented as history, never as a live gauge.
        """
        if not PROJECTS_DIR.is_dir():
            return None
        cutoff = time.time() - LIMIT_EVENT_DAYS * 86400
        candidates: list[tuple[float, Path]] = []
        for path in PROJECTS_DIR.glob("*/*.jsonl"):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime >= cutoff:
                candidates.append((mtime, path))
        candidates.sort(reverse=True)

        for _, path in candidates[:LIMIT_EVENT_FILE_CAP]:
            try:
                text = path.read_text(errors="ignore")
            except OSError:
                continue
            if "You've hit your" not in text:
                continue
            best: Optional[dict[str, Any]] = None
            for line in text.splitlines():
                if "You've hit your" not in line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                content = (record.get("message") or {}).get("content")
                blocks = content if isinstance(content, list) else []
                for block in blocks:
                    if not isinstance(block, dict) or block.get("type") != "text":
                        continue
                    match = _LIMIT_TEXT.search(block.get("text") or "")
                    if match:
                        best = {"text": match.group(0).strip(),
                                "kind": match.group(1),
                                "timestamp": record.get("timestamp"),
                                "sessionId": path.stem}
            if best:
                return best
        return None
