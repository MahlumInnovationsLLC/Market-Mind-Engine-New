"""In-process pub/sub for streaming LLM events to WebSocket subscribers.

`WS /ws/llm-logs` subscribes; `runner.run_debate` publishes.  Subscribers
that fall behind have their queue head dropped (default policy) so the
producer never blocks.  See :mod:`app.ws_backpressure`.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..ws_backpressure import (
    DROP_OLDEST,
    Subscription,
    resolve_maxsize,
)


log = logging.getLogger(__name__)


class LLMLogHub:
    def __init__(self, queue_size: int | None = None) -> None:
        self._subs: set[Subscription] = set()
        self._queue_size = queue_size if queue_size is not None else resolve_maxsize("llm-logs")

    def subscribe(
        self,
        *,
        maxsize: int | None = None,
        policy: str = DROP_OLDEST,
        label: str = "llm-logs",
    ) -> Subscription:
        sub = Subscription(
            hub="llm-logs",
            label=label,
            maxsize=maxsize or self._queue_size,
            policy=policy,
        )
        self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        self._subs.discard(sub)
        sub.release()

    async def publish(self, event: dict[str, Any]) -> None:
        for sub in list(self._subs):
            sub.offer(event)

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)


llm_log_hub = LLMLogHub()
