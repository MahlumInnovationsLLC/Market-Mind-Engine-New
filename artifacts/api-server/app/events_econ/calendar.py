"""Market → econ-release resolver for the events-econ vertical (task #202).

Reads the `econ_releases` cache populated by `app.feeds.econ_calendar`
and returns the rows scoped to a parsed ticker's release_type within
a lookback / lookahead window.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import db
from .tickers import EventsEconTicker


log = logging.getLogger(__name__)


DEFAULT_LOOKBACK_DAYS = 730  # 2yr per spec
DEFAULT_LOOKAHEAD_DAYS = 90


@dataclass(frozen=True)
class ReleaseRow:
    source: str
    signal_id: str
    release_type: str
    release_at: datetime
    consensus: float | None
    previous: float | None
    actual: float | None
    revised_previous: float | None
    payload: dict[str, Any]


async def resolve_releases(
    ticker: EventsEconTicker,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    lookahead_days: int = DEFAULT_LOOKAHEAD_DAYS,
    limit: int = 200,
) -> list[ReleaseRow]:
    """Return historical + upcoming release rows for the ticker's release_type.

    Best-effort: any DB failure logs at debug and returns [] so a sick
    cache table never crashes the predictor (it falls through to the
    prior probability).
    """
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=max(1, lookback_days))
    end = now + timedelta(days=max(1, lookahead_days))
    try:
        rows = await db.fetch(
            """
            SELECT source, signal_id, release_type, release_at,
                   consensus, previous, actual, revised_previous, payload
              FROM econ_releases
             WHERE release_type = $1
               AND release_at BETWEEN $2 AND $3
             ORDER BY release_at DESC
             LIMIT $4
            """,
            ticker.release_type, start, end, int(limit),
        )
    except Exception as e:
        log.debug("econ release lookup failed for %s: %s", ticker.release_type, e)
        return []
    out: list[ReleaseRow] = []
    for r in rows:
        payload = r["payload"]
        if isinstance(payload, str):
            try:
                import json as _json
                payload = _json.loads(payload)
            except Exception:
                payload = {}
        out.append(
            ReleaseRow(
                source=str(r["source"]),
                signal_id=str(r["signal_id"]),
                release_type=str(r["release_type"]),
                release_at=r["release_at"],
                consensus=(float(r["consensus"]) if r["consensus"] is not None else None),
                previous=(float(r["previous"]) if r["previous"] is not None else None),
                actual=(float(r["actual"]) if r["actual"] is not None else None),
                revised_previous=(
                    float(r["revised_previous"]) if r["revised_previous"] is not None else None
                ),
                payload=dict(payload or {}),
            )
        )
    return out


async def upcoming_releases(
    *,
    within_hours: float = 168.0,
    limit: int = 50,
) -> list[ReleaseRow]:
    """Releases scheduled within the next `within_hours` (default 7 days).

    Used by the dashboard calendar panel and the pre-release blackout
    sub-check on `gate_market_cooldown`.
    """
    now = datetime.now(timezone.utc)
    end = now + timedelta(hours=max(1.0, within_hours))
    try:
        rows = await db.fetch(
            """
            SELECT source, signal_id, release_type, release_at,
                   consensus, previous, actual, revised_previous, payload
              FROM econ_releases
             WHERE release_at >= $1 AND release_at <= $2
               AND actual IS NULL
             ORDER BY release_at ASC
             LIMIT $3
            """,
            now, end, int(limit),
        )
    except Exception as e:
        log.debug("upcoming releases read failed: %s", e)
        return []
    out: list[ReleaseRow] = []
    for r in rows:
        payload = r["payload"]
        if isinstance(payload, str):
            try:
                import json as _json
                payload = _json.loads(payload)
            except Exception:
                payload = {}
        out.append(
            ReleaseRow(
                source=str(r["source"]),
                signal_id=str(r["signal_id"]),
                release_type=str(r["release_type"]),
                release_at=r["release_at"],
                consensus=(float(r["consensus"]) if r["consensus"] is not None else None),
                previous=(float(r["previous"]) if r["previous"] is not None else None),
                actual=(float(r["actual"]) if r["actual"] is not None else None),
                revised_previous=(
                    float(r["revised_previous"]) if r["revised_previous"] is not None else None
                ),
                payload=dict(payload or {}),
            )
        )
    return out


async def next_release_at(release_type: str) -> datetime | None:
    """Return the next scheduled release time for the given type, or None."""
    try:
        v = await db.fetchval(
            """
            SELECT release_at
              FROM econ_releases
             WHERE release_type = $1
               AND release_at >= now()
               AND actual IS NULL
             ORDER BY release_at ASC
             LIMIT 1
            """,
            release_type.upper(),
        )
        return v
    except Exception as e:
        log.debug("next_release_at failed for %s: %s", release_type, e)
        return None
