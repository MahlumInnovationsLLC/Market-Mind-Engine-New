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
    # Default raised from 60s → 180s to cut Supabase disk-IO; reconciler
    # only needs to catch drift, not run live. Env override still wins.
    raw = os.environ.get("AXIOM_RECONCILER_INTERVAL_SECS")
    try:
        v = float(raw) if raw is not None else 180.0
    except (TypeError, ValueError):
        v = 180.0
    return max(10.0, v)


def _drift_tolerance() -> int:
    raw = os.environ.get("AXIOM_RECONCILER_DRIFT_TOLERANCE")
    try:
        v = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        v = 0
    return max(0, v)


def _resolution_enabled() -> bool:
    """Operator-resolution workflow: queue table writes + auto-skeleton.

    Default OFF so the schema (migration 0031) can land safely without
    changing runtime behaviour. Operators flip the flag once they've
    acknowledged the new ``/system → reconciler`` panel exists.
    """
    raw = (os.environ.get("AXIOM_RECONCILER_RESOLUTION_ENABLED") or "").strip()
    return raw == "1" or raw.lower() == "true"


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
_last_orphan_skeletons: int = 0
_last_snoozed_drifts: int = 0
_total_orphan_skeletons: int = 0


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
        "last_orphan_skeletons": _last_orphan_skeletons,
        "last_snoozed_drifts": _last_snoozed_drifts,
        "total_drift_alerts": _total_drift_alerts,
        "total_replayed_fills": _total_replayed_fills,
        "total_orphan_skeletons": _total_orphan_skeletons,
        "fills_cursor_ts": _fills_cursor_ts,
        "interval_seconds": _interval_seconds(),
        "drift_tolerance": _drift_tolerance(),
        "resolution_enabled": _resolution_enabled(),
    }


# ---------------------------------------------------------------------------
# Core diff
# ---------------------------------------------------------------------------


async def _local_signed_positions(
    exchange: str = "kalshi",
) -> dict[str, int]:
    """Sum open `positions` rows on ``exchange``, signed by side.

    Returns `{market_ticker: signed_qty}` where signed_qty is positive
    for net YES exposure and negative for net NO exposure — matching
    the convention Kalshi's `/portfolio/positions` returns in the
    `position` field.

    ``exchange`` defaults to ``'kalshi'`` so existing call sites stay
    unchanged; the polymarket reconciler (task #715) passes
    ``'polymarket'`` to namespace the same query.
    """
    out: dict[str, int] = {}
    try:
        rows = await db.fetch(
            """
            SELECT market_id,
                   side,
                   coalesce(quantity, 0)::int AS qty
              FROM positions
             WHERE coalesce(exchange, 'kalshi') = $1
               AND coalesce(status, 'open') = 'open'
               AND market_id IS NOT NULL
            """,
            exchange,
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


# ---------------------------------------------------------------------------
# Operator-resolution workflow (task #709) — backed by
# ``position_drift_resolutions``.  Everything below is a no-op when
# ``AXIOM_RECONCILER_RESOLUTION_ENABLED`` is unset / "0", so the schema
# migration can land without changing runtime behaviour.
# ---------------------------------------------------------------------------


async def _upsert_drift_resolution(
    *,
    ticker: str,
    vertical: str | None,
    local_qty: int,
    exchange_qty: int,
    drift: int,
    exchange: str = "kalshi",
) -> str:
    """Upsert the active drift-resolution row for
    ``(exchange, ticker)`` and return the resulting state
    (``'open'``, ``'snoozed'``, or ``''`` on error).

    The per-exchange namespace lets the same queue serve Kalshi and
    Polymarket without collision: ``(kalshi, MKT-A)`` and
    ``(polymarket, MKT-A)`` are independent active rows (task #715).
    ``exchange`` defaults to ``'kalshi'`` so legacy call sites remain
    correct.

    Behaviour:
      * No active row → INSERT new ``open`` row, return ``'open'``.
      * Active row state=``'open'`` → UPDATE qtys + ``last_seen_at``,
        return ``'open'``.
      * Active row state=``'snoozed'`` and ``snoozed_until > now()`` →
        UPDATE qtys + ``last_seen_at``, return ``'snoozed'`` so the
        caller suppresses alert dispatch.
      * Active row state=``'snoozed'`` and ``snoozed_until <= now()`` →
        transition to ``'open'`` (auto-expire the snooze), return
        ``'open'`` so the alert path runs again.
    """
    try:
        existing = await db.fetchrow(
            """
            SELECT id, state, snoozed_until
              FROM position_drift_resolutions
             WHERE exchange = $1
               AND ticker = $2
               AND state IN ('open', 'snoozed')
             ORDER BY id DESC
             LIMIT 1
            """,
            exchange, ticker,
        )
        if existing is None:
            await db.execute(
                """
                INSERT INTO position_drift_resolutions
                    (exchange, ticker, vertical, local_qty, exchange_qty,
                     drift_qty, state, first_seen_at, last_seen_at,
                     created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, 'open',
                        now(), now(), now(), now())
                """,
                exchange, ticker, vertical,
                int(local_qty), int(exchange_qty), int(drift),
            )
            return "open"

        state_now = (existing["state"] or "open").lower()
        snoozed_until = existing["snoozed_until"]
        # Snooze auto-expiry: a snoozed row whose TTL has elapsed
        # transitions back to ``open`` so the operator alert can fire
        # again. Clearing ``snoozed_until`` keeps the row honest.
        if state_now == "snoozed" and snoozed_until is not None:
            from datetime import datetime, timezone
            now_utc = datetime.now(timezone.utc)
            # asyncpg returns timezone-aware datetime for timestamptz.
            if snoozed_until <= now_utc:
                state_now = "open"
                await db.execute(
                    """
                    UPDATE position_drift_resolutions
                       SET state = 'open',
                           snoozed_until = NULL,
                           local_qty = $2,
                           exchange_qty = $3,
                           drift_qty = $4,
                           last_seen_at = now(),
                           updated_at = now()
                     WHERE id = $1
                    """,
                    existing["id"], int(local_qty), int(exchange_qty),
                    int(drift),
                )
                return "open"

        # Same-state refresh — keep the latest qtys + bump last_seen_at.
        await db.execute(
            """
            UPDATE position_drift_resolutions
               SET local_qty = $2,
                   exchange_qty = $3,
                   drift_qty = $4,
                   last_seen_at = now(),
                   updated_at = now()
             WHERE id = $1
            """,
            existing["id"], int(local_qty), int(exchange_qty), int(drift),
        )
        return state_now
    except Exception as e:
        log.debug("position_drift_resolutions upsert failed (%s): %s",
                  ticker, e)
        return ""


async def _has_any_local_position_row(
    ticker: str, exchange: str = "kalshi",
) -> bool:
    """True if ANY ``positions`` row exists for
    ``(exchange, ticker)`` (open or closed). Used to distinguish a true
    orphan (no row at all) from a stale-but-known position. Only true
    orphans get the auto-skeleton so we never resurrect a closed
    position.
    """
    try:
        v = await db.fetchval(
            """
            SELECT 1
              FROM positions
             WHERE coalesce(exchange, 'kalshi') = $1
               AND market_id = $2
             LIMIT 1
            """,
            exchange, ticker,
        )
        return v is not None
    except Exception as e:
        log.debug("orphan-check positions read failed (%s): %s", ticker, e)
        # Fail-safe: pretend there IS a local row so we don't create a
        # spurious skeleton during a transient DB error.
        return True


def _orphan_synth_id(ticker: str, exchange: str) -> str:
    """Audit-tag the synthetic orphan ``positions.order_id``.

    Kalshi keeps the legacy unprefixed shape (``recon-orphan-<ticker>``)
    so existing rows and audit queries continue to work; non-kalshi
    venues prefix with the exchange so ``(kalshi, MKT-A)`` and
    ``(polymarket, MKT-A)`` cannot collide on the unique
    ``positions.order_id`` index.
    """
    if exchange == "kalshi":
        return f"recon-orphan-{ticker}"
    return f"recon-orphan-{exchange}-{ticker}"


def _accept_truth_synth_id(ticker: str, exchange: str) -> str:
    """Same audit-tag scheme as ``_orphan_synth_id`` but for the
    accept-exchange-truth correction row.
    """
    if exchange == "kalshi":
        return f"recon-accept-truth-{ticker}"
    return f"recon-accept-truth-{exchange}-{ticker}"


def _alert_metric_name(ticker: str, exchange: str) -> str:
    """Per-exchange identity for ``model_health_alerts.metric_name`` so
    a kalshi ``MKT-A`` drift and a polymarket ``MKT-A`` drift don't
    suppress / overwrite each other through the per-ticker dedupe
    window or the ``last_alert_*`` enrichment join (task #715).

    Kalshi keeps the bare ticker shape so historical alert rows and
    operator queries continue to match; non-kalshi venues are
    namespaced ``<exchange>:<ticker>``.
    """
    if exchange == "kalshi":
        return ticker
    return f"{exchange}:{ticker}"


async def _create_orphan_skeleton(
    *, ticker: str, exchange_qty: int, vertical: str | None,
    exchange: str = "kalshi",
) -> bool:
    """Insert a synthetic ``positions`` row for a true orphan exchange
    position on ``exchange`` so the next diff loop sees no drift.

    Idempotent via ``ON CONFLICT (order_id) DO UPDATE`` — re-running for
    the same ticker just refreshes the quantity and side.
    """
    if exchange_qty == 0:
        return False
    side = "yes" if exchange_qty > 0 else "no"
    qty = abs(int(exchange_qty))
    # ``order_id`` carries the audit prefix (see ``_orphan_synth_id``)
    # so analysts can filter synthetic rows without a dedicated column.
    synth_id = _orphan_synth_id(ticker, exchange)
    try:
        await db.execute(
            """
            INSERT INTO positions (
                order_id, market_id, side, vertical, exchange,
                quantity, avg_price, status, filled_count, over_filled
            ) VALUES (
                $1, $2, $3, $4, $6,
                $5, 0.0, 'open', $5, false
            )
            ON CONFLICT (order_id) DO UPDATE
               SET quantity = EXCLUDED.quantity,
                   filled_count = EXCLUDED.filled_count,
                   side = EXCLUDED.side,
                   status = 'open'
            """,
            synth_id, ticker, side, vertical, qty, exchange,
        )
    except Exception as e:
        log.warning("orphan-skeleton insert failed (%s): %s", ticker, e)
        return False

    # Mark the resolution row resolved so it doesn't sit in the open
    # queue forever — the orphan was handled programmatically. This row
    # IS the manual-reconciliation audit record (resolver_email +
    # resolution_reason + state + resolved_at give the full audit
    # trail; no separate events table is required).
    try:
        await db.execute(
            """
            UPDATE position_drift_resolutions
               SET state = 'resolved_orphan_skeleton',
                   resolver_email = 'reconciler@axiom',
                   resolution_reason = 'auto-created skeleton positions row',
                   resolved_at = now(),
                   updated_at = now()
             WHERE exchange = $2
               AND ticker = $1
               AND state IN ('open', 'snoozed')
            """,
            ticker, exchange,
        )
    except Exception as e:
        log.debug("orphan-skeleton resolution update failed (%s): %s",
                  ticker, e)

    # Operators see the auto-skeleton in app logs and in /system →
    # reconciler (the resolution row above is the structured trace).
    # We deliberately do NOT add an alerts-table audit insert here —
    # the diff cycle that triggered this path already raised the
    # warn-level position_drift alert at line ~796.
    log.info(
        "reconciler.orphan_skeleton ticker=%s side=%s qty=%d "
        "(synthetic positions row %s)",
        ticker, side, qty, synth_id,
    )
    return True


async def list_drifts(
    *, state: str = "open", limit: int = 100,
    exchange: str = "kalshi",
) -> list[dict[str, Any]]:
    """Operator-console view of the drift-resolution queue, enriched
    with the context an operator needs to triage each row at a glance:

      * ``market_title`` — human-readable market title (resolved via
        the market-title cache; falls back to ``None`` if unknown).
      * ``age_seconds`` — wall-clock time since ``first_seen_at`` so
        the UI can sort/colour by staleness without parsing dates.
      * ``last_alert_at`` / ``last_alert_severity`` /
        ``last_alert_message`` — the latest matching
        ``model_health_alerts`` row (``alert_type='position_drift'``,
        ``metric_name=ticker``) so the operator sees the same context
        the alert email contained.

    ``state`` is one of ``open`` (default — actionable queue),
    ``snoozed``, ``resolved`` (any ``resolved_*`` terminal), or
    ``all``.
    """
    state = (state or "open").lower()
    limit = max(1, min(500, int(limit or 100)))
    where = "pdr.exchange = $2"
    if state == "open":
        where += " AND pdr.state = 'open'"
    elif state == "snoozed":
        where += " AND pdr.state = 'snoozed'"
    elif state == "resolved":
        where += " AND pdr.state LIKE 'resolved_%'"
    try:
        rows = await db.fetch(
            f"""
            SELECT pdr.id, pdr.exchange, pdr.ticker, pdr.vertical,
                   pdr.local_qty, pdr.exchange_qty, pdr.drift_qty,
                   pdr.state, pdr.resolver_email, pdr.resolution_reason,
                   pdr.snoozed_until, pdr.first_seen_at, pdr.last_seen_at,
                   pdr.resolved_at, pdr.created_at,
                   GREATEST(0,
                     EXTRACT(EPOCH FROM (now() - pdr.first_seen_at)))::bigint
                     AS age_seconds,
                   alert.created_at  AS last_alert_at,
                   alert.severity    AS last_alert_severity,
                   alert.message     AS last_alert_message
              FROM position_drift_resolutions pdr
              LEFT JOIN LATERAL (
                  SELECT created_at, severity, message
                    FROM model_health_alerts
                   WHERE alert_type = 'position_drift'
                     AND metric_name = CASE
                         WHEN pdr.exchange = 'kalshi' THEN pdr.ticker
                         ELSE pdr.exchange || ':' || pdr.ticker
                     END
                   ORDER BY created_at DESC
                   LIMIT 1
              ) alert ON true
             WHERE {where}
             ORDER BY pdr.last_seen_at DESC
             LIMIT $1
            """,
            limit, exchange,
        )
    except Exception as e:
        log.warning("list_drifts query failed: %s", e)
        return []

    out = [dict(r) for r in rows]
    # Best-effort title enrichment — failures here are non-fatal, the
    # row already has ticker + drift context to be actionable.
    try:
        from .market_titles import resolve_titles
        tickers = [r["ticker"] for r in out if r.get("ticker")]
        if tickers:
            titles = await resolve_titles(tickers)
            for r in out:
                r["market_title"] = titles.get(r["ticker"])
        else:
            for r in out:
                r["market_title"] = None
    except Exception as e:
        log.debug("list_drifts title enrichment failed: %s", e)
        for r in out:
            r.setdefault("market_title", None)
    return out


async def accept_exchange_truth(
    *, ticker: str, resolver_email: str | None, reason: str | None = None,
    exchange: str = "kalshi",
) -> dict[str, Any]:
    """Operator action: accept the exchange's reported quantity as truth.

    Closes every open local ``positions`` row for the ticker (preserving
    history — they stay in the table with ``status='closed'``) and
    inserts a single synthetic row carrying the exchange's signed
    quantity, tagged ``recon-accept-truth`` for audit. Acknowledges the
    open ``model_health_alerts`` rows for the ticker and resolves the
    drift queue entry.

    Idempotent: if there is no active drift row for ``ticker`` the
    call returns ``{"changed": False}`` without touching ``positions``
    so re-clicks from the operator console don't double-resolve.
    """
    active = await db.fetchrow(
        """
        SELECT id, local_qty, exchange_qty, drift_qty
          FROM position_drift_resolutions
         WHERE exchange = $1
           AND ticker = $2
           AND state IN ('open', 'snoozed')
         ORDER BY id DESC
         LIMIT 1
        """,
        exchange, ticker,
    )
    if active is None:
        return {"changed": False, "ticker": ticker, "reason": "no_active_drift"}

    exch_qty = int(active["exchange_qty"] or 0)
    local_qty = int(active["local_qty"] or 0)
    drift_qty = int(active["drift_qty"] or 0)

    # Steps 1-4 must commit or roll back as a single unit so we never
    # leave the DB half-reconciled (e.g. drift marked resolved while
    # local positions still show the old quantity). Only the email
    # notification at step 5 is best-effort outside the transaction —
    # an SMTP hiccup must not undo a successful reconciliation.
    side = "yes" if exch_qty > 0 else "no"
    qty = abs(exch_qty)
    synth_id = _accept_truth_synth_id(ticker, exchange)
    try:
        async with db.acquire() as conn:
            async with conn.transaction():
                # 1. Close existing open rows for this ticker.
                #    coalesce() catches historical NULL exchange/status
                #    rows that strict equality would skip.
                await conn.execute(
                    """
                    UPDATE positions
                       SET status = 'closed',
                           quantity = 0
                     WHERE coalesce(exchange, 'kalshi') = $2
                       AND market_id = $1
                       AND coalesce(status, 'open') = 'open'
                    """,
                    ticker, exchange,
                )

                # 2. Insert synthetic correction row when exchange has
                #    non-zero qty. The ``recon-accept-truth-`` prefix
                #    on order_id is the audit signal — analysts can
                #    filter synthetic rows by LIKE without a dedicated
                #    column.
                if exch_qty != 0:
                    await conn.execute(
                        """
                        INSERT INTO positions (
                            order_id, market_id, side, vertical, exchange,
                            quantity, avg_price, status, filled_count,
                            over_filled
                        ) VALUES (
                            $1, $2, $3, NULL, $5,
                            $4, 0.0, 'open', $4, false
                        )
                        ON CONFLICT (order_id) DO UPDATE
                           SET quantity = EXCLUDED.quantity,
                               filled_count = EXCLUDED.filled_count,
                               side = EXCLUDED.side,
                               status = 'open'
                        """,
                        synth_id, ticker, side, qty, exchange,
                    )

                # 3. Acknowledge any unacked operator alerts for this
                #    (exchange, ticker) so the /system → health banner
                #    clears. ``metric_name`` was written by
                #    ``_raise_position_drift_alert`` /
                #    ``_record_auto_correction`` via
                #    ``_alert_metric_name`` (kalshi: bare ticker;
                #    non-kalshi: ``<exchange>:<ticker>``), so we ack
                #    against the same identity here — otherwise a
                #    polymarket resolution would silently leave the
                #    ``polymarket:<ticker>`` alert rows unacked and
                #    keep the health banner red.
                await conn.execute(
                    """
                    UPDATE model_health_alerts
                       SET acknowledged = true,
                           acknowledged_at = now(),
                           acknowledged_by = $1
                     WHERE alert_type = 'position_drift'
                       AND metric_name = $2
                       AND acknowledged = false
                    """,
                    resolver_email or "operator",
                    _alert_metric_name(ticker, exchange),
                )

                # 4. Resolve the drift queue entry. This row IS the
                #    manual reconciliation audit record — resolver_email
                #    + resolution_reason + resolved_at give the full
                #    "who/why/when" trail. No separate events table.
                await conn.execute(
                    """
                    UPDATE position_drift_resolutions
                       SET state = 'resolved_accept_exchange',
                           resolver_email = $2,
                           resolution_reason = $3,
                           resolved_at = now(),
                           updated_at = now()
                     WHERE id = $1
                    """,
                    active["id"], resolver_email, reason,
                )
    except Exception as e:
        # Any failure inside the transaction triggers a rollback. Bail
        # out without claiming the drift was resolved so the operator
        # console still shows it as actionable.
        log.warning(
            "accept_exchange_truth transaction failed (%s): %s",
            ticker, e,
        )
        return {
            "changed": False, "ticker": ticker,
            "reason": f"transaction_failed:{type(e).__name__}",
        }

    # 5. Confirmation email — best-effort, dedupe so a flurry of clicks
    #    doesn't spam inboxes.
    try:
        from ..notifications import alerts as _alerts
        if await _alerts.dedupe_or_skip(
            "position_drift_resolved",
            f"{ticker}:accept:{active['id']}",
            ttl_seconds=3600,
        ):
            _alerts.position_drift_resolved(
                ticker=ticker,
                action="accept_exchange_truth",
                local_qty=local_qty,
                exchange_qty=exch_qty,
                drift_qty=drift_qty,
                resolver_email=resolver_email,
                reason=reason,
            )
    except Exception as e:  # pragma: no cover
        log.warning("position_drift_resolved email failed (%s): %s",
                    ticker, e)

    return {
        "changed": True,
        "ticker": ticker,
        "exchange_qty": exch_qty,
        "local_qty_before": local_qty,
        "drift_qty": drift_qty,
        "resolution_id": int(active["id"]),
    }


async def snooze_drift(
    *,
    ticker: str,
    until: "datetime | str",
    resolver_email: str | None,
    reason: str | None = None,
    exchange: str = "kalshi",
) -> dict[str, Any]:
    """Operator action: silence drift alerts for ``ticker`` until the
    absolute timestamp ``until``.

    ``until`` accepts either a timezone-aware ``datetime`` or an ISO
    8601 string. The value is validated to be in the future (≥ 5
    minutes from now to leave room for clock skew) and within 30 days
    (so a fat-fingered ``2099`` doesn't silence the alert forever).

    Does NOT touch ``positions`` — snooze is for drifts the operator
    has acknowledged as known-benign (e.g. a settled market the
    exchange hasn't propagated yet) and just wants the alert noise to
    stop temporarily. Auto-expires via ``_upsert_drift_resolution``.

    Idempotent on re-snooze: extends the TTL on an existing snoozed
    row.
    """
    from datetime import datetime, timedelta, timezone
    if isinstance(until, str):
        try:
            # Accept "Z" suffix for UTC.
            iso = until.replace("Z", "+00:00")
            until_dt = datetime.fromisoformat(iso)
        except ValueError:
            return {
                "changed": False, "ticker": ticker,
                "reason": f"invalid_until:{until!r}",
            }
    else:
        until_dt = until
    if until_dt.tzinfo is None:
        until_dt = until_dt.replace(tzinfo=timezone.utc)
    now_utc = datetime.now(timezone.utc)
    min_until = now_utc + timedelta(minutes=5)
    max_until = now_utc + timedelta(days=30)
    if until_dt < min_until:
        return {
            "changed": False, "ticker": ticker,
            "reason": "until_too_soon (must be ≥ 5 min from now)",
        }
    if until_dt > max_until:
        return {
            "changed": False, "ticker": ticker,
            "reason": "until_too_far (must be ≤ 30 days from now)",
        }

    active = await db.fetchrow(
        """
        SELECT id, local_qty, exchange_qty, drift_qty
          FROM position_drift_resolutions
         WHERE exchange = $1
           AND ticker = $2
           AND state IN ('open', 'snoozed')
         ORDER BY id DESC
         LIMIT 1
        """,
        exchange, ticker,
    )
    if active is None:
        return {"changed": False, "ticker": ticker, "reason": "no_active_drift"}

    try:
        await db.execute(
            """
            UPDATE position_drift_resolutions
               SET state = 'snoozed',
                   snoozed_until = $2,
                   resolver_email = $3,
                   resolution_reason = $4,
                   updated_at = now()
             WHERE id = $1
            """,
            active["id"], until_dt, resolver_email, reason,
        )
    except Exception as e:
        log.warning("snooze_drift update failed (%s): %s", ticker, e)
        return {"changed": False, "ticker": ticker, "reason": f"db_error:{e}"}

    try:
        from ..notifications import alerts as _alerts
        if await _alerts.dedupe_or_skip(
            "position_drift_resolved",
            f"{ticker}:snooze:{active['id']}",
            ttl_seconds=3600,
        ):
            _alerts.position_drift_resolved(
                ticker=ticker,
                action="snooze",
                local_qty=int(active["local_qty"] or 0),
                exchange_qty=int(active["exchange_qty"] or 0),
                drift_qty=int(active["drift_qty"] or 0),
                resolver_email=resolver_email,
                reason=reason,
                snoozed_until=until_dt.isoformat(),
            )
    except Exception as e:  # pragma: no cover
        log.warning("snooze confirmation email failed (%s): %s", ticker, e)

    return {
        "changed": True,
        "ticker": ticker,
        "snoozed_until": until_dt.isoformat(),
        "resolution_id": int(active["id"]),
    }


async def queue_health() -> dict[str, Any]:
    """Snapshot of the operator drift-resolution queue for health alerting.

    Returns a small dict with the only three numbers an operator needs
    to gauge whether the queue is keeping pace:

      * ``open_count`` — rows in state ``open`` (actionable backlog).
      * ``oldest_open_age_seconds`` — wall-clock seconds since the
        oldest open row's ``first_seen_at`` (0 when nothing is open).
      * ``snoozed_count`` — rows in state ``snoozed`` (deliberately
        silenced; surfaced for visibility, not gating).

    Failure-safe: any DB error returns zeroes so the health snapshot
    stays composable. Task #716.
    """
    try:
        row = await db.fetchrow(
            """
            SELECT
              count(*) FILTER (WHERE state = 'open')::bigint     AS open_count,
              count(*) FILTER (WHERE state = 'snoozed')::bigint  AS snoozed_count,
              COALESCE(
                EXTRACT(EPOCH FROM (
                  now() - min(first_seen_at) FILTER (WHERE state = 'open')
                ))::bigint,
                0
              ) AS oldest_open_age_seconds
              FROM position_drift_resolutions
            """
        )
    except Exception as e:
        log.debug("queue_health query failed: %s", e)
        return {
            "open_count": 0,
            "oldest_open_age_seconds": 0,
            "snoozed_count": 0,
        }
    if row is None:
        return {
            "open_count": 0,
            "oldest_open_age_seconds": 0,
            "snoozed_count": 0,
        }
    return {
        "open_count": int(row["open_count"] or 0),
        "oldest_open_age_seconds": int(row["oldest_open_age_seconds"] or 0),
        "snoozed_count": int(row["snoozed_count"] or 0),
    }


async def _self_heal_resolutions(
    active_drift_tickers: set[str], exchange: str = "kalshi",
) -> None:
    """Mark any ``open`` resolution rows on ``exchange`` whose drift
    has cleared on its own (e.g. a delayed WS fill arrived) as
    ``resolved_self_healed``.

    Scoped per-exchange so a quiet kalshi cycle doesn't sweep
    polymarket rows (or vice versa). Snoozed rows are intentionally
    left alone — they were silenced deliberately and we don't want a
    transient zero-drift cycle to quietly cancel the operator's
    snooze.
    """
    try:
        await db.execute(
            """
            UPDATE position_drift_resolutions
               SET state = 'resolved_self_healed',
                   resolved_at = now(),
                   updated_at = now()
             WHERE state = 'open'
               AND exchange = $2
               AND ticker <> ALL($1::text[])
            """,
            list(active_drift_tickers), exchange,
        )
    except Exception as e:
        log.debug("self-heal resolutions sweep failed: %s", e)


async def _raise_position_drift_alert(
    *,
    ticker: str,
    local_qty: int,
    exchange_qty: int,
    drift: int,
    tolerance: int,
    exchange: str = "kalshi",
) -> bool:
    """Persist + email a drift alert (deduped per ticker for 1 hour).

    Returns True if a fresh alert was raised, False if suppressed by
    the dedupe window.
    """
    global _total_drift_alerts
    venue_label = exchange.title() if exchange != "kalshi" else "Kalshi"
    metric_name = _alert_metric_name(ticker, exchange)
    msg = (
        f"{venue_label} position drift on {ticker}: "
        f"local={local_qty} exchange={exchange_qty} "
        f"drift={drift} (tolerance={tolerance})"
    )
    raised = False
    try:
        # Suppress same-hour duplicates of the SAME (exchange, ticker)
        # AT THE SAME drift magnitude. The dedupe key is
        # `(metric_name, drift_qty)` where metric_name encodes the
        # exchange — so a kalshi 5-contract drift on MKT-A doesn't
        # suppress a polymarket 5-contract drift on MKT-A, and a
        # ticker whose drift jumps from 5 -> 50 still produces a fresh
        # alert (the magnitude change is itself news).
        recent = await db.fetchval(
            """
            SELECT 1 FROM model_health_alerts
             WHERE alert_type = 'position_drift'
               AND metric_name = $1
               AND metric_value = $2
               AND created_at > now() - interval '1 hour'
             LIMIT 1
            """,
            metric_name,
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
                metric_name,
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
                "position_drift",
                f"{metric_name}:{drift}",
                ttl_seconds=3600,
            ):
                _alerts.drift(
                    vertical="trading",
                    metric=f"position_drift:{metric_name}",
                    value=float(drift),
                    threshold=float(tolerance),
                    note=msg,
                )
        except Exception as e:  # pragma: no cover
            log.warning("position_drift email dispatch failed: %s", e)
        _total_drift_alerts += 1
    return raised


async def _diff_positions(
    exchange_positions: list[Any],
    exchange: str = "kalshi",
) -> tuple[int, int, int, int]:
    """Compare exchange-reported positions vs our local DB.

    Returns ``(drift_count, auto_corrected, orphan_skeletons,
    snoozed_drifts)``. ``drift_count`` is the number of tickers whose
    drift EXCEEDED the tolerance and raised an alert. ``auto_corrected``
    is the number of tickers whose drift was within the tolerance
    (logged-only). ``orphan_skeletons`` is the number of true-orphan
    exchange positions that got an auto-skeleton ``positions`` row this
    cycle (resolution flag only). ``snoozed_drifts`` is the number of
    drifts where an active operator snooze suppressed the alert.

    ``exchange`` controls which venue's positions table partition we
    compare against and which row namespace we write to in the
    ``position_drift_resolutions`` queue (task #715).
    """
    tolerance = _drift_tolerance()
    resolution_on = _resolution_enabled()
    local = await _local_signed_positions(exchange=exchange)
    exch_signed: dict[str, int] = {}
    for p in exchange_positions or []:
        ticker = getattr(p, "market_ticker", None) or ""
        if not ticker:
            continue
        exch_signed[ticker] = int(getattr(p, "position", 0) or 0)

    drift_count = 0
    auto_corrected = 0
    orphan_skeletons = 0
    snoozed_drifts = 0
    active_drift_tickers: set[str] = set()
    seen: set[str] = set()
    for ticker, exch_qty in exch_signed.items():
        seen.add(ticker)
        local_qty = int(local.get(ticker, 0))
        diff = abs(local_qty - exch_qty)
        if diff == 0:
            continue
        if diff <= tolerance:
            await _record_auto_correction(
                ticker=ticker, local_qty=local_qty, exchange_qty=exch_qty,
                drift=diff, tolerance=tolerance, exchange=exchange,
            )
            auto_corrected += 1
            continue

        # Operator-resolution path: queue the drift, honour active
        # snooze, and (for true orphans) auto-skeleton the local row so
        # the next cycle stops re-firing on the same ticker.
        if resolution_on:
            res_state = await _upsert_drift_resolution(
                ticker=ticker, vertical=None,
                local_qty=local_qty, exchange_qty=exch_qty, drift=diff,
                exchange=exchange,
            )
            active_drift_tickers.add(ticker)
            if res_state == "snoozed":
                snoozed_drifts += 1
                continue
            # Orphan: exchange has it but no local row exists at all.
            # Distinguish from "stale local row drifted to 0" by
            # checking ANY row (open or closed) — the latter has a
            # row with status='closed' and shouldn't be resurrected.
            if local_qty == 0 and not await _has_any_local_position_row(
                ticker, exchange=exchange,
            ):
                created = await _create_orphan_skeleton(
                    ticker=ticker, exchange_qty=exch_qty, vertical=None,
                    exchange=exchange,
                )
                if created:
                    orphan_skeletons += 1
                    # Skip the operator-page alert path — the INFO
                    # audit row from `_create_orphan_skeleton` covers
                    # the visibility need, and we don't want to wake
                    # someone for a self-resolving orphan.
                    continue

        await _raise_position_drift_alert(
            ticker=ticker, local_qty=local_qty, exchange_qty=exch_qty,
            drift=diff, tolerance=tolerance, exchange=exchange,
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
                drift=diff, tolerance=tolerance, exchange=exchange,
            )
            auto_corrected += 1
            continue
        if resolution_on:
            res_state = await _upsert_drift_resolution(
                ticker=ticker, vertical=None,
                local_qty=local_qty, exchange_qty=0, drift=diff,
                exchange=exchange,
            )
            active_drift_tickers.add(ticker)
            if res_state == "snoozed":
                snoozed_drifts += 1
                continue
        await _raise_position_drift_alert(
            ticker=ticker, local_qty=local_qty, exchange_qty=0,
            drift=diff, tolerance=tolerance, exchange=exchange,
        )
        drift_count += 1

    if resolution_on:
        await _self_heal_resolutions(active_drift_tickers, exchange=exchange)

    return drift_count, auto_corrected, orphan_skeletons, snoozed_drifts


async def _record_auto_correction(
    *,
    ticker: str,
    local_qty: int,
    exchange_qty: int,
    drift: int,
    tolerance: int,
    exchange: str = "kalshi",
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
    venue_label = exchange.title() if exchange != "kalshi" else "Kalshi"
    metric_name = _alert_metric_name(ticker, exchange)
    msg = (
        f"{venue_label} position drift within tolerance on {ticker}: "
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
            metric_name, float(drift), float(tolerance), msg,
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
    global _last_orphan_skeletons, _last_snoozed_drifts
    global _total_orphan_skeletons

    summary: dict[str, Any] = {
        "skipped": False,
        "drift_count": 0,
        "auto_corrected": 0,
        "replayed_fills": 0,
        "unknown_fills": 0,
        "duplicate_fills": 0,
        "orphan_skeletons": 0,
        "snoozed_drifts": 0,
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

        (
            drift_count, auto_corrected, orphan_skeletons, snoozed_drifts,
        ) = await _diff_positions(positions)
        summary["drift_count"] = drift_count
        summary["auto_corrected"] = auto_corrected
        summary["replayed_fills"] = replayed
        summary["duplicate_fills"] = duplicates
        summary["unknown_fills"] = unknown
        summary["orphan_skeletons"] = orphan_skeletons
        summary["snoozed_drifts"] = snoozed_drifts

        _last_drift_count = drift_count
        _last_auto_corrected = auto_corrected
        _last_missed_fills = replayed
        _last_unknown_fills = unknown
        _last_orphan_skeletons = orphan_skeletons
        _last_snoozed_drifts = snoozed_drifts
        _total_orphan_skeletons += orphan_skeletons
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
    global _last_error
    while not _stop.is_set():
        try:
            await _tick_once()
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # noqa: BLE001 — tick must never kill loop
            _last_error = f"tick crashed: {type(e).__name__}: {e}"
            log.exception("kalshi reconciler tick raised — continuing")
        try:
            await asyncio.wait_for(_stop.wait(), timeout=_interval_seconds())
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            # A stray cancellation of the inner sleep used to silently
            # kill the loop, leaving the dashboard pinned at
            # "kalshi_reconciler DOWN".  Only exit when _stop is
            # actually set; otherwise keep ticking.
            if _stop.is_set():
                return
            log.warning(
                "kalshi reconciler sleep cancelled but stop not set — continuing"
            )
            continue
        else:
            return


async def _supervised_loop() -> None:
    """Self-healing wrapper around :func:`_loop`.

    If ``_loop`` ever returns or raises while ``_stop`` is not set,
    log loudly and restart after a short back-off so the operator
    health card never gets stuck at "kalshi_reconciler DOWN" because
    of a one-shot transient (Kalshi 5xx storm, DB blip, stray
    ``CancelledError``).
    """
    global _last_error
    backoff = 5.0
    while not _stop.is_set():
        try:
            await _loop()
            if _stop.is_set():
                return
            log.warning(
                "kalshi reconciler loop returned unexpectedly — restarting in %ss",
                backoff,
            )
            _last_error = f"loop returned unexpectedly; restarted in {backoff}s"
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # noqa: BLE001 — supervisor catches all
            log.exception(
                "kalshi reconciler loop crashed — restarting in %ss", backoff
            )
            _last_error = f"loop crashed: {type(e).__name__}: {e}"
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
    if not _enabled():
        log.info("kalshi reconciler not started (disabled / paper / no creds)")
        return
    _stop.clear()
    _started_at = time.time()
    _task = asyncio.create_task(_supervised_loop(), name="kalshi-reconciler")
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
