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


async def _latest_event_balance_cents(
    exchange: str = "kalshi", trading_mode: str = "live"
) -> int | None:
    """Return the last recorded ``balance_cents`` for ``(exchange,
    trading_mode)``.  Round-11 Bug #6 — pre-#752 the lookup ignored
    ``trading_mode``, so the paper seed event would silently anchor the
    Kalshi live sync's first delta computation against the paper
    bankroll, manifesting as a huge spurious delta on the very next
    tick.
    """
    try:
        v = await db.fetchval(
            "SELECT balance_cents FROM bankroll_events "
            "WHERE exchange = $1 AND trading_mode = $2 "
            "ORDER BY id DESC LIMIT 1",
            exchange,
            trading_mode,
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
    trading_mode: str = "live",
) -> dict[str, Any] | None:
    """Insert a bankroll_events row if ``balance_cents`` differs from the
    last recorded balance for ``(exchange, trading_mode)``.  Returns the
    inserted row dict or None if no write was needed (or the write
    failed).

    The ``trading_mode`` argument keeps the paper and live ledgers
    isolated end-to-end (Round-11 Bug #6 / Task #752 step 3).  The
    Kalshi sync loop always writes ``"live"`` (its only source of truth
    is the live exchange API); the paper-bankroll seeder writes
    ``"paper"`` once on first boot from
    :func:`ensure_paper_bankroll_seeded`.
    """
    prev = await _latest_event_balance_cents(exchange, trading_mode)
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
              (exchange, reason, delta_cents, balance_cents, details, trading_mode)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6)
            RETURNING id, occurred_at, exchange, reason,
                      delta_cents, balance_cents, trading_mode
            """,
            exchange,
            reason,
            int(delta),
            int(balance_cents),
            json.dumps(details or {}),
            trading_mode,
        )
        return dict(row) if row else None
    except Exception as e:
        log.warning("bankroll_events insert failed (%s, %s, %s¢): %s",
                    reason, trading_mode, balance_cents, e)
        return None


# ---------------------------------------------------------------------------
# Paper-mode bankroll seed (Task #752 step 3 — Round-11 Bug #6).
# ---------------------------------------------------------------------------


def _paper_bankroll_seed_cents() -> int:
    """Resolve the paper-mode starting bankroll from
    ``AXIOM_PAPER_BANKROLL_USD`` (default $1,000).

    Used once on first boot to anchor the paper ledger so the daily-loss
    pct breaker and the dashboard equity curve have a denominator on
    day-1 — without this seed, the paper breaker would have no
    start-of-day bankroll, and the dashboard equity tile would show an
    empty state until the first paper trade settled.
    """
    raw = os.environ.get("AXIOM_PAPER_BANKROLL_USD")
    default_usd = 1000.0
    if raw is None or not raw.strip():
        usd = default_usd
    else:
        try:
            usd = float(raw)
        except ValueError:
            log.warning(
                "AXIOM_PAPER_BANKROLL_USD is not a number: %r — defaulting to $%.2f",
                raw, default_usd,
            )
            usd = default_usd
    if usd <= 0:
        usd = default_usd
    return int(round(usd * 100.0))


async def ensure_paper_bankroll_seeded(
    *, exchange: str = "kalshi"
) -> dict[str, Any] | None:
    """Idempotently seed the paper bankroll ledger.

    Inserts a single ``reason='paper_seed'`` row at
    ``AXIOM_PAPER_BANKROLL_USD`` cents iff the paper ledger for the
    given exchange is empty.  Subsequent boots are no-ops because
    :func:`_latest_event_balance_cents` already returns a value.

    Called from the API startup hook (alongside :func:`start`) and from
    the orchestrator pre-flight (so a brand-new deploy in paper mode
    can run a cycle immediately without operator action).
    """
    existing = await _latest_event_balance_cents(
        exchange=exchange, trading_mode="paper"
    )
    if existing is not None:
        return None
    seed_cents = _paper_bankroll_seed_cents()
    log.info(
        "seeding paper bankroll for %s: $%.2f", exchange, seed_cents / 100.0
    )
    return await record_balance(
        seed_cents,
        reason="paper_seed",
        details={"source": "AXIOM_PAPER_BANKROLL_USD"},
        exchange=exchange,
        trading_mode="paper",
    )


async def _tick_once() -> None:
    global _last_tick_at, _last_error, _last_balance_cents
    try:
        async with kalshi_client_mod.KalshiClient() as c:
            bal = await c.get_balance()
        bal_cents = int(bal.balance_cents)
        _last_balance_cents = bal_cents
        # The Kalshi sync loop is the live exchange's source of truth
        # — explicit ``trading_mode="live"`` so the paper ledger can
        # never be polluted by an exchange-sync row.
        await record_balance(
            bal_cents, reason="exchange_sync", trading_mode="live"
        )
        _last_error = None
    except Exception as e:
        _last_error = f"{type(e).__name__}: {e}"
        log.debug("bankroll sync tick failed: %s", _last_error)
    finally:
        _last_tick_at = time.time()


async def _loop() -> None:
    global _last_error
    while not _stop.is_set():
        try:
            await _tick_once()
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # noqa: BLE001 — tick must never kill loop
            _last_error = f"tick crashed: {type(e).__name__}: {e}"
            log.exception("bankroll sync tick raised — continuing")
        try:
            await asyncio.wait_for(_stop.wait(), timeout=_interval_seconds())
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            # A stray cancellation of the inner _stop.wait() future used
            # to silently kill the loop, leaving the dashboard pinned at
            # "SYNC STOPPED" with no log trail.  Only exit when _stop is
            # *actually* set; otherwise keep polling.
            if _stop.is_set():
                return
            log.warning(
                "bankroll sync sleep cancelled but stop not set — continuing"
            )
            continue
        else:
            return


async def _supervised_loop() -> None:
    """Self-healing wrapper around :func:`_loop`.

    If ``_loop`` ever returns or raises while ``_stop`` is not set, log
    loudly and restart it after a short back-off so the dashboard never
    gets stuck at "SYNC STOPPED" because of a one-shot transient
    (DNS blip, Kalshi 5xx storm, stray ``CancelledError``, etc.).
    """
    backoff = 5.0
    while not _stop.is_set():
        try:
            await _loop()
            if _stop.is_set():
                return
            log.warning(
                "bankroll sync loop returned unexpectedly — restarting in %ss",
                backoff,
            )
        except asyncio.CancelledError:
            raise
        except BaseException:  # noqa: BLE001 — supervisor catches everything
            log.exception(
                "bankroll sync loop crashed — restarting in %ss", backoff
            )
        try:
            await asyncio.wait_for(_stop.wait(), timeout=backoff)
        except asyncio.TimeoutError:
            continue
        else:
            return


async def start() -> None:
    global _task, _started_at
    if _task and not _task.done():
        return
    # Always idempotently seed the paper ledger first — even when the
    # live sync loop is disabled, the paper breaker / paper dashboard
    # need a starting bankroll anchor.  A failed seed is logged and
    # swallowed inside :func:`ensure_paper_bankroll_seeded`.
    try:
        await ensure_paper_bankroll_seeded()
    except Exception as e:  # noqa: BLE001 — best-effort
        log.warning("paper bankroll seed failed at startup: %s", e)
    if (os.environ.get("BANKROLL_SYNC_ENABLED", "1") or "").strip() == "0":
        log.info("bankroll sync disabled by env")
        return
    _stop.clear()
    _started_at = time.time()
    _task = asyncio.create_task(_supervised_loop(), name="bankroll-sync")
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
