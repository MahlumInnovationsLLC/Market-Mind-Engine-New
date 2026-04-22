"""Market → econ-release resolver for the events-econ vertical (task #202).

Reads the `econ_releases` cache populated by `app.feeds.econ_calendar`
and returns the rows scoped to a parsed ticker's release_type within
a lookback / lookahead window.  When the ticker carries a `when`
token (24DEC / 25Q1 / 25W08 / 25MAR), the resolver narrows the
candidate set to releases whose `release_at` falls inside that window
so the model anchors on the *correct* upcoming print rather than
whichever same-type release happens to be next.
"""
from __future__ import annotations

import calendar as _stdcal
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import db
from .tickers import EventsEconTicker


log = logging.getLogger(__name__)


DEFAULT_LOOKBACK_DAYS = 730  # 2yr per spec
DEFAULT_LOOKAHEAD_DAYS = 90


_MONTH_TO_NUM = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


_WHEN_MONTH_RE = re.compile(r"^(\d{2})([A-Z]{3})$")          # 24DEC
_WHEN_QUARTER_RE = re.compile(r"^(\d{2})Q([1-4])$")          # 25Q1
_WHEN_WEEK_RE = re.compile(r"^(\d{2})W(\d{1,2})$")           # 25W08
_WHEN_MONTH_DAY_RE = re.compile(r"^(\d{2})([A-Z]{3})(\d{1,2})$")  # 25MAR15


def parse_when_to_window(when: str | None) -> tuple[datetime, datetime] | None:
    """Translate a ticker `when` token to a UTC (start, end) window.

    Returns None when the token can't be parsed, in which case the
    caller falls back to the broader release-type-only filter.  Year
    is interpreted as ``2000 + YY``.
    """
    if not when:
        return None
    w = when.upper().strip()
    m = _WHEN_MONTH_DAY_RE.match(w)
    if m:
        yy, mon, day = m.groups()
        month = _MONTH_TO_NUM.get(mon)
        if not month:
            return None
        try:
            d = datetime(2000 + int(yy), month, int(day), tzinfo=timezone.utc)
            return d, d + timedelta(days=1)
        except ValueError:
            return None
    m = _WHEN_MONTH_RE.match(w)
    if m:
        yy, mon = m.groups()
        month = _MONTH_TO_NUM.get(mon)
        if not month:
            return None
        try:
            year = 2000 + int(yy)
            start = datetime(year, month, 1, tzinfo=timezone.utc)
            last_day = _stdcal.monthrange(year, month)[1]
            end = datetime(year, month, last_day, 23, 59, 59, tzinfo=timezone.utc)
            return start, end
        except ValueError:
            return None
    m = _WHEN_QUARTER_RE.match(w)
    if m:
        yy, q = m.groups()
        try:
            year = 2000 + int(yy)
            qn = int(q)
            start_month = (qn - 1) * 3 + 1
            end_month = start_month + 2
            start = datetime(year, start_month, 1, tzinfo=timezone.utc)
            last_day = _stdcal.monthrange(year, end_month)[1]
            end = datetime(year, end_month, last_day, 23, 59, 59, tzinfo=timezone.utc)
            return start, end
        except ValueError:
            return None
    m = _WHEN_WEEK_RE.match(w)
    if m:
        yy, wk = m.groups()
        try:
            year = 2000 + int(yy)
            week = int(wk)
            jan4 = datetime(year, 1, 4, tzinfo=timezone.utc)
            week1_monday = jan4 - timedelta(days=jan4.weekday())
            start = week1_monday + timedelta(weeks=week - 1)
            end = start + timedelta(days=7)
            return start, end
        except ValueError:
            return None
    return None


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

    The result includes the *specific upcoming release* matching the
    ticker's `when` window (when parseable) plus the broader settled
    history needed for variance estimation.  When `when` is
    unparseable we fall back to the broader release-type-only filter.

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
    # If the ticker carries a parseable `when` window, restrict the
    # *upcoming-release* row to that window so the model anchors on
    # the right specific print.  Settled history (rows with `actual`
    # set) is preserved unfiltered for the variance estimator.
    window = parse_when_to_window(ticker.when)
    if window is not None:
        win_start, win_end = window
        upcoming_in_window = [
            r for r in out
            if r.actual is None
            and r.release_at is not None
            and (
                (r.release_at if r.release_at.tzinfo else
                 r.release_at.replace(tzinfo=timezone.utc))
                >= win_start
            )
            and (
                (r.release_at if r.release_at.tzinfo else
                 r.release_at.replace(tzinfo=timezone.utc))
                <= win_end
            )
        ]
        settled = [r for r in out if r.actual is not None]
        out = [*upcoming_in_window, *settled]
    return out


async def resolved_release_for_ticker(
    ticker: EventsEconTicker,
) -> ReleaseRow | None:
    """Return the specific upcoming release row matching this ticker.

    Used by the gate to scope the pre-release blackout to the
    candidate's *own* release instance rather than every same-type
    contract on the board.
    """
    window = parse_when_to_window(ticker.when)
    rows = await resolve_releases(ticker)
    upcoming = [r for r in rows if r.actual is None and r.release_at is not None]
    if not upcoming:
        return None
    if window is not None:
        win_start, win_end = window
        scoped = [
            r for r in upcoming
            if r.release_at is not None
            and (r.release_at if r.release_at.tzinfo else
                 r.release_at.replace(tzinfo=timezone.utc)) >= win_start
            and (r.release_at if r.release_at.tzinfo else
                 r.release_at.replace(tzinfo=timezone.utc)) <= win_end
        ]
        if scoped:
            scoped.sort(key=lambda r: r.release_at)
            return scoped[0]
        # If the ticker carries a window but no row matches, the
        # blackout cannot be triggered — return None so the gate
        # falls through.
        return None
    upcoming.sort(key=lambda r: r.release_at)
    return upcoming[0]


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
