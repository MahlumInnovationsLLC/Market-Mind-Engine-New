"""Email suppression list — bounces + manual unsubscribes.

A row in ``email_suppressions`` means we will not send *any* user-facing
email to that address.  Operator alerts (``alerts.py``) bypass this list
entirely — the operator inbox is configured separately.

Sources of suppression:
  * Postmark inbound webhook (HardBounce, SpamComplaint, ManualSuppression)
    posts to ``/api/notifications/postmark-webhook`` and lands here with
    reason="bounce" / "complaint".
  * The public ``/api/notifications/unsubscribe?email=…&token=…`` endpoint
    inserts a row with reason="unsubscribe".
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any


log = logging.getLogger("axiom.notifications.suppression")


_table_ready = False
_lock = asyncio.Lock()


async def ensure_table() -> None:
    """Schema is now owned by the Alembic baseline migration; no-op."""
    global _table_ready
    _table_ready = True


def _norm(email: str) -> str:
    return (email or "").strip().lower()


async def is_suppressed(email: str) -> bool:
    if not email:
        return False
    await ensure_table()
    from .. import db
    try:
        row = await db.fetchval(
            "SELECT 1 FROM email_suppressions WHERE email=$1",
            _norm(email),
        )
        return bool(row)
    except Exception as e:
        log.warning("is_suppressed read failed: %s", e)
        # Fail open — better to risk a single duplicate send than to
        # silently drop a payment-failed notice.
        return False


async def suppress(email: str, reason: str, detail: str | None = None) -> bool:
    if not email:
        return False
    await ensure_table()
    from .. import db
    try:
        await db.execute(
            """
            INSERT INTO email_suppressions (email, reason, detail)
            VALUES ($1, $2, $3)
            ON CONFLICT (email) DO UPDATE
              SET reason = EXCLUDED.reason,
                  detail = COALESCE(EXCLUDED.detail, email_suppressions.detail),
                  created_at = now()
            """,
            _norm(email), reason, detail,
        )
        log.info("email suppressed: %s (%s)", _norm(email), reason)
        return True
    except Exception as e:
        log.warning("suppress write failed: %s", e)
        return False


async def unsuppress(email: str) -> bool:
    if not email:
        return False
    await ensure_table()
    from .. import db
    try:
        await db.execute(
            "DELETE FROM email_suppressions WHERE email=$1",
            _norm(email),
        )
        return True
    except Exception as e:
        log.warning("unsuppress failed: %s", e)
        return False


async def list_recent(limit: int = 100) -> list[dict[str, Any]]:
    await ensure_table()
    from .. import db
    try:
        rows = await db.fetch(
            """
            SELECT email, reason, detail, created_at
              FROM email_suppressions
             ORDER BY created_at DESC
             LIMIT $1
            """,
            int(max(1, min(limit, 500))),
        )
        return [
            {
                "email": r["email"],
                "reason": r["reason"],
                "detail": r["detail"],
                "created_at": r["created_at"].isoformat()
                if r["created_at"] else None,
            }
            for r in rows
        ]
    except Exception as e:
        log.warning("list_recent failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Postmark inbound webhook decoder
# ---------------------------------------------------------------------------


_BOUNCE_REASON_MAP = {
    "HardBounce": "bounce",
    "SpamComplaint": "complaint",
    "ManualSuppression": "manual",
    "BadEmailAddress": "bounce",
}


async def apply_postmark_event(payload: dict[str, Any]) -> dict[str, Any]:
    """Translate a Postmark webhook payload into a suppression row.

    Postmark sends one event per POST, with ``RecordType`` = "Bounce"
    or "SpamComplaint" or "SubscriptionChange".  We only act on hard
    bounces, complaints, and explicit suppression changes — soft
    bounces (Transient) are ignored so a one-off network glitch
    doesn't permanently mute a real user.
    """
    record_type = str(payload.get("RecordType") or "")
    email = (payload.get("Email") or payload.get("Recipient") or "").strip()

    if record_type == "Bounce":
        bounce_type = str(payload.get("Type") or "")
        if bounce_type not in _BOUNCE_REASON_MAP:
            return {"action": "ignored", "reason": f"bounce_type={bounce_type}"}
        reason = _BOUNCE_REASON_MAP[bounce_type]
        await suppress(email, reason, detail=str(payload.get("Description") or "")[:300])
        return {"action": "suppressed", "email": email, "reason": reason}

    if record_type == "SpamComplaint":
        await suppress(email, "complaint", detail="postmark spam complaint")
        return {"action": "suppressed", "email": email, "reason": "complaint"}

    if record_type == "SubscriptionChange":
        # Postmark's customer-facing unsubscribe link feeds back here.
        suppressed = bool(payload.get("SuppressSending"))
        if suppressed:
            await suppress(email, "manual", detail="postmark subscription change")
            return {"action": "suppressed", "email": email, "reason": "manual"}
        await unsuppress(email)
        return {"action": "unsuppressed", "email": email}

    return {"action": "ignored", "reason": f"record_type={record_type}"}
