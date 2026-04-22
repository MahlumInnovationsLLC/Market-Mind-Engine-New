"""Market → entertainment-signal resolver for the entertainment vertical (task #203).

Reads the `entertainment_signals` cache populated by
`app.feeds.entertainment_signals` and returns the rows scoped to a
parsed ticker's signal_type / title within a lookback / lookahead
window.  When the ticker carries a `when` token (25MAR / 25Q1 /
25W08 / 25), the resolver narrows the candidate set to signals whose
`event_at` falls inside that window.
"""
from __future__ import annotations

import calendar as _stdcal
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import db
from .tickers import EntertainmentTicker


log = logging.getLogger(__name__)


DEFAULT_LOOKBACK_DAYS = 1825   # 5yr — awards / box-office histories are long
DEFAULT_LOOKAHEAD_DAYS = 365


_MONTH_TO_NUM = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


_WHEN_MONTH_RE = re.compile(r"^(\d{2})([A-Z]{3})$")          # 25MAR
_WHEN_QUARTER_RE = re.compile(r"^(\d{2})Q([1-4])$")          # 25Q1
_WHEN_WEEK_RE = re.compile(r"^(\d{2})W(\d{1,2})$")           # 25W08
_WHEN_MONTH_DAY_RE = re.compile(r"^(\d{2})([A-Z]{3})(\d{1,2})$")  # 25MAR15
_WHEN_YEAR_RE = re.compile(r"^(\d{2,4})$")                    # 25 (awards cycle)


def parse_when_to_window(when: str | None) -> tuple[datetime, datetime] | None:
    """Translate a ticker `when` token to a UTC (start, end) window.

    Returns None when the token can't be parsed.  Year tokens (25 ->
    2025) cover the full calendar year — used for awards-cycle tickers
    (KXOSCAR-25-...) where the ceremony lands on a single date but the
    eligibility / nomination window spans the year.
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
    m = _WHEN_YEAR_RE.match(w)
    if m:
        try:
            yy = int(m.group(1))
            year = 2000 + yy if yy < 100 else yy
            start = datetime(year, 1, 1, tzinfo=timezone.utc)
            end = datetime(year, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
            return start, end
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class SignalRow:
    source: str
    signal_id: str
    signal_type: str
    title: str
    event_at: datetime
    value: float | None
    settled: bool
    outcome: str | None
    payload: dict[str, Any]


def _row_from_db(r: dict[str, Any]) -> SignalRow:
    payload = r["payload"]
    if isinstance(payload, str):
        try:
            import json as _json
            payload = _json.loads(payload)
        except Exception:
            payload = {}
    return SignalRow(
        source=str(r["source"]),
        signal_id=str(r["signal_id"]),
        signal_type=str(r["signal_type"]),
        title=str(r["title"]),
        event_at=r["event_at"],
        value=(float(r["value"]) if r["value"] is not None else None),
        settled=bool(r["settled"]),
        outcome=(str(r["outcome"]) if r["outcome"] is not None else None),
        payload=dict(payload or {}),
    )


async def resolve_signals(
    ticker: EntertainmentTicker,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    lookahead_days: int = DEFAULT_LOOKAHEAD_DAYS,
    limit: int = 200,
) -> list[SignalRow]:
    """Return historical + upcoming signal rows for the ticker's signal_type.

    The result includes the *specific upcoming signal* matching the
    ticker's `when` window (when parseable) plus the broader settled
    history needed for the variance / Bayesian estimators.
    """
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=max(1, lookback_days))
    end = now + timedelta(days=max(1, lookahead_days))
    try:
        rows = await db.fetch(
            """
            SELECT source, signal_id, signal_type, title, event_at,
                   value, settled, outcome, payload
              FROM entertainment_signals
             WHERE signal_type = $1
               AND event_at BETWEEN $2 AND $3
             ORDER BY event_at DESC
             LIMIT $4
            """,
            ticker.signal_type, start, end, int(limit),
        )
    except Exception as e:
        log.debug("entertainment signal lookup failed for %s: %s", ticker.signal_type, e)
        return []
    out = [_row_from_db(r) for r in rows]
    window = parse_when_to_window(ticker.when)
    if window is not None:
        win_start, win_end = window
        upcoming_in_window = [
            r for r in out
            if not r.settled
            and r.event_at is not None
            and (
                (r.event_at if r.event_at.tzinfo else
                 r.event_at.replace(tzinfo=timezone.utc))
                >= win_start
            )
            and (
                (r.event_at if r.event_at.tzinfo else
                 r.event_at.replace(tzinfo=timezone.utc))
                <= win_end
            )
        ]
        settled = [r for r in out if r.settled]
        out = [*upcoming_in_window, *settled]
    return out


async def settled_history_for_type(
    signal_type: str,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    limit: int = 200,
) -> list[SignalRow]:
    """Return *settled* rows of a signal type for variance / base-rate work."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=max(1, lookback_days))
    try:
        rows = await db.fetch(
            """
            SELECT source, signal_id, signal_type, title, event_at,
                   value, settled, outcome, payload
              FROM entertainment_signals
             WHERE signal_type = $1
               AND settled = true
               AND event_at >= $2
             ORDER BY event_at DESC
             LIMIT $3
            """,
            signal_type, start, limit,
        )
    except Exception as e:
        log.debug("entertainment settled history lookup failed for %s: %s", signal_type, e)
        return []
    return [_row_from_db(r) for r in rows]


async def resolved_signal_for_ticker(
    ticker: EntertainmentTicker,
) -> SignalRow | None:
    """Return the specific upcoming signal row matching this ticker.

    Used by the gate to scope the release-day blackout to the
    candidate's *own* event instance rather than every same-type
    contract on the board.
    """
    window = parse_when_to_window(ticker.when)
    rows = await resolve_signals(ticker)
    upcoming = [r for r in rows if not r.settled and r.event_at is not None]
    if not upcoming:
        return None
    title_lc = ticker.title.lower()
    title_matches = [
        r for r in upcoming if title_lc in r.title.lower() or r.title.lower() in title_lc
    ]
    pool = title_matches or upcoming
    if window is not None:
        win_start, win_end = window
        scoped = [
            r for r in pool
            if r.event_at is not None
            and (r.event_at if r.event_at.tzinfo else
                 r.event_at.replace(tzinfo=timezone.utc)) >= win_start
            and (r.event_at if r.event_at.tzinfo else
                 r.event_at.replace(tzinfo=timezone.utc)) <= win_end
        ]
        if scoped:
            scoped.sort(key=lambda r: r.event_at)
            return scoped[0]
        return None
    pool.sort(key=lambda r: r.event_at)
    return pool[0]


async def upcoming_signals(
    *,
    within_hours: float = 168.0,
    limit: int = 50,
    signal_type: str | None = None,
) -> list[SignalRow]:
    """Signals scheduled within the next `within_hours` (default 7 days).

    Used by the dashboard weekend-release calendar and the awards
    leaderboard panels.
    """
    now = datetime.now(timezone.utc)
    end = now + timedelta(hours=max(1.0, within_hours))
    try:
        if signal_type is not None:
            rows = await db.fetch(
                """
                SELECT source, signal_id, signal_type, title, event_at,
                       value, settled, outcome, payload
                  FROM entertainment_signals
                 WHERE signal_type = $4
                   AND event_at >= $1 AND event_at <= $2
                   AND settled = false
                 ORDER BY event_at ASC
                 LIMIT $3
                """,
                now, end, int(limit), signal_type,
            )
        else:
            rows = await db.fetch(
                """
                SELECT source, signal_id, signal_type, title, event_at,
                       value, settled, outcome, payload
                  FROM entertainment_signals
                 WHERE event_at >= $1 AND event_at <= $2
                   AND settled = false
                 ORDER BY event_at ASC
                 LIMIT $3
                """,
                now, end, int(limit),
            )
    except Exception as e:
        log.debug("upcoming entertainment signals read failed: %s", e)
        return []
    return [_row_from_db(r) for r in rows]


async def awards_leaderboard(
    award_type: str,
    *,
    limit: int = 20,
) -> list[SignalRow]:
    """Return the leading nominees / candidates for an upcoming awards cycle.

    Settled rows for the same ``signal_type`` are filtered out — the
    leaderboard only surfaces the live race.  Sorted by rolling
    posterior probability stored on `value` (0..1) where the poller
    has computed it; otherwise falls back to event_at ASC.
    """
    try:
        rows = await db.fetch(
            """
            SELECT source, signal_id, signal_type, title, event_at,
                   value, settled, outcome, payload
              FROM entertainment_signals
             WHERE signal_type = $1
               AND settled = false
             ORDER BY value DESC NULLS LAST, event_at ASC
             LIMIT $2
            """,
            award_type.upper(), int(limit),
        )
    except Exception as e:
        log.debug("awards leaderboard lookup failed for %s: %s", award_type, e)
        return []
    return [_row_from_db(r) for r in rows]
