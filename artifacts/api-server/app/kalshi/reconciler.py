"""Continuous Kalshi position + fill reconciler.

A single asyncio background task that periodically pulls
`/portfolio/positions` and `/portfolio/fills`, diffs the result against
our local DB-of-record, and:

  * silently auto-corrects drifts within `AXIOM_RECONCILER_DRIFT_TOLERANCE`
    (default 0 contracts — the WS pipeline is the primary truth source,
    so any drift is an audit signal);
  * raises a `model_health_alerts` row + dispatches a `drift` email when
    the drift exceeds the tolerance (deduped per ticker for one hour);
  * back-fills missed fills into `order_fills` and re-runs the same
    weighted-average rollup the WS handler uses, so a dropped fill
    event can never leave a position permanently mis-stated;
  * exposes a snapshot via :func:`state` for the `/api/health/snapshot`
    `kalshi_reconciler` card.

Failure is logged and swallowed so a flaky Kalshi REST endpoint never
takes the trading process down — the next tick simply retries.

Per cutover task #266.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

from .. import db
from ..config import get_settings
from ..exchange_kill_switch import _kill
from . import client as kalshi_client_mod


log = logging.getLogger("axiom.kalshi.reconciler")


# ---------------------------------------------------------------------------
# Tuning knobs
# ---------------------------------------------------------------------------


def _interval_seconds() -> float:
    raw = os.environ.get("AXIOM_RECONCILER_INTERVAL_SECS")
    try:
        v = float(raw) if raw is not None else 60.0
    except (TypeError, ValueError):
        v = 60.0
    return max(10.0, v)


def _drift_tolerance() -> int:
    raw = os.environ.get("AXIOM_RECONCILER_DRIFT_TOLERANCE")
    try:
        v = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        v = 0
    return max(0, v)


def _enabled() -> bool:
    if (os.environ.get("AXIOM_RECONCILER_ENABLED", "1") or "").strip() == "0":
        return False
    s = get_settings()
    if not (s.kalshi_api_key_id and s.kalshi_private_key):
        return False
    if (s.trading_mode or "").lower() == "paper":
        return False
    return True


# ---------------------------------------------------------------------------
# Module state (surfaced via /api/health/snapshot)
# ---------------------------------------------------------------------------


_task: asyncio.Task | None = None
_stop = asyncio.Event()
_started_at: float | None = None
_last_tick_at: float | None = None
_last_error: str | None = None
_last_skipped_reason: str | None = None
_last_drift_count: int = 0
_last_auto_corrected: int = 0
_last_missed_fills: int = 0
_last_unknown_fills: int = 0
_total_drift_alerts: int = 0
_total_replayed_fills: int = 0
_fills_cursor_ts: int | None = None  # epoch seconds; latest fill we processed


def state() -> dict[str, Any]:
    return {
        "running": _task is not None and not _task.done(),
        "started_at": _started_at,
        "last_tick_at": _last_tick_at,
        "last_error": _last_error,
        "last_skipped_reason": _last_skipped_reason,
        "last_drift_count": _last_drift_count,
        "last_auto_corrected": _last_auto_corrected,
        "last_missed_fills": _last_missed_fills,
        "last_unknown_fills": _last_unknown_fills,
        "total_drift_alerts": _total_drift_alerts,
        "total_replayed_fills": _total_replayed_fills,
        "fills_cursor_ts": _fills_cursor_ts,
        "interval_seconds": _interval_seconds(),
        "drift_tolerance": _drift_tolerance(),
    }


# ---------------------------------------------------------------------------
# Core diff
# ---------------------------------------------------------------------------


async def _local_signed_positions() -> dict[str, int]:
    """Sum open `positions` rows on Kalshi, signed by side.

    Returns `{market_ticker: signed_qty}` where signed_qty is positive
    for net YES exposure and negative for net NO exposure — matching
    the convention Kalshi's `/portfolio/positions` returns in the
    `position` field.
    """
    out: dict[str, int] = {}
    try:
        rows = await db.fetch(
            """
            SELECT market_id,
                   side,
                   coalesce(quantity, 0)::int AS qty
              FROM positions
             WHERE coalesce(exchange, 'kalshi') = 'kalshi'
               AND coalesce(status, 'open') = 'open'
               AND market_id IS NOT NULL
            """
        )
    except Exception as e:
        log.debug("local position read failed: %s", e)
        return out
    for r in rows:
        ticker = r["market_id"]
        side = (r["side"] or "yes").lower()
        qty = int(r["qty"] or 0)
        signed = qty if side == "yes" else -qty
        out[ticker] = out.get(ticker, 0) + signed
    return out


async def _raise_position_drift_alert(
    *,
    ticker: str,
    local_qty: int,
    exchange_qty: int,
    drift: int,
    tolerance: int,
) -> bool:
    """Persist + email a drift alert (deduped per ticker for 1 hour).

    Returns True if a fresh alert was raised, False if suppressed by
    the dedupe window.
    """
    global _total_drift_alerts
    msg = (
        f"Kalshi position drift on {ticker}: "
        f"local={local_qty} exchange={exchange_qty} "
        f"drift={drift} (tolerance={tolerance})"
    )
    raised = False
    try:
        # Suppress same-hour duplicates of the SAME ticker AT THE SAME
        # drift magnitude. The dedupe key is `(ticker, drift_qty)` so a
        # ticker whose drift jumps from 5 -> 50 still produces a fresh
        # alert (the magnitude change is itself news), while a stable
        # 5-contract drift is suppressed for an hour.
        recent = await db.fetchval(
            """
            SELECT 1 FROM model_health_alerts
             WHERE alert_type = 'position_drift'
               AND metric_name = $1
               AND metric_value = $2
               AND created_at > now() - interval '1 hour'
             LIMIT 1
            """,
            ticker,
            float(drift),
        )
        if not recent:
            await db.execute(
                """
                INSERT INTO model_health_alerts
                    (vertical, alert_type, severity, metric_name,
                     metric_value, threshold, message, acknowledged)
                VALUES ($1, 'position_drift', $2, $3, $4, $5, $6, false)
                ON CONFLICT (id) DO NOTHING
                """,
                "trading",
                "high" if drift > max(1, tolerance * 5) else "warn",
                ticker,
                float(drift),
                float(tolerance),
                msg,
            )
            raised = True
    except Exception as e:
        log.warning("position_drift alert insert failed for %s: %s", ticker, e)

    if raised:
        try:
            from ..notifications import alerts as _alerts
            if await _alerts.dedupe_or_skip(
                "position_drift", f"{ticker}:{drift}", ttl_seconds=3600
            ):
                _alerts.drift(
                    vertical="trading",
                    metric=f"position_drift:{ticker}",
                    value=float(drift),
                    threshold=float(tolerance),
                    note=msg,
                )
        except Exception as e:  # pragma: no cover
            log.warning("position_drift email dispatch failed: %s", e)
        _total_drift_alerts += 1
    return raised


async def _diff_positions(exchange_positions: list[Any]) -> tuple[int, int]:
    """Compare exchange-reported positions vs our local DB.

    Returns `(drift_count, auto_corrected)`. `drift_count` is the number
    of tickers whose drift EXCEEDED the tolerance and therefore raised
    an alert. `auto_corrected` is the number of tickers whose drift was
    within the tolerance (logged-only, no alert).
    """
    tolerance = _drift_tolerance()
    local = await _local_signed_positions()
    exchange: dict[str, int] = {}
    for p in exchange_positions or []:
        ticker = getattr(p, "market_ticker", None) or ""
        if not ticker:
            continue
        exchange[ticker] = int(getattr(p, "position", 0) or 0)

    drift_count = 0
    auto_corrected = 0
    seen: set[str] = set()
    for ticker, exch_qty in exchange.items():
        seen.add(ticker)
        local_qty = int(local.get(ticker, 0))
        diff = abs(local_qty - exch_qty)
        if diff == 0:
            continue
        if diff <= tolerance:
            await _record_auto_correction(
                ticker=ticker, local_qty=local_qty, exchange_qty=exch_qty,
                drift=diff, tolerance=tolerance,
            )
            auto_corrected += 1
            continue
        await _raise_position_drift_alert(
            ticker=ticker, local_qty=local_qty, exchange_qty=exch_qty,
            drift=diff, tolerance=tolerance,
        )
        drift_count += 1
    # Tickers we have locally but the exchange reports none for: a stale
    # open position in our DB that the exchange already cleared. Treat
    # the same way (drift = local_qty).
    for ticker, local_qty in local.items():
        if ticker in seen or local_qty == 0:
            continue
        diff = abs(local_qty)
        if diff <= tolerance:
            await _record_auto_correction(
                ticker=ticker, local_qty=local_qty, exchange_qty=0,
                drift=diff, tolerance=tolerance,
            )
            auto_corrected += 1
            continue
        await _raise_position_drift_alert(
            ticker=ticker, local_qty=local_qty, exchange_qty=0,
            drift=diff, tolerance=tolerance,
        )
        drift_count += 1
    return drift_count, auto_corrected


async def _record_auto_correction(
    *,
    ticker: str,
    local_qty: int,
    exchange_qty: int,
    drift: int,
    tolerance: int,
) -> None:
    """Persist a within-tolerance drift as an INFO-level, pre-acknowledged
    `model_health_alerts` row so an operator can audit small drifts in
    the same UI without being paged.

    We deliberately do NOT mutate `positions.quantity` here: the WS
    pipeline + the next durable fill replay are the truth path; a
    blind quantity overwrite based on a single REST snapshot risks
    racing against an in-flight WS fill on the same ticker. The
    audit row is the "correction" — it documents that we observed
    and accepted the small drift.
    """
    msg = (
        f"Kalshi position drift within tolerance on {ticker}: "
        f"local={local_qty} exchange={exchange_qty} "
        f"drift={drift} (tolerance={tolerance})"
    )
    try:
        await db.execute(
            """
            INSERT INTO model_health_alerts
                (vertical, alert_type, severity, metric_name,
                 metric_value, threshold, message, acknowledged)
            VALUES ('trading', 'position_drift', 'info', $1, $2, $3, $4, true)
            ON CONFLICT (id) DO NOTHING
            """,
            ticker, float(drift), float(tolerance), msg,
        )
    except Exception as e:
        log.debug("auto-correction audit insert failed for %s: %s", ticker, e)


# ---------------------------------------------------------------------------
# Missed-fill replay
# ---------------------------------------------------------------------------


async def _lookup_local_order(exchange_order_id: str) -> dict[str, Any] | None:
    """Find the most-recent order_lifecycle row whose metadata recorded
    this exchange order id.

    Returns `{order_id, market_id, side, vertical, requested}` or None
    when the order is unknown to us (e.g. someone placed a manual order
    via the Kalshi web UI). `requested` is the order's submitted
    contract size (0 when unknown — in which case the replay path
    skips the over-fill cap and accepts whatever the exchange reports).
    """
    try:
        row = await db.fetchrow(
            """
            SELECT order_id,
                   market_id,
                   side,
                   coalesce(vertical, 'sports') AS vertical
              FROM order_lifecycle
             WHERE metadata->>'exchange_order_id' = $1
             ORDER BY id DESC
             LIMIT 1
            """,
            exchange_order_id,
        )
    except Exception as e:
        log.debug("order_lifecycle lookup failed for %s: %s", exchange_order_id, e)
        return None
    if row is None:
        return None
    requested = 0
    try:
        req_row = await db.fetchrow(
            """
            SELECT max(coalesce(contracts, 0))::int AS req
              FROM order_lifecycle
             WHERE order_id = $1
               AND event_type IN ('submitted', 'placed', 'created')
            """,
            row["order_id"],
        )
        requested = int(req_row["req"] or 0) if req_row else 0
    except Exception:
        requested = 0
    return {
        "order_id": row["order_id"],
        "market_id": row["market_id"],
        "side": row["side"] or "yes",
        "vertical": row["vertical"] or "sports",
        "requested": requested,
    }


async def _replay_fill(fill: dict[str, Any]) -> str:
    """Replay a single Kalshi fill into our local pipeline.

    Returns one of: `'replayed'`, `'duplicate'`, `'unknown'`,
    `'skipped'`. The reconciler tally counts everything except
    `'duplicate'` (which is the steady state — most fills the API
    returns we already saw via the WS).
    """
    global _total_replayed_fills

    exch_id = str(fill.get("order_id") or fill.get("orderId") or "")
    fill_id = str(
        fill.get("trade_id") or fill.get("fill_id") or fill.get("id") or ""
    )
    if not exch_id or not fill_id:
        return "skipped"

    side = (fill.get("side") or "yes").lower()
    if side == "yes":
        price = int(fill.get("yes_price") or 0)
    else:
        price = int(fill.get("no_price") or 0)
    count = int(fill.get("count") or 0)
    if count <= 0 or price <= 0:
        return "skipped"

    local = await _lookup_local_order(exch_id)
    if local is None:
        log.warning(
            "reconciler saw fill for unknown order: exchange_order_id=%s "
            "ticker=%s count=%s price=%s",
            exch_id, fill.get("ticker"), count, price,
        )
        return "unknown"

    order_id = local["order_id"]
    market_id = local["market_id"] or (fill.get("ticker") or "")
    vertical = local["vertical"]
    local_side = (local["side"] or side or "yes").lower()
    requested = int(local.get("requested") or 0)

    # Lazy import to keep the import graph cycle-free at module load.
    from . import lifecycle as _lifecycle

    fresh = await _lifecycle._record_fill_idempotent(
        order_id=order_id, fill_id=fill_id, count=count, price_cents=price,
    )
    if not fresh:
        return "duplicate"

    # Re-derive the cumulative rollup from the durable order_fills
    # table by REPLAYING fills in arrival order (`received_at`) and
    # capping accepted contracts at the requested size — exactly the
    # semantics `handle_ws_fill` uses. This means an over-fill is
    # discarded from the latest-arriving fill, not proportionally
    # scaled across all fills. Mixed-price fills (e.g. 3 @ 50¢ then
    # 5 @ 60¢ with requested=5) therefore produce the same avg the WS
    # path would have produced.
    try:
        rows = await db.fetch(
            """
            SELECT count, price_cents
              FROM order_fills
             WHERE order_id = $1
             ORDER BY received_at ASC, fill_id ASC
            """,
            order_id,
        )
    except Exception as e:
        log.warning("rollup recompute failed for %s: %s", order_id, e)
        rows = [{"count": count, "price_cents": price}]

    accepted_total = 0
    accepted_weighted = 0
    over_filled = False
    cap = requested if requested > 0 else None
    for r in rows or []:
        c = int(r["count"] or 0)
        p = int(r["price_cents"] or 0)
        if cap is not None:
            remaining = cap - accepted_total
            if remaining <= 0:
                over_filled = True
                continue
            take = min(c, remaining)
            if take < c:
                over_filled = True
        else:
            take = c
        accepted_total += take
        accepted_weighted += take * p

    if accepted_total > 0:
        avg_cents = int(round(accepted_weighted / accepted_total))
    else:
        avg_cents = price

    await _lifecycle._upsert_position_rollup(
        order_id=order_id,
        market_id=market_id,
        side=local_side,  # type: ignore[arg-type]
        vertical=vertical,
        quantity=accepted_total,
        avg_price_cents=avg_cents,
        over_filled=over_filled,
        closed=False,
    )

    # Audit row so a human can see the reconciler — not the WS — was
    # the source of this fill.
    try:
        await db.execute(
            """
            INSERT INTO order_lifecycle
                (order_id, market_id, event_type, side, price_cents,
                 spread_cents, volume, offset_cents, contracts,
                 is_maker, vertical, metadata)
            VALUES ($1, $2, 'filled', $3, $4, 2, 0, 0, $5, false, $6, $7::jsonb)
            """,
            order_id, market_id, local_side, price, count, vertical,
            json.dumps({
                "source": "reconciler_replay",
                "exchange_order_id": exch_id,
                "fill_id": fill_id,
                "fill": fill,
            }),
        )
    except Exception as e:
        log.debug("reconciler audit row insert failed: %s", e)

    _total_replayed_fills += 1
    return "replayed"


# ---------------------------------------------------------------------------
# Tick + loop
# ---------------------------------------------------------------------------


async def _tick_once(
    *,
    client_factory=None,
) -> dict[str, Any]:
    """Run one reconciliation pass.

    Returns a dict summarising the work done — used by tests and by the
    health card. `client_factory` is a hook for tests; in production we
    construct a fresh KalshiClient per tick.
    """
    global _last_tick_at, _last_error, _last_skipped_reason
    global _last_drift_count, _last_auto_corrected
    global _last_missed_fills, _last_unknown_fills
    global _fills_cursor_ts

    summary: dict[str, Any] = {
        "skipped": False,
        "drift_count": 0,
        "auto_corrected": 0,
        "replayed_fills": 0,
        "unknown_fills": 0,
        "duplicate_fills": 0,
        "error": None,
    }

    if not _enabled():
        _last_skipped_reason = "disabled (paper mode or missing creds)"
        summary["skipped"] = True
        summary["reason"] = _last_skipped_reason
        _last_tick_at = time.time()
        return summary

    if _kill.halted:
        _last_skipped_reason = "kill switch active"
        summary["skipped"] = True
        summary["reason"] = _last_skipped_reason
        _last_tick_at = time.time()
        return summary

    _last_skipped_reason = None
    factory = client_factory or (lambda: kalshi_client_mod.KalshiClient())
    try:
        async with factory() as client:
            positions = await client.get_positions(limit=200)
            # Use a 5-minute lookback when we don't yet have a cursor so
            # the very first tick after restart still picks up fresh
            # fills the WS handler may have missed during the restart.
            min_ts = _fills_cursor_ts or int(time.time() - 300)
            fills = await client.list_fills(min_ts=min_ts, limit=200)

        # IMPORTANT ORDER: replay missed fills BEFORE diffing positions.
        # If the WS dropped a fill, our local position is stale until we
        # replay the fill into the rollup table. Diffing first would
        # raise a (false) drift alert this same tick. Replay first, then
        # diff, so alerts only fire on truly unexplained drift.
        replayed = duplicates = unknown = 0
        latest_ts = _fills_cursor_ts or 0
        for f in fills:
            outcome = await _replay_fill(f)
            if outcome == "replayed":
                replayed += 1
            elif outcome == "duplicate":
                duplicates += 1
            elif outcome == "unknown":
                unknown += 1
            ts_raw = (
                f.get("created_time")
                or f.get("ts")
                or f.get("timestamp")
                or 0
            )
            try:
                ts_int = int(ts_raw)
                # Some endpoints emit milliseconds — normalise.
                if ts_int > 10**12:
                    ts_int = ts_int // 1000
                if ts_int > latest_ts:
                    latest_ts = ts_int
            except (TypeError, ValueError):
                pass

        drift_count, auto_corrected = await _diff_positions(positions)
        summary["drift_count"] = drift_count
        summary["auto_corrected"] = auto_corrected
        summary["replayed_fills"] = replayed
        summary["duplicate_fills"] = duplicates
        summary["unknown_fills"] = unknown

        _last_drift_count = drift_count
        _last_auto_corrected = auto_corrected
        _last_missed_fills = replayed
        _last_unknown_fills = unknown
        if latest_ts:
            _fills_cursor_ts = latest_ts
        _last_error = None
    except Exception as e:
        _last_error = f"{type(e).__name__}: {e}"
        summary["error"] = _last_error
        log.debug("reconciler tick failed: %s", _last_error)
    finally:
        _last_tick_at = time.time()
    return summary


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
    if not _enabled():
        log.info("kalshi reconciler not started (disabled / paper / no creds)")
        return
    _stop.clear()
    _started_at = time.time()
    _task = asyncio.create_task(_loop(), name="kalshi-reconciler")
    log.info("kalshi reconciler started (interval=%ss, tolerance=%s)",
             _interval_seconds(), _drift_tolerance())


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
