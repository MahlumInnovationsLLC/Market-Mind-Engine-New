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

_schema_ready = False


async def ensure_schema() -> None:
    """Create the raw RC mirror table on first use."""
    global _schema_ready
    if _schema_ready:
        return
    async with db.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rc_subscriptions (
                key TEXT PRIMARY KEY,
                app_user_id TEXT,
                user_id TEXT,
                product_id TEXT,
                entitlement TEXT,
                tier TEXT,
                event_type TEXT,
                event_id TEXT,
                store TEXT,
                expires_at TIMESTAMPTZ,
                raw JSONB,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS rc_subscriptions_user_idx
            ON rc_subscriptions (user_id)
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rc_events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                payload JSONB
            )
            """
        )
    _schema_ready = True


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

    await ensure_schema()
    await billing.ensure_schema()
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
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="revenuecat webhook auth not configured",
        )
    auth = request.headers.get("authorization") or ""
    if auth != expected_auth:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid revenuecat auth header",
        )

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="invalid json"
        )

    event = payload.get("event") if isinstance(payload, dict) else None
    if not isinstance(event, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="missing event payload",
        )

    event_id = str(event.get("id") or "")
    if event_id and await _is_duplicate_event(event_id):
        return {"status": "duplicate", "event_id": event_id}

    try:
        await _apply_event(event)
    except Exception as e:
        log.exception("rc webhook apply failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="webhook handler failed",
        )

    if event_id:
        await _mark_event_processed(event_id, str(event.get("type") or ""), event)

    return {"status": "ok", "event_id": event_id}
