"""Usage and rate-limit reporting for Codex and Claude Code.

The two providers expose very different surfaces, and the difference is reported
honestly rather than papered over:

* **Codex** has a real, supported live API -- `account/rateLimits/read` on the
  app-server returns the 5-hour and weekly windows with a used percentage and a reset
  time, plus plan, credits and reset credits.
* **Claude Code** has no HTTP endpoint for this, but its `/usage` command works
  headlessly (`claude -p "/usage"`) and reports the live session and weekly windows.
  Two flags matter: `--no-session-persistence`, or every poll leaves a junk session in
  the dashboard, and `--output-format json`, so the text is a field rather than
  something scraped off stdout. It costs no quota -- the run reports `num_turns: 0`
  and `total_cost_usd: 0`.

When `/usage` cannot be parsed we fall back to plan plus locally-computed token totals
and the last limit actually hit, and say so, rather than showing an invented number.
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
#: Claude's usage comes from spawning `claude`, which takes a few seconds, so it is
#: cached well above the dashboard's poll interval.
CLAUDE_TTL = 120.0
#: `claude -p "/usage"` takes ~4s on a warm machine.
CLAUDE_USAGE_TIMEOUT = 45.0
#: How far back to look for a "you've hit your limit" message.
LIMIT_EVENT_DAYS = 14
LIMIT_EVENT_FILE_CAP = 60

_LIMIT_TEXT = re.compile(r"You've hit your ([a-z]+) limit[^\"\\]*")

#: `Current session: 36% used · resets Sep 1 at 5pm (Europe/Prague)`
_USAGE_LINE = re.compile(
    r"^\s*(?P<label>[A-Z][^:]*?):\s*(?P<pct>\d+(?:\.\d+)?)%\s*used"
    r"(?:\s*[·.]\s*resets\s+(?P<reset>.+?))?\s*$")
#: `Sep 1 at 5pm (Europe/Prague)`, or just `7:50pm (Europe/Prague)`
_RESET_TEXT = re.compile(
    r"^(?:(?P<month>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+at\s+)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<meridiem>am|pm)?"
    r"(?:\s*\((?P<tz>[\w/+-]+)\))?", re.IGNORECASE)
#: `Last 24h · 296 requests · 13 sessions`
_ACTIVITY_LINE = re.compile(
    r"^Last (?P<period>\d+[hd])\s*[·.]\s*(?P<requests>[\d,]+) requests"
    r"\s*[·.]\s*(?P<sessions>[\d,]+) sessions")

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


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


def parse_reset(text: str, *, now: Optional[float] = None) -> Optional[float]:
    """Turn `Sep 1 at 5pm (Europe/Prague)` into a unix timestamp.

    Claude states resets in a named timezone rather than an offset, and omits the year
    -- and omits the date entirely for windows resetting today. Anything unparseable
    returns None so the caller shows the original text instead of a wrong time.
    """
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    match = _RESET_TEXT.match(text.strip())
    if not match:
        return None

    tzname = match.group("tz")
    try:
        tz = ZoneInfo(tzname) if tzname else None
    except (ZoneInfoNotFoundError, ValueError):
        tz = None

    reference = datetime.fromtimestamp(now or time.time(), tz=tz)

    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    meridiem = (match.group("meridiem") or "").lower()
    if meridiem == "pm" and hour != 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None

    month_name = match.group("month")
    if month_name:
        month = _MONTHS.get(month_name.lower())
        if not month:
            return None
        day = int(match.group("day"))
        year = reference.year
        try:
            when = reference.replace(year=year, month=month, day=day, hour=hour,
                                     minute=minute, second=0, microsecond=0)
        except ValueError:
            return None
        # No year in the text, so a date that already passed means next year.
        if (when - reference).days < -180:
            try:
                when = when.replace(year=year + 1)
            except ValueError:
                return None
    else:
        when = reference.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if when <= reference:
            when += timedelta(days=1)
    return when.timestamp()


def _clean_window_label(raw: str) -> str:
    """`Current week (all models)` -> `Weekly`, `Current week (Fable)` -> `Weekly · Fable`."""
    label = raw.strip()
    label = re.sub(r"^current\s+", "", label, flags=re.IGNORECASE).strip()
    match = re.match(r"^(?P<base>[\w ]+?)\s*\((?P<qualifier>[^)]+)\)$", label)
    qualifier = None
    if match:
        label, qualifier = match.group("base").strip(), match.group("qualifier").strip()
    label = {"session": "Session", "week": "Weekly", "day": "Daily",
             "month": "Monthly"}.get(label.lower(), label[:1].upper() + label[1:])
    if qualifier and qualifier.lower() not in ("all models", "all"):
        label = f"{label} · {qualifier}"
    return label


def parse_usage_report(text: str, *, now: Optional[float] = None
                       ) -> tuple[list[UsageWindow], dict[str, Any]]:
    """Parse the output of `claude -p "/usage"`."""
    windows: list[UsageWindow] = []
    activity: dict[str, Any] = {}

    for line in text.splitlines():
        match = _USAGE_LINE.match(line)
        if match:
            reset_text = (match.group("reset") or "").strip()
            windows.append(UsageWindow(
                label=_clean_window_label(match.group("label")),
                used_percent=float(match.group("pct")),
                resets_at=parse_reset(reset_text, now=now) if reset_text else None,
            ))
            if reset_text and windows[-1].resets_at is None:
                # Keep what Claude said rather than dropping it on the floor.
                activity.setdefault("resetText", {})[windows[-1].label] = reset_text
            continue

        activity_match = _ACTIVITY_LINE.match(line.strip())
        if activity_match:
            activity[f"last{activity_match.group('period').upper()}"] = {
                "requests": int(activity_match.group("requests").replace(",", "")),
                "sessions": int(activity_match.group("sessions").replace(",", "")),
            }
    return windows, activity


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
            # Both are several seconds of network and subprocess; run them together so
            # a cold read costs the slower one rather than their sum.
            codex, claude = await asyncio.gather(
                self._codex(force=force), self._claude(force=force))
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

        usage = ProviderUsage(provider="anthropic_claude", label="Claude Code")

        binary = ClaudeCodeProvider.binary()
        if not binary:
            usage.error = "claude binary not found"
            usage.note = ("The claude binary was not found, so no usage could be read.")
            return usage

        try:
            proc = subprocess.run([binary, "auth", "status", "--json"],
                                  capture_output=True, text=True, timeout=25,
                                  env=self._subprocess_env())
            if proc.returncode == 0:
                status = json.loads(proc.stdout)
                usage.available = bool(status.get("loggedIn"))
                usage.plan = status.get("subscriptionType")
                usage.account = status.get("email")
            else:
                usage.error = (proc.stderr or proc.stdout).strip()[:200] or "auth status failed"
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            usage.error = f"{type(exc).__name__}: {exc}"[:200]

        windows, activity, report_error = self._claude_live_windows(binary)
        usage.windows = windows
        if not windows:
            # No invented percentage: say what failed and fall back to what is real.
            usage.note = (
                "Could not read live limits from `claude /usage`"
                + (f" ({report_error})" if report_error else "")
                + ". Showing plan, locally-computed token usage and the last limit hit.")
        elif activity:
            usage.note = ("Session and weekly limits come from `claude /usage`. The "
                          "request counts below are approximate and cover only "
                          "sessions on this Mac.")

        usage.totals = self._claude_totals()
        if activity:
            usage.totals = {**(usage.totals or {}), "activity": activity}
        usage.last_limit_event = self._claude_last_limit_event()
        return usage

    @staticmethod
    def _subprocess_env() -> dict[str, str]:
        """Environment for spawning `claude`.

        `USER` is load-bearing and easy to miss: without it `claude -p "/usage"`
        silently prints a cost summary instead of the usage report -- no error, no
        non-zero exit. A LaunchAgent inherits almost nothing, so it is filled in here
        rather than relying on how the daemon happened to be started.
        """
        env = dict(os.environ)
        env.setdefault("HOME", str(Path.home()))
        env.setdefault("USER", Path.home().name)
        env.setdefault("LOGNAME", env["USER"])
        env.setdefault("TMPDIR", "/tmp")
        path = env.get("PATH", "")
        local_bin = str(Path.home() / ".local" / "bin")
        if local_bin not in path.split(":"):
            env["PATH"] = f"{local_bin}:{path}" if path else local_bin
        return env

    @staticmethod
    def _claude_live_windows(binary: str
                             ) -> tuple[list[UsageWindow], dict[str, Any], Optional[str]]:
        """Read the live windows from Claude Code's own `/usage` command.

        `--no-session-persistence` matters: without it every poll leaves an empty
        session behind, which would fill the dashboard with junk. The command reports
        `num_turns: 0` and no cost, so polling it does not spend quota.
        """
        try:
            proc = subprocess.run(
                [binary, "-p", "/usage", "--no-session-persistence",
                 "--output-format", "json"],
                capture_output=True, text=True, timeout=CLAUDE_USAGE_TIMEOUT,
                cwd=str(Path.home()), env=UsageService._subprocess_env())
        except subprocess.TimeoutExpired:
            return [], {}, "timed out"
        except (OSError, subprocess.SubprocessError) as exc:
            return [], {}, f"{type(exc).__name__}: {exc}"[:120]

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip().splitlines()
            return [], {}, (detail[0][:120] if detail else f"exit {proc.returncode}")

        text = proc.stdout
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            pass
        else:
            if payload.get("is_error"):
                return [], {}, str(payload.get("result") or "reported an error")[:120]
            text = str(payload.get("result") or "")

        windows, activity = parse_usage_report(text)
        if not windows:
            return [], activity, "no usage lines in the output"
        return windows, activity, None

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
