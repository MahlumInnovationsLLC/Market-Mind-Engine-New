"""Periodic refresh job for the Kalshi market-tick cache (task #359).

Companion to :mod:`app.kalshi.tick_cache`.  Every load of
``/trade/:ref`` reads from ``market_price_ticks`` first and only falls
back to a synchronous Kalshi candlesticks call when the cache is
missing buckets in the requested window.  This loop keeps the cache
warm: it scans recently-traded Kalshi markets, asks the cache helper
to top up the most-recent window, and sleeps.

By the time an operator clicks into a trade the buckets are usually
already in Postgres, so the chart renders without waiting on Kalshi.
If this loop is disabled the trade-detail handler still works — it
just falls back to its previous synchronous behaviour for any market
whose buckets aren't already cached.

Lifecycle, env-var contract and state shape mirror
:mod:`app.crypto_ohlcv_retention` so the lifespan wiring in
``app.main`` looks the same for every background loop.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from .. import db
from . import client as kalshi_client
from . import tick_cache


log = logging.getLogger("axiom.kalshi.tick_cache_refresh")


# Defaults are conservative: we only need to keep the markets a
# trader is likely to revisit warm, which is overwhelmingly the
# last day's worth of Kalshi trades.  All knobs are operator-tunable
# via env vars; see :func:`state` for the resolved view.
DEFAULT_INTERVAL_SECONDS = 15 * 60       # 15-minute sweep (raised from 5m to cut Supabase disk-IO)
DEFAULT_LOOKBACK_HOURS = 24              # markets traded in the last 24h
DEFAULT_WINDOW_HOURS = 4                 # cache the last 4h of bars per market
DEFAULT_PERIOD_INTERVAL = 1              # 1-minute bars (most useful resolution)
DEFAULT_MAX_MARKETS = 25                 # cap per sweep so a busy day is bounded


def _env_int(name: str, default: int, *, floor: int = 1) -> int:
    raw = os.environ.get(name)
    try:
        v = int(float(raw)) if raw is not None else default
    except (TypeError, ValueError):
        v = default
    return max(floor, v)


def _env_float(name: str, default: float, *, floor: float = 1.0) -> float:
    raw = os.environ.get(name)
    try:
        v = float(raw) if raw is not None else default
    except (TypeError, ValueError):
        v = default
    return max(floor, v)


def _interval_seconds() -> float:
    # 60-second floor so a typo can't hammer Kalshi.
    return _env_float(
        "AXIOM_TICK_CACHE_REFRESH_INTERVAL_SECONDS",
        float(DEFAULT_INTERVAL_SECONDS),
        floor=60.0,
    )


def _lookback_hours() -> int:
    return _env_int(
        "AXIOM_TICK_CACHE_LOOKBACK_HOURS", DEFAULT_LOOKBACK_HOURS, floor=1,
    )


def _window_hours() -> int:
    return _env_int(
        "AXIOM_TICK_CACHE_WINDOW_HOURS", DEFAULT_WINDOW_HOURS, floor=1,
    )


def _period_interval() -> int:
    raw = os.environ.get("AXIOM_TICK_CACHE_PERIOD_INTERVAL")
    try:
        v = int(float(raw)) if raw is not None else DEFAULT_PERIOD_INTERVAL
    except (TypeError, ValueError):
        v = DEFAULT_PERIOD_INTERVAL
    # Kalshi only accepts 1, 60 or 1440. Snap to the nearest valid bucket.
    if v not in (1, 60, 1440):
        v = DEFAULT_PERIOD_INTERVAL
    return v


def _max_markets() -> int:
    return _env_int(
        "AXIOM_TICK_CACHE_MAX_MARKETS", DEFAULT_MAX_MARKETS, floor=1,
    )


_task: asyncio.Task[Any] | None = None
_stop = asyncio.Event()
_started_at: float | None = None
_last_tick_at: float | None = None
_last_error: str | None = None
_last_markets: int = 0
_last_buckets_written: int = 0
_total_buckets_written: int = 0


def state() -> dict[str, Any]:
    """In-memory snapshot of the loop for /api/health surfaces."""
    return {
        "running": _task is not None and not _task.done(),
        "started_at": _started_at,
        "last_tick_at": _last_tick_at,
        "last_error": _last_error,
        "last_markets": _last_markets,
        "last_buckets_written": _last_buckets_written,
        "total_buckets_written": _total_buckets_written,
        "interval_seconds": _interval_seconds(),
        "lookback_hours": _lookback_hours(),
        "window_hours": _window_hours(),
        "period_interval": _period_interval(),
        "max_markets": _max_markets(),
    }


async def _active_market_tickers(lookback_hours: int, limit: int) -> list[str]:
    """Fetch distinct Kalshi tickers traded inside the lookback window.

    ``trades.market_id`` carries the Kalshi ticker for the
    ``exchange='kalshi'`` rows; we restrict to that exchange so we
    don't waste Kalshi calls on Polymarket condition IDs.  Ordered by
    most-recent activity first so a small ``limit`` still covers the
    markets a trader is most likely to revisit.
    """
    try:
        rows = await db.fetch(
            """
            SELECT market_id,
                   max(executed_at) AS last_traded
              FROM trades
             WHERE exchange = 'kalshi'
               AND market_id IS NOT NULL
               AND market_id <> ''
               AND executed_at >= now() - ($1::int * INTERVAL '1 hour')
             GROUP BY market_id
             ORDER BY last_traded DESC
             LIMIT $2
            """,
            int(lookback_hours), int(limit),
        )
    except Exception as e:  # noqa: BLE001 — best-effort
        log.warning("active markets lookup failed: %s", e)
        return []
    return [r["market_id"] for r in rows if r.get("market_id")]


async def refresh_once() -> int:
    """Run a single sweep. Returns the number of buckets newly cached.

    Opens one Kalshi client and reuses it across every market in the
    sweep so per-market TLS/auth setup is amortized.  Per-market
    failures are logged and counted in ``_last_error`` but do not
    abort the sweep — one sick market ticker can't stop us from
    refreshing the rest.
    """
    global _last_tick_at, _last_error, _last_markets
    global _last_buckets_written, _total_buckets_written

    lookback = _lookback_hours()
    window_seconds = _window_hours() * 3600
    period_interval = _period_interval()
    limit = _max_markets()

    tickers = await _active_market_tickers(lookback, limit)
    _last_markets = len(tickers)
    if not tickers:
        _last_buckets_written = 0
        _last_error = None
        _last_tick_at = time.time()
        return 0

    try:
        client = kalshi_client.KalshiClient()
    except Exception as e:  # noqa: BLE001
        msg = f"kalshi client unavailable: {e}"
        log.info("tick cache refresh: %s", msg)
        _last_buckets_written = 0
        _last_error = msg
        _last_tick_at = time.time()
        return 0

    end_ts = int(time.time())
    start_ts = end_ts - window_seconds

    written_total = 0
    last_err: str | None = None
    try:
        for ticker in tickers:
            try:
                written = await tick_cache.refresh_market_window(
                    ticker,
                    period_interval=period_interval,
                    start_ts=start_ts,
                    end_ts=end_ts,
                    client=client,
                )
                written_total += int(written or 0)
            except Exception as e:  # noqa: BLE001
                last_err = f"{type(e).__name__}: {e}"
                log.warning(
                    "tick cache refresh failed for %s: %s", ticker, last_err,
                )
    finally:
        try:
            await client.aclose()
        except Exception:
            pass

    _last_buckets_written = written_total
    _total_buckets_written += written_total
    _last_error = last_err
    _last_tick_at = time.time()
    if written_total:
        log.info(
            "tick cache refresh: %d bucket(s) topped up across %d market(s)",
            written_total, len(tickers),
        )
    return written_total


async def _loop() -> None:
    while not _stop.is_set():
        try:
            await refresh_once()
        except Exception as e:  # noqa: BLE001 — never let the loop die
            log.warning("tick cache refresh sweep raised: %s", e)
        try:
            await asyncio.wait_for(_stop.wait(), timeout=_interval_seconds())
        except asyncio.TimeoutError:
            continue
        else:
            return


async def start() -> None:
    """Spawn the refresh loop. Idempotent — safe to call twice."""
    global _task, _started_at, _stop
    if _task and not _task.done():
        return
    if (os.environ.get("AXIOM_TICK_CACHE_REFRESH_ENABLED", "1") or "").strip() == "0":
        log.info("tick cache refresh disabled by env")
        return
    # Re-create the Event in case start() is called from a fresh event
    # loop (test reuse pattern), otherwise the Event remains bound to
    # the previous loop and `wait_for` raises.
    _stop = asyncio.Event()
    _started_at = time.time()
    _task = asyncio.create_task(_loop(), name="kalshi-tick-cache-refresh")
    log.info(
        "tick cache refresh started "
        "(interval=%ss, lookback=%sh, window=%sh, bars=%sm, max_markets=%s)",
        _interval_seconds(), _lookback_hours(), _window_hours(),
        _period_interval(), _max_markets(),
    )


async def stop() -> None:
    """Signal the loop to exit and wait for it to finish."""
    global _task
    _stop.set()
    if _task:
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):
            pass
    _task = None


def _reset_state_for_tests() -> None:
    """Test helper — clears every counter and the task handle."""
    global _task, _started_at, _last_tick_at, _last_error
    global _last_markets, _last_buckets_written, _total_buckets_written
    _task = None
    _started_at = None
    _last_tick_at = None
    _last_error = None
    _last_markets = 0
    _last_buckets_written = 0
    _total_buckets_written = 0
