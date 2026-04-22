"""Expo push notifications.

Sends push messages to the Expo Push API for the mobile app, with two
small persisted tables:

  * ``expo_push_tokens`` — one row per device. Multiple devices per user
    are supported. The Expo token is the natural primary key (no two
    devices share one); we re-bind a token to whichever user signed in
    on it most recently.
  * ``user_notification_prefs`` — per-user toggles for live-trade and
    halt pushes. Default: both enabled. Anyone with no row at all
    receives both alert types as long as they have a registered token.

The dispatcher is fire-and-forget (uses ``asyncio.create_task``) so
trade and kill-switch callers never block on Expo's HTTP roundtrip.
Failures are logged but never raised — a push outage must not break the
trading loop.

Receipts (delivery acknowledgements) are intentionally not polled here:
Expo recommends checking them ~15 minutes after send to detect
``DeviceNotRegistered``.  We instead lazy-prune tokens when we receive a
``DeviceNotRegistered`` ticket back from the initial /send call, which
covers the common case (uninstalled app) without needing a background
job.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Iterable

import httpx

from .. import db


log = logging.getLogger("axiom.notifications.push")


EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS expo_push_tokens (
    expo_token      text PRIMARY KEY,
    clerk_user_id   text NOT NULL,
    platform        text,
    last_seen_at    timestamptz NOT NULL DEFAULT now(),
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS expo_push_tokens_user_idx
    ON expo_push_tokens (clerk_user_id);

CREATE TABLE IF NOT EXISTS user_notification_prefs (
    clerk_user_id     text PRIMARY KEY,
    notify_live_trade boolean NOT NULL DEFAULT true,
    notify_halt       boolean NOT NULL DEFAULT true,
    updated_at        timestamptz NOT NULL DEFAULT now()
);
"""


_schema_ready = False
_schema_lock = asyncio.Lock()


async def ensure_schema() -> None:
    """Create the push tables if missing. Idempotent."""
    global _schema_ready
    if _schema_ready:
        return
    async with _schema_lock:
        if _schema_ready:
            return
        try:
            async with db.acquire() as c:
                await c.execute(_SCHEMA_SQL)
            _schema_ready = True
            log.info("push schema ready")
        except Exception as e:
            log.warning("push schema bootstrap failed: %s", e)


# ---------------------------------------------------------------------------
# Token registration
# ---------------------------------------------------------------------------


def _looks_like_expo_token(token: str) -> bool:
    if not isinstance(token, str):
        return False
    t = token.strip()
    return t.startswith("ExponentPushToken[") and t.endswith("]") and 20 <= len(t) <= 200


async def register_token(
    *, clerk_user_id: str, expo_token: str, platform: str | None
) -> bool:
    """Persist or refresh the device push token for a user.

    Returns True on success, False if the token is malformed or the DB
    write fails.
    """
    if not _looks_like_expo_token(expo_token):
        return False
    plat = (platform or "").strip()[:32] or None
    await ensure_schema()
    try:
        await db.execute(
            """
            INSERT INTO expo_push_tokens (expo_token, clerk_user_id, platform)
            VALUES ($1, $2, $3)
            ON CONFLICT (expo_token) DO UPDATE SET
              clerk_user_id = EXCLUDED.clerk_user_id,
              platform      = COALESCE(EXCLUDED.platform, expo_push_tokens.platform),
              last_seen_at  = now()
            """,
            expo_token.strip(), clerk_user_id, plat,
        )
        return True
    except Exception as e:
        log.warning("register_token failed for %s: %s", clerk_user_id, e)
        return False


async def unregister_token(*, clerk_user_id: str, expo_token: str) -> bool:
    await ensure_schema()
    try:
        result = await db.execute(
            "DELETE FROM expo_push_tokens "
            "WHERE expo_token = $1 AND clerk_user_id = $2",
            expo_token.strip(), clerk_user_id,
        )
        return "DELETE" in str(result)
    except Exception as e:
        log.warning("unregister_token failed for %s: %s", clerk_user_id, e)
        return False


async def _drop_token(expo_token: str) -> None:
    try:
        await db.execute(
            "DELETE FROM expo_push_tokens WHERE expo_token = $1", expo_token
        )
    except Exception as e:
        log.warning("drop_token failed for %s: %s", expo_token, e)


# ---------------------------------------------------------------------------
# Notification preferences
# ---------------------------------------------------------------------------


async def get_prefs(clerk_user_id: str) -> dict[str, bool]:
    await ensure_schema()
    try:
        row = await db.fetchrow(
            "SELECT notify_live_trade, notify_halt "
            "FROM user_notification_prefs WHERE clerk_user_id = $1",
            clerk_user_id,
        )
    except Exception as e:
        log.warning("get_prefs failed for %s: %s", clerk_user_id, e)
        row = None
    if row is None:
        return {"notify_live_trade": True, "notify_halt": True}
    return {
        "notify_live_trade": bool(row["notify_live_trade"]),
        "notify_halt": bool(row["notify_halt"]),
    }


async def set_prefs(
    *, clerk_user_id: str,
    notify_live_trade: bool | None = None,
    notify_halt: bool | None = None,
) -> dict[str, bool]:
    current = await get_prefs(clerk_user_id)
    next_lt = current["notify_live_trade"] if notify_live_trade is None else bool(notify_live_trade)
    next_h  = current["notify_halt"]       if notify_halt       is None else bool(notify_halt)
    try:
        await db.execute(
            """
            INSERT INTO user_notification_prefs (
              clerk_user_id, notify_live_trade, notify_halt, updated_at
            ) VALUES ($1, $2, $3, now())
            ON CONFLICT (clerk_user_id) DO UPDATE SET
              notify_live_trade = EXCLUDED.notify_live_trade,
              notify_halt       = EXCLUDED.notify_halt,
              updated_at        = now()
            """,
            clerk_user_id, next_lt, next_h,
        )
    except Exception as e:
        log.warning("set_prefs failed for %s: %s", clerk_user_id, e)
    return {"notify_live_trade": next_lt, "notify_halt": next_h}


# ---------------------------------------------------------------------------
# Recipient resolution
# ---------------------------------------------------------------------------


async def _tokens_for_pref(pref_column: str) -> list[str]:
    """Return every Expo token whose owner has the given pref enabled
    (or has no prefs row, i.e. defaults).

    We left-join `user_notification_prefs` so a user with a registered
    device but no row gets the default-true treatment.
    """
    if pref_column not in ("notify_live_trade", "notify_halt"):
        raise ValueError(f"bad pref column {pref_column!r}")
    await ensure_schema()
    sql = (
        "SELECT t.expo_token FROM expo_push_tokens t "
        "LEFT JOIN user_notification_prefs p "
        "  ON p.clerk_user_id = t.clerk_user_id "
        f"WHERE COALESCE(p.{pref_column}, true) = true"
    )
    try:
        rows = await db.fetch(sql)
        return [r["expo_token"] for r in rows]
    except Exception as e:
        log.warning("tokens_for_pref %s failed: %s", pref_column, e)
        return []


# ---------------------------------------------------------------------------
# Expo Push API
# ---------------------------------------------------------------------------


def _is_push_enabled() -> bool:
    """Push dispatch can be disabled in tests / local dev via env var."""
    flag = os.environ.get("AXIOM_PUSH_DISABLED", "").strip().lower()
    return flag not in ("1", "true", "yes", "on")


async def send_to_tokens(
    tokens: Iterable[str],
    *,
    title: str,
    body: str,
    data: dict[str, Any] | None = None,
    channel_id: str | None = None,
) -> dict[str, Any]:
    """POST one or more push messages to the Expo Push API in a single batch.

    Returns a small summary: ``{"sent": N, "ok": M, "errors": [...]}``.
    Any token rejected with ``DeviceNotRegistered`` is removed.
    """
    tokens = [t for t in tokens if _looks_like_expo_token(t)]
    if not tokens:
        return {"sent": 0, "ok": 0, "errors": []}
    if not _is_push_enabled():
        log.info("push disabled (AXIOM_PUSH_DISABLED) — would send to %d", len(tokens))
        return {"sent": len(tokens), "ok": 0, "errors": [], "disabled": True}

    messages = [
        {
            "to": t,
            "title": title,
            "body": body,
            "sound": "default",
            "priority": "high",
            "data": data or {},
            **({"channelId": channel_id} if channel_id else {}),
        }
        for t in tokens
    ]

    errors: list[str] = []
    ok_count = 0
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                EXPO_PUSH_URL,
                json=messages,
                headers={
                    "accept": "application/json",
                    "accept-encoding": "gzip, deflate",
                    "content-type": "application/json",
                },
            )
        if r.status_code >= 400:
            errors.append(f"HTTP {r.status_code}: {r.text[:200]}")
            return {"sent": len(tokens), "ok": 0, "errors": errors}
        payload = r.json() or {}
        tickets = payload.get("data") or []
        if not isinstance(tickets, list):
            tickets = []
        for tok, ticket in zip(tokens, tickets):
            if not isinstance(ticket, dict):
                continue
            status = ticket.get("status")
            if status == "ok":
                ok_count += 1
                continue
            err = (ticket.get("details") or {}).get("error") if isinstance(ticket.get("details"), dict) else None
            errors.append(f"{tok[:24]}…: {ticket.get('message') or err or 'error'}")
            if err == "DeviceNotRegistered":
                # Don't await — fire prune in background.
                asyncio.create_task(_drop_token(tok))
    except Exception as e:
        log.warning("expo push send failed: %s", e)
        errors.append(str(e))
    return {"sent": len(tokens), "ok": ok_count, "errors": errors}


# ---------------------------------------------------------------------------
# High-level fan-out for our two alert types
# ---------------------------------------------------------------------------


async def _dispatch_for_alert(
    pref_column: str,
    *,
    title: str,
    body: str,
    data: dict[str, Any],
    channel_id: str,
) -> dict[str, Any]:
    tokens = await _tokens_for_pref(pref_column)
    if not tokens:
        return {"sent": 0, "ok": 0, "errors": [], "no_recipients": True}
    return await send_to_tokens(
        tokens, title=title, body=body, data=data, channel_id=channel_id,
    )


def push_live_trade(
    *,
    market_ticker: str,
    side: str,
    action: str,
    contracts: int,
    price_cents: int | None = None,
    vertical: str | None = None,
) -> None:
    """Fire-and-forget push fan-out for a LIVE fill.

    Caller may invoke this from sync code paths (e.g. inside the email
    alert helper) — we schedule onto the running loop and never raise.
    """
    px = f"{price_cents}c" if price_cents is not None else "market"
    title = "Live trade filled"
    body = f"{action.upper()} {side.upper()} {contracts}x {market_ticker} @ {px}"
    data = {
        "type": "live_trade",
        "market_ticker": market_ticker,
        "side": side,
        "action": action,
        "contracts": contracts,
        "price_cents": price_cents,
        "vertical": vertical,
        "deep_link": "/positions",
    }
    _schedule(_dispatch_for_alert(
        "notify_live_trade",
        title=title, body=body, data=data, channel_id="trades",
    ))


def push_halt(*, reason: str | None) -> None:
    """Fire-and-forget push fan-out for a kill-switch trip."""
    title = "Trading halted"
    body = f"Kill switch tripped: {reason or 'operator halt'}"
    data = {
        "type": "halt",
        "reason": reason,
        "deep_link": "/pipeline",
    }
    _schedule(_dispatch_for_alert(
        "notify_halt",
        title=title, body=body, data=data, channel_id="halts",
    ))


def _schedule(coro) -> None:
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(coro)
    except RuntimeError:
        try:
            asyncio.run(coro)
        except Exception as e:  # pragma: no cover
            log.warning("inline push dispatch failed: %s", e)
