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

-- Per-user email channel toggles (task #112). Defaults match the
-- previous "everyone gets every email" behaviour for the standard
-- summaries; the live-trade firehose is opt-in because it is noisy
-- and gated behind the Elite tier.
ALTER TABLE user_notification_prefs
    ADD COLUMN IF NOT EXISTS email_daily_summary  boolean NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS email_drift          boolean NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS email_halt           boolean NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS email_live_trade     boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS email_weekly_roundup boolean NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS sms_live_trade       boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS telegram_live_trade  boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS sms_phone_number     text;
"""


# All boolean toggles owned by `user_notification_prefs`, with their
# default value when no row exists for the user. Kept in one place so
# `get_prefs` / `set_prefs` / the `/api/me/notification-prefs` endpoint
# stay in sync as new alert types are added.
PREF_COLUMNS: dict[str, bool] = {
    "notify_live_trade":    True,   # mobile push — live fill
    "notify_halt":          True,   # mobile push — kill switch
    "email_daily_summary":  True,
    "email_drift":          True,
    "email_halt":           True,
    "email_live_trade":     False,  # noisy + Elite-only feature
    "email_weekly_roundup": True,
    "sms_live_trade":       False,  # Elite-only premium channel
    "telegram_live_trade":  False,  # Elite-only premium channel
}

# Subset of toggles that require the Elite (or higher) tier to actually
# fire — the UI shows them disabled with an upgrade hint for sub-Elite
# users, and the dispatch path filters opted-in users by tier as well.
ELITE_ONLY_TOGGLES: frozenset[str] = frozenset({
    "sms_live_trade",
    "telegram_live_trade",
})


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


async def list_user_devices(clerk_user_id: str) -> list[dict[str, object]]:
    """Return a non-secret summary of the operator's registered push devices.

    Used by the web /profile page to surface "X devices connected" without
    leaking raw expo tokens. Each row contains platform + last_seen_at +
    a short masked token suffix so the user can disambiguate devices.
    """
    await ensure_schema()
    try:
        rows = await db.fetch(
            """
            SELECT expo_token, platform, last_seen_at, created_at
              FROM expo_push_tokens
             WHERE clerk_user_id = $1
             ORDER BY last_seen_at DESC NULLS LAST, created_at DESC NULLS LAST
            """,
            clerk_user_id,
        )
    except Exception as e:
        log.warning("list_user_devices failed for %s: %s", clerk_user_id, e)
        return []
    out: list[dict[str, object]] = []
    for r in rows:
        token = str(r.get("expo_token") or "")
        # Show only the trailing 6 chars before the closing bracket so the
        # user can tell devices apart without us echoing the full token.
        suffix = token[-8:-1] if token.endswith("]") else token[-6:]
        out.append(
            {
                "platform": r.get("platform"),
                "last_seen_at": r.get("last_seen_at"),
                "created_at": r.get("created_at"),
                "token_suffix": suffix,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Notification preferences
# ---------------------------------------------------------------------------


_PREF_COLS_SQL = ", ".join(PREF_COLUMNS.keys())


def _defaults() -> dict[str, bool]:
    return dict(PREF_COLUMNS)


async def get_prefs(clerk_user_id: str) -> dict[str, object]:
    """Return all per-user notification toggles + the optional SMS phone.

    Missing rows yield the documented defaults so the UI can render
    deterministic state for first-time users.
    """
    await ensure_schema()
    try:
        row = await db.fetchrow(
            f"SELECT {_PREF_COLS_SQL}, sms_phone_number "
            "FROM user_notification_prefs WHERE clerk_user_id = $1",
            clerk_user_id,
        )
    except Exception as e:
        log.warning("get_prefs failed for %s: %s", clerk_user_id, e)
        row = None
    out: dict[str, object] = _defaults()  # type: ignore[assignment]
    out["sms_phone_number"] = None
    if row is not None:
        for k in PREF_COLUMNS:
            out[k] = bool(row[k])
        out["sms_phone_number"] = row["sms_phone_number"]
    return out


async def set_prefs(
    *, clerk_user_id: str,
    sms_phone_number: str | None | object = ...,
    **toggles: bool | None,
) -> dict[str, object]:
    """Partial update of per-user prefs.

    Each known boolean toggle in ``toggles`` is applied when not
    ``None``; unknown keys are ignored so bumping the schema doesn't
    break older clients. ``sms_phone_number`` follows the same
    convention but uses a sentinel (``...``) for "leave unchanged" so
    callers can explicitly pass ``None`` to clear the number.
    """
    current = await get_prefs(clerk_user_id)
    next_vals: dict[str, bool] = {}
    for col in PREF_COLUMNS:
        v = toggles.get(col)
        next_vals[col] = bool(current[col]) if v is None else bool(v)
    next_phone: str | None = (
        current["sms_phone_number"]  # type: ignore[assignment]
        if sms_phone_number is ...
        else (str(sms_phone_number).strip() or None) if sms_phone_number else None
    )

    cols = list(PREF_COLUMNS.keys()) + ["sms_phone_number"]
    placeholders = ", ".join(f"${i + 2}" for i in range(len(cols)))
    set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols)
    sql = (
        f"INSERT INTO user_notification_prefs (clerk_user_id, {', '.join(cols)}, updated_at) "
        f"VALUES ($1, {placeholders}, now()) "
        f"ON CONFLICT (clerk_user_id) DO UPDATE SET {set_clause}, updated_at = now()"
    )
    args = [clerk_user_id] + [next_vals[c] for c in PREF_COLUMNS] + [next_phone]
    try:
        await db.execute(sql, *args)
    except Exception as e:
        log.warning("set_prefs failed for %s: %s", clerk_user_id, e)

    out: dict[str, object] = dict(next_vals)  # type: ignore[assignment]
    out["sms_phone_number"] = next_phone
    return out


# ---------------------------------------------------------------------------
# Per-user recipient resolution for email / SMS / Telegram alerts
# (consumed by `notifications.alerts` so every dispatch path respects
# the per-user toggles + Elite gating).
# ---------------------------------------------------------------------------


# Status values that count as an actively paying subscription. Mirrors
# the set used by `billing.get_user_subscription` (`active`, `trialing`,
# `past_due`); `founder` is a sentinel tier with no real Stripe row so
# we just match the tier directly.
_ACTIVE_SUB_STATUSES = ("active", "trialing", "past_due")
_ELITE_TIERS = ("elite", "founder")


async def _user_rows_for_email(pref_column: str, *, elite_only: bool) -> list[dict[str, object]]:
    if pref_column not in PREF_COLUMNS:
        raise ValueError(f"unknown pref column {pref_column!r}")
    await ensure_schema()
    # LEFT JOIN so users who never opened /settings/notifications still
    # match — they inherit the documented default for `pref_column`
    # (which is `true` for most email channels). Anyone who has saved
    # prefs at least once is honoured exactly.
    default_sql = "true" if PREF_COLUMNS[pref_column] else "false"
    sql = f"""
        SELECT u.clerk_user_id, u.email
          FROM app_users u
          LEFT JOIN user_notification_prefs p ON p.clerk_user_id = u.clerk_user_id
          LEFT JOIN subscriptions s ON s.user_id = u.clerk_user_id
         WHERE u.email IS NOT NULL
           AND u.email <> ''
           AND COALESCE(u.approved, false) = true
           AND COALESCE(p.{pref_column}, {default_sql}) = true
    """
    if elite_only:
        sql += (
            " AND ( (LOWER(COALESCE(s.tier,'')) IN ('elite','founder') "
            "       AND COALESCE(s.status,'') IN ('active','trialing','past_due')) "
            "    OR LOWER(COALESCE(s.tier,'')) = 'founder' )"
        )
    try:
        rows = await db.fetch(sql)
        return [{"clerk_user_id": r["clerk_user_id"], "email": r["email"]} for r in rows]
    except Exception as e:
        log.warning("user_rows_for_email %s failed: %s", pref_column, e)
        return []


async def opted_in_emails(pref_column: str, *, elite_only: bool | None = None) -> list[str]:
    """Email addresses of users who explicitly opted in to ``pref_column``.

    ``elite_only`` defaults to ``True`` for the Elite-gated channels
    (e.g. ``email_live_trade``) so a Pro user who somehow flipped the
    toggle still won't receive premium-only blasts.
    """
    if elite_only is None:
        elite_only = pref_column in ELITE_ONLY_TOGGLES
    rows = await _user_rows_for_email(pref_column, elite_only=elite_only)
    seen: set[str] = set()
    out: list[str] = []
    for r in rows:
        e = str(r["email"] or "").strip().lower()
        if e and e not in seen:
            seen.add(e)
            out.append(str(r["email"]))
    return out


async def opted_in_sms_phones(pref_column: str = "sms_live_trade") -> list[str]:
    """E.164-ish phone numbers of Elite users opted in to SMS for the
    given channel. Always Elite-gated."""
    if pref_column not in PREF_COLUMNS:
        raise ValueError(f"unknown pref column {pref_column!r}")
    await ensure_schema()
    sql = f"""
        SELECT p.sms_phone_number
          FROM user_notification_prefs p
          LEFT JOIN subscriptions s ON s.user_id = p.clerk_user_id
         WHERE p.{pref_column} = true
           AND p.sms_phone_number IS NOT NULL
           AND p.sms_phone_number <> ''
           AND ( (LOWER(COALESCE(s.tier,'')) IN ('elite','founder')
                  AND COALESCE(s.status,'') IN ('active','trialing','past_due'))
              OR LOWER(COALESCE(s.tier,'')) = 'founder' )
    """
    try:
        rows = await db.fetch(sql)
        return [str(r["sms_phone_number"]).strip() for r in rows if r["sms_phone_number"]]
    except Exception as e:
        log.warning("opted_in_sms_phones %s failed: %s", pref_column, e)
        return []


async def opted_in_telegram_chats(pref_column: str = "telegram_live_trade") -> list[int]:
    """Telegram chat ids for Elite users opted in to Telegram on the
    given channel. Skips users who haven't completed the bot binding."""
    if pref_column not in PREF_COLUMNS:
        raise ValueError(f"unknown pref column {pref_column!r}")
    await ensure_schema()
    sql = f"""
        SELECT t.telegram_chat_id
          FROM user_notification_prefs p
          JOIN telegram_bindings t ON t.clerk_user_id = p.clerk_user_id
          LEFT JOIN subscriptions s ON s.user_id = p.clerk_user_id
         WHERE p.{pref_column} = true
           AND t.telegram_chat_id IS NOT NULL
           AND t.bound_at IS NOT NULL
           AND ( (LOWER(COALESCE(s.tier,'')) IN ('elite','founder')
                  AND COALESCE(s.status,'') IN ('active','trialing','past_due'))
              OR LOWER(COALESCE(s.tier,'')) = 'founder' )
    """
    try:
        rows = await db.fetch(sql)
        return [int(r["telegram_chat_id"]) for r in rows if r["telegram_chat_id"] is not None]
    except Exception as e:
        log.warning("opted_in_telegram_chats %s failed: %s", pref_column, e)
        return []


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
