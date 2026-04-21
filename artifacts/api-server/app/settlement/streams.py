"""In-process pub/sub for the settlement pipeline (mirrors orchestrator
streams).  Each WS subscriber gets a bounded queue; if a subscriber
falls behind we drop the oldest event so a slow consumer can't stall
the publisher.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any


log = logging.getLogger(__name__)
_QUEUE_SIZE = 256


class _Hub:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue[dict[str, Any]]] = set()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_QUEUE_SIZE)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subs.discard(q)

    async def publish(self, event: dict[str, Any]) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    _ = q.get_nowait()
                except Exception:
                    pass
                try:
                    q.put_nowait(event)
                except Exception:
                    pass


settlement_event_hub = _Hub()
