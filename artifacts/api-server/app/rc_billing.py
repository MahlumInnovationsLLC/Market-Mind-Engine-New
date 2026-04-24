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


def _delivery_retention_days() -> int:
    """How many days of `rc_webhook_deliveries` history to keep.

    The dashboard tile only ever queries the last 24h and the silence
    detector only reads ``max(received_at)``, so anything older than
    the retention window is dead weight that would otherwise grow the
    audit table unboundedly. Default 30 days — comfortably wider than
    any rolling-window report we'd ever want to run off this table
    while still bounding growth. Refuse 0/negative so a typo can't
    immediately wipe the entire audit history.
    """
    raw = os.environ.get("RC_WEBHOOK_DELIVERY_RETENTION_DAYS")
    try:
        v = int(float(raw)) if raw is not None else 30
    except (TypeError, ValueError):
        v = 30
    return max(1, v)


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
# Silence detector + delivery audit prune (cron-style background loop)
#
# A single asyncio task runs both:
#   * `_silence_check_once` — fires an alert when no webhook events have
#     arrived during business hours.
#   * `_prune_deliveries_once` — deletes `rc_webhook_deliveries` rows
#     older than `RC_WEBHOOK_DELIVERY_RETENTION_DAYS` (default 30) so
#     the audit table stays bounded the way the alert dedupe table now
#     does.
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


async def _prune_deliveries_once() -> int:
    """Delete `rc_webhook_deliveries` rows older than the retention window.

    Co-located with the silence detector because both run on the same
    cadence and both only need ``rc_webhook_deliveries`` to be cheap to
    query. The dashboard tile only reads the last 24h and the silence
    detector only reads ``max(received_at)``, so dropping anything past
    the retention horizon has no effect on either consumer.

    Returns the number of rows deleted (0 on DB error so a flaky
    connection can't make the loop look like it's silently winning).
    """
    days = _delivery_retention_days()
    try:
        async with db.acquire() as conn:
            status_str = await conn.execute(
                """
                DELETE FROM rc_webhook_deliveries
                WHERE received_at < now() - make_interval(days => $1)
                """,
                days,
            )
    except Exception as e:
        log.warning("rc_webhook_deliveries prune failed: %s", e)
        return 0
    deleted = 0
    try:
        parts = (status_str or "").split()
        if len(parts) >= 2 and parts[0].upper() == "DELETE":
            deleted = int(parts[1])
    except (ValueError, AttributeError, TypeError):
        deleted = 0
    if deleted:
        log.info(
            "rc_webhook_deliveries prune: deleted %d row(s) older than %d day(s)",
            deleted,
            days,
        )
    return deleted


async def _silence_loop(interval_seconds: int = 600) -> None:
    while True:
        try:
            await _silence_check_once()
        except Exception as e:  # pragma: no cover
            log.warning("rc webhook silence loop iteration failed: %s", e)
        try:
            await _prune_deliveries_once()
        except Exception as e:  # pragma: no cover
            log.warning("rc webhook delivery prune iteration failed: %s", e)
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


# ---------------------------------------------------------------------------
# Operator-driven retry of stuck deliveries (task #283)
#
# When `_apply_event` throws repeatedly for the same event_id the operator
# would otherwise have to grep the raw payload out of logs and re-POST it
# against the webhook URL by hand. We surface the recent failures on the
# admin dashboard tile and let the operator re-run them in-place.
# ---------------------------------------------------------------------------


async def list_failed_events(limit: int = 20) -> list[dict[str, Any]]:
    """Recent webhook events whose *latest* delivery was a 5xx.

    Joined back to ``rc_subscriptions`` so the UI can tell whether the
    stored raw payload is available — the retry path can only re-run an
    event when we have the original payload to feed back into
    ``_apply_event``. ``rc_subscriptions.raw`` is written first inside
    ``_apply_event`` so most apply-time failures still leave a raw row.
    """
    limit = max(1, min(int(limit or 20), 100))
    sql = """
        WITH per_event AS (
            SELECT event_id, max(received_at) AS last_at, count(*) AS attempts
            FROM rc_webhook_deliveries
            WHERE event_id IS NOT NULL
              AND received_at >= now() - interval '7 days'
            GROUP BY event_id
        ),
        latest AS (
            SELECT DISTINCT ON (event_id)
                   event_id, status, http_status, event_type, received_at, error
            FROM rc_webhook_deliveries
            WHERE event_id IS NOT NULL
              AND received_at >= now() - interval '7 days'
            ORDER BY event_id, received_at DESC
        )
        SELECT l.event_id,
               l.event_type,
               l.received_at AS last_failed_at,
               p.attempts,
               l.error AS last_error,
               (s.raw IS NOT NULL) AS raw_available,
               s.user_id,
               s.app_user_id
        FROM latest l
        JOIN per_event p USING (event_id)
        LEFT JOIN LATERAL (
            SELECT raw, user_id, app_user_id
            FROM rc_subscriptions
            WHERE event_id = l.event_id
            ORDER BY updated_at DESC
            LIMIT 1
        ) s ON TRUE
        WHERE l.status = $1
        ORDER BY l.received_at DESC
        LIMIT $2
    """
    out: list[dict[str, Any]] = []
    try:
        async with db.acquire() as conn:
            rows = await conn.fetch(sql, DELIVERY_STATUS_5XX, limit)
            for r in rows:
                out.append(
                    {
                        "event_id": r["event_id"],
                        "event_type": r["event_type"],
                        "last_failed_at": (
                            r["last_failed_at"].isoformat()
                            if r["last_failed_at"]
                            else None
                        ),
                        "attempts": int(r["attempts"] or 0),
                        "last_error": r["last_error"],
                        "raw_available": bool(r["raw_available"]),
                        "user_id": r["user_id"],
                        "app_user_id": r["app_user_id"],
                    }
                )
    except Exception as e:
        log.warning("rc failed-events query failed: %s", e)
    return out


async def _load_raw_event(event_id: str) -> tuple[dict[str, Any] | None, str | None]:
    """Return ``(event_payload, user_id)`` for a stored event, or ``(None, None)``."""
    try:
        async with db.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT raw, user_id
                FROM rc_subscriptions
                WHERE event_id = $1
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                event_id,
            )
    except Exception as e:
        log.warning("rc load raw event %s failed: %s", event_id, e)
        return None, None
    if not row:
        return None, None
    raw = row["raw"]
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except Exception as e:
            log.warning("rc raw payload for %s is not json: %s", event_id, e)
            return None, row.get("user_id")
    elif isinstance(raw, dict):
        payload = raw
    else:
        return None, row.get("user_id")
    return payload, row.get("user_id")


async def retry_event(event_id: str) -> dict[str, Any]:
    """Re-run ``_apply_event`` for a previously-failed delivery.

    Returns ``{"status": "ok"|"failed"|"missing", ...}``. The caller is
    responsible for inserting the admin audit row — this function only
    touches the webhook plumbing (raw mirror, derived row, delivery
    audit, idempotency table).
    """
    event_id = (event_id or "").strip()
    if not event_id:
        return {"status": "missing", "error": "event_id required"}

    payload, user_id = await _load_raw_event(event_id)
    if payload is None:
        return {
            "status": "missing",
            "error": "no stored payload for event_id",
            "user_id": user_id,
        }

    event_type = str(payload.get("type") or "") or None
    try:
        await _apply_event(payload)
    except Exception as e:
        log.exception("rc webhook retry apply failed for %s: %s", event_id, e)
        await _record_delivery(
            status_bucket=DELIVERY_STATUS_5XX,
            http_status=500,
            event_id=event_id,
            event_type=event_type,
            error=f"retry: {str(e)[:480]}",
        )
        await _maybe_alert_failure_spike()
        return {
            "status": "failed",
            "error": str(e)[:500],
            "event_id": event_id,
            "event_type": event_type,
            "user_id": user_id,
        }

    await _mark_event_processed(event_id, event_type or "", payload)
    await _record_delivery(
        status_bucket=DELIVERY_STATUS_OK,
        http_status=200,
        event_id=event_id,
        event_type=event_type,
        error="retry: applied",
    )
    return {
        "status": "ok",
        "event_id": event_id,
        "event_type": event_type,
        "user_id": user_id,
    }
