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
from datetime import datetime, timezone
from typing import Any

from .. import db
from . import control, executor, scraper


log = logging.getLogger(__name__)


POLL_INTERVAL_SECONDS = 60
STALE_THRESHOLD_SECONDS = 120

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
    stale = (
        age_seconds is None or age_seconds > STALE_THRESHOLD_SECONDS
        if mode == control.INPUT_MODE_EDGE_TERMINAL
        else False
    )
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
        "poll_interval_seconds": POLL_INTERVAL_SECONDS,
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
    log.info("edge_terminal poller started (interval=%ss)", POLL_INTERVAL_SECONDS)


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
    while not _stop.is_set():
        try:
            current_mode = await control.get_input_mode()
            if current_mode != control.INPUT_MODE_EDGE_TERMINAL:
                log.info(
                    "edge_terminal poller exiting (input_mode=%s)", current_mode
                )
                return
            await _one_tick()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("edge_terminal poll tick failed: %s", e)
            await _record_status(error=str(e), signal_count=0)
        try:
            await asyncio.wait_for(_stop.wait(), timeout=POLL_INTERVAL_SECONDS)
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

    inserted = 0
    for sig in signals:
        try:
            row_id = await _insert_signal(sig)
            if row_id is not None:
                inserted += 1
        except Exception as e:
            log.warning("edge_terminal signal insert failed: %s", e)

    # Drain after every poll regardless of insert count so any signals
    # that were inserted-but-not-yet-placed on a prior crash still fire.
    try:
        await executor.drain_pending()
    except Exception as e:
        log.exception("edge_terminal executor drain failed: %s", e)
        if not error:
            error = f"drain_failed: {e}"

    await _record_status(error=error, signal_count=inserted)


async def _insert_signal(sig: dict[str, Any]) -> int | None:
    """Insert a signal row.  Returns the new id, or None on conflict
    (the unique index dropped a duplicate)."""
    raw = sig.get("raw") if isinstance(sig.get("raw"), dict) else {}
    new_id = await db.fetchval(
        """
        INSERT INTO edge_terminal_signals (
            vertical, matchup, selection, signal_date,
            edge_pct, model_prob, market_price,
            kalshi_ticker, market_title, raw, status
        ) VALUES (
            $1, $2, $3, $4::date,
            $5, $6, $7,
            $8, $9, $10::jsonb, 'pending'
        )
        ON CONFLICT (vertical, matchup, selection, signal_date) DO NOTHING
        RETURNING id
        """,
        sig.get("vertical"),
        sig.get("matchup"),
        sig.get("selection"),
        sig.get("signal_date"),
        sig.get("edge_pct"),
        sig.get("model_prob"),
        sig.get("market_price"),
        sig.get("kalshi_ticker"),
        sig.get("market_title"),
        json.dumps(raw),
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
