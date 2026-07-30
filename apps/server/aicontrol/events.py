"""In-process pub/sub feeding the WebSocket layer."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

log = logging.getLogger("aicontrol.events")


class EventBus:
    def __init__(self, *, queue_size: int = 512) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._queue_size = queue_size

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def publish(self, type_: str, **payload: Any) -> None:
        message = {"type": type_, "ts": time.time(), **payload}
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # A client that cannot keep up loses the oldest event rather than
                # stalling every other subscriber.
                try:
                    queue.get_nowait()
                    queue.put_nowait(message)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    log.warning("dropping event for a saturated subscriber")

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
