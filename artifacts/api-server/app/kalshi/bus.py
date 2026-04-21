"""In-process pub/sub for Kalshi events (orderbook updates, fills, lifecycle).

Subscribers receive every published message via their own bounded asyncio.Queue.
Slow subscribers DROP the oldest messages so a single hung consumer cannot stall
the publisher.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator

log = logging.getLogger(__name__)


class _Bus:
    def __init__(self, *, max_queue: int = 256) -> None:
        self._subs: set[asyncio.Queue[Any]] = set()
        self._lock = asyncio.Lock()
        self._max_queue = max_queue

    async def publish(self, message: dict[str, Any]) -> None:
        async with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                # Drop oldest then enqueue
                try:
                    _ = q.get_nowait()
                    q.put_nowait(message)
                    log.warning("bus subscriber backlog full; dropped oldest message")
                except Exception:
                    pass

    async def subscribe(self) -> asyncio.Queue[Any]:
        q: asyncio.Queue[Any] = asyncio.Queue(maxsize=self._max_queue)
        async with self._lock:
            self._subs.add(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue[Any]) -> None:
        async with self._lock:
            self._subs.discard(q)

    async def stream(self) -> AsyncIterator[dict[str, Any]]:
        q = await self.subscribe()
        try:
            while True:
                msg = await q.get()
                yield msg
        finally:
            await self.unsubscribe(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)


bus = _Bus()
