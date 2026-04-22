"""Continuous Kalshi balance -> `bankroll_events` sync.

A single asyncio background task that, every `BANKROLL_SYNC_INTERVAL_SECONDS`
(default 60s), pulls the current Kalshi `/portfolio/balance` and, when the
balance differs from the most recent `bankroll_events.balance_cents`, writes
a single delta row with `reason='exchange_sync'`. The bankroll-floor risk
gate and the equity curve then read from this table so both always reflect
the live exchange balance.

Failure is logged and swallowed so a flaky Kalshi REST endpoint never takes
down the trading process; the next tick simply retries.

Per task #104.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from . import db
from .kalshi import client as kalshi_client_mod


log = logging.getLogger("axiom.bankroll_sync")


def _interval_seconds() -> float:
    raw = os.environ.get("BANKROLL_SYNC_INTERVAL_SECONDS")
    try:
        v = float(raw) if raw is not None else 60.0
    except (TypeError, ValueError):
        v = 60.0
    return max(5.0, v)


_task: asyncio.Task | None = None
_stop = asyncio.Event()
_started_at: float | None = None
_last_tick_at: float | None = None
_last_error: str | None = None
_last_balance_cents: int | None = None


def state() -> dict[str, Any]:
    return {
        "running": _task is not None and not _task.done(),
        "started_at": _started_at,
        "last_tick_at": _last_tick_at,
        "last_error": _last_error,
        "last_balance_cents": _last_balance_cents,
        "interval_seconds": _interval_seconds(),
    }


async def _latest_event_balance_cents() -> int | None:
    try:
        v = await db.fetchval(
            "SELECT balance_cents FROM bankroll_events "
            "WHERE exchange = 'kalshi' "
            "ORDER BY id DESC LIMIT 1"
        )
        return int(v) if v is not None else None
    except Exception as e:
        log.debug("latest bankroll event read failed: %s", e)
        return None


async def record_balance(
    balance_cents: int,
    *,
    reason: str = "exchange_sync",
    details: dict[str, Any] | None = None,
    exchange: str = "kalshi",
) -> dict[str, Any] | None:
    """Insert a bankroll_events row if `balance_cents` differs from the
    last recorded balance for this exchange. Returns the inserted row dict
    or None if no write was needed (or the write failed).
    """
    prev = await _latest_event_balance_cents()
    delta = balance_cents - (prev or 0)
    # On the very first observation we must seed the row even though we
    # have no "delta" to compare against — equity curve and floor gate
    # need at least one anchor point to read from.
    if prev is not None and delta == 0:
        return None
    import json
    try:
        row = await db.fetchrow(
            """
            INSERT INTO bankroll_events
              (exchange, reason, delta_cents, balance_cents, details)
            VALUES ($1, $2, $3, $4, $5::jsonb)
            RETURNING id, occurred_at, exchange, reason,
                      delta_cents, balance_cents
            """,
            exchange,
            reason,
            int(delta),
            int(balance_cents),
            json.dumps(details or {}),
        )
        return dict(row) if row else None
    except Exception as e:
        log.warning("bankroll_events insert failed (%s, %s¢): %s",
                    reason, balance_cents, e)
        return None


async def _tick_once() -> None:
    global _last_tick_at, _last_error, _last_balance_cents
    try:
        async with kalshi_client_mod.KalshiClient() as c:
            bal = await c.get_balance()
        bal_cents = int(bal.balance_cents)
        _last_balance_cents = bal_cents
        await record_balance(bal_cents, reason="exchange_sync")
        _last_error = None
    except Exception as e:
        _last_error = f"{type(e).__name__}: {e}"
        log.debug("bankroll sync tick failed: %s", _last_error)
    finally:
        _last_tick_at = time.time()


async def _loop() -> None:
    while not _stop.is_set():
        await _tick_once()
        try:
            await asyncio.wait_for(_stop.wait(), timeout=_interval_seconds())
        except asyncio.TimeoutError:
            continue
        else:
            return


async def start() -> None:
    global _task, _started_at
    if _task and not _task.done():
        return
    if (os.environ.get("BANKROLL_SYNC_ENABLED", "1") or "").strip() == "0":
        log.info("bankroll sync disabled by env")
        return
    _stop.clear()
    _started_at = time.time()
    _task = asyncio.create_task(_loop(), name="bankroll-sync")
    log.info("bankroll sync started (interval=%ss)", _interval_seconds())


async def stop() -> None:
    global _task
    _stop.set()
    if _task:
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):
            pass
    _task = None
