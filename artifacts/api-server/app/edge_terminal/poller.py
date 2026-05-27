"""60-second Edge Terminal poll loop.

Single asyncio task — only ever starts when ``app.input_mode`` is
``edge_terminal``.  Each tick:

  1. Re-reads the input_mode (operator may have flipped it back).
  2. Calls :func:`scraper.scrape_signals`.
  3. INSERTs each pick into ``edge_terminal_signals``.  The
     ``(vertical, matchup, selection, signal_date)`` unique index
     drops duplicates silently.
  4. Calls :func:`executor.drain_pending` to fire any new signals.
  5. Writes a status row so the dashboard can show stale-poller
     indicator (>120 s without a successful poll = grey).

Failures never kill the loop — they're caught, recorded on the
status row, and the loop sleeps for the configured interval before
trying again.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .. import db
from . import control, executor, scraper


log = logging.getLogger(__name__)


# Poll cadence is jittered each tick so we don't pound the upstream
# scraper at a predictable 60s drumbeat — the random spread between
# MIN and MAX makes the request pattern look human and stretches the
# stale-threshold cushion (we wait up to MAX before declaring stale).
POLL_INTERVAL_MIN_SECONDS = 40
POLL_INTERVAL_MAX_SECONDS = 60
# Stale threshold is 2× the max so a single missed cycle doesn't
# light up the dashboard.
STALE_THRESHOLD_SECONDS = POLL_INTERVAL_MAX_SECONDS * 2

# Active-hours window: only scrape between 04:00 and 22:00 Mountain
# Time.  Edge Terminal doesn't publish overnight, and we'd rather idle
# than burn login attempts on an empty board.  Honours DST via
# America/Denver (zoneinfo).  Overridable for tests / non-US operators
# via AXIOM_EDGE_TERMINAL_TZ / _ACTIVE_HOUR_START / _END.
ACTIVE_TZ = ZoneInfo(os.environ.get("AXIOM_EDGE_TERMINAL_TZ", "America/Denver"))
ACTIVE_HOUR_START = int(os.environ.get("AXIOM_EDGE_TERMINAL_ACTIVE_HOUR_START", "4"))
ACTIVE_HOUR_END = int(os.environ.get("AXIOM_EDGE_TERMINAL_ACTIVE_HOUR_END", "22"))


def _next_interval_seconds() -> float:
    """Pick the next sleep interval — random jitter inside the
    configured min/max window so consecutive polls are not predictable."""
    if POLL_INTERVAL_MAX_SECONDS <= POLL_INTERVAL_MIN_SECONDS:
        return float(POLL_INTERVAL_MAX_SECONDS)
    return random.uniform(POLL_INTERVAL_MIN_SECONDS, POLL_INTERVAL_MAX_SECONDS)


def _in_active_window(now: datetime | None = None) -> bool:
    """True iff the current local hour in ACTIVE_TZ is inside
    [ACTIVE_HOUR_START, ACTIVE_HOUR_END).  4-22 = 4am-10pm."""
    local = (now or _now()).astimezone(ACTIVE_TZ)
    return ACTIVE_HOUR_START <= local.hour < ACTIVE_HOUR_END

_task: asyncio.Task[Any] | None = None
_stop = asyncio.Event()


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def get_status() -> dict[str, Any]:
    """Snapshot for the dashboard tile."""
    mode = await control.get_input_mode()
    row: dict[str, Any] | None = None
    try:
        r = await db.fetchrow(
            "SELECT last_poll_at, last_ok_at, last_error, "
            "last_signal_count, updated_at "
            "FROM edge_terminal_poller_status WHERE id = 1"
        )
        row = dict(r) if r else None
    except Exception as e:  # pragma: no cover — defensive
        log.warning("edge_terminal status read failed: %s", e)
    last_ok = row.get("last_ok_at") if row else None
    age_seconds: float | None = None
    if isinstance(last_ok, datetime):
        age_seconds = (_now() - last_ok).total_seconds()
    # Always surface staleness when in edge_terminal mode -- the poller
    # is expected to run continuously, so any gap > threshold is a real
    # incident regardless of local clock.
    stale = (
        age_seconds is None or age_seconds > STALE_THRESHOLD_SECONDS
        if mode == control.INPUT_MODE_EDGE_TERMINAL
        else False
    )
    active = _in_active_window()
    return {
        "input_mode": mode,
        "running": _task is not None and not _task.done(),
        "last_poll_at": _iso(row.get("last_poll_at") if row else None),
        "last_ok_at": _iso(last_ok),
        "last_error": (row or {}).get("last_error"),
        "last_signal_count": int((row or {}).get("last_signal_count") or 0),
        "age_seconds": age_seconds,
        "stale": stale,
        "stale_threshold_seconds": STALE_THRESHOLD_SECONDS,
        "poll_interval_min_seconds": POLL_INTERVAL_MIN_SECONDS,
        "poll_interval_max_seconds": POLL_INTERVAL_MAX_SECONDS,
        "active_window": {
            "tz": str(ACTIVE_TZ),
            "start_hour": ACTIVE_HOUR_START,
            "end_hour": ACTIVE_HOUR_END,
            "currently_active": active,
        },
    }


def _iso(v: Any) -> str | None:
    if isinstance(v, datetime):
        return v.isoformat()
    return None


async def start() -> None:
    """Idempotent.  Spawns the poller task if input_mode is
    edge_terminal AND we don't already have a running task."""
    global _task
    mode = await control.ensure_seeded()
    if mode != control.INPUT_MODE_EDGE_TERMINAL:
        log.info("edge_terminal poller skipped (input_mode=%s)", mode)
        return
    if _task is not None and not _task.done():
        return
    _stop.clear()
    _task = asyncio.create_task(_loop(), name="edge-terminal-poller")
    log.info(
        "edge_terminal poller started (interval=%s-%ss, active=%02d-%02d %s)",
        POLL_INTERVAL_MIN_SECONDS,
        POLL_INTERVAL_MAX_SECONDS,
        ACTIVE_HOUR_START,
        ACTIVE_HOUR_END,
        ACTIVE_TZ,
    )


async def stop() -> None:
    global _task
    _stop.set()
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):
            pass
        _task = None
    log.info("edge_terminal poller stopped")


async def _loop() -> None:
    # Keep the task alive across runtime mode flips — only an explicit
    # ``stop()`` (or process shutdown) ends the loop.  When the mode is
    # ``pipeline`` we just skip the scrape/drain and re-check on the
    # next tick so a DB-side flip back to ``edge_terminal`` resumes
    # polling without needing a process restart.
    while not _stop.is_set():
        try:
            current_mode = await control.get_input_mode()
            if current_mode != control.INPUT_MODE_EDGE_TERMINAL:
                log.debug(
                    "edge_terminal poller idle (input_mode=%s)", current_mode
                )
            else:
                # Poll continuously at the configured cadence -- the
                # earlier 04:00-22:00 MT gate silently suppressed signals
                # outside that window, which is wrong for a 24/7 sports
                # board (UFC, live overnight games, intl. tennis).
                await _one_tick()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("edge_terminal poll tick failed: %s", e)
            await _record_status(error=str(e), signal_count=0)
        try:
            await asyncio.wait_for(_stop.wait(), timeout=_next_interval_seconds())
        except asyncio.TimeoutError:
            pass
        else:
            return


async def _one_tick() -> None:
    signals: list[dict[str, Any]] = []
    error: str | None = None
    try:
        signals = await scraper.scrape_signals()
    except scraper.ScraperNotConfigured as e:
        error = f"not_configured: {e}"
    except scraper.ScraperUnavailable as e:
        error = f"unavailable: {e}"
    except Exception as e:  # pragma: no cover — defensive
        error = f"scrape_failed: {e}"
    # Always emit an INFO line so prod logs show the poller is alive
    # and what each tick actually saw -- previously a silent
    # ``ScraperUnavailable`` (no chromium installed in the deploy image)
    # left zero footprint between the "poller started" line and the
    # next restart, making the system look dead when it was just
    # mis-configured.
    if error:
        log.warning("edge_terminal tick error: %s", error)
    else:
        log.info("edge_terminal tick: scraped %d signal(s)", len(signals))

    inserted = 0
    for sig in signals:
        try:
            row_id = await _insert_signal(sig)
            if row_id is not None:
                inserted += 1
        except Exception as e:
            log.warning(
                "edge_terminal signal insert failed: %s: %r | sig keys=%s vertical=%r matchup=%r selection=%r signal_date=%r",
                type(e).__name__,
                e,
                sorted(sig.keys()) if isinstance(sig, dict) else None,
                sig.get("vertical") if isinstance(sig, dict) else None,
                sig.get("matchup") if isinstance(sig, dict) else None,
                sig.get("selection") if isinstance(sig, dict) else None,
                sig.get("signal_date") if isinstance(sig, dict) else None,
                exc_info=True,
            )

    # Drain after every poll regardless of insert count so any signals
    # that were inserted-but-not-yet-placed on a prior crash still fire.
    try:
        drain_counts = await executor.drain_pending()
        log.info(
            "edge_terminal drain: scraped=%d inserted=%d placed=%d skipped=%d failed=%d",
            len(signals),
            inserted,
            int((drain_counts or {}).get("placed") or 0),
            int((drain_counts or {}).get("skipped") or 0),
            int((drain_counts or {}).get("failed") or 0),
        )
    except Exception as e:
        log.exception("edge_terminal executor drain failed: %s", e)
        if not error:
            error = f"drain_failed: {e}"

    await _record_status(error=error, signal_count=inserted)


async def _insert_signal(sig: dict[str, Any]) -> int | None:
    """Insert a signal row.  Returns the new id, or None on conflict
    (the unique index dropped a duplicate)."""
    raw = sig.get("raw") if isinstance(sig.get("raw"), dict) else {}
    # Normalize side: scraper may give us YES/NO/yes/no/None.  Anything
    # not explicitly NO (case-insensitive) is treated as YES so we
    # preserve historic default behavior for sources that don't expose
    # an explicit side hint.
    side_raw = sig.get("side")
    side = "NO" if isinstance(side_raw, str) and side_raw.strip().upper() == "NO" else "YES"
    # asyncpg binds `DATE` parameters by calling `.toordinal()` on the
    # Python value *before* the server-side `$4::date` cast runs, so a
    # bare ISO string ("2026-05-26") raises
    # "'str' object has no attribute 'toordinal'" and every signal is
    # silently dropped.  Coerce here so the scraper can hand us either
    # a `date` (live path) or an ISO string (fixture tests) without
    # caring which.
    signal_date_raw = sig.get("signal_date")
    if isinstance(signal_date_raw, str):
        try:
            signal_date_val = date.fromisoformat(signal_date_raw)
        except ValueError:
            signal_date_val = None
    else:
        signal_date_val = signal_date_raw
    new_id = await db.fetchval(
        """
        INSERT INTO edge_terminal_signals (
            vertical, matchup, selection, signal_date,
            edge_pct, model_prob, market_price,
            kalshi_ticker, market_title, raw, status, side,
            pick_type, avg_odds, ev_pct,
            source_screenshot_url, kalshi_market_id
        ) VALUES (
            $1, $2, $3, $4::date,
            $5, $6, $7,
            $8, $9, $10::jsonb, 'pending', $11,
            $12, $13, $14,
            $15, $16
        )
        ON CONFLICT (vertical, matchup, selection, signal_date) DO NOTHING
        RETURNING id
        """,
        sig.get("vertical"),
        sig.get("matchup"),
        sig.get("selection"),
        signal_date_val,
        sig.get("edge_pct"),
        sig.get("model_prob"),
        sig.get("market_price"),
        sig.get("kalshi_ticker"),
        sig.get("market_title"),
        json.dumps(raw),
        side,
        sig.get("pick_type"),
        sig.get("avg_odds"),
        sig.get("ev_pct"),
        sig.get("source_screenshot_url"),
        sig.get("kalshi_market_id"),
    )
    return int(new_id) if new_id is not None else None


async def _record_status(*, error: str | None, signal_count: int) -> None:
    try:
        await db.execute(
            """
            UPDATE edge_terminal_poller_status
            SET last_poll_at = now(),
                last_ok_at = CASE WHEN $1::text IS NULL THEN now() ELSE last_ok_at END,
                last_error = $1,
                last_signal_count = $2,
                updated_at = now()
            WHERE id = 1
            """,
            error,
            signal_count,
        )
    except Exception as e:  # pragma: no cover — defensive
        log.warning("edge_terminal status update failed: %s", e)
