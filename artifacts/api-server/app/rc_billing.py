"""RevenueCat webhook integration.

Mirrors the Stripe pattern in :mod:`billing`:

* The full RevenueCat event payload is stored verbatim in
  ``rc_subscriptions`` (raw mirror), keyed by RC subscription id (or
  app_user_id when no subscription id is present).
* A *derived* row is upserted into the existing ``subscriptions``
  table — the same table our ``/api/me/subscription`` endpoint reads —
  so a Pro/Elite tier obtained via mobile in-app purchase grants the
  same backend access as a tier obtained via Stripe checkout.

Entitlement → tier mapping
--------------------------
RevenueCat models access via *entitlements*. We expect two
entitlements to be configured in the RC dashboard:

* ``pro``   — granted by ``pro_monthly`` / ``pro_annual``
* ``elite`` — granted by ``elite_monthly`` / ``elite_annual``

If neither is active the user is mapped back to the free tier.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, Request, status

from . import billing, db

log = logging.getLogger(__name__)

_ENTITLEMENT_PRO = "pro"
_ENTITLEMENT_ELITE = "elite"

_PROVIDER = "revenuecat"


# ---------------------------------------------------------------------------
# Delivery audit + alert thresholds (task #252)
#
# Every webhook delivery — including auth failures, malformed bodies,
# duplicates and 5xx apply errors — is recorded in `rc_webhook_deliveries`
# so the operator dashboard can show a 24h breakdown and a cron-style
# silence detector can fire when no events have arrived during business
# hours.
# ---------------------------------------------------------------------------

# Status buckets used by the dashboard tile. Keep in sync with the
# admin endpoint and React tile.
DELIVERY_STATUS_OK = "ok"
DELIVERY_STATUS_DUPLICATE = "duplicate"
DELIVERY_STATUS_4XX = "http_4xx"
DELIVERY_STATUS_5XX = "http_5xx"
ALL_DELIVERY_STATUSES = (
    DELIVERY_STATUS_OK,
    DELIVERY_STATUS_DUPLICATE,
    DELIVERY_STATUS_4XX,
    DELIVERY_STATUS_5XX,
)


def _failure_threshold_pct() -> float:
    """5xx percentage over the last hour that trips the alert."""
    raw = os.environ.get("RC_WEBHOOK_5XX_THRESHOLD_PCT", "20")
    try:
        return max(0.0, float(raw))
    except Exception:
        return 20.0


def _silence_threshold_seconds() -> int:
    """Seconds of inbound silence (during business hours) that trips the alert."""
    raw = os.environ.get("RC_WEBHOOK_SILENCE_SECONDS", "3600")
    try:
        return max(60, int(raw))
    except Exception:
        return 3600


def _business_hours_utc() -> tuple[int, int]:
    """Inclusive [start_hour, end_hour) range, in UTC, when silence alerts fire."""
    raw = os.environ.get("RC_WEBHOOK_BUSINESS_HOURS_UTC", "13-23")
    try:
        a, b = raw.split("-", 1)
        start, end = int(a), int(b)
        if 0 <= start < 24 and 0 < end <= 24 and start < end:
            return start, end
    except Exception:
        pass
    return 13, 23  # ~9am–7pm ET default


def _in_business_hours(now: datetime | None = None) -> bool:
    now = now or datetime.now(tz=timezone.utc)
    start, end = _business_hours_utc()
    return start <= now.hour < end


async def _record_delivery(
    *,
    status_bucket: str,
    http_status: int,
    event_id: str | None,
    event_type: str | None,
    error: str | None = None,
) -> None:
    """Append a single delivery audit row. Best-effort; never raises."""
    try:
        async with db.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO rc_webhook_deliveries (
                    status, http_status, event_id, event_type, error
                )
                VALUES ($1, $2, $3, $4, $5)
                """,
                status_bucket,
                int(http_status),
                event_id,
                event_type,
                (error or None),
            )
    except Exception as e:
        log.warning("rc_webhook_deliveries insert failed: %s", e)


async def _hourly_5xx_rate() -> tuple[int, int, float]:
    """Return (total_count, 5xx_count, pct) for the last 60 minutes."""
    try:
        async with db.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    count(*) FILTER (
                        WHERE received_at >= now() - interval '1 hour'
                    ) AS total,
                    count(*) FILTER (
                        WHERE received_at >= now() - interval '1 hour'
                          AND status = $1
                    ) AS errs
                FROM rc_webhook_deliveries
                """,
                DELIVERY_STATUS_5XX,
            )
    except Exception as e:
        log.warning("hourly 5xx rate query failed: %s", e)
        return 0, 0, 0.0
    if not row:
        return 0, 0, 0.0
    total = int(row["total"] or 0)
    errs = int(row["errs"] or 0)
    pct = (100.0 * errs / total) if total else 0.0
    return total, errs, pct


async def _maybe_alert_failure_spike() -> None:
    """Fire the 5xx-spike alert when the per-hour rate crosses the threshold.

    Called after every recorded 5xx outcome. Dedupes through the
    `notification_dedupe` table for an hour so we don't paste the
    operator inbox during an extended outage.
    """
    threshold = _failure_threshold_pct()
    total, errs, pct = await _hourly_5xx_rate()
    # Require at least 5 deliveries in the window so a single failure
    # in a quiet hour doesn't trip a 100% rate.
    if total < 5 or pct < threshold:
        return
    try:
        from .notifications import alerts as _alerts

        ok = await _alerts.dedupe_or_skip(
            "rc-webhook-failures", "5xx-spike", ttl_seconds=3600
        )
        if not ok:
            return
        _alerts.rc_webhook_failure_rate(
            window_minutes=60,
            total=total,
            failures=errs,
            pct=pct,
            threshold_pct=threshold,
        )
    except Exception as e:  # pragma: no cover — alerting must not break the webhook
        log.warning("rc webhook failure alert dispatch failed: %s", e)


def _ms_to_dt(ms: Any) -> datetime | None:
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000.0, tz=timezone.utc)
    except Exception:
        return None


def _entitlement_tier(entitlements: list[str]) -> str:
    if _ENTITLEMENT_ELITE in entitlements:
        return billing.TIER_ELITE
    if _ENTITLEMENT_PRO in entitlements:
        return billing.TIER_PRO
    return billing.TIER_FREE


def _product_tier(product_id: str | None) -> str | None:
    if not product_id:
        return None
    pid = product_id.lower()
    if pid.startswith("elite"):
        return billing.TIER_ELITE
    if pid.startswith("pro"):
        return billing.TIER_PRO
    return None


def _interval_from_product(product_id: str | None) -> str | None:
    if not product_id:
        return None
    pid = product_id.lower()
    if pid.endswith("annual") or pid.endswith("yearly"):
        return "year"
    if pid.endswith("monthly"):
        return "month"
    return None


# Event types that grant access (or refresh an existing grant).
_GRANT_EVENTS = {
    "INITIAL_PURCHASE",
    "RENEWAL",
    "PRODUCT_CHANGE",
    "UNCANCELLATION",
    "TRANSFER",
    "TEMPORARY_ENTITLEMENT_GRANT",
    "NON_RENEWING_PURCHASE",
}
# Auto-renew turned off but the user keeps access until ``expiration_at_ms``.
_CANCEL_EVENTS = {"CANCELLATION"}
# Hard-revoke events: access ends immediately regardless of expiration_at_ms.
_HARD_REVOKE_EVENTS = {
    "EXPIRATION",
    "SUBSCRIPTION_PAUSED",
    "SUBSCRIPTION_EXTENDED",  # treated as state-only; falls through below
}
# Soft-revoke events that should not flip tier=free if entitlement is still
# present in the payload (grace-period style).
_SOFT_EVENTS = {"BILLING_ISSUE"}


async def _is_duplicate_event(event_id: str) -> bool:
    try:
        async with db.acquire() as conn:
            row = await conn.fetchval(
                "SELECT 1 FROM rc_events WHERE event_id=$1", event_id
            )
        return bool(row)
    except Exception as e:
        log.warning("rc_events duplicate check failed: %s", e)
        return False


async def _mark_event_processed(
    event_id: str, event_type: str, payload: dict[str, Any]
) -> None:
    try:
        async with db.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO rc_events (event_id, event_type, payload)
                VALUES ($1, $2, $3::jsonb)
                ON CONFLICT (event_id) DO NOTHING
                """,
                event_id,
                event_type,
                json.dumps(payload),
            )
    except Exception as e:
        log.warning("rc_events insert failed: %s", e)


async def _apply_event(event: dict[str, Any]) -> None:
    event_type = str(event.get("type") or "").upper()
    event_id = str(event.get("id") or "")
    app_user_id = event.get("app_user_id") or event.get("original_app_user_id")
    if not app_user_id:
        log.warning("rc event %s missing app_user_id; ignoring", event_id)
        return

    # ``app_user_id`` is the Clerk user id we passed into Purchases.configure.
    user_id = str(app_user_id)
    product_id = event.get("product_id")
    store = event.get("store")
    expires_at = _ms_to_dt(event.get("expiration_at_ms"))
    rc_sub_id = (
        event.get("subscriber_attributes", {}).get("$subscriptionId", {}).get("value")
        if isinstance(event.get("subscriber_attributes"), dict)
        else None
    )
    key = rc_sub_id or f"{user_id}:{product_id or 'unknown'}"

    # Determine entitlement(s) currently active for this user from the
    # event payload — RevenueCat includes the post-event entitlement
    # state on each webhook delivery.
    entitlement_ids: list[str] = []
    entitlement_block = event.get("entitlement_ids") or event.get("entitlements")
    if isinstance(entitlement_block, list):
        entitlement_ids = [str(x) for x in entitlement_block]
    elif isinstance(entitlement_block, dict):
        entitlement_ids = [str(k) for k in entitlement_block.keys()]
    elif event.get("entitlement_id"):
        entitlement_ids = [str(event["entitlement_id"])]

    # Resolve current entitlement-derived tier from the event payload.
    payload_tier = _entitlement_tier(entitlement_ids)
    if payload_tier == billing.TIER_FREE:
        payload_tier = _product_tier(product_id) or billing.TIER_FREE
    has_active_entitlement = bool(entitlement_ids) or payload_tier != billing.TIER_FREE
    expires_in_future = bool(expires_at and expires_at > datetime.now(tz=timezone.utc))

    cancel_at_period_end = False
    tier: str | None
    sub_status: str | None

    if event_type in _GRANT_EVENTS:
        tier = payload_tier
        sub_status = "active"
    elif event_type in _CANCEL_EVENTS:
        # Auto-renew off — keep access until expiration.
        if expires_in_future and has_active_entitlement:
            tier = payload_tier
            sub_status = "active"
            cancel_at_period_end = True
        else:
            tier = billing.TIER_FREE
            sub_status = "canceled"
    elif event_type in _SOFT_EVENTS:
        # Billing issue — RC keeps the entitlement live during grace.
        if has_active_entitlement:
            tier = payload_tier
            sub_status = "past_due"
        else:
            tier = billing.TIER_FREE
            sub_status = "past_due"
    elif event_type in _HARD_REVOKE_EVENTS:
        if has_active_entitlement and expires_in_future:
            # State change with entitlement still active — keep access.
            tier = payload_tier
            sub_status = "active"
        else:
            tier = billing.TIER_FREE
            sub_status = "inactive"
    else:
        # Unknown event type — record raw, do not touch derived row.
        tier = None
        sub_status = None

    interval = _interval_from_product(product_id)
    primary_entitlement = (
        _ENTITLEMENT_ELITE
        if _ENTITLEMENT_ELITE in entitlement_ids
        else (_ENTITLEMENT_PRO if _ENTITLEMENT_PRO in entitlement_ids else None)
    )

    async with db.acquire() as conn:
        # 1. Raw mirror
        await conn.execute(
            """
            INSERT INTO rc_subscriptions (
                key, app_user_id, user_id, product_id, entitlement, tier,
                event_type, event_id, store, expires_at, raw, updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, NOW())
            ON CONFLICT (key) DO UPDATE SET
                app_user_id = EXCLUDED.app_user_id,
                user_id = EXCLUDED.user_id,
                product_id = EXCLUDED.product_id,
                entitlement = EXCLUDED.entitlement,
                tier = EXCLUDED.tier,
                event_type = EXCLUDED.event_type,
                event_id = EXCLUDED.event_id,
                store = EXCLUDED.store,
                expires_at = EXCLUDED.expires_at,
                raw = EXCLUDED.raw,
                updated_at = NOW()
            """,
            key,
            str(app_user_id),
            user_id,
            product_id,
            primary_entitlement,
            tier,
            event_type,
            event_id,
            store,
            expires_at,
            json.dumps(event),
        )

        # 2. Derived per-user row
        if tier is not None and sub_status is not None:
            await conn.execute(
                """
                INSERT INTO subscriptions (
                    user_id, tier, status,
                    stripe_customer_id, stripe_subscription_id, stripe_price_id,
                    interval, current_period_end, cancel_at_period_end, updated_at
                )
                VALUES ($1, $2, $3, NULL, $4, $5, $6, $7, $8, NOW())
                ON CONFLICT (user_id) DO UPDATE SET
                    tier = EXCLUDED.tier,
                    status = EXCLUDED.status,
                    stripe_subscription_id = EXCLUDED.stripe_subscription_id,
                    stripe_price_id = EXCLUDED.stripe_price_id,
                    interval = COALESCE(EXCLUDED.interval, subscriptions.interval),
                    current_period_end = EXCLUDED.current_period_end,
                    cancel_at_period_end = EXCLUDED.cancel_at_period_end,
                    updated_at = NOW()
                """,
                user_id,
                tier,
                sub_status,
                f"rc:{rc_sub_id}" if rc_sub_id else f"rc:{user_id}:{product_id}",
                f"{_PROVIDER}:{product_id}" if product_id else None,
                interval,
                expires_at,
                cancel_at_period_end,
            )


async def handle_webhook(request: Request) -> dict[str, object]:
    """Validate, dedupe and apply a RevenueCat webhook delivery.

    The webhook is **always** auth-gated. Configure
    ``REVENUECAT_WEBHOOK_AUTH_HEADER`` (matches the ``Authorization`` value
    you set in the RC dashboard webhook config). If the secret is unset
    the endpoint refuses every request — this prevents an unauthenticated
    attacker from forging tier upgrades against any Clerk user id.
    """
    expected_auth = os.environ.get("REVENUECAT_WEBHOOK_AUTH_HEADER")
    if not expected_auth:
        log.error(
            "REVENUECAT_WEBHOOK_AUTH_HEADER is not configured; rejecting"
            " RevenueCat webhook delivery to avoid forged subscription updates."
        )
        await _record_delivery(
            status_bucket=DELIVERY_STATUS_5XX,
            http_status=503,
            event_id=None,
            event_type=None,
            error="auth header env not configured",
        )
        await _maybe_alert_failure_spike()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="revenuecat webhook auth not configured",
        )
    auth = request.headers.get("authorization") or ""
    if auth != expected_auth:
        await _record_delivery(
            status_bucket=DELIVERY_STATUS_4XX,
            http_status=401,
            event_id=None,
            event_type=None,
            error="invalid auth header",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid revenuecat auth header",
        )

    try:
        payload = await request.json()
    except Exception:
        await _record_delivery(
            status_bucket=DELIVERY_STATUS_4XX,
            http_status=400,
            event_id=None,
            event_type=None,
            error="invalid json",
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="invalid json"
        )

    event = payload.get("event") if isinstance(payload, dict) else None
    if not isinstance(event, dict):
        await _record_delivery(
            status_bucket=DELIVERY_STATUS_4XX,
            http_status=400,
            event_id=None,
            event_type=None,
            error="missing event payload",
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="missing event payload",
        )

    event_id = str(event.get("id") or "")
    event_type = str(event.get("type") or "") or None
    if event_id and await _is_duplicate_event(event_id):
        await _record_delivery(
            status_bucket=DELIVERY_STATUS_DUPLICATE,
            http_status=200,
            event_id=event_id,
            event_type=event_type,
        )
        return {"status": "duplicate", "event_id": event_id}

    try:
        await _apply_event(event)
    except Exception as e:
        log.exception("rc webhook apply failed: %s", e)
        await _record_delivery(
            status_bucket=DELIVERY_STATUS_5XX,
            http_status=500,
            event_id=event_id or None,
            event_type=event_type,
            error=str(e)[:500],
        )
        await _maybe_alert_failure_spike()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="webhook handler failed",
        )

    if event_id:
        await _mark_event_processed(event_id, str(event.get("type") or ""), event)

    await _record_delivery(
        status_bucket=DELIVERY_STATUS_OK,
        http_status=200,
        event_id=event_id or None,
        event_type=event_type,
    )
    return {"status": "ok", "event_id": event_id}


# ---------------------------------------------------------------------------
# Stats query (powers /api/admin/rc-webhook/stats)
# ---------------------------------------------------------------------------


async def get_webhook_stats(*, hours: int = 24) -> dict[str, Any]:
    """Aggregate the last `hours` of webhook deliveries for the admin tile."""
    hours = max(1, min(int(hours or 24), 168))
    totals = {s: 0 for s in ALL_DELIVERY_STATUSES}
    hourly: list[dict[str, Any]] = []
    last_event_at: datetime | None = None
    last_5xx_at: datetime | None = None
    try:
        async with db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT date_trunc('hour', received_at) AS hour,
                       status,
                       count(*) AS n
                FROM rc_webhook_deliveries
                WHERE received_at >= now() - make_interval(hours => $1)
                GROUP BY 1, 2
                ORDER BY 1
                """,
                hours,
            )
            for r in rows:
                bucket = str(r["status"])
                n = int(r["n"] or 0)
                totals[bucket] = totals.get(bucket, 0) + n
                hourly.append(
                    {
                        "hour": r["hour"].isoformat() if r["hour"] else None,
                        "status": bucket,
                        "count": n,
                    }
                )
            last_event_at = await conn.fetchval(
                "SELECT max(received_at) FROM rc_webhook_deliveries"
            )
            last_5xx_at = await conn.fetchval(
                "SELECT max(received_at) FROM rc_webhook_deliveries WHERE status = $1",
                DELIVERY_STATUS_5XX,
            )
    except Exception as e:
        log.warning("rc webhook stats query failed: %s", e)

    _, errs_60m, pct_60m = await _hourly_5xx_rate()
    return {
        "hours": hours,
        "totals": totals,
        "hourly": hourly,
        "last_event_at": last_event_at.isoformat() if last_event_at else None,
        "last_5xx_at": last_5xx_at.isoformat() if last_5xx_at else None,
        "alert": {
            "failure_threshold_pct": _failure_threshold_pct(),
            "failures_last_hour": errs_60m,
            "failure_rate_pct": pct_60m,
            "silence_threshold_seconds": _silence_threshold_seconds(),
            "business_hours_utc": list(_business_hours_utc()),
            "in_business_hours": _in_business_hours(),
        },
    }


# ---------------------------------------------------------------------------
# Silence detector (cron-style background loop)
# ---------------------------------------------------------------------------


_silence_task: asyncio.Task[None] | None = None


async def _silence_check_once() -> None:
    if not _in_business_hours():
        return
    threshold = _silence_threshold_seconds()
    try:
        async with db.acquire() as conn:
            last = await conn.fetchval(
                "SELECT max(received_at) FROM rc_webhook_deliveries"
            )
    except Exception as e:
        log.warning("rc webhook silence query failed: %s", e)
        return
    now = datetime.now(tz=timezone.utc)
    if last is None:
        silence_seconds: float = float(threshold + 1)
    else:
        silence_seconds = (now - last).total_seconds()
    if silence_seconds < threshold:
        return
    try:
        from .notifications import alerts as _alerts

        ok = await _alerts.dedupe_or_skip(
            "rc-webhook-silence", "no-events", ttl_seconds=threshold
        )
        if not ok:
            return
        _alerts.rc_webhook_silence(
            silence_seconds=int(silence_seconds),
            threshold_seconds=int(threshold),
            last_event_at=last.isoformat() if last else None,
        )
    except Exception as e:  # pragma: no cover
        log.warning("rc webhook silence alert dispatch failed: %s", e)


async def _silence_loop(interval_seconds: int = 600) -> None:
    while True:
        try:
            await _silence_check_once()
        except Exception as e:  # pragma: no cover
            log.warning("rc webhook silence loop iteration failed: %s", e)
        await asyncio.sleep(interval_seconds)


async def start() -> None:
    """Start the background silence detector. Idempotent."""
    global _silence_task
    if _silence_task and not _silence_task.done():
        return
    _silence_task = asyncio.create_task(
        _silence_loop(), name="rc-webhook-silence-loop"
    )


async def stop() -> None:
    global _silence_task
    if _silence_task is None:
        return
    _silence_task.cancel()
    try:
        await _silence_task
    except (asyncio.CancelledError, Exception):
        pass
    _silence_task = None
