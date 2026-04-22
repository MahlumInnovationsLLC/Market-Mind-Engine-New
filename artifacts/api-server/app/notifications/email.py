"""User-facing subscription lifecycle emails.

These are the *member* emails sent in response to billing events
(payment failed / subscription will cancel / cancelled).  Operator
alerts live next door in ``alerts.py``.

Each public helper:
  * Resolves the public base URL (used in unsubscribe links + the
    "manage billing" CTA).
  * Skips the send when the recipient is on the suppression list
    (``email_suppressions``: bounces + manual unsubscribes).
  * Records a churn-events row so the daily operator digest has
    something to summarise.
  * Hands off to ``postmark.send`` with an explicit ``to=[email]`` —
    these never go to operators.

Configuration (env vars):
  - POSTMARK_SERVER_TOKEN     (required to actually send)
  - POSTMARK_FROM_EMAIL       (must be a verified sender)
  - AXIOM_PUBLIC_BASE_URL     (used for /billing + unsubscribe URLs;
                               falls back to REPLIT_DEV_DOMAIN)
  - AXIOM_UNSUBSCRIBE_SECRET  (HMAC key for unsubscribe tokens; falls
                               back to STRIPE_WEBHOOK_SECRET so a
                               fresh install just works)
"""
from __future__ import annotations

import asyncio
import hmac
import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from . import postmark, suppression, churn


log = logging.getLogger("axiom.notifications.email")


def _public_base_url() -> str:
    explicit = (os.environ.get("AXIOM_PUBLIC_BASE_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    dev = (os.environ.get("REPLIT_DEV_DOMAIN") or "").strip()
    if dev:
        return f"https://{dev}".rstrip("/")
    return "http://localhost:3000"


def _unsub_secret() -> bytes:
    raw = (
        os.environ.get("AXIOM_UNSUBSCRIBE_SECRET")
        or os.environ.get("STRIPE_WEBHOOK_SECRET")
        or "axiom-default-unsub-secret"
    )
    return raw.encode("utf-8")


def make_unsubscribe_token(email: str) -> str:
    """HMAC-sign the email so the public unsubscribe endpoint can
    verify the request without us having to keep a per-user token."""
    msg = (email or "").lower().encode("utf-8")
    return hmac.new(_unsub_secret(), msg, hashlib.sha256).hexdigest()


def verify_unsubscribe_token(email: str, token: str) -> bool:
    expected = make_unsubscribe_token(email)
    return hmac.compare_digest(expected, token or "")


def _unsubscribe_url(email: str) -> str:
    base = _public_base_url()
    tok = make_unsubscribe_token(email)
    return f"{base}/api/notifications/unsubscribe?email={quote(email)}&token={tok}"


def _billing_url() -> str:
    return f"{_public_base_url()}/billing"


def _footer(email: str) -> str:
    return (
        "\n\n--\n"
        "You received this because you have an Axiom Predict subscription.\n"
        f"Manage billing: {_billing_url()}\n"
        f"Unsubscribe from these emails: {_unsubscribe_url(email)}\n"
    )


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Public helpers (one per lifecycle event)
# ---------------------------------------------------------------------------


def _format_period_end(period_end: datetime | str | None) -> str:
    if isinstance(period_end, datetime):
        return period_end.strftime("%Y-%m-%d")
    if isinstance(period_end, str) and period_end:
        return period_end[:10]
    return "(unknown)"


async def _send_user(
    *,
    email: str | None,
    subject: str,
    text_body: str,
    tag: str,
    user_id: str | None,
    event_type: str,
    dedupe_key: str,
    details: dict[str, Any] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Common path: dedupe → suppression check → record → send."""
    if not email:
        log.info("user email skipped (no recipient): %s", event_type)
        # Still record the churn event so the operator digest reflects it.
        await churn.record(
            event_type=event_type,
            user_id=user_id,
            email=None,
            details=details,
            email_sent=False,
            skipped_reason="no_email",
        )
        return {"sent": False, "skipped_reason": "no_email"}

    # Cross-process dedupe (Stripe re-delivers the same webhook on retry).
    from . import alerts as _alerts
    claimed = await _alerts.dedupe_or_skip(
        f"user_email:{event_type}",
        dedupe_key,
        ttl_seconds=24 * 3600,
    )
    if not claimed:
        log.info("user email suppressed by dedupe: %s/%s", event_type, dedupe_key)
        return {"sent": False, "skipped_reason": "dedupe"}

    if await suppression.is_suppressed(email):
        log.info("user email suppressed (bounce/unsub): %s -> %s", event_type, email)
        await churn.record(
            event_type=event_type,
            user_id=user_id,
            email=email,
            details=details,
            email_sent=False,
            skipped_reason="suppressed",
        )
        return {"sent": False, "skipped_reason": "suppressed"}

    body = text_body + _footer(email)
    await churn.record(
        event_type=event_type,
        user_id=user_id,
        email=email,
        details=details,
        email_sent=not dry_run,
    )
    results = await postmark.send(
        subject=subject,
        text_body=body,
        to=[email],
        tag=tag,
        dry_run=dry_run,
    )
    return {"sent": True, "dry_run": dry_run, "results": results}


def payment_failed(
    *,
    email: str | None,
    user_id: str | None = None,
    invoice_id: str | None = None,
    amount_cents: int | None = None,
    currency: str | None = None,
    next_attempt: datetime | str | None = None,
    dry_run: bool = False,
) -> "asyncio.Task | dict[str, Any]":
    amt = (
        f"${amount_cents/100:.2f} {(currency or 'USD').upper()}"
        if amount_cents is not None
        else "your subscription payment"
    )
    next_s = _format_period_end(next_attempt)
    subject = "[Axiom Predict] Your payment didn't go through"
    body = (
        f"Hi,\n\n"
        f"We couldn't charge {amt} for your Axiom Predict subscription.\n"
        f"Stripe will automatically retry"
        + (f" on or around {next_s}" if next_s != "(unknown)" else "")
        + ".  To avoid losing access, please update your payment method:\n\n"
        f"  {_billing_url()}\n\n"
        f"If the retry also fails your subscription will be paused.\n"
        f"Reply to this email if you need a hand.\n\n"
        f"— The Axiom Predict team\n"
    )
    return _fire_and_forget(
        _send_user(
            email=email,
            subject=subject,
            text_body=body,
            tag="user-payment-failed",
            user_id=user_id,
            event_type="payment_failed",
            dedupe_key=f"{user_id or email}:{invoice_id or _utc_now_iso()[:10]}",
            details={
                "invoice_id": invoice_id,
                "amount_cents": amount_cents,
                "currency": currency,
            },
            dry_run=dry_run,
        )
    )


def subscription_will_cancel(
    *,
    email: str | None,
    user_id: str | None = None,
    stripe_subscription_id: str | None = None,
    period_end: datetime | str | None = None,
    tier: str | None = None,
    dry_run: bool = False,
) -> "asyncio.Task | dict[str, Any]":
    end_s = _format_period_end(period_end)
    subject = "[Axiom Predict] Your subscription is set to cancel"
    body = (
        f"Hi,\n\n"
        f"You've cancelled your Axiom Predict {(tier or 'subscription').title()} plan.\n"
        f"You'll keep full access until {end_s}, after which your account "
        f"will drop to the free tier.\n\n"
        f"Changed your mind?  You can re-enable auto-renew any time:\n"
        f"  {_billing_url()}\n\n"
        f"Thanks for giving us a try.\n\n"
        f"— The Axiom Predict team\n"
    )
    return _fire_and_forget(
        _send_user(
            email=email,
            subject=subject,
            text_body=body,
            tag="user-will-cancel",
            user_id=user_id,
            event_type="will_cancel",
            dedupe_key=f"{user_id or email}:{stripe_subscription_id or 'sub'}",
            details={
                "stripe_subscription_id": stripe_subscription_id,
                "period_end": end_s,
                "tier": tier,
            },
            dry_run=dry_run,
        )
    )


def subscription_canceled(
    *,
    email: str | None,
    user_id: str | None = None,
    stripe_subscription_id: str | None = None,
    tier: str | None = None,
    dry_run: bool = False,
) -> "asyncio.Task | dict[str, Any]":
    subject = "[Axiom Predict] Your subscription has ended"
    body = (
        f"Hi,\n\n"
        f"Your Axiom Predict {(tier or 'subscription').title()} plan has ended "
        f"and your account is now on the free tier.  You'll keep read-only "
        f"access to the dashboard.\n\n"
        f"If you'd like to come back, you can resubscribe at any time:\n"
        f"  {_billing_url()}\n\n"
        f"We'd love to know what we could do better — just reply to this email.\n\n"
        f"— The Axiom Predict team\n"
    )
    return _fire_and_forget(
        _send_user(
            email=email,
            subject=subject,
            text_body=body,
            tag="user-canceled",
            user_id=user_id,
            event_type="canceled",
            dedupe_key=f"{user_id or email}:{stripe_subscription_id or 'sub'}",
            details={
                "stripe_subscription_id": stripe_subscription_id,
                "tier": tier,
            },
            dry_run=dry_run,
        )
    )


def _fire_and_forget(coro):
    """Schedule the send on the running loop, returning the Task.

    Webhook handlers must not block on Postmark IO — Stripe will time
    out the webhook and start retrying.  When called with no running
    loop (sync test code), run the coroutine inline and return its
    result instead.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            return asyncio.run(coro)
        except Exception as e:  # pragma: no cover
            log.warning("inline user-email send failed: %s", e)
            return {"sent": False, "error": str(e)}
    return loop.create_task(coro)
