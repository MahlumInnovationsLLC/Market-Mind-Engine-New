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
import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
-- Per-user log of every push we *attempted to deliver* (task #133).
-- One row per recipient per dispatch so the mobile app can show an
-- in-app history beyond what the OS notification tray retains. We
-- only record entries for users with at least one registered device,
-- since logging "would-have-been-sent" rows for desktop-only users
-- would just create unread clutter on devices that never see them.
CREATE TABLE IF NOT EXISTS mobile_notification_log (
    id              bigserial PRIMARY KEY,
    clerk_user_id   text NOT NULL,
    type            text NOT NULL,
    title           text NOT NULL,
    body            text NOT NULL,
    deep_link       text,
    data            jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    read_at         timestamptz
);

CREATE INDEX IF NOT EXISTS mobile_notification_log_user_idx
    ON mobile_notification_log (clerk_user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS mobile_notification_log_unread_idx
    ON mobile_notification_log (clerk_user_id)
    WHERE read_at IS NULL;

ALTER TABLE user_notification_prefs
    ADD COLUMN IF NOT EXISTS email_daily_summary       boolean NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS email_drift               boolean NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS email_halt                boolean NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS email_live_trade          boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS email_weekly_roundup      boolean NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS email_trade_confirmation  boolean NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS digest_partials           boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS sms_live_trade            boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS telegram_live_trade       boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS sms_phone_number          text,
    -- Per-user "do not disturb" window for mobile push (task #131).
    -- All three fields must be set for the window to take effect; any
    -- NULL disables the feature so existing users see no behaviour
    -- change. `start == end` is also treated as disabled (zero-length).
    -- Hours are 0-23 in the user's IANA `quiet_hours_tz`. If start <
    -- end, the window is the same calendar day; if start > end, the
    -- window wraps midnight (e.g. 22 → 7 = 10pm to 7am).
    ADD COLUMN IF NOT EXISTS quiet_hours_start_hour    smallint,
    ADD COLUMN IF NOT EXISTS quiet_hours_end_hour      smallint,
    ADD COLUMN IF NOT EXISTS quiet_hours_tz            text,
    -- Mobile push for position settlement (task #132). Default on so the
    -- natural follow-up to a live-trade fill push (the resolution) is
    -- delivered without requiring users to opt in.
    ADD COLUMN IF NOT EXISTS notify_settlement         boolean NOT NULL DEFAULT true,
    -- One-time SMS verification of the saved phone number (task #191).
    -- We refuse to dispatch paid Twilio messages until the operator
    -- confirms ownership of the destination number with a 6-digit code,
    -- so a typo doesn't quietly burn revenue (or worse, ship trade
    -- alerts to a stranger). `verified_at` is the proof; `code` +
    -- `expires_at` track the in-flight challenge and are cleared on
    -- success or when the phone is changed.
    ADD COLUMN IF NOT EXISTS sms_phone_verified_at             timestamptz,
    ADD COLUMN IF NOT EXISTS sms_phone_verification_code       text,
    ADD COLUMN IF NOT EXISTS sms_phone_verification_expires_at timestamptz;
"""


# Verification challenge lives for ten minutes — long enough that the
# Twilio relay + a distracted user can complete the round trip, short
# enough that an old code is useless if the phone leaves their hands.
SMS_VERIFICATION_TTL_SECONDS = 600


# Boolean toggles that *send paid SMS*. Listed explicitly so the gate
# in the prefs endpoint and the dispatch-side filter (`opted_in_sms_phones`)
# stay in sync as new SMS channels are added.
SMS_PREF_COLUMNS: frozenset[str] = frozenset({"sms_live_trade"})


# Non-boolean per-user pref columns. Kept separate from `PREF_COLUMNS`
# (which is boolean-only) so the existing toggle-driven plumbing stays
# simple. Each entry maps the column name to its Python default.
QUIET_HOURS_COLUMNS: dict[str, Any] = {
    "quiet_hours_start_hour": None,
    "quiet_hours_end_hour":   None,
    "quiet_hours_tz":         None,
}


# All boolean toggles owned by `user_notification_prefs`, with their
# default value when no row exists for the user. Kept in one place so
# `get_prefs` / `set_prefs` / the `/api/me/notification-prefs` endpoint
# stay in sync as new alert types are added.
PREF_COLUMNS: dict[str, bool] = {
    "notify_live_trade":       True,   # mobile push — live fill
    "notify_halt":             True,   # mobile push — kill switch
    "notify_settlement":       True,   # mobile push — position settled
    "email_daily_summary":     True,
    "email_drift":             True,
    "email_halt":              True,
    "email_live_trade":        False,  # noisy + Elite-only feature
    "email_weekly_roundup":    True,
    # Per-user HTML trade confirmation sent on each fill (or consolidated
    # at terminal state when `digest_partials` is on). Distinct from the
    # operator-broadcast `email_live_trade`. Default on so a user never
    # misses a fill receipt; they can silence it from /settings.
    "email_trade_confirmation": True,
    # When true, partial fills for a single order are deferred and
    # combined into a single confirmation email at terminal state
    # (`filled`). Default off — most users want a receipt per fill.
    "digest_partials":         False,
    "sms_live_trade":          False,  # Elite-only premium channel
    "telegram_live_trade":     False,  # Elite-only premium channel
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


_VERIFICATION_COLS = (
    "sms_phone_verified_at",
    "sms_phone_verification_code",
    "sms_phone_verification_expires_at",
)


async def get_prefs(clerk_user_id: str) -> dict[str, object]:
    """Return all per-user notification toggles + the optional SMS phone.

    Missing rows yield the documented defaults so the UI can render
    deterministic state for first-time users.
    """
    await ensure_schema()
    extra_cols = (
        ["sms_phone_number"]
        + list(QUIET_HOURS_COLUMNS.keys())
        + list(_VERIFICATION_COLS)
    )
    try:
        row = await db.fetchrow(
            f"SELECT {_PREF_COLS_SQL}, {', '.join(extra_cols)} "
            "FROM user_notification_prefs WHERE clerk_user_id = $1",
            clerk_user_id,
        )
    except Exception as e:
        log.warning("get_prefs failed for %s: %s", clerk_user_id, e)
        row = None
    out: dict[str, object] = _defaults()  # type: ignore[assignment]
    out["sms_phone_number"] = None
    for k, default in QUIET_HOURS_COLUMNS.items():
        out[k] = default
    for k in _VERIFICATION_COLS:
        out[k] = None
    if row is not None:
        for k in PREF_COLUMNS:
            out[k] = bool(row[k])
        out["sms_phone_number"] = row["sms_phone_number"]
        for k in QUIET_HOURS_COLUMNS:
            out[k] = row[k]
        for k in _VERIFICATION_COLS:
            out[k] = row[k]
    out["sms_phone_verified"] = out.get("sms_phone_verified_at") is not None
    return out


_UNSET: Any = object()


def _normalize_quiet_hour(v: Any) -> int | None:
    if v is None:
        return None
    try:
        h = int(v)
    except (TypeError, ValueError):
        raise ValueError("quiet hour must be an integer 0-23")
    if not 0 <= h <= 23:
        raise ValueError("quiet hour must be 0-23")
    return h


def _normalize_quiet_tz(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        ZoneInfo(s)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"unknown time zone {s!r}")
    return s


async def set_prefs(
    *, clerk_user_id: str,
    sms_phone_number: str | None | object = ...,
    quiet_hours_start_hour: Any = _UNSET,
    quiet_hours_end_hour: Any = _UNSET,
    quiet_hours_tz: Any = _UNSET,
    **toggles: bool | None,
) -> dict[str, object]:
    """Partial update of per-user prefs.

    Each known boolean toggle in ``toggles`` is applied when not
    ``None``; unknown keys are ignored so bumping the schema doesn't
    break older clients. ``sms_phone_number`` follows the same
    convention but uses a sentinel (``...``) for "leave unchanged" so
    callers can explicitly pass ``None`` to clear the number.

    The three ``quiet_hours_*`` fields use a separate ``_UNSET`` sentinel
    so callers can explicitly pass ``None`` to clear any of them
    individually (e.g. blanking the timezone effectively disables the
    window without forgetting the saved start/end hours).
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

    # Whenever the destination phone changes (including being set for
    # the first time, or cleared back to NULL) the previous verification
    # is no longer meaningful — drop the proof and any in-flight code so
    # the user is forced to re-verify before paid SMS resumes.
    phone_changed = next_phone != current.get("sms_phone_number")
    if phone_changed:
        next_verification: dict[str, Any] = {c: None for c in _VERIFICATION_COLS}
    else:
        next_verification = {c: current.get(c) for c in _VERIFICATION_COLS}

    next_qh: dict[str, Any] = {
        k: current[k] for k in QUIET_HOURS_COLUMNS
    }
    if quiet_hours_start_hour is not _UNSET:
        next_qh["quiet_hours_start_hour"] = _normalize_quiet_hour(quiet_hours_start_hour)
    if quiet_hours_end_hour is not _UNSET:
        next_qh["quiet_hours_end_hour"] = _normalize_quiet_hour(quiet_hours_end_hour)
    if quiet_hours_tz is not _UNSET:
        next_qh["quiet_hours_tz"] = _normalize_quiet_tz(quiet_hours_tz)

    cols = (
        list(PREF_COLUMNS.keys())
        + ["sms_phone_number"]
        + list(QUIET_HOURS_COLUMNS.keys())
        + list(_VERIFICATION_COLS)
    )
    placeholders = ", ".join(f"${i + 2}" for i in range(len(cols)))
    set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols)
    sql = (
        f"INSERT INTO user_notification_prefs (clerk_user_id, {', '.join(cols)}, updated_at) "
        f"VALUES ($1, {placeholders}, now()) "
        f"ON CONFLICT (clerk_user_id) DO UPDATE SET {set_clause}, updated_at = now()"
    )
    args = (
        [clerk_user_id]
        + [next_vals[c] for c in PREF_COLUMNS]
        + [next_phone]
        + [next_qh[c] for c in QUIET_HOURS_COLUMNS]
        + [next_verification[c] for c in _VERIFICATION_COLS]
    )
    try:
        await db.execute(sql, *args)
    except Exception as e:
        log.warning("set_prefs failed for %s: %s", clerk_user_id, e)

    out: dict[str, object] = dict(next_vals)  # type: ignore[assignment]
    out["sms_phone_number"] = next_phone
    for k in QUIET_HOURS_COLUMNS:
        out[k] = next_qh[k]
    for k in _VERIFICATION_COLS:
        out[k] = next_verification[k]
    out["sms_phone_verified"] = next_verification["sms_phone_verified_at"] is not None
    return out


# ---------------------------------------------------------------------------
# SMS phone-number verification (task #191)
# ---------------------------------------------------------------------------


async def issue_phone_verification_code(
    clerk_user_id: str,
) -> tuple[str, str, datetime] | None:
    """Generate a fresh 6-digit code for the user's saved phone number.

    Persists the code + expiry, and clears any prior verification so the
    number is treated as unverified until the new code is confirmed.
    Returns ``(code, phone, expires_at)`` on success, or ``None`` if the
    user has no phone on file (caller should surface a 400 in that case).
    """
    await ensure_schema()
    try:
        row = await db.fetchrow(
            "SELECT sms_phone_number FROM user_notification_prefs "
            "WHERE clerk_user_id = $1",
            clerk_user_id,
        )
    except Exception as e:
        log.warning("issue_phone_verification_code lookup failed: %s", e)
        return None
    phone = (row or {}).get("sms_phone_number") if row else None
    phone = (str(phone).strip() if phone else "") or None
    if not phone:
        return None
    code = f"{secrets.randbelow(1_000_000):06d}"
    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=SMS_VERIFICATION_TTL_SECONDS
    )
    try:
        await db.execute(
            """
            UPDATE user_notification_prefs
               SET sms_phone_verification_code = $2,
                   sms_phone_verification_expires_at = $3,
                   sms_phone_verified_at = NULL,
                   updated_at = now()
             WHERE clerk_user_id = $1
            """,
            clerk_user_id, code, expires_at,
        )
    except Exception as e:
        log.warning("issue_phone_verification_code persist failed: %s", e)
        return None
    return code, phone, expires_at


async def confirm_phone_verification_code(
    clerk_user_id: str, code: str
) -> bool:
    """Mark the user's saved phone as verified iff ``code`` matches.

    Constant-time string comparison via :func:`secrets.compare_digest`
    so a slow timing oracle can't be used to brute-force the 6-digit
    space. Returns ``False`` for missing / mismatched / expired codes —
    the caller surfaces a generic error so we don't leak which.
    """
    await ensure_schema()
    submitted = (code or "").strip()
    if len(submitted) != 6 or not submitted.isdigit():
        return False
    try:
        row = await db.fetchrow(
            "SELECT sms_phone_number, sms_phone_verification_code, "
            "       sms_phone_verification_expires_at "
            "  FROM user_notification_prefs WHERE clerk_user_id = $1",
            clerk_user_id,
        )
    except Exception as e:
        log.warning("confirm_phone_verification_code lookup failed: %s", e)
        return False
    if not row:
        return False
    expected = row.get("sms_phone_verification_code")
    expires = row.get("sms_phone_verification_expires_at")
    phone = row.get("sms_phone_number")
    if not expected or not phone:
        return False
    if expires is not None:
        try:
            now = datetime.now(timezone.utc)
            exp = expires if expires.tzinfo else expires.replace(tzinfo=timezone.utc)
            if exp < now:
                return False
        except Exception:
            return False
    if not secrets.compare_digest(str(expected), submitted):
        return False
    try:
        await db.execute(
            """
            UPDATE user_notification_prefs
               SET sms_phone_verified_at = now(),
                   sms_phone_verification_code = NULL,
                   sms_phone_verification_expires_at = NULL,
                   updated_at = now()
             WHERE clerk_user_id = $1
            """,
            clerk_user_id,
        )
    except Exception as e:
        log.warning("confirm_phone_verification_code persist failed: %s", e)
        return False
    return True


# ---------------------------------------------------------------------------
# Quiet-hours evaluation
# ---------------------------------------------------------------------------


def is_in_quiet_hours(
    start_hour: Any,
    end_hour: Any,
    tz_name: Any,
    *,
    now: datetime | None = None,
) -> bool:
    """True if the user's current local time falls inside their DND window.

    All three inputs must be present and valid for the window to apply.
    Equal start/end hours mean "no window" (a zero-length range, not
    24h) — to mute around the clock, users should disable push entirely.
    """
    if start_hour is None or end_hour is None or not tz_name:
        return False
    try:
        start = int(start_hour)
        end = int(end_hour)
    except (TypeError, ValueError):
        return False
    if not (0 <= start <= 23 and 0 <= end <= 23):
        return False
    if start == end:
        return False
    try:
        tz = ZoneInfo(str(tz_name))
    except (ZoneInfoNotFoundError, ValueError):
        return False
    local_hour = (now or datetime.now(tz)).astimezone(tz).hour
    if start < end:
        return start <= local_hour < end
    # Overnight window, e.g. 22 -> 7 means [22, 24) ∪ [0, 7).
    return local_hour >= start or local_hour < end


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
    # `sms_phone_verified_at IS NOT NULL` enforces task #191's
    # ownership-confirmation gate: an opted-in user with an unverified
    # number is silently skipped on dispatch, so a typo can't bill us
    # for an undeliverable Twilio segment.
    sql = f"""
        SELECT p.sms_phone_number
          FROM user_notification_prefs p
          LEFT JOIN subscriptions s ON s.user_id = p.clerk_user_id
         WHERE p.{pref_column} = true
           AND p.sms_phone_number IS NOT NULL
           AND p.sms_phone_number <> ''
           AND p.sms_phone_verified_at IS NOT NULL
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
    (or has no prefs row, i.e. defaults), and is not currently inside
    their configured quiet-hours window.

    We left-join `user_notification_prefs` so a user with a registered
    device but no row gets the default-true treatment. Quiet-hours
    filtering happens in Python because Postgres timezone math on
    user-supplied IANA strings would crash the whole query if any one
    user has a malformed value.
    """
    if pref_column not in ("notify_live_trade", "notify_halt", "notify_settlement"):
        raise ValueError(f"bad pref column {pref_column!r}")
    await ensure_schema()
    sql = (
        "SELECT t.expo_token, "
        "       p.quiet_hours_start_hour, "
        "       p.quiet_hours_end_hour, "
        "       p.quiet_hours_tz "
        "  FROM expo_push_tokens t "
        "  LEFT JOIN user_notification_prefs p "
        "    ON p.clerk_user_id = t.clerk_user_id "
        f" WHERE COALESCE(p.{pref_column}, true) = true"
    )
    try:
        rows = await db.fetch(sql)
    except Exception as e:
        log.warning("tokens_for_pref %s failed: %s", pref_column, e)
        return []
    out: list[str] = []
    for r in rows:
        if is_in_quiet_hours(
            r["quiet_hours_start_hour"],
            r["quiet_hours_end_hour"],
            r["quiet_hours_tz"],
        ):
            continue
        out.append(r["expo_token"])
    return out


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
        from ..http_retry import retry_request

        async with httpx.AsyncClient(timeout=10.0) as client:
            async def _do():
                return await client.post(
                    EXPO_PUSH_URL,
                    json=messages,
                    headers={
                        "accept": "application/json",
                        "accept-encoding": "gzip, deflate",
                        "content-type": "application/json",
                    },
                )

            # Expo's push API is idempotent in the receipts model: a
            # duplicated batch yields duplicate tickets but never a
            # double notification (the device dedupes on token+content
            # within the throttle window), so it is safe to retry on
            # 5xx and transport errors.
            r = await retry_request(_do, client_name="expo_push", max_attempts=3)
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
    # Persist a per-user history row for every recipient *before* the
    # network roundtrip — even if Expo rejects the batch, the user
    # should still see the event in the in-app feed.
    await _record_dispatch_for_pref(
        pref_column,
        type_=str(data.get("type") or pref_column),
        title=title,
        body=body,
        deep_link=str(data.get("deep_link") or "") or None,
        data=data,
    )
    return await send_to_tokens(
        tokens, title=title, body=body, data=data, channel_id=channel_id,
    )


# ---------------------------------------------------------------------------
# In-app notification history (task #133)
# ---------------------------------------------------------------------------


# Cap log size per user so a runaway dispatcher can't grow the table
# without bound. We keep ~3x the page size shown in the mobile app.
_LOG_RETAIN_PER_USER = 200


async def _record_dispatch_for_pref(
    pref_column: str,
    *,
    type_: str,
    title: str,
    body: str,
    deep_link: str | None,
    data: dict[str, Any],
) -> None:
    """Insert one log row per opted-in user with a registered device.

    Mirrors the recipient set used by `_tokens_for_pref` (DISTINCT on
    user) so the in-app feed matches what was actually pushed.
    """
    if pref_column not in ("notify_live_trade", "notify_halt", "notify_settlement"):
        return
    sql_users = (
        "SELECT DISTINCT t.clerk_user_id FROM expo_push_tokens t "
        "LEFT JOIN user_notification_prefs p "
        "  ON p.clerk_user_id = t.clerk_user_id "
        f"WHERE COALESCE(p.{pref_column}, true) = true"
    )
    try:
        rows = await db.fetch(sql_users)
    except Exception as e:
        log.warning("notification_log user lookup failed: %s", e)
        return
    if not rows:
        return
    payload = json.dumps(data or {}, default=str)
    args_list = [
        (str(r["clerk_user_id"]), type_, title, body, deep_link, payload)
        for r in rows
    ]
    try:
        await db.executemany(
            "INSERT INTO mobile_notification_log "
            "(clerk_user_id, type, title, body, deep_link, data) "
            "VALUES ($1, $2, $3, $4, $5, $6::jsonb)",
            args_list,
        )
    except Exception as e:
        log.warning("notification_log insert failed: %s", e)
        return
    # Trim per-user history; cheap to do inline because each user only
    # ever has at most _LOG_RETAIN_PER_USER + 1 rows after this insert.
    user_ids = list({a[0] for a in args_list})
    try:
        await db.execute(
            """
            DELETE FROM mobile_notification_log
             WHERE clerk_user_id = ANY($1::text[])
               AND id NOT IN (
                 SELECT id FROM (
                   SELECT id, row_number() OVER (
                            PARTITION BY clerk_user_id
                            ORDER BY created_at DESC, id DESC
                          ) AS rn
                     FROM mobile_notification_log
                    WHERE clerk_user_id = ANY($1::text[])
                 ) ranked
                 WHERE rn <= $2
               )
            """,
            user_ids, _LOG_RETAIN_PER_USER,
        )
    except Exception as e:
        log.warning("notification_log trim failed: %s", e)


async def list_notifications(
    clerk_user_id: str, *, limit: int = 50,
) -> list[dict[str, Any]]:
    await ensure_schema()
    limit = max(1, min(int(limit or 50), 200))
    try:
        rows = await db.fetch(
            """
            SELECT id, type, title, body, deep_link, data,
                   created_at, read_at
              FROM mobile_notification_log
             WHERE clerk_user_id = $1
             ORDER BY created_at DESC, id DESC
             LIMIT $2
            """,
            clerk_user_id, limit,
        )
    except Exception as e:
        log.warning("list_notifications failed for %s: %s", clerk_user_id, e)
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        raw = r.get("data")
        if isinstance(raw, str):
            try:
                payload: Any = json.loads(raw)
            except Exception:
                payload = {}
        else:
            payload = raw or {}
        out.append(
            {
                "id": int(r["id"]),
                "type": r["type"],
                "title": r["title"],
                "body": r["body"],
                "deep_link": r.get("deep_link"),
                "data": payload,
                "created_at": r["created_at"],
                "read_at": r.get("read_at"),
            }
        )
    return out


async def unread_notification_count(clerk_user_id: str) -> int:
    await ensure_schema()
    try:
        v = await db.fetchval(
            "SELECT count(*) FROM mobile_notification_log "
            "WHERE clerk_user_id = $1 AND read_at IS NULL",
            clerk_user_id,
        )
        return int(v or 0)
    except Exception as e:
        log.warning("unread_notification_count failed for %s: %s", clerk_user_id, e)
        return 0


async def mark_notifications_read(
    clerk_user_id: str, *, ids: list[int] | None = None,
) -> int:
    """Mark notifications as read. If `ids` is None, marks all unread."""
    await ensure_schema()
    try:
        if ids:
            clean = [int(i) for i in ids if i is not None]
            if not clean:
                return 0
            res = await db.execute(
                "UPDATE mobile_notification_log SET read_at = now() "
                "WHERE clerk_user_id = $1 AND id = ANY($2::bigint[]) "
                "  AND read_at IS NULL",
                clerk_user_id, clean,
            )
        else:
            res = await db.execute(
                "UPDATE mobile_notification_log SET read_at = now() "
                "WHERE clerk_user_id = $1 AND read_at IS NULL",
                clerk_user_id,
            )
    except Exception as e:
        log.warning("mark_notifications_read failed for %s: %s", clerk_user_id, e)
        return 0
    # asyncpg returns "UPDATE N"
    try:
        return int(str(res).split()[-1])
    except Exception:
        return 0


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


def push_settlement(
    *,
    market_ticker: str,
    vertical: str | None,
    pnl: float,
    outcome_yes: int,
) -> None:
    """Fire-and-forget push fan-out for a position resolution.

    Body carries realized P&L so the user doesn't have to open the app
    to see the outcome. Tap deep-links into the positions screen so the
    fuller breakdown is one hop away.
    """
    try:
        pnl_f = float(pnl)
    except (TypeError, ValueError):
        pnl_f = 0.0
    if pnl_f > 0:
        title = "Position settled — won"
        sign = "+"
    elif pnl_f < 0:
        title = "Position settled — lost"
        sign = "-"
    else:
        title = "Position settled"
        sign = ""
    body = f"{market_ticker}: {sign}${abs(pnl_f):.2f} realized P&L"
    data = {
        "type": "settlement",
        "market_ticker": market_ticker,
        "vertical": vertical,
        "pnl": pnl_f,
        "outcome_yes": outcome_yes,
        "deep_link": "/positions",
    }
    _schedule(_dispatch_for_alert(
        "notify_settlement",
        title=title, body=body, data=data, channel_id="settlement",
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
