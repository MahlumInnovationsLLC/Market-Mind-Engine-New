"""Market → polls resolver for the elections vertical (task #201).

Reads the `election_polls` cache populated by `app.feeds.election_polls`
and returns the relevant rows for a given parsed ticker, scoped by the
canonical `scope` key + a lookback window.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import db
from .tickers import ElectionTicker


log = logging.getLogger(__name__)


DEFAULT_LOOKBACK_DAYS = 60


@dataclass(frozen=True)
class PollRow:
    source: str
    signal_id: str
    scope: str
    candidate: str
    kind: str
    observed_at: datetime
    sample_size: int | None
    support_pct: float | None
    weight: float
    payload: dict[str, Any]


async def resolve_polls(
    ticker: ElectionTicker,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    limit: int = 100,
) -> list[PollRow]:
    """Return recent poll rows that bear on this ticker.

    Best-effort: any database failure logs at debug and returns [] so
    a sick cache table never crashes the predictor (it falls through
    to the prior probability).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, lookback_days))
    try:
        rows = await db.fetch(
            """
            SELECT source, signal_id, scope, candidate, kind, observed_at,
                   sample_size, support_pct, weight, payload
              FROM election_polls
             WHERE scope = $1
               AND observed_at >= $2
             ORDER BY observed_at DESC
             LIMIT $3
            """,
            ticker.scope, cutoff, int(limit),
        )
    except Exception as e:
        log.debug("poll lookup failed for %s: %s", ticker.scope, e)
        return []
    out: list[PollRow] = []
    for r in rows:
        payload = r["payload"]
        if isinstance(payload, str):
            try:
                import json as _json
                payload = _json.loads(payload)
            except Exception:
                payload = {}
        out.append(
            PollRow(
                source=str(r["source"]),
                signal_id=str(r["signal_id"]),
                scope=str(r["scope"]),
                candidate=str(r["candidate"]),
                kind=str(r["kind"]),
                observed_at=r["observed_at"],
                sample_size=int(r["sample_size"]) if r["sample_size"] is not None else None,
                support_pct=(
                    float(r["support_pct"]) if r["support_pct"] is not None else None
                ),
                weight=float(r["weight"] or 0.0),
                payload=dict(payload or {}),
            )
        )
    return out
