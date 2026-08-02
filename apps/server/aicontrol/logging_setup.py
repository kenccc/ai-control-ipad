"""Structured logging.

Every record carries provider / session / repository when the caller supplies them, so
logs can be filtered by agent. Anything that looks like a credential is redacted before
it is written -- API keys must never reach a log file.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from typing import Any

_REDACTIONS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)(\S+)"),
    re.compile(r"(?i)(token[\"']?\s*[:=]\s*[\"']?)([A-Za-z0-9_\-.]{8,})"),
    re.compile(r"(?i)(api[_-]?key[\"']?\s*[:=]\s*[\"']?)([A-Za-z0-9_\-.]{8,})"),
    re.compile(r"\b(sk-[A-Za-z0-9_\-]{12,})"),
)

_STANDARD = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message", "asctime", "taskName"}


def redact(text: str) -> str:
    for pattern in _REDACTIONS:
        text = pattern.sub(lambda m: (m.group(1) + "<redacted>"
                                      if m.lastindex and m.lastindex >= 2
                                      else "<redacted>"), text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


def configure_logging(level: str | None = None) -> None:
    root = logging.getLogger()
    if getattr(root, "_aicontrol_configured", False):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter() if os.environ.get("AICONTROL_LOG_FORMAT", "json") == "json"
        else logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, (level or os.environ.get("AICONTROL_LOG_LEVEL", "INFO")).upper(),
                          logging.INFO))
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    root._aicontrol_configured = True  # type: ignore[attr-defined]
