"""In-process pub/sub for Kalshi events (orderbook updates, fills, lifecycle).

Subscribers receive every published message via their own bounded asyncio.Queue.
Slow subscribers DROP the oldest messages by default so a single hung consumer
cannot stall the publisher; opt into ``policy="close"`` to instead force-close
the connection on overflow (used for live orderbook channels where stale
events are worse than no events).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator

from ..ws_backpressure import (
    DEFAULT_MAXSIZE,
    DROP_OLDEST,
    Subscription,
)

log = logging.getLogger(__name__)


class _Bus:
    def __init__(self, *, max_queue: int = DEFAULT_MAXSIZE) -> None:
        self._subs: set[Subscription] = set()
        self._lock = asyncio.Lock()
        self._max_queue = max_queue

    async def publish(self, message: dict[str, Any]) -> None:
        async with self._lock:
            subs = list(self._subs)
        for sub in subs:
            sub.offer(message)

    async def subscribe(
        self,
        *,
        maxsize: int | None = None,
        policy: str = DROP_OLDEST,
        label: str = "kalshi",
    ) -> Subscription:
        sub = Subscription(
            hub="kalshi",
            label=label,
            maxsize=maxsize or self._max_queue,
            policy=policy,
        )
        async with self._lock:
            self._subs.add(sub)
        return sub

    async def unsubscribe(self, sub: Subscription) -> None:
        async with self._lock:
            self._subs.discard(sub)
        sub.release()

    async def stream(self) -> AsyncIterator[dict[str, Any]]:
        sub = await self.subscribe(label="kalshi-stream")
        try:
            while True:
                msg = await sub.get()
                yield msg
        finally:
            await self.unsubscribe(sub)

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)


bus = _Bus()
