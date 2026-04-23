"""In-process pub/sub for pipeline events (mirror of swarm.streams)."""
from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from typing import Any

from .. import db
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

    On top of the in-memory ring, every event with a ``cycle_id`` is
    also persisted to ``pipeline_events`` (best-effort) so the snapshot
    endpoint can hydrate the dashboard from Postgres after an API
    restart, when the ring is cold (task #121).
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
        await self._persist(event)
        if not self._subs:
            return
        for sub in list(self._subs):
            sub.offer(event)

    async def _persist(self, event: dict[str, Any]) -> None:
        """Mirror an event into ``pipeline_events`` so the snapshot
        endpoint survives an API restart. Best-effort: a missing table
        (fresh dev DB before migrations) or transient pool failure is
        logged at debug and swallowed — the live WS stream and the
        in-memory ring are still authoritative for the current process.
        """
        cycle_id = event.get("cycle_id")
        if cycle_id is None:
            # recent_events() already skips events without a cycle_id;
            # mirroring them would just bloat the table.
            return
        try:
            payload = json.dumps(event, default=str)
        except (TypeError, ValueError) as e:
            log.debug("pipeline event not JSON-serialisable, skipped: %s", e)
            return
        try:
            await db.execute(
                """
                INSERT INTO pipeline_events (cycle_id, type, seq, payload)
                VALUES ($1, $2, $3, $4::jsonb)
                """,
                int(cycle_id),
                str(event.get("type") or ""),
                int(event.get("_seq") or 0),
                payload,
            )
        except Exception as e:
            log.debug("pipeline_events insert failed (best-effort): %s", e)

    def recent_events(self, max_cycles: int = 5) -> list[dict[str, Any]]:
        """Return buffered events from the most recent ``max_cycles``
        distinct ``cycle_id`` values, preserving original publish order.

        Events without a ``cycle_id`` are skipped — those don't belong
        to any one cycle card and would just confuse the dashboard
        hydration.  An empty buffer (cold start) returns ``[]`` — call
        :meth:`recent_events_with_fallback` if you want a DB-backed
        snapshot to survive API restarts.
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

    async def recent_events_with_fallback(
        self, max_cycles: int = 5
    ) -> list[dict[str, Any]]:
        """Same as :meth:`recent_events` but falls back to the
        ``pipeline_events`` table when the in-memory ring is cold (e.g.
        the API server just restarted and no new cycle has fired yet).
        """
        if max_cycles <= 0:
            return []
        in_mem = self.recent_events(max_cycles=max_cycles)
        if in_mem:
            return in_mem
        try:
            rows = await db.fetch(
                """
                WITH recent_cycles AS (
                    SELECT DISTINCT cycle_id
                      FROM pipeline_events
                     WHERE cycle_id IS NOT NULL
                     ORDER BY cycle_id DESC
                     LIMIT $1
                )
                SELECT payload, seq, id
                  FROM pipeline_events
                 WHERE cycle_id IN (SELECT cycle_id FROM recent_cycles)
                 ORDER BY seq ASC, id ASC
                """,
                int(max_cycles),
            )
        except Exception as e:
            log.debug("pipeline_events fallback read failed: %s", e)
            return []
        out: list[dict[str, Any]] = []
        for r in rows:
            raw = r["payload"]
            if isinstance(raw, str):
                try:
                    ev = json.loads(raw)
                except Exception:
                    continue
            elif isinstance(raw, dict):
                ev = raw
            else:
                continue
            # `_seq` is already inside the stored payload; trust the
            # column only if the payload was missing it (older rows).
            ev.setdefault("_seq", int(r["seq"]))
            out.append(ev)
        return out

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)


pipeline_event_hub = _PipelineEventHub()
