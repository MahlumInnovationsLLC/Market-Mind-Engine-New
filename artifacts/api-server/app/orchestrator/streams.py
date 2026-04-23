"""In-process pub/sub for pipeline events (mirror of swarm.streams)."""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any

from ..ws_backpressure import (
    DROP_OLDEST,
    Subscription,
    resolve_maxsize,
)


log = logging.getLogger(__name__)


class _PipelineEventHub:
    """Bounded-queue fan-out hub.  Slow subscribers get dropped events
    (or a forced close, depending on the per-subscriber policy) rather
    than blocking the producer; this is what keeps a stuck websocket
    from stalling the orchestrator cycle.

    Also keeps a bounded ring buffer of recently-published events so a
    REST snapshot endpoint can hydrate the pipeline page on first mount
    or after a reload.  Each published event is stamped with a
    monotonic ``_seq`` so the client can dedupe snapshot+stream.
    """

    BUFFER_MAXSIZE = 500

    def __init__(self) -> None:
        # Resolve at construction time so the env var override applies
        # without re-importing the module.
        self.QUEUE_MAXSIZE = resolve_maxsize("pipeline")
        self._subs: set[Subscription] = set()
        self._lock = asyncio.Lock()
        self._buffer: deque[dict[str, Any]] = deque(maxlen=self.BUFFER_MAXSIZE)
        self._seq: int = 0

    def subscribe(
        self,
        *,
        maxsize: int | None = None,
        policy: str = DROP_OLDEST,
        label: str = "pipeline",
    ) -> Subscription:
        sub = Subscription(
            hub="pipeline",
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
        self._seq += 1
        event["_seq"] = self._seq
        self._buffer.append(event)
        if not self._subs:
            return
        for sub in list(self._subs):
            sub.offer(event)

    def recent_events(self, max_cycles: int = 5) -> list[dict[str, Any]]:
        """Return buffered events from the most recent ``max_cycles``
        distinct ``cycle_id`` values, preserving original publish order.

        Events without a ``cycle_id`` are skipped — those don't belong
        to any one cycle card and would just confuse the dashboard
        hydration.  An empty buffer (cold start) returns ``[]``.
        """
        if max_cycles <= 0 or not self._buffer:
            return []
        seen: set[Any] = set()
        order: list[Any] = []
        for ev in reversed(self._buffer):
            cid = ev.get("cycle_id")
            if cid is None:
                continue
            if cid not in seen:
                seen.add(cid)
                order.append(cid)
                if len(order) >= max_cycles:
                    break
        if not seen:
            return []
        return [ev for ev in self._buffer if ev.get("cycle_id") in seen]

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)


pipeline_event_hub = _PipelineEventHub()
