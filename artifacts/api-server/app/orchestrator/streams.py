"""In-process pub/sub for pipeline events (mirror of swarm.streams)."""
from __future__ import annotations

import asyncio
import logging
from typing import Any


log = logging.getLogger(__name__)


class _PipelineEventHub:
    """Bounded-queue fan-out hub.  Slow subscribers get dropped events
    rather than blocking the producer; this is what keeps a stuck
    websocket from stalling the orchestrator cycle."""

    QUEUE_MAXSIZE = 256

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue[dict[str, Any]]] = set()
        self._lock = asyncio.Lock()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self.QUEUE_MAXSIZE)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subs.discard(q)

    async def publish(self, event: dict[str, Any]) -> None:
        if not self._subs:
            return
        for q in list(self._subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # drop one stale event to make room rather than block
                try:
                    _ = q.get_nowait()
                except Exception:
                    pass
                try:
                    q.put_nowait(event)
                except Exception as e:
                    log.warning("pipeline event drop: %s", e)


pipeline_event_hub = _PipelineEventHub()
