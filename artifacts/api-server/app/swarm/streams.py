"""In-process pub/sub for streaming LLM events to WebSocket subscribers.

`WS /ws/llm-logs` subscribes; `runner.run_debate` publishes.  Subscribers
that fall behind are dropped rather than blocking the producer.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any


log = logging.getLogger(__name__)


class LLMLogHub:
    def __init__(self, queue_size: int = 256) -> None:
        self._subs: set[asyncio.Queue[dict[str, Any]]] = set()
        self._queue_size = queue_size

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_size)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subs.discard(q)

    async def publish(self, event: dict[str, Any]) -> None:
        # Snapshot so iteration is safe if a subscriber unsubscribes mid-publish.
        for q in list(self._subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Slow subscriber — drop it rather than blocking the swarm.
                log.warning("llm log subscriber dropped (queue full)")
                self._subs.discard(q)


llm_log_hub = LLMLogHub()
