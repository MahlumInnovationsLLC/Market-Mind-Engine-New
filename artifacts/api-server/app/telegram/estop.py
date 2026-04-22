"""Emergency-stop helper shared between the HTTP handler and the bot.

Originally the logic lived inline in ``app.main.admin_emergency_stop``.
Per task #111 the Telegram concierge needs to perform the same set of
side effects (cancel resting Kalshi orders, trip the kill switch,
persist ``risk.trading_mode='halt'``, write an audit row, fire the
operator alert) when an authenticated operator confirms ``/estop`` —
so the body is now a free function the HTTP handler delegates to.

Also defines the per-Telegram-user pending-confirm state machine so
the bot module stays focused on transport.
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any

from .. import db
from ..kalshi import lifecycle as kalshi_lifecycle
from ..notifications import alerts as notif_alerts


log = logging.getLogger(__name__)


# Confirm code lifetime (seconds). 60s is long enough for the operator
# to type the code back but short enough that a stale chat scrollback
# can't accidentally trip a halt later.
CONFIRM_TTL_SECONDS = 60
CONFIRM_CODE_LEN = 6


@dataclass
class PendingEstop:
    code: str
    expires_at: float  # monotonic unix seconds
    reason: str


_pending: dict[int, PendingEstop] = {}
_pending_lock = asyncio.Lock()


def _gen_confirm_code() -> str:
    # Numeric-only so it's trivial to type on a phone keyboard.
    return "".join(str(secrets.randbelow(10)) for _ in range(CONFIRM_CODE_LEN))


async def begin_confirm(telegram_user_id: int, reason: str) -> PendingEstop:
    """Record a pending /estop and return the confirm code to display."""
    pending = PendingEstop(
        code=_gen_confirm_code(),
        expires_at=time.time() + CONFIRM_TTL_SECONDS,
        reason=(reason or "operator emergency stop").strip()[:200] or "operator emergency stop",
    )
    async with _pending_lock:
        _pending[int(telegram_user_id)] = pending
    return pending


async def consume_confirm(telegram_user_id: int, code: str) -> PendingEstop | None:
    """Return the pending entry iff ``code`` matches and is still alive.

    The entry is removed on success, on mismatch (so a fat-finger
    invalidates the round and the operator must restart with /estop),
    and on expiry.
    """
    code = (code or "").strip()
    async with _pending_lock:
        pending = _pending.get(int(telegram_user_id))
        if pending is None:
            return None
        # Drop on expiry or mismatch — both cases force a fresh /estop.
        if pending.expires_at < time.time() or pending.code != code:
            _pending.pop(int(telegram_user_id), None)
            return None
        _pending.pop(int(telegram_user_id), None)
        return pending


async def cancel_pending(telegram_user_id: int) -> bool:
    async with _pending_lock:
        return _pending.pop(int(telegram_user_id), None) is not None


async def execute_emergency_stop(
    *,
    actor_user_id: str,
    actor_email: str | None,
    reason: str,
) -> dict[str, Any]:
    """Run the emergency-stop side effects.

    Mirrors the body of ``app.main.admin_emergency_stop`` (task #104)
    but as a plain async function so non-HTTP callers (the Telegram
    bot) can reuse it. Each side effect is wrapped in try/except so a
    single failure (e.g. flaky cancel) doesn't prevent the rest of the
    halt — the kill switch trip is the safety-critical step and we
    want it to land even if the audit write later errors.
    """
    reason = (reason or "operator emergency stop").strip() or "operator emergency stop"

    # 1. Cancel every resting Kalshi order.
    try:
        canceled = await kalshi_lifecycle.cancel_all_resting()
    except Exception:
        log.exception("emergency-stop: cancel_all_resting failed")
        canceled = 0
    canceled_count = int(canceled or 0)

    # 2. Trip the process-wide kill switch.
    kalshi_lifecycle.halt(reason)

    # 3. Persist trading_mode=halt so the cycle gate keeps refusing
    #    placement after a restart (kill switch is in-memory only).
    try:
        await db.execute(
            """
            INSERT INTO system_settings (key, value, updated_at)
            VALUES ('risk.trading_mode', 'halt', now())
            ON CONFLICT (key) DO UPDATE
              SET value = EXCLUDED.value,
                  updated_at = EXCLUDED.updated_at
            """,
        )
    except Exception as e:
        log.warning("emergency-stop: trading_mode persist failed: %s", e)

    # 4. Audit row capturing what happened.
    try:
        await db.execute(
            """
            INSERT INTO admin_audit_log (
                actor_user_id, actor_email,
                target_user_id, target_email,
                action, before_value, after_value
            ) VALUES ($1, $2, $1, $2, $3, $4::jsonb, $5::jsonb)
            """,
            actor_user_id,
            actor_email,
            "emergency_stop",
            _json.dumps({"trading_mode": "live_or_paper"}),
            _json.dumps({
                "trading_mode": "halt",
                "reason": reason,
                "canceled_orders": canceled_count,
            }),
        )
    except Exception as e:
        log.warning("emergency-stop: audit log insert failed: %s", e)

    # 5. Email the on-call operator with cancel count + actor.
    try:
        notif_alerts.emergency_stop(
            actor=actor_email or actor_user_id,
            canceled_count=canceled_count,
            reason=reason,
        )
    except Exception as e:
        log.warning("emergency-stop: alert dispatch failed: %s", e)

    log.warning(
        "EMERGENCY STOP by %s (%s): canceled=%d reason=%r",
        actor_email or actor_user_id, actor_user_id, canceled_count, reason,
    )

    return {
        "ok": True,
        "canceled_orders": canceled_count,
        "trading_mode": "halt",
        "reason": reason,
        "kill_switch": kalshi_lifecycle.kill_switch_state(),
    }
