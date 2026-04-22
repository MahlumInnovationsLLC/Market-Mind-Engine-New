"""Market → evidence resolver for the tech-AI vertical (task #199).

Reads the `tech_evidence` cache populated by `app.feeds.tech_evidence`
and returns the relevant rows for a given parsed ticker, scoped by
symbol class and a lookback window.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import db
from .tickers import TechAITicker


log = logging.getLogger(__name__)


DEFAULT_LOOKBACK_DAYS = 30


@dataclass(frozen=True)
class EvidenceRow:
    source: str
    signal_id: str
    symbol: str
    kind: str
    observed_at: datetime
    weight: float
    payload: dict[str, Any]


async def resolve_evidence(
    ticker: TechAITicker,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    limit: int = 50,
) -> list[EvidenceRow]:
    """Return recent evidence rows that bear on this ticker.

    Best-effort: any database failure logs at debug and returns [] so
    a sick cache table never crashes the predictor (it falls through
    to the prior probability).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, lookback_days))
    try:
        rows = await db.fetch(
            """
            SELECT source, signal_id, symbol, kind, observed_at,
                   weight, payload
              FROM tech_evidence
             WHERE symbol = $1
               AND observed_at >= $2
             ORDER BY observed_at DESC
             LIMIT $3
            """,
            ticker.symbol, cutoff, int(limit),
        )
    except Exception as e:
        log.debug("evidence lookup failed for %s: %s", ticker.symbol, e)
        return []
    out: list[EvidenceRow] = []
    for r in rows:
        payload = r["payload"]
        if isinstance(payload, str):
            try:
                import json as _json
                payload = _json.loads(payload)
            except Exception:
                payload = {}
        out.append(
            EvidenceRow(
                source=str(r["source"]),
                signal_id=str(r["signal_id"]),
                symbol=str(r["symbol"]),
                kind=str(r["kind"]),
                observed_at=r["observed_at"],
                weight=float(r["weight"] or 0.0),
                payload=dict(payload or {}),
            )
        )
    return out
