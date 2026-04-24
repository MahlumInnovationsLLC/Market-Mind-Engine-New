"""Historical entertainment-signal backfill (task #225).

The hourly pollers in :mod:`app.feeds.entertainment_signals` only see
the *current* upstream feed (latest weekend chart, latest awards
headlines, this-week's Spotify Top 200).  When the entertainment
vertical first comes online the `entertainment_signals` table is
effectively empty, so the hybrid model degrades to its 0.5 fallback
(no usable log-normal sigma, no awards base rate, no charts hit-share)
and the cycle dispatcher rejects almost every candidate.

This module provides a one-shot, admin-triggered *backfill* that walks
each registered upstream archive backwards N days (default 730 — two
years) so the predictor has calibrated priors from cycle 1.

Design:
  * :class:`HistoricalFetcher` describes one source + a per-window
    async fetcher.  Each builtin fetcher hits a date-templated URL
    (Box Office Mojo weekend archive, Spotify weekly chart download,
    Rotten Tomatoes browse archive, awards historical winners JSON)
    and returns rows in the shape that
    :func:`app.feeds.entertainment_signals.upsert_signals` accepts.
  * :func:`run_backfill` iterates each registered fetcher across the
    lookback window, upserts results, and tracks per-source counts.
  * Single-flight via an :class:`asyncio.Lock`: a second POST returns
    409 while a backfill is in progress.
  * Idempotent: every row carries a deterministic ``signal_id`` and
    upserts via the existing ``ON CONFLICT (source, signal_id)``
    clause.  Re-running the backfill never duplicates rows.
  * Final snapshot persisted to ``system_settings`` so the dashboard
    panel can surface ``last_run_at`` + per-source row counts across
    process restarts.
"""
from __future__ import annotations

import asyncio
import calendar as _stdcal
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from .. import db
from ..feeds import entertainment_signals as _feeds
from ..feeds.entertainment_signals import upsert_signals


log = logging.getLogger(__name__)


DEFAULT_BACKFILL_DAYS = 730        # two years
MIN_BACKFILL_DAYS = 7
MAX_BACKFILL_DAYS = 365 * 5        # five years — plenty for any awards head


# ---------------------------------------------------------------------------
# Fetcher registry
# ---------------------------------------------------------------------------


_FetcherFn = Callable[[datetime, datetime], Awaitable[list[dict[str, Any]]]]


@dataclass(frozen=True)
class HistoricalFetcher:
    """One historical-signal source.

    ``cadence_days`` is the stride between successive fetch windows.
    For weekly sources (box office, spotify) it's 7; for the awards
    historical winners JSON it's the entire lookback (one shot).

    ``fetch`` returns a list of upsertable signal rows in the shape
    accepted by :func:`upsert_signals`.
    """

    source: str
    cadence_days: int
    fetch: _FetcherFn


# ---------------------------------------------------------------------------
# Status tracking
# ---------------------------------------------------------------------------


_STATUS_KEY = "entertainment.backfill.last_run"


@dataclass
class BackfillStatus:
    state: str = "idle"             # idle | running | completed | failed
    started_at: str | None = None
    completed_at: str | None = None
    duration_seconds: float = 0.0
    days: int = 0
    rows_by_source: dict[str, int] = field(default_factory=dict)
    rows_total: int = 0
    sources_failed: list[str] = field(default_factory=list)
    last_error: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_seconds": round(self.duration_seconds, 3),
            "days": self.days,
            "rows_by_source": dict(self.rows_by_source),
            "rows_total": self.rows_total,
            "sources_failed": list(self.sources_failed),
            "last_error": self.last_error,
        }


_status = BackfillStatus()
_lock = asyncio.Lock()


def is_running() -> bool:
    """True iff a backfill currently holds the single-flight lock."""
    return _lock.locked()


def get_status() -> dict[str, Any]:
    """In-process snapshot of the most recent / current backfill run."""
    return _status.snapshot()


async def load_persisted_status() -> dict[str, Any] | None:
    """Read the most-recent successful backfill snapshot from
    ``system_settings`` so the dashboard panel can show last-run info
    even after a process restart.

    Returns None when no backfill has ever completed (or when the row
    can't be read — best-effort)."""
    try:
        row = await db.fetchrow(
            "SELECT value FROM system_settings WHERE key = $1",
            _STATUS_KEY,
        )
    except Exception as e:  # pragma: no cover — defensive
        log.debug("backfill status load failed: %s", e)
        return None
    if not row or not row.get("value"):
        return None
    try:
        return json.loads(row["value"])
    except Exception:
        return None


async def _persist_status(snapshot: dict[str, Any]) -> None:
    try:
        await db.execute(
            """
            INSERT INTO system_settings (key, value, updated_at)
            VALUES ($1, $2, now())
            ON CONFLICT (key) DO UPDATE
              SET value = EXCLUDED.value,
                  updated_at = now()
            """,
            _STATUS_KEY, json.dumps(snapshot),
        )
    except Exception as e:
        log.warning("backfill status persist failed: %s", e)


# ---------------------------------------------------------------------------
# Builtin historical fetchers
# ---------------------------------------------------------------------------


# Real upstream archive URLs.  All are best-effort: a missing or
# network-unreachable source returns an empty list and the run
# continues with the remaining sources.
_BOX_OFFICE_HIST_URL_TEMPLATE = (
    "https://www.boxofficemojo.com/weekend/{year}W{week:02d}/"
)
_SPOTIFY_HIST_URL_TEMPLATE = (
    "https://charts.spotify.com/charts/view/regional-global-weekly/"
    "{date:%Y-%m-%d}/download"
)
_RT_HIST_URL_TEMPLATE = (
    "https://www.rottentomatoes.com/browse/movies_at_home/year:{year}"
)


_BOM_ROW_RE = re.compile(
    r'<tr[^>]*>\s*'
    r'<td[^>]*>(?P<rank>\d+)</td>\s*'
    r'<td[^>]*>(?:<[^>]+>)*(?P<title>[^<]+)(?:</[^>]+>)*</td>\s*'
    r'<td[^>]*>\$?(?P<gross>[\d,]+)</td>',
    re.IGNORECASE | re.DOTALL,
)


def _box_office_rows_from_html(
    html: str, week_end: datetime
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    iso_week = week_end.strftime("%G-W%V")
    for m in _BOM_ROW_RE.finditer(html):
        try:
            rank = int(m.group("rank"))
        except ValueError:
            continue
        title = m.group("title").strip()[:200]
        if not title:
            continue
        try:
            gross_raw = float(m.group("gross").replace(",", ""))
        except ValueError:
            continue
        gross_m = gross_raw / 1_000_000.0 if gross_raw > 1_000_000 else gross_raw
        sig_id = f"hist/{iso_week}/{rank}/{title}"[:300]
        out.append({
            "source": "box_office_mojo_history",
            "signal_id": sig_id,
            "signal_type": "BOX",
            "title": title,
            "event_at": week_end,
            "value": gross_m,
            "settled": True,
            "outcome": "released",
            "payload": {
                "rank": rank, "iso_week": iso_week,
                "gross_usd": gross_raw,
            },
        })
    return out


async def _fetch_box_office_window(
    start: datetime, end: datetime,
) -> list[dict[str, Any]]:
    """Fetch one weekend's worth of historical box-office rows."""
    week_end = end if end.tzinfo else end.replace(tzinfo=timezone.utc)
    iso_year, iso_week, _ = week_end.isocalendar()
    url = _BOX_OFFICE_HIST_URL_TEMPLATE.format(year=iso_year, week=iso_week)
    body = await _feeds._http_get_text(url, timeout=10.0)
    if not body:
        return []
    return _box_office_rows_from_html(body, week_end)


async def _fetch_spotify_window(
    start: datetime, end: datetime,
) -> list[dict[str, Any]]:
    """Fetch one week of Spotify weekly chart history."""
    week_end = end if end.tzinfo else end.replace(tzinfo=timezone.utc)
    url = _SPOTIFY_HIST_URL_TEMPLATE.format(date=week_end)
    body = await _feeds._http_get_text(url, timeout=15.0)
    if not body:
        return []
    rows = body.splitlines()
    if len(rows) <= 1:
        return []
    header = [h.strip().lower() for h in rows[0].split(",")]
    try:
        i_rank = header.index("rank")
        i_track = (
            header.index("track name") if "track name" in header
            else header.index("track")
        )
        i_artist = (
            header.index("artist") if "artist" in header
            else header.index("artists")
        )
    except ValueError:
        return []
    out: list[dict[str, Any]] = []
    iso_week = week_end.strftime("%G-W%V")
    for raw in rows[1:201]:
        parts = [p.strip().strip('"') for p in raw.split(",")]
        if len(parts) <= max(i_rank, i_track, i_artist):
            continue
        try:
            rank = int(parts[i_rank])
        except ValueError:
            continue
        track = parts[i_track][:200]
        artist = parts[i_artist][:200]
        if not track or not artist:
            continue
        out.append({
            "source": "spotify_charts_history",
            "signal_id": f"hist/{iso_week}/{rank}/{artist}/{track}"[:300],
            "signal_type": "SPOTIFY",
            "title": f"{artist} - {track}",
            "event_at": week_end,
            "value": float(rank),
            "settled": True,
            "outcome": "charted",
            "payload": {
                "artist": artist, "track": track,
                "rank": rank, "iso_week": iso_week,
            },
        })
    return out


_RT_HIST_ROW_RE = re.compile(
    r'data-title="(?P<title>[^"]+)"[^>]*data-tomato="(?P<score>\d{1,3})"',
    re.IGNORECASE,
)


async def _fetch_rotten_tomatoes_window(
    start: datetime, end: datetime,
) -> list[dict[str, Any]]:
    """Best-effort RT yearly browse-archive scrape.

    The browse pages only carry tomatometer scores for movies released
    that calendar year, so we fetch one URL per year intersected by
    the lookback window."""
    end_utc = end if end.tzinfo else end.replace(tzinfo=timezone.utc)
    year = end_utc.year
    url = _RT_HIST_URL_TEMPLATE.format(year=year)
    body = await _feeds._http_get_text(url, timeout=15.0)
    if not body:
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in _RT_HIST_ROW_RE.finditer(body):
        title = m.group("title").strip()[:200]
        if not title or title in seen:
            continue
        try:
            score = float(m.group("score"))
        except ValueError:
            continue
        if not (0.0 <= score <= 100.0):
            continue
        seen.add(title)
        out.append({
            "source": "rotten_tomatoes_history",
            "signal_id": f"hist/{year}/{title}"[:300],
            "signal_type": "RT",
            "title": title,
            "event_at": end_utc,
            "value": score,
            "settled": True,
            "outcome": "scored",
            "payload": {"year": year, "score": score},
        })
    return out


async def _fetch_awards_window(
    start: datetime, end: datetime,
) -> list[dict[str, Any]]:
    """Pull the curated historical-winners JSON.

    The source is a single static JSON document covering every prior
    cycle for OSCAR / EMMY / GRAMMY, so we only call it once per
    backfill (cadence == lookback length) and filter by year."""
    body = await _feeds._http_get_text(
        _feeds._AWARDS_HISTORICAL_OUTCOMES_URL, timeout=15.0,
    )
    if not body:
        return []
    try:
        doc = json.loads(body)
    except Exception as e:  # pragma: no cover — defensive
        log.debug("awards historical JSON parse failed: %s", e)
        return []
    out: list[dict[str, Any]] = []
    start_year = start.year
    end_year = end.year
    for award_type, entries in (doc or {}).items():
        if award_type not in {"OSCAR", "EMMY", "GRAMMY"}:
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            try:
                year = int(entry.get("year"))
                race = str(entry.get("race") or "").upper()
                winner = str(entry.get("winner") or "").upper()
            except Exception:
                continue
            if not race or not winner:
                continue
            if year < start_year or year > end_year:
                continue
            event_at = datetime(year, 1, 1, tzinfo=timezone.utc)
            out.append({
                "source": "awards_historical_outcomes",
                "signal_id": f"{award_type}/{year}/{race}/{winner}"[:300],
                "signal_type": award_type,
                "title": winner,
                "event_at": event_at,
                "value": 1.0,
                "settled": True,
                "outcome": "win",
                "payload": {"race_category": race, "year": year},
            })
    return out


def _builtin_fetchers() -> list[HistoricalFetcher]:
    return [
        HistoricalFetcher(
            source="box_office_mojo_history",
            cadence_days=7,
            fetch=_fetch_box_office_window,
        ),
        HistoricalFetcher(
            source="spotify_charts_history",
            cadence_days=7,
            fetch=_fetch_spotify_window,
        ),
        HistoricalFetcher(
            source="rotten_tomatoes_history",
            cadence_days=365,
            fetch=_fetch_rotten_tomatoes_window,
        ),
        HistoricalFetcher(
            source="awards_historical_outcomes",
            cadence_days=DEFAULT_BACKFILL_DAYS,
            fetch=_fetch_awards_window,
        ),
    ]


# ---------------------------------------------------------------------------
# Run loop
# ---------------------------------------------------------------------------


def _iter_windows(
    start: datetime, end: datetime, cadence_days: int,
) -> list[tuple[datetime, datetime]]:
    """Walk [start, end] in fixed-width windows ending at `end`.

    Windows are emitted newest-first so a partial run still leaves
    fresh history at the top of the table."""
    out: list[tuple[datetime, datetime]] = []
    if cadence_days <= 0:
        return [(start, end)]
    cur_end = end
    step = timedelta(days=cadence_days)
    while cur_end > start:
        cur_start = max(start, cur_end - step)
        out.append((cur_start, cur_end))
        cur_end = cur_start
        if cur_start <= start:
            break
    return out


async def run_backfill(
    *,
    days: int = DEFAULT_BACKFILL_DAYS,
    fetchers: list[HistoricalFetcher] | None = None,
) -> dict[str, Any]:
    """Walk each registered historical fetcher back ``days`` days.

    Returns the final status snapshot.  Raises :class:`RuntimeError`
    when another backfill is already in flight (the API endpoint
    translates this into 409)."""
    days = max(MIN_BACKFILL_DAYS, min(MAX_BACKFILL_DAYS, int(days)))
    if _lock.locked():
        raise RuntimeError("entertainment backfill already running")

    used_fetchers = fetchers if fetchers is not None else _builtin_fetchers()

    async with _lock:
        global _status
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=days)
        _status = BackfillStatus(
            state="running",
            started_at=now.isoformat(),
            days=days,
        )
        t0 = now
        try:
            for fetcher in used_fetchers:
                src_count = 0
                try:
                    for win_start, win_end in _iter_windows(
                        start, now, fetcher.cadence_days,
                    ):
                        try:
                            rows = await fetcher.fetch(win_start, win_end)
                        except Exception as e:
                            log.warning(
                                "backfill fetch failed (%s window=%s..%s): %s",
                                fetcher.source,
                                win_start.date(), win_end.date(), e,
                            )
                            continue
                        if not rows:
                            continue
                        try:
                            written = await upsert_signals(rows)
                        except Exception as e:
                            log.warning(
                                "backfill upsert failed (%s): %s",
                                fetcher.source, e,
                            )
                            continue
                        src_count += int(written)
                except Exception as e:
                    log.warning(
                        "backfill source crashed (%s): %s",
                        fetcher.source, e,
                    )
                    _status.sources_failed.append(fetcher.source)
                    _status.last_error = f"{fetcher.source}: {e}"
                _status.rows_by_source[fetcher.source] = src_count
                _status.rows_total += src_count
        except Exception as e:  # pragma: no cover — defensive
            _status.state = "failed"
            _status.last_error = str(e)
            _status.completed_at = datetime.now(timezone.utc).isoformat()
            _status.duration_seconds = (
                datetime.now(timezone.utc) - t0
            ).total_seconds()
            await _persist_status(_status.snapshot())
            raise
        _status.state = "completed"
        finished = datetime.now(timezone.utc)
        _status.completed_at = finished.isoformat()
        _status.duration_seconds = (finished - t0).total_seconds()
        snap = _status.snapshot()
        await _persist_status(snap)
        return snap


def reset_status_for_tests() -> None:
    """Drop the in-process snapshot — wired into pytest fixtures."""
    global _status
    _status = BackfillStatus()


__all__ = [
    "BackfillStatus",
    "DEFAULT_BACKFILL_DAYS",
    "HistoricalFetcher",
    "get_status",
    "is_running",
    "load_persisted_status",
    "reset_status_for_tests",
    "run_backfill",
]
