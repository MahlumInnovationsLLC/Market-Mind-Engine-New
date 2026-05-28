"""Persist ``market_settle_complete`` events to ``settlement_events``.

Long-lived subscriber on the settlement event hub.  When a market
finalises, the WS event is also written to Postgres so the dashboard's
recent-settlements panel survives API restarts and is consistent
across replicas (the WS only delivers events that occur while a
client is connected).

Lives under :mod:`app.lifecycle` so it picks up the per-loop restart /
backoff / observability handling instead of the bespoke inline
``asyncio.create_task`` block that previously lived in ``main.py``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .. import db
from .base import BackgroundLoop

log = logging.getLogger("axiom.lifecycle.settlement_recent_tap")


class SettlementRecentTap(BackgroundLoop):
    name = "settlement_recent_tap"

    def __init__(self) -> None:
        super().__init__()
        # Bind to the hub lazily on first tick so test code can
        # monkeypatch the hub before any subscription happens.
        self._queue = None

    async def _ensure_subscribed(self) -> None:
        if self._queue is not None:
            return
        from ..settlement import settlement_event_hub
        self._queue = settlement_event_hub.subscribe(label="recent-tap")

    async def tick(self) -> None:
        await self._ensure_subscribed()
        assert self._queue is not None
        ev = await self._queue.get()
        if isinstance(ev, dict) and ev.get("type") == "market_settle_complete":
            await _record_recent_settlement(ev)
        self._mark_tick()

    async def stop(self, *, timeout: float = 5.0) -> None:
        await super().stop(timeout=timeout)
        if self._queue is not None:
            try:
                from ..settlement import settlement_event_hub
                settlement_event_hub.unsubscribe(self._queue)
            except Exception as e:  # pragma: no cover
                log.debug("settlement_recent_tap unsubscribe failed: %s", e)
            self._queue = None


async def _record_recent_settlement(event: dict[str, Any]) -> None:
    """Persist a market_settle_complete event.  Failures are logged but
    do not propagate — the WS feed still delivers the live event."""
    try:
        verticals = event.get("verticals")
        if not isinstance(verticals, list):
            verticals = None
        else:
            verticals = [str(v) for v in verticals]
        outcome = event.get("outcome_yes")
        outcome_int = int(outcome) if isinstance(outcome, (int, float)) else None
        trades = event.get("trades_settled")
        trades_int = int(trades) if isinstance(trades, (int, float)) else None
        alerts = event.get("alerts_raised")
        alerts_int = int(alerts) if isinstance(alerts, (int, float)) else None
        market_id = event.get("market_id")
        market_str = str(market_id) if market_id is not None else None
        await db.execute(
            """
            INSERT INTO settlement_events (
                event_type, market_id, outcome_yes, trades_settled,
                alerts_raised, verticals, payload
            ) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
            """,
            str(event.get("type") or "market_settle_complete"),
            market_str,
            outcome_int,
            trades_int,
            alerts_int,
            verticals,
            json.dumps(event, default=str),
        )
    except Exception as e:  # pragma: no cover
        log.warning("settlement event persist failed: %s", e)


# Singleton — registered by :func:`register_default_loops`.
instance = SettlementRecentTap()


def register_default_loops() -> None:
    """Idempotently register every native loop owned by this package."""
    instance.register()
