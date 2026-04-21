"""In-process pub/sub for pipeline events (mirror of swarm.streams)."""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any


log = logging.getLogger(__name__)


class _PipelineEventHub:
    """Bounded-queue fan-out hub.  Slow subscribers get dropped events
    rather than blocking the producer; this is what keeps a stuck
    websocket from stalling the orchestrator cycle.

    Also keeps a bounded ring buffer of recently-published events so a
    REST snapshot endpoint can hydrate the pipeline page on first mount
    or after a reload.  Each published event is stamped with a
    monotonic ``_seq`` so the client can dedupe snapshot+stream.
    """

    QUEUE_MAXSIZE = 256
    BUFFER_MAXSIZE = 500

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue[dict[str, Any]]] = set()
        self._lock = asyncio.Lock()
        self._buffer: deque[dict[str, Any]] = deque(maxlen=self.BUFFER_MAXSIZE)
        self._seq: int = 0

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self.QUEUE_MAXSIZE)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subs.discard(q)

    async def publish(self, event: dict[str, Any]) -> None:
        self._seq += 1
        event["_seq"] = self._seq
        self._buffer.append(event)
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


pipeline_event_hub = _PipelineEventHub()
