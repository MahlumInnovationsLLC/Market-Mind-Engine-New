"""Stripe billing + tier gating.

Tier model
----------
- ``free``    — default for any signed-in user with no active subscription.
- ``pro``     — paid monthly/annual; unlocks live signals, full swarm
                transcripts, and manual cycle execution.
- ``elite``   — superset of pro; reserved for future entitlements.
- ``founder`` — full access, billing bypassed.  Resolved from the
                operator allow-list (``OPERATOR_ALLOWED_EMAILS``) and
                the explicit ``STRIPE_FOUNDER_USERS`` env var.

Source of truth
---------------
The ``subscriptions`` table is the only thing we ever read from at
runtime.  Every Stripe webhook event id is recorded in
``stripe_events`` for idempotency and replay safety.  Stripe Checkout
+ Customer Portal are the only mutation paths users see; the
webhook handler does the actual writes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

import stripe
from fastapi import Depends, HTTPException, Request

from . import db
from .auth import AuthedUser, _allowed_emails, _email_from_claims, _fetch_clerk_user, _email_from_clerk_user, require_auth


log = logging.getLogger("axiom.billing")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


TIER_FREE = "free"
TIER_PRO = "pro"
TIER_ELITE = "elite"
TIER_FOUNDER = "founder"
ALL_TIERS = (TIER_FREE, TIER_PRO, TIER_ELITE, TIER_FOUNDER)

# Numeric ranking for "is this user at least X" checks.
_TIER_RANK = {
    TIER_FREE: 0,
    TIER_PRO: 1,
    TIER_ELITE: 2,
    TIER_FOUNDER: 3,
}

# Statuses Stripe reports that we treat as "the user can use the product".
_ACTIVE_STATUSES = {"active", "trialing", "past_due"}


def stripe_configured() -> bool:
    return bool(os.environ.get("STRIPE_SECRET_KEY"))


def _stripe_client() -> Any:
    """Return the configured Stripe SDK module (raises 503 if absent)."""
    key = os.environ.get("STRIPE_SECRET_KEY")
    if not key:
        raise HTTPException(status_code=503, detail="stripe is not configured")
    stripe.api_key = key
    return stripe


def _founder_emails() -> set[str]:
    extras = {
        e.strip().lower()
        for e in (os.environ.get("STRIPE_FOUNDER_USERS") or "").split(",")
        if e.strip()
    }
    return _allowed_emails() | extras


@dataclass(frozen=True)
class PriceConfig:
    tier: str
    interval: str  # "month" | "year"
    env_key: str
    price_id: str | None


def _price_catalog() -> list[PriceConfig]:
    """The four supported price IDs, sourced from environment variables.

    Prices missing from the environment render as "coming soon" on the
    pricing page rather than crashing.  This lets the page be useful
    immediately, with the operator filling in the ``price_…`` IDs after
    creating products in Stripe.
    """
    keys = [
        (TIER_PRO, "month", "STRIPE_PRICE_PRO_MONTHLY"),
        (TIER_PRO, "year", "STRIPE_PRICE_PRO_ANNUAL"),
        (TIER_ELITE, "month", "STRIPE_PRICE_ELITE_MONTHLY"),
        (TIER_ELITE, "year", "STRIPE_PRICE_ELITE_ANNUAL"),
    ]
    out: list[PriceConfig] = []
    for tier, interval, env_key in keys:
        out.append(
            PriceConfig(
                tier=tier,
                interval=interval,
                env_key=env_key,
                price_id=(os.environ.get(env_key) or "").strip() or None,
            )
        )
    return out


def _resolve_price(price_id: str) -> PriceConfig | None:
    for cfg in _price_catalog():
        if cfg.price_id == price_id:
            return cfg
    return None


# ---------------------------------------------------------------------------
# Database schema bootstrap (idempotent)
# ---------------------------------------------------------------------------


_SCHEMA_LOCK = asyncio.Lock()
_schema_ready = False


async def ensure_schema() -> None:
    """Schema is now owned by the Alembic baseline migration; this is a
    no-op kept for call-site compatibility with the rest of the app."""
    global _schema_ready
    _schema_ready = True


# ---------------------------------------------------------------------------
# Tier resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UserSubscription:
    user_id: str
    email: str | None
    tier: str
    status: str
    stripe_customer_id: str | None
    stripe_subscription_id: str | None
    stripe_price_id: str | None
    interval: str | None
    current_period_end: datetime | None
    cancel_at_period_end: bool
    is_founder: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "email": self.email,
            "tier": self.tier,
            "status": self.status,
            "is_founder": self.is_founder,
            "stripe_customer_id": self.stripe_customer_id,
            "stripe_subscription_id": self.stripe_subscription_id,
            "stripe_price_id": self.stripe_price_id,
            "interval": self.interval,
            "current_period_end": (
                self.current_period_end.isoformat()
                if self.current_period_end
                else None
            ),
            "cancel_at_period_end": self.cancel_at_period_end,
        }


async def _resolve_email(user: AuthedUser) -> str | None:
    email = _email_from_claims(user.claims)
    if email:
        return email
    data = await _fetch_clerk_user(user.user_id)
    return _email_from_clerk_user(data)


async def get_user_subscription(user: AuthedUser) -> UserSubscription:
    """Return the caller's effective subscription record.

    Founder bypass is checked first so the founder/operator console
    works even if the ``subscriptions`` table is empty.
    """
    await ensure_schema()
    email = await _resolve_email(user)
    is_founder = bool(email and email in _founder_emails())

    row: dict[str, Any] | None = None
    try:
        async with db.acquire() as conn:
            r = await conn.fetchrow(
                "SELECT * FROM subscriptions WHERE user_id=$1", user.user_id
            )
            if r:
                row = dict(r)
    except Exception as e:
        log.warning("subscription lookup failed: %s", e)

    tier = TIER_FREE
    status = "inactive"
    cust = sub_id = price_id = interval = None
    period_end: datetime | None = None
    cancel_at_period_end = False
    if row:
        tier = str(row.get("tier") or TIER_FREE)
        status = str(row.get("status") or "inactive")
        cust = row.get("stripe_customer_id")
        sub_id = row.get("stripe_subscription_id")
        price_id = row.get("stripe_price_id")
        interval = row.get("interval")
        period_end = row.get("current_period_end")
        cancel_at_period_end = bool(row.get("cancel_at_period_end") or False)

    # Treat tier as effective only if the subscription is in an active
    # status — otherwise downgrade to free.  Founder always wins.
    if tier in (TIER_PRO, TIER_ELITE) and status not in _ACTIVE_STATUSES:
        tier = TIER_FREE

    if is_founder:
        tier = TIER_FOUNDER
        status = "active"

    return UserSubscription(
        user_id=user.user_id,
        email=email,
        tier=tier,
        status=status,
        stripe_customer_id=cust,
        stripe_subscription_id=sub_id,
        stripe_price_id=price_id,
        interval=interval,
        current_period_end=period_end,
        cancel_at_period_end=cancel_at_period_end,
        is_founder=is_founder,
    )


def require_tier(min_tier: str):
    """FastAPI dependency factory: 403 if the caller is below ``min_tier``.

    Usage::

        @app.post("/api/swarm/debate")
        async def swarm_debate(
            body: ...,
            sub: UserSubscription = Depends(require_tier("pro")),
        ): ...
    """
    if min_tier not in _TIER_RANK:
        raise ValueError(f"unknown tier {min_tier!r}")

    async def _dep(user: AuthedUser = Depends(require_auth)) -> UserSubscription:
        sub = await get_user_subscription(user)
        if _TIER_RANK[sub.tier] < _TIER_RANK[min_tier]:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "tier_required",
                    "required_tier": min_tier,
                    "current_tier": sub.tier,
                },
            )
        return sub

    return _dep


# ---------------------------------------------------------------------------
# Stripe Checkout + Customer Portal
# ---------------------------------------------------------------------------


def _public_base_url() -> str:
    """Return the externally-visible base URL for redirect URLs.

    Prefers the explicit ``AXIOM_PUBLIC_BASE_URL`` env var; falls back
    to the Replit dev domain.  Both are normalised to no-trailing-slash.
    """
    explicit = (os.environ.get("AXIOM_PUBLIC_BASE_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    dev = (os.environ.get("REPLIT_DEV_DOMAIN") or "").strip()
    if dev:
        return f"https://{dev}".rstrip("/")
    return "http://localhost:3000"


async def _ensure_customer(user: AuthedUser, sub: UserSubscription) -> str:
    """Return the user's Stripe customer id, creating one if needed.

    Race-safe: takes a row lock on the user's ``subscriptions`` row and
    re-checks for an existing ``stripe_customer_id`` inside the
    transaction before calling Stripe.  Without this, two concurrent
    checkout requests from the same user would create two Stripe
    customers and orphan one of them.
    """
    if sub.stripe_customer_id:
        return sub.stripe_customer_id
    await ensure_schema()
    async with db.acquire() as conn:
        async with conn.transaction():
            # Ensure a row exists so SELECT FOR UPDATE can lock it.
            await conn.execute(
                """
                INSERT INTO subscriptions (user_id, email, tier, status)
                VALUES ($1, $2, 'free', 'inactive')
                ON CONFLICT (user_id) DO NOTHING
                """,
                user.user_id,
                sub.email,
            )
            existing = await conn.fetchval(
                "SELECT stripe_customer_id FROM subscriptions WHERE user_id=$1 FOR UPDATE",
                user.user_id,
            )
            if existing:
                return existing
            sclient = _stripe_client()
            try:
                cust = sclient.Customer.create(
                    email=sub.email or None,
                    metadata={"clerk_user_id": user.user_id},
                    # Idempotency key tied to the Clerk user id ensures that
                    # if Stripe receives two parallel creates we still end up
                    # with a single customer.
                    idempotency_key=f"axiom-customer:{user.user_id}",
                )
            except stripe.StripeError as e:
                log.exception("stripe customer create failed")
                raise HTTPException(status_code=502, detail=f"stripe error: {e}")
            customer_id = cust["id"]
            await conn.execute(
                """
                UPDATE subscriptions
                SET stripe_customer_id = $2,
                    email = COALESCE($3, email),
                    updated_at = NOW()
                WHERE user_id = $1
                """,
                user.user_id,
                customer_id,
                sub.email,
            )
            return customer_id


async def create_checkout_session(
    user: AuthedUser, price_id: str
) -> dict[str, Any]:
    if not price_id:
        raise HTTPException(status_code=400, detail="price_id is required")
    cfg = _resolve_price(price_id)
    if cfg is None:
        raise HTTPException(status_code=400, detail="unknown price_id")
    sclient = _stripe_client()
    sub = await get_user_subscription(user)
    if sub.is_founder:
        # Founders never need to check out — and we don't want to
        # create accidental Stripe customers for them.
        raise HTTPException(
            status_code=409,
            detail="founder accounts do not require a subscription",
        )
    customer_id = await _ensure_customer(user, sub)
    base = _public_base_url()
    success = f"{base}/billing?checkout=success&session_id={{CHECKOUT_SESSION_ID}}"
    cancel = f"{base}/pricing?checkout=cancelled"
    try:
        session = sclient.checkout.Session.create(
            mode="subscription",
            customer=customer_id,
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=success,
            cancel_url=cancel,
            client_reference_id=user.user_id,
            metadata={
                "clerk_user_id": user.user_id,
                "tier": cfg.tier,
                "interval": cfg.interval,
            },
            subscription_data={
                "metadata": {
                    "clerk_user_id": user.user_id,
                    "tier": cfg.tier,
                    "interval": cfg.interval,
                },
            },
        )
    except stripe.StripeError as e:
        log.exception("stripe checkout create failed")
        raise HTTPException(status_code=502, detail=f"stripe error: {e}")
    return {"id": session["id"], "url": session["url"]}


async def create_portal_session(user: AuthedUser) -> dict[str, Any]:
    sub = await get_user_subscription(user)
    if sub.is_founder:
        raise HTTPException(
            status_code=409,
            detail="founder accounts do not require a portal session",
        )
    if not sub.stripe_customer_id:
        raise HTTPException(
            status_code=409,
            detail="no Stripe customer yet — start a checkout first",
        )
    sclient = _stripe_client()
    base = _public_base_url()
    try:
        session = sclient.billing_portal.Session.create(
            customer=sub.stripe_customer_id,
            return_url=f"{base}/billing",
        )
    except stripe.StripeError as e:
        log.exception("stripe portal create failed")
        raise HTTPException(status_code=502, detail=f"stripe error: {e}")
    return {"url": session["url"]}


# ---------------------------------------------------------------------------
# Pricing catalogue (for the public /pricing page)
# ---------------------------------------------------------------------------


def public_pricing_catalog() -> dict[str, Any]:
    """Static tier metadata + the configured (or missing) price IDs.

    Kept here so the web app doesn't need to encode pricing copy.
    """
    prices = _price_catalog()

    def _for(tier: str, interval: str) -> dict[str, Any] | None:
        for p in prices:
            if p.tier == tier and p.interval == interval:
                return {"price_id": p.price_id, "configured": p.price_id is not None}
        return None

    return {
        "tiers": [
            {
                "id": TIER_FREE,
                "name": "Free",
                "tagline": "Watch the operator console at a delay.",
                "monthly_usd": 0,
                "annual_usd": 0,
                "features": [
                    "Read-only dashboard (15-minute delay)",
                    "Daily settlement summaries",
                    "Top-line PnL only",
                ],
                "monthly": None,
                "annual": None,
                "cta": "Start free",
            },
            {
                "id": TIER_PRO,
                "name": "Pro",
                "tagline": "Live signals + full swarm transcripts.",
                "monthly_usd": 49,
                "annual_usd": 490,
                "features": [
                    "Real-time pipeline monitor",
                    "Full LLM swarm transcripts (all 6 personas)",
                    "Manual cycle execution",
                    "Email alerts on settlement drift",
                ],
                "monthly": _for(TIER_PRO, "month"),
                "annual": _for(TIER_PRO, "year"),
                "cta": "Upgrade to Pro",
                "highlight": True,
            },
            {
                "id": TIER_ELITE,
                "name": "Elite",
                "tagline": "Everything in Pro plus institutional features.",
                "monthly_usd": 199,
                "annual_usd": 1990,
                "features": [
                    "Everything in Pro",
                    "Custom risk-gate overrides",
                    "Per-vertical Brier alert thresholds",
                    "Priority support channel",
                ],
                "monthly": _for(TIER_ELITE, "month"),
                "annual": _for(TIER_ELITE, "year"),
                "cta": "Upgrade to Elite",
            },
            {
                "id": TIER_FOUNDER,
                "name": "Founder",
                "tagline": "Operator allow-list — billing bypassed.",
                "monthly_usd": 0,
                "annual_usd": 0,
                "features": [
                    "Full operator console access",
                    "All trading + settlement endpoints",
                    "No payment required",
                ],
                "monthly": None,
                "annual": None,
                "cta": "Contact us",
            },
        ],
        "publishable_key_present": bool(
            os.environ.get("VITE_STRIPE_PUBLISHABLE_KEY")
        ),
        "stripe_configured": stripe_configured(),
    }


# ---------------------------------------------------------------------------
# Webhook handler
# ---------------------------------------------------------------------------


async def _is_duplicate_event(event_id: str) -> bool:
    """Return True if this event id has already been successfully processed."""
    await ensure_schema()
    try:
        async with db.acquire() as conn:
            row = await conn.fetchval(
                "SELECT 1 FROM stripe_events WHERE event_id=$1",
                event_id,
            )
        return bool(row)
    except Exception as e:
        log.warning("stripe_events duplicate check failed: %s", e)
        return False  # Better to reprocess than to silently drop.


async def _mark_event_processed(
    event_id: str, event_type: str, payload: dict[str, Any]
) -> None:
    """Record an event id ONLY after its handler succeeded.

    On unique-violation we silently no-op (a parallel webhook won the
    race and processed the same event).
    """
    await ensure_schema()
    try:
        async with db.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO stripe_events (event_id, event_type, payload)
                VALUES ($1, $2, $3::jsonb)
                ON CONFLICT (event_id) DO NOTHING
                """,
                event_id,
                event_type,
                json.dumps(payload),
            )
    except Exception as e:
        log.warning("stripe_events insert failed: %s", e)


def _ts_to_dt(ts: Any) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except Exception:
        return None


def _period_end_from_subscription(sub: dict[str, Any]) -> datetime | None:
    """Read ``current_period_end`` with a fallback to subscription items.

    Stripe's newer API versions report the period end on each
    subscription item rather than at the top level, so we look in both
    places to stay forward-compatible.
    """
    top = _ts_to_dt(sub.get("current_period_end"))
    if top:
        return top
    items = (sub.get("items") or {}).get("data") or []
    for item in items:
        if not isinstance(item, dict):
            continue
        candidate = _ts_to_dt(item.get("current_period_end"))
        if candidate:
            return candidate
    return None


def _interval_from_subscription(sub: dict[str, Any]) -> str | None:
    items = (sub.get("items") or {}).get("data") or []
    for item in items:
        plan = item.get("plan") or item.get("price") or {}
        recurring = plan.get("recurring") if isinstance(plan, dict) else None
        if isinstance(recurring, dict):
            return recurring.get("interval")
        interval = plan.get("interval") if isinstance(plan, dict) else None
        if interval:
            return interval
    return None


def _price_id_from_subscription(sub: dict[str, Any]) -> str | None:
    items = (sub.get("items") or {}).get("data") or []
    for item in items:
        price = item.get("price") or item.get("plan") or {}
        if isinstance(price, dict) and price.get("id"):
            return price["id"]
    return None


def _tier_for_price(price_id: str | None) -> str:
    if not price_id:
        return TIER_FREE
    cfg = _resolve_price(price_id)
    return cfg.tier if cfg else TIER_PRO  # default unknown paid prices to pro


async def _apply_subscription(sub_obj: dict[str, Any]) -> None:
    """Upsert a Stripe subscription payload into ``subscriptions``."""
    customer_id = sub_obj.get("customer")
    if isinstance(customer_id, dict):
        customer_id = customer_id.get("id")
    if not customer_id:
        log.warning("subscription event missing customer id")
        return
    metadata = sub_obj.get("metadata") or {}
    user_id = metadata.get("clerk_user_id")
    status = str(sub_obj.get("status") or "inactive")
    price_id = _price_id_from_subscription(sub_obj)
    interval = _interval_from_subscription(sub_obj)
    period_end = _period_end_from_subscription(sub_obj)
    cancel_at_period_end = bool(sub_obj.get("cancel_at_period_end") or False)
    sub_id = sub_obj.get("id")

    if status == "canceled":
        tier = TIER_FREE
    elif status in _ACTIVE_STATUSES:
        tier = _tier_for_price(price_id)
    else:
        tier = TIER_FREE

    await ensure_schema()
    # Resolve the prior state so we can fire transition-based alerts
    # (tier change, will-cancel, cancelled) without spamming on every
    # webhook redelivery.  Read before the upsert so the comparison is
    # meaningful.
    prev_tier: str | None = None
    prev_status: str | None = None
    prev_cap: bool | None = None
    prev_email: str | None = None
    if user_id:
        try:
            row = await db.fetchrow(
                "SELECT tier, status, cancel_at_period_end, email "
                "FROM subscriptions WHERE user_id=$1",
                user_id,
            )
            if row:
                prev_tier = (row["tier"] or None)
                prev_status = (row["status"] or None)
                prev_cap = bool(row["cancel_at_period_end"]) if row["cancel_at_period_end"] is not None else None
                prev_email = row["email"]
        except Exception:
            pass
    elif customer_id:
        try:
            row = await db.fetchrow(
                "SELECT user_id, tier, status, cancel_at_period_end, email "
                "FROM subscriptions WHERE stripe_customer_id=$1",
                customer_id,
            )
            if row:
                user_id = row["user_id"] or user_id
                prev_tier = (row["tier"] or None)
                prev_status = (row["status"] or None)
                prev_cap = bool(row["cancel_at_period_end"]) if row["cancel_at_period_end"] is not None else None
                prev_email = row["email"]
        except Exception:
            pass
    async with db.acquire() as conn:
        # 1. Raw mirror — keep the unmodified Stripe payload.
        if sub_id:
            await conn.execute(
                """
                INSERT INTO stripe_subscriptions (
                    stripe_subscription_id, stripe_customer_id, user_id,
                    status, stripe_price_id, interval,
                    current_period_end, cancel_at_period_end, raw, updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, NOW())
                ON CONFLICT (stripe_subscription_id) DO UPDATE SET
                    stripe_customer_id = EXCLUDED.stripe_customer_id,
                    user_id = COALESCE(EXCLUDED.user_id, stripe_subscriptions.user_id),
                    status = EXCLUDED.status,
                    stripe_price_id = EXCLUDED.stripe_price_id,
                    interval = EXCLUDED.interval,
                    current_period_end = EXCLUDED.current_period_end,
                    cancel_at_period_end = EXCLUDED.cancel_at_period_end,
                    raw = EXCLUDED.raw,
                    updated_at = NOW()
                """,
                sub_id,
                customer_id,
                user_id,
                status,
                price_id,
                interval,
                period_end,
                cancel_at_period_end,
                json.dumps(sub_obj),
            )

        # 2. Derived per-user row — the runtime source of truth that
        # ``get_user_subscription`` reads.
        if user_id:
            await conn.execute(
                """
                INSERT INTO subscriptions (
                    user_id, tier, status, stripe_customer_id,
                    stripe_subscription_id, stripe_price_id, interval,
                    current_period_end, cancel_at_period_end, updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW())
                ON CONFLICT (user_id) DO UPDATE SET
                    tier = EXCLUDED.tier,
                    status = EXCLUDED.status,
                    stripe_customer_id = EXCLUDED.stripe_customer_id,
                    stripe_subscription_id = EXCLUDED.stripe_subscription_id,
                    stripe_price_id = EXCLUDED.stripe_price_id,
                    interval = EXCLUDED.interval,
                    current_period_end = EXCLUDED.current_period_end,
                    cancel_at_period_end = EXCLUDED.cancel_at_period_end,
                    updated_at = NOW()
                """,
                user_id,
                tier,
                status,
                customer_id,
                sub_id,
                price_id,
                interval,
                period_end,
                cancel_at_period_end,
            )
        else:
            # Fall back to matching by Stripe customer id, which we
            # populate at checkout-create time.
            await conn.execute(
                """
                UPDATE subscriptions SET
                    tier = $2,
                    status = $3,
                    stripe_subscription_id = $4,
                    stripe_price_id = $5,
                    interval = $6,
                    current_period_end = $7,
                    cancel_at_period_end = $8,
                    updated_at = NOW()
                WHERE stripe_customer_id = $1
                """,
                customer_id,
                tier,
                status,
                sub_id,
                price_id,
                interval,
                period_end,
                cancel_at_period_end,
            )

    # Tier-change alert — fire on any real transition, including the
    # first-ever paid subscription where prev_tier is None.  We treat
    # `None` as the "free" baseline so a None→free webhook (e.g. a
    # cancelled-then-stale event) doesn't spam, but None→pro/elite
    # *does* alert.  Comparison is against the prev_tier we read above,
    # before the upsert.
    _prev_normalized = prev_tier or TIER_FREE
    if user_id and _prev_normalized != tier:
        try:
            from .notifications import alerts as _alerts
            # User email is best-effort; the operator can look up the
            # Clerk user_id if needed.
            user_email = None
            try:
                meta_email = (sub_obj.get("metadata") or {}).get("email")
                if isinstance(meta_email, str):
                    user_email = meta_email
            except Exception:
                user_email = None
            # Persistent dedupe: Stripe will re-deliver the same
            # `customer.subscription.updated` webhook on retry; without
            # this guard we'd email the operator twice for one real
            # transition.  Key off (user_id, new_tier) so a real
            # subsequent transition (e.g. pro -> elite) isn't suppressed.
            should_send = await _alerts.dedupe_or_skip(
                "tier_change",
                f"{user_id}:{tier}",
                ttl_seconds=24 * 3600,
            )
            if should_send:
                _alerts.tier_change(
                    user_email=user_email,
                    user_id=user_id,
                    old_tier=_prev_normalized,
                    new_tier=tier,
                    status=status,
                    stripe_subscription_id=sub_id,
                )
        except Exception as e:  # pragma: no cover
            log.warning("tier_change alert dispatch failed: %s", e)

    # User-facing lifecycle emails — fire on real transitions only.
    # cancel_at_period_end going False -> True is the "scheduled to
    # cancel" event; status -> canceled is the actual end-of-life.
    # We resolve the recipient address from the metadata, the prior
    # subscriptions row, or a Stripe customer fetch as a last resort.
    try:
        from .notifications import email as _user_email

        recipient = _resolve_recipient_email(sub_obj, prev_email, customer_id)
        # 1) "Your subscription is set to cancel at period end."
        if (
            cancel_at_period_end
            and not (prev_cap or False)
            and status in _ACTIVE_STATUSES
        ):
            _user_email.subscription_will_cancel(
                email=recipient,
                user_id=user_id,
                stripe_subscription_id=sub_id,
                period_end=period_end,
                tier=_prev_normalized if _prev_normalized != TIER_FREE else tier,
            )
        # 2) "Your subscription has ended."  Fire when status crosses
        # into 'canceled' from any other state.
        if status == "canceled" and (prev_status or "") != "canceled":
            _user_email.subscription_canceled(
                email=recipient,
                user_id=user_id,
                stripe_subscription_id=sub_id,
                tier=_prev_normalized if _prev_normalized != TIER_FREE else None,
            )
    except Exception as e:  # pragma: no cover
        log.warning("user lifecycle email dispatch failed: %s", e)


def _resolve_recipient_email(
    sub_obj: dict[str, Any],
    prev_email: str | None,
    customer_id: str | None,
) -> str | None:
    """Best-effort recipient lookup for user-facing emails.

    Order: subscription metadata.email (cheapest, present when we set
    it during checkout), the cached email from our subscriptions row,
    a Stripe Customer fetch (only attempted when the SDK is configured
    and we have a customer id).
    """
    meta = (sub_obj.get("metadata") or {})
    cand = meta.get("email") if isinstance(meta, dict) else None
    if isinstance(cand, str) and "@" in cand:
        return cand
    if prev_email and "@" in prev_email:
        return prev_email
    if customer_id and stripe_configured():
        try:
            cust = stripe.Customer.retrieve(customer_id)
            email = cust.get("email") if isinstance(cust, dict) else getattr(cust, "email", None)
            if isinstance(email, str) and "@" in email:
                return email
        except Exception as e:  # pragma: no cover
            log.warning("stripe customer email lookup failed: %s", e)
    return None


async def _apply_invoice(invoice: dict[str, Any], paid: bool) -> None:
    """Update payment status when an invoice succeeds/fails."""
    sub_id = invoice.get("subscription")
    customer_id = invoice.get("customer")
    if not (sub_id or customer_id):
        return
    new_status = "active" if paid else "past_due"
    await ensure_schema()
    async with db.acquire() as conn:
        if sub_id:
            await conn.execute(
                """
                UPDATE subscriptions
                SET status = $2, updated_at = NOW()
                WHERE stripe_subscription_id = $1
                """,
                sub_id,
                new_status,
            )
        elif customer_id:
            await conn.execute(
                """
                UPDATE subscriptions
                SET status = $2, updated_at = NOW()
                WHERE stripe_customer_id = $1
                """,
                customer_id,
                new_status,
            )

    # User-facing payment-failed email.  We resolve the recipient from
    # the invoice payload first (Stripe always includes it on a
    # payment_failed) and fall back to our cached subscriptions row.
    if paid:
        return
    try:
        from .notifications import email as _user_email

        recipient: str | None = None
        cand = invoice.get("customer_email")
        if isinstance(cand, str) and "@" in cand:
            recipient = cand
        user_id: str | None = None
        try:
            row = None
            if sub_id:
                row = await db.fetchrow(
                    "SELECT user_id, email FROM subscriptions "
                    "WHERE stripe_subscription_id=$1",
                    sub_id,
                )
            elif customer_id:
                row = await db.fetchrow(
                    "SELECT user_id, email FROM subscriptions "
                    "WHERE stripe_customer_id=$1",
                    customer_id,
                )
            if row:
                user_id = row["user_id"]
                if not recipient and row.get("email") and "@" in (row.get("email") or ""):
                    recipient = row["email"]
        except Exception:
            pass

        next_attempt_ts = invoice.get("next_payment_attempt")
        next_attempt = _ts_to_dt(next_attempt_ts) if next_attempt_ts else None
        _user_email.payment_failed(
            email=recipient,
            user_id=user_id,
            invoice_id=invoice.get("id"),
            amount_cents=int(invoice.get("amount_due") or 0) or None,
            currency=invoice.get("currency"),
            next_attempt=next_attempt,
        )
    except Exception as e:  # pragma: no cover
        log.warning("payment_failed email dispatch failed: %s", e)


async def handle_webhook(request: Request) -> dict[str, Any]:
    """Verify the Stripe signature and dispatch to the appropriate handler."""
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
    if not secret:
        raise HTTPException(status_code=503, detail="webhook secret not configured")
    payload = await request.body()
    sig = request.headers.get("stripe-signature")
    if not sig:
        raise HTTPException(status_code=400, detail="missing stripe-signature header")
    try:
        event = stripe.Webhook.construct_event(payload, sig, secret)
    except (stripe.SignatureVerificationError, ValueError) as e:
        log.warning("stripe signature verification failed: %s", e)
        raise HTTPException(status_code=400, detail="invalid signature")

    event_id = event.get("id") or ""
    event_type = event.get("type") or ""
    data_obj = (event.get("data") or {}).get("object") or {}

    # Idempotency: skip events we've already FULLY processed.  We
    # deliberately check (and write) AFTER the handler runs so a
    # transient handler failure leaves the event un-acknowledged and
    # Stripe will retry it, instead of silently dropping the update.
    if await _is_duplicate_event(event_id):
        log.info("stripe duplicate event %s (%s) — already processed", event_id, event_type)
        return {"received": True, "duplicate": True}

    try:
        if event_type == "checkout.session.completed":
            sub_id = data_obj.get("subscription")
            if sub_id:
                sclient = _stripe_client()
                sub_obj = sclient.Subscription.retrieve(sub_id)
                await _apply_subscription(dict(sub_obj))
        elif event_type in (
            "customer.subscription.created",
            "customer.subscription.updated",
            "customer.subscription.deleted",
        ):
            await _apply_subscription(dict(data_obj))
        elif event_type == "invoice.payment_succeeded":
            await _apply_invoice(dict(data_obj), paid=True)
        elif event_type == "invoice.payment_failed":
            await _apply_invoice(dict(data_obj), paid=False)
        else:
            log.info("stripe event ignored: %s", event_type)
    except HTTPException:
        # Re-raise validation/auth errors; otherwise fall through to 502
        # below so Stripe retries.
        raise
    except Exception as e:
        log.exception("stripe webhook handler failed for %s", event_type)
        # Return 5xx so Stripe retries.  We have NOT yet inserted into
        # stripe_events, so the retry will re-run the handler.
        raise HTTPException(
            status_code=502,
            detail=f"webhook handler failed for {event_type}: {e}",
        )

    # Only mark the event as processed once the handler has succeeded.
    await _mark_event_processed(event_id, event_type, dict(event))
    return {"received": True}
