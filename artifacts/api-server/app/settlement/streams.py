"""In-process pub/sub for the settlement pipeline (mirrors orchestrator
streams).  Each WS subscriber gets a bounded queue; if a subscriber
falls behind we drop the oldest event so a slow consumer can't stall
the publisher.  Subscribers can opt into the close-on-overflow policy
instead — see :mod:`app.ws_backpressure`.
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


class _Hub:
    def __init__(self) -> None:
        self.QUEUE_MAXSIZE = resolve_maxsize("settlement")
        self._subs: set[Subscription] = set()

    def subscribe(
        self,
        *,
        maxsize: int | None = None,
        policy: str = DROP_OLDEST,
        label: str = "settlement",
    ) -> Subscription:
        sub = Subscription(
            hub="settlement",
            label=label,
            maxsize=maxsize or self.QUEUE_MAXSIZE,
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


settlement_event_hub = _Hub()
