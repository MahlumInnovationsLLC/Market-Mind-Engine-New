"""High-level alert helpers — one function per alert type.

Each helper:
  * Builds a subject + body once (Postmark templates are overkill here).
  * Calls postmark.send_in_background() so the caller never waits on
    network IO.
  * Is a no-op when Postmark isn't configured (logged via postmark).

Alert types (per task #11 spec):
  - tier_change          (any subscription tier transition)
  - live_trade           (Elite+ — actually triggers when a non-paper
                          fill arrives; tier filter is on the recipient
                          side)
  - daily_pnl_summary    (scheduled, fires once per UTC day)
  - halt                 (kill switch tripped)
  - drift                (settlement detected per-vertical Brier drift)

Every helper accepts a `dry_run=True` so the operator can hit the test
endpoint and see what an alert *would* look like without spamming
mailboxes.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Iterable

from . import postmark


log = logging.getLogger("axiom.notifications.alerts")


# ---------------------------------------------------------------------------
# Per-user opt-in mapping.
#
# Each operator-broadcast tag maps to the `user_notification_prefs`
# column that controls per-user email opt-ins. Users who toggle the
# matching pref on (and meet any tier gating, e.g. Elite for the
# live-trade firehose) are added to the recipient list alongside the
# existing operator alert emails.
# ---------------------------------------------------------------------------


_TAG_TO_USER_PREF: dict[str, str] = {
    "daily-summary":    "email_daily_summary",
    "daily-loss-halt":  "email_halt",
    "drift":            "email_drift",
    "halt":             "email_halt",
    "live-trade":       "email_live_trade",
    "persona-brier":    "email_drift",
    "weekly-roundup":   "email_weekly_roundup",
}


# ---------------------------------------------------------------------------
# Persistent dedupe — generalized `(alert_type, dedupe_key)` with TTL.
#
# The same `notification_dedupe` table created by `daily_summary` is used.
# Composite key is encoded as `f"{alert_type}:{dedupe_key}"` so the
# original PRIMARY KEY (`key TEXT`) keeps working.  We add an optional
# `expires_at TIMESTAMPTZ` column for TTL semantics; the daily-summary
# rows leave it NULL and rely on their `date_utc` comparison.
#
# Each call site that wants idempotency calls `await dedupe_or_skip(...)`
# *before* dispatching the alert.  Returns True if the caller is the
# winner (should send), False if a previous send is still within the TTL
# window and the alert must be suppressed.
# ---------------------------------------------------------------------------


# In-memory counter of how many alert dispatches were dropped by the
# dedupe layer, keyed by `alert_type`.  Resets on process restart —
# exposed via `/api/notifications/status` so operators can tell
# "everything is quiet" from "the dedupe is hiding something".
_suppression_counts: dict[str, int] = {}


def suppression_counts() -> dict[str, int]:
    """Snapshot of dedupe-suppressed alert counts since process start."""
    return dict(_suppression_counts)


def reset_suppression_counts() -> None:
    """Test helper — wipe the in-memory counter."""
    _suppression_counts.clear()


def _compose_key(alert_type: str, dedupe_key: str) -> str:
    return f"{alert_type}:{dedupe_key}"


async def dedupe_or_skip(
    alert_type: str,
    dedupe_key: str,
    *,
    ttl_seconds: int = 3600,
) -> bool:
    """Atomically claim `(alert_type, dedupe_key)` for the next
    `ttl_seconds`.

    Returns True if the caller is the first to claim the key in this
    TTL window (i.e. should send the alert).  Returns False when an
    earlier send is still within the window.  Fails open (returns True)
    on any DB error so we'd rather risk a duplicate than swallow a
    halt/drift alert.
    """
    full_key = _compose_key(alert_type, dedupe_key)
    from .. import db
    try:
        row = await db.fetchrow(
            """
            INSERT INTO notification_dedupe (key, expires_at)
            VALUES ($1, now() + make_interval(secs => $2))
            ON CONFLICT (key) DO UPDATE
              SET expires_at = EXCLUDED.expires_at,
                  sent_at    = now()
              WHERE notification_dedupe.expires_at IS NULL
                 OR notification_dedupe.expires_at <= now()
            RETURNING 1 AS ok
            """,
            full_key, int(ttl_seconds),
        )
        claimed = bool(row)
        if not claimed:
            _suppression_counts[alert_type] = (
                _suppression_counts.get(alert_type, 0) + 1
            )
        return claimed
    except Exception as e:
        log.warning("dedupe_or_skip claim failed for %s: %s", full_key, e)
        return True


# ---------------------------------------------------------------------------
# Per-user "recent alerts" history.
#
# Each row in `notification_dedupe` represents one alert dispatch claim.
# Some claims are global (operator broadcasts that fan out to every
# opted-in user — `halt`, `drift`, `daily_pnl_summary`, `live_trade`,
# `weekly_roundup`); others are user-scoped and embed the recipient
# `user_id` directly in the dedupe key (`trade_confirmation`,
# `tier_change`).
#
# The settings page surfaces the last ~30 entries the calling user was
# eligible to receive, so a subscriber can sanity-check "did I actually
# get yesterday's drift alert?". The mapping below is the single source
# of truth for which alert types are user-visible, what label to show,
# and which preference columns gate each delivery channel.
# ---------------------------------------------------------------------------


# alert_type -> ("scope", "label", channels)
#   scope:    "broadcast" (every opted-in user is a recipient) or
#             "user" (key prefix `<alert_type>:<user_id>:...` filters by recipient)
#   channels: list of (channel_kind, pref_column or None) — `None` means
#             the channel always fires regardless of opt-in (e.g. tier
#             change emails are transactional).
USER_VISIBLE_ALERTS: dict[str, dict[str, Any]] = {
    "daily_pnl_summary": {
        "label": "Daily P&L summary",
        "scope": "broadcast",
        "channels": [("email", "email_daily_summary")],
    },
    "weekly_roundup": {
        "label": "Weekly roundup",
        "scope": "broadcast",
        "channels": [("email", "email_weekly_roundup")],
    },
    "drift": {
        "label": "Drift alert",
        "scope": "broadcast",
        "channels": [("email", "email_drift")],
    },
    "position_drift": {
        "label": "Position drift alert",
        "scope": "broadcast",
        "channels": [("email", "email_drift")],
    },
    "persona_daily_brier": {
        "label": "Persona Brier degradation",
        "scope": "broadcast",
        "channels": [("email", "email_drift")],
    },
    "halt": {
        "label": "Trading halt",
        "scope": "broadcast",
        "channels": [
            ("email", "email_halt"),
            ("push", "notify_halt"),
        ],
    },
    "daily_loss_halt": {
        "label": "Daily loss halt",
        "scope": "broadcast",
        "channels": [
            ("email", "email_halt"),
            ("push", "notify_halt"),
        ],
    },
    "live_trade": {
        "label": "Live trade fill",
        "scope": "broadcast",
        "channels": [
            ("email", "email_live_trade"),
            ("push", "notify_live_trade"),
            ("sms", "sms_live_trade"),
            ("telegram", "telegram_live_trade"),
        ],
    },
    "trade_confirmation": {
        "label": "Trade confirmation",
        "scope": "user",
        "channels": [("email", "email_trade_confirmation")],
    },
    "tier_change": {
        "label": "Subscription change",
        "scope": "user",
        "channels": [("email", None)],
    },
}


async def list_user_history(
    *, user_id: str, limit: int = 30
) -> list[dict[str, Any]]:
    """Return the most recent alert dispatches the given user was
    eligible for, newest first.

    Combines `notification_dedupe` rows (the source of truth for "was
    an alert claimed/dispatched") with the user's current preference
    snapshot to compute the per-channel breakdown each row would have
    used. Failures fall back to an empty list — this endpoint is
    advisory and must never break the settings page.
    """
    limit = max(1, min(200, int(limit or 30)))
    from .. import db
    from . import push as _push

    # Build a SQL prefix predicate per user-visible alert type so we
    # only scan rows that can ever surface for this user. `key` is
    # either equal to the alert_type (e.g. `daily_pnl_summary`) or
    # starts with `<alert_type>:`. User-scoped types additionally embed
    # the recipient `user_id` after the alert_type.
    #
    # We deliberately use PostgreSQL's `starts_with(key, $prefix)`
    # rather than `key LIKE $prefix || '%'` because Clerk user IDs
    # contain underscores ("user_xyz") and SQL `LIKE` treats `_` as a
    # single-character wildcard — that would let one user's history
    # query match another user's `trade_confirmation:user_zyx:...` keys.
    predicates: list[str] = []
    params: list[Any] = []
    for alert_type, info in USER_VISIBLE_ALERTS.items():
        if info["scope"] == "user":
            params.append(f"{alert_type}:{user_id}:")
            predicates.append(f"starts_with(key, ${len(params)})")
            params.append(f"{alert_type}:{user_id}")
            predicates.append(f"key = ${len(params)}")
        else:
            params.append(f"{alert_type}:")
            predicates.append(f"starts_with(key, ${len(params)})")
            params.append(alert_type)
            predicates.append(f"key = ${len(params)}")
    params.append(limit)
    where_sql = " OR ".join(predicates)
    try:
        rows = await db.fetch(
            f"""
            SELECT key, sent_at, expires_at, date_utc
              FROM notification_dedupe
             WHERE sent_at IS NOT NULL
               AND ({where_sql})
             ORDER BY sent_at DESC
             LIMIT ${len(params)}
            """,
            *params,
        )
    except Exception as e:  # pragma: no cover — advisory endpoint
        log.warning("list_user_history fetch failed: %s", e)
        return []

    try:
        prefs = await _push.get_prefs(user_id)
    except Exception as e:  # pragma: no cover
        log.warning("list_user_history get_prefs failed: %s", e)
        prefs = {}

    out: list[dict[str, Any]] = []
    # Match longest alert_type prefix first so e.g. `daily_loss_halt`
    # never gets misclassified as `daily_pnl_summary`. The ordering
    # also disambiguates `live_trade` from any future `live_trade_*`.
    types_by_len = sorted(
        USER_VISIBLE_ALERTS.keys(), key=len, reverse=True
    )
    for r in rows:
        key = r.get("key") or ""
        alert_type = None
        for t in types_by_len:
            if key == t or key.startswith(f"{t}:"):
                alert_type = t
                break
        if not alert_type:
            continue
        info = USER_VISIBLE_ALERTS[alert_type]
        sent_at = r.get("sent_at")
        try:
            sent_iso = sent_at.isoformat() if sent_at else None
        except Exception:
            sent_iso = str(sent_at) if sent_at else None
        channels = []
        for kind, pref_col in info["channels"]:
            opted_in = True if pref_col is None else bool(prefs.get(pref_col))
            channels.append({
                "kind": kind,
                "pref_column": pref_col,
                "opted_in": opted_in,
            })
        out.append({
            "alert_type": alert_type,
            "label": info["label"],
            "scope": info["scope"],
            "sent_at": sent_iso,
            "dedupe_key": key,
            "channels": channels,
            # We only know the dedupe row was claimed (the alert was
            # *attempted*); per-recipient delivery success would need a
            # separate outbox table. Surface that distinction honestly
            # so the UI doesn't imply confirmed delivery.
            "attempted": True,
        })
    return out


async def gc_expired_dedupe(*, older_than_days: int = 7) -> dict[str, Any]:
    """Delete `notification_dedupe` rows whose TTL expired more than
    `older_than_days` days ago.

    Rows with `expires_at IS NULL` (e.g. the daily-summary marker that
    uses `date_utc` instead of a TTL) are preserved.

    Returns ``{"deleted": int, "remaining": int}`` and logs the same
    counts so operators can confirm the cleanup ran. Fails soft (logs
    + returns zeros) on DB error.
    """
    from .. import db
    try:
        deleted = await db.fetchval(
            """
            WITH d AS (
              DELETE FROM notification_dedupe
               WHERE expires_at IS NOT NULL
                 AND expires_at < now() - make_interval(days => $1)
              RETURNING 1
            )
            SELECT count(*)::int FROM d
            """,
            int(older_than_days),
        )
        remaining = await db.fetchval(
            "SELECT count(*)::int FROM notification_dedupe"
        )
        out = {
            "deleted": int(deleted or 0),
            "remaining": int(remaining or 0),
        }
        log.info(
            "notification_dedupe gc: deleted=%s remaining=%s",
            out["deleted"], out["remaining"],
        )
        return out
    except Exception as e:
        log.warning("notification_dedupe gc failed: %s", e)
        return {"deleted": 0, "remaining": 0, "error": str(e)}


def _hash_reason(reason: str | None) -> str:
    h = hashlib.sha256((reason or "").encode("utf-8")).hexdigest()
    return h[:16]


def _schedule_dedupe_dispatch(
    alert_type: str,
    dedupe_key: str,
    ttl_seconds: int,
    dispatch: "callable[[], dict[str, Any]]",
) -> None:
    """Sync helper — schedule an async dedupe + dispatch on the running
    event loop.  Used by sync call sites (kill switch trip)."""
    async def _runner() -> None:
        try:
            ok = await dedupe_or_skip(alert_type, dedupe_key, ttl_seconds=ttl_seconds)
            if ok:
                dispatch()
            else:
                log.info("alert suppressed by dedupe: %s/%s", alert_type, dedupe_key)
        except Exception as e:  # pragma: no cover
            log.warning("scheduled dedupe dispatch failed: %s", e)
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_runner())
    except RuntimeError:
        # No running loop (e.g. unit test calling sync code) — run inline.
        try:
            asyncio.run(_runner())
        except Exception as e:  # pragma: no cover
            log.warning("inline dedupe dispatch failed: %s", e)


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _format_lines(items: Iterable[tuple[str, Any]]) -> str:
    width = max((len(k) for k, _ in items), default=0)
    return "\n".join(f"  {k.ljust(width)}  {v}" for k, v in items)


# ---------------------------------------------------------------------------
# Tier change
# ---------------------------------------------------------------------------


def tier_change(
    *,
    user_email: str | None,
    user_id: str,
    old_tier: str,
    new_tier: str,
    status: str | None = None,
    stripe_subscription_id: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    direction = (
        "upgraded"
        if _tier_rank(new_tier) > _tier_rank(old_tier)
        else "downgraded"
        if _tier_rank(new_tier) < _tier_rank(old_tier)
        else "changed"
    )
    subject = (
        f"[axiom] Subscription {direction}: {old_tier} -> {new_tier} "
        f"({user_email or user_id})"
    )
    body = (
        f"A subscription {direction}.\n\n"
        + _format_lines(
            [
                ("user_email", user_email or "(unknown)"),
                ("user_id", user_id),
                ("old_tier", old_tier),
                ("new_tier", new_tier),
                ("status", status or "(n/a)"),
                ("stripe_subscription_id", stripe_subscription_id or "(none)"),
                ("at", _utc_now_iso()),
            ]
        )
        + "\n"
    )
    return _dispatch(subject, body, tag="tier-change", dry_run=dry_run)


def _tier_rank(tier: str) -> int:
    order = {"free": 0, "pro": 1, "elite": 2, "founder": 3}
    return order.get((tier or "").lower(), -1)


# ---------------------------------------------------------------------------
# Live trade execution (Elite-tier alert per spec)
# ---------------------------------------------------------------------------


def live_trade(
    *,
    market_ticker: str,
    side: str,
    action: str,
    contracts: int,
    price_cents: int | None = None,
    order_id: str | None = None,
    fill_id: str | None = None,
    vertical: str | None = None,
    exchange: str = "kalshi",
    dry_run: bool = False,
) -> dict[str, Any]:
    px = f"{price_cents}¢" if price_cents is not None else "(market)"
    venue = (exchange or "kalshi").lower()
    venue_label = venue.title()
    subject = (
        f"[axiom][LIVE {venue}] {action.upper()} {side.upper()} {contracts}x "
        f"{market_ticker} @ {px}"
    )
    body = (
        f"A LIVE {venue_label} order filled (not paper).\n\n"
        + _format_lines(
            [
                ("exchange", venue),
                ("market", market_ticker),
                ("side", side),
                ("action", action),
                ("contracts", contracts),
                ("price_cents", px),
                ("order_id", order_id or "(unknown)"),
                ("fill_id", fill_id or "(unknown)"),
                ("vertical", vertical or "(unknown)"),
                ("at", _utc_now_iso()),
            ]
        )
        + "\n"
    )
    result = _dispatch(subject, body, tag="live-trade", dry_run=dry_run)
    if not dry_run:
        try:
            from . import push as _push
            _push.push_live_trade(
                market_ticker=market_ticker,
                side=side,
                action=action,
                contracts=int(contracts),
                price_cents=price_cents,
                vertical=vertical,
            )
        except Exception as e:  # pragma: no cover — push must not break alerting
            log.warning("live_trade push dispatch failed: %s", e)
        # Elite-only premium channels: SMS + Telegram fan-out for any
        # opted-in user who's on an active Elite/Founder plan.
        try:
            _schedule_premium_live_trade(subject=subject, body_short=(
                f"LIVE {venue} {action.upper()} {side.upper()} {contracts}x "
                f"{market_ticker} @ {px}"
            ))
        except Exception as e:  # pragma: no cover
            log.warning("live_trade premium-channel dispatch failed: %s", e)
    return result


def _schedule_premium_live_trade(*, subject: str, body_short: str) -> None:
    """Fan out a live-trade alert to Elite SMS + Telegram opt-ins.

    Both channels are gated on the user being on an active Elite (or
    Founder) plan; the resolution happens server-side in
    ``push.opted_in_*`` so we can't accidentally bill SMS for a Pro
    user who flipped the toggle.
    """
    from . import push as _push, sms as _sms
    from ..telegram import bot as _tg_bot

    async def _runner() -> None:
        try:
            phones = await _push.opted_in_sms_phones("sms_live_trade")
            if phones:
                await _sms.send(phones, body_short, tag="live-trade")
        except Exception as e:  # pragma: no cover
            log.warning("live_trade SMS fan-out failed: %s", e)
        try:
            chats = await _push.opted_in_telegram_chats("telegram_live_trade")
            if chats:
                _tg_bot.send_to_chats_in_background(chats, f"{subject}\n\n{body_short}")
        except Exception as e:  # pragma: no cover
            log.warning("live_trade Telegram fan-out failed: %s", e)

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_runner())
    except RuntimeError:
        try:
            asyncio.run(_runner())
        except Exception as e:  # pragma: no cover
            log.warning("inline premium live_trade dispatch failed: %s", e)


# ---------------------------------------------------------------------------
# Per-user trade confirmation email (task #269).
#
# Unlike `live_trade` (operator-broadcast), this is a single branded
# HTML receipt sent to the end user whose order filled. It carries
# market/side/qty/price/position detail and a link back to the
# dashboard. Trigger lives in `kalshi.lifecycle.handle_ws_fill`.
# ---------------------------------------------------------------------------


def _public_base_url() -> str:
    """Reuse the public-URL resolver from `notifications.email` so the
    dashboard CTA ends up on the same origin as billing links."""
    from . import email as _email
    return _email._public_base_url()


def _fmt_cents_usd(c: int | None) -> str:
    if c is None:
        return "—"
    sign = "-" if c < 0 else ""
    return f"{sign}${abs(int(c)) / 100.0:,.2f}"


def _render_trade_confirmation_html(
    *,
    market_title: str,
    market_ticker: str,
    side: str,
    action: str,
    contracts: int,
    avg_price_cents: int,
    total_cost_cents: int,
    current_position: int,
    dashboard_url: str,
    vertical: str | None,
    order_id: str,
    fill_id: str,
    timestamp_iso: str,
    partial: bool,
    digest: bool,
    fills_summed: int | None,
) -> str:
    side_label = (side or "").upper()
    action_label = (action or "").upper()
    fill_note = ""
    if digest and fills_summed is not None and fills_summed > 1:
        fill_note = (
            f'<div style="color:#6B7280;font-size:12px;margin-top:4px;">'
            f"Consolidated across {fills_summed} partial fills.</div>"
        )
    elif partial:
        fill_note = (
            '<div style="color:#6B7280;font-size:12px;margin-top:4px;">'
            "Partial fill — remaining contracts are still resting.</div>"
        )
    rows = [
        ("Market", f"{market_title} <span style=\"color:#6B7280;\">({market_ticker})</span>"),
        ("Side", side_label),
        ("Action", action_label),
        ("Contracts filled", f"{contracts}"),
        ("Average price", f"{avg_price_cents}¢"),
        ("Total cost", _fmt_cents_usd(total_cost_cents)),
        ("Position after fill", f"{current_position}"),
    ]
    if vertical:
        rows.append(("Vertical", vertical))
    rows.extend([
        ("Order ID", f'<code style="font-family:monospace;font-size:12px;">{order_id}</code>'),
        ("Fill ID", f'<code style="font-family:monospace;font-size:12px;">{fill_id}</code>'),
        ("Timestamp", timestamp_iso),
    ])
    row_html = "".join(
        f'<tr><td style="padding:8px 12px;color:#6B7280;font-size:12px;'
        f'text-transform:uppercase;letter-spacing:0.08em;'
        f'border-bottom:1px solid #E5E7EB;white-space:nowrap;">{k}</td>'
        f'<td style="padding:8px 12px;color:#111827;font-size:14px;'
        f'border-bottom:1px solid #E5E7EB;">{v}</td></tr>'
        for k, v in rows
    )
    return f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#F9FAFB;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Inter,Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#F9FAFB;padding:24px 0;">
    <tr><td align="center">
      <table width="560" cellpadding="0" cellspacing="0" style="background:#FFFFFF;border:1px solid #E5E7EB;border-radius:8px;overflow:hidden;">
        <tr>
          <td style="background:#111827;color:#F9FAFB;padding:20px 24px;">
            <div style="font-size:11px;letter-spacing:0.16em;text-transform:uppercase;color:#9CA3AF;">Axiom Predict</div>
            <div style="font-size:20px;font-weight:700;margin-top:4px;">Trade confirmation</div>
          </td>
        </tr>
        <tr>
          <td style="padding:20px 24px 8px 24px;">
            <div style="font-size:14px;color:#111827;">
              Your Kalshi order just filled{' (partially)' if partial and not digest else ''}.
            </div>
            {fill_note}
          </td>
        </tr>
        <tr>
          <td style="padding:8px 24px 8px 24px;">
            <table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #E5E7EB;border-radius:6px;border-collapse:separate;">
              {row_html}
            </table>
          </td>
        </tr>
        <tr>
          <td style="padding:16px 24px 24px 24px;">
            <a href="{dashboard_url}" style="display:inline-block;background:#2563EB;color:#FFFFFF;text-decoration:none;padding:10px 16px;border-radius:6px;font-weight:600;font-size:14px;">View in dashboard</a>
          </td>
        </tr>
        <tr>
          <td style="padding:12px 24px 20px 24px;border-top:1px solid #E5E7EB;color:#6B7280;font-size:11px;line-height:1.5;">
            You're receiving this because you opted in to trade confirmations.
            Turn them off any time from Settings → Notifications.
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def _render_trade_confirmation_text(
    *,
    market_title: str,
    market_ticker: str,
    side: str,
    action: str,
    contracts: int,
    avg_price_cents: int,
    total_cost_cents: int,
    current_position: int,
    dashboard_url: str,
    vertical: str | None,
    order_id: str,
    fill_id: str,
    timestamp_iso: str,
    partial: bool,
    digest: bool,
    fills_summed: int | None,
) -> str:
    lines: list[tuple[str, Any]] = [
        ("market", f"{market_title} ({market_ticker})"),
        ("side", (side or "").upper()),
        ("action", (action or "").upper()),
        ("contracts_filled", contracts),
        ("avg_price", f"{avg_price_cents}¢"),
        ("total_cost", _fmt_cents_usd(total_cost_cents)),
        ("position_after", current_position),
    ]
    if vertical:
        lines.append(("vertical", vertical))
    lines.extend([
        ("order_id", order_id),
        ("fill_id", fill_id),
        ("at", timestamp_iso),
    ])
    header = "Your Kalshi order just filled"
    if digest and fills_summed is not None and fills_summed > 1:
        header += f" (consolidated across {fills_summed} partial fills)"
    elif partial:
        header += " (partial — remaining contracts still resting)"
    return (
        f"{header}.\n\n"
        + _format_lines(lines)
        + f"\n\nView in dashboard: {dashboard_url}\n"
        + "\n--\n"
        + "You're receiving this because you opted in to trade confirmations.\n"
        + "Turn them off from Settings -> Notifications.\n"
    )


async def _send_trade_confirmation(
    *,
    user_id: str,
    email: str,
    subject: str,
    text_body: str,
    html_body: str,
    dedupe_key: str,
    dry_run: bool,
) -> dict[str, Any]:
    claimed = await dedupe_or_skip(
        "trade_confirmation", dedupe_key, ttl_seconds=7 * 24 * 3600
    )
    if not claimed:
        log.info(
            "trade_confirmation suppressed by dedupe: %s", dedupe_key
        )
        return {"sent": False, "skipped_reason": "dedupe"}
    from . import suppression as _supp
    try:
        if await _supp.is_suppressed(email):
            log.info("trade_confirmation suppressed (bounce/unsub): %s", email)
            return {"sent": False, "skipped_reason": "suppressed"}
    except Exception as e:  # pragma: no cover
        log.warning("trade_confirmation suppression check failed: %s", e)
    try:
        results = await postmark.send(
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            to=[email],
            tag="trade-confirmation",
            dry_run=dry_run,
        )
        return {"sent": True, "dry_run": dry_run, "results": results}
    except Exception as e:  # pragma: no cover
        log.warning("trade_confirmation send failed for %s: %s", user_id, e)
        return {"sent": False, "error": str(e)}


def trade_confirmation(
    *,
    user_id: str,
    email: str | None,
    market_ticker: str,
    market_title: str | None,
    side: str,
    action: str,
    contracts: int,
    avg_price_cents: int,
    total_cost_cents: int | None,
    current_position: int,
    order_id: str,
    fill_id: str,
    vertical: str | None = None,
    partial: bool = False,
    digest: bool = False,
    fills_summed: int | None = None,
    dashboard_url: str | None = None,
    dry_run: bool = False,
) -> "asyncio.Task | dict[str, Any]":
    """Per-user HTML trade confirmation.

    Non-blocking: schedules the send on the running loop and returns
    the ``asyncio.Task``. Runs inline when no loop is available (sync
    test path).
    """
    if not email:
        log.info(
            "trade_confirmation skipped (no email) user=%s order=%s fill=%s",
            user_id, order_id, fill_id,
        )
        return {"sent": False, "skipped_reason": "no_email"}
    title = market_title or market_ticker
    total_c = (
        int(avg_price_cents) * int(contracts)
        if total_cost_cents is None
        else int(total_cost_cents)
    )
    url = dashboard_url or f"{_public_base_url()}/positions"
    ts = _utc_now_iso()
    subject = (
        f"[Axiom Predict] Filled: {action.upper()} {side.upper()} "
        f"{contracts}x {market_ticker} @ {avg_price_cents}¢"
    )
    html_body = _render_trade_confirmation_html(
        market_title=title, market_ticker=market_ticker,
        side=side, action=action, contracts=int(contracts),
        avg_price_cents=int(avg_price_cents),
        total_cost_cents=int(total_c),
        current_position=int(current_position),
        dashboard_url=url, vertical=vertical,
        order_id=order_id, fill_id=fill_id, timestamp_iso=ts,
        partial=partial, digest=digest, fills_summed=fills_summed,
    )
    text_body = _render_trade_confirmation_text(
        market_title=title, market_ticker=market_ticker,
        side=side, action=action, contracts=int(contracts),
        avg_price_cents=int(avg_price_cents),
        total_cost_cents=int(total_c),
        current_position=int(current_position),
        dashboard_url=url, vertical=vertical,
        order_id=order_id, fill_id=fill_id, timestamp_iso=ts,
        partial=partial, digest=digest, fills_summed=fills_summed,
    )
    dedupe_key = f"{user_id}:{order_id}:{fill_id}"
    if dry_run:
        log.info(
            "trade_confirmation dry_run user=%s order=%s fill=%s",
            user_id, order_id, fill_id,
        )
        return {
            "dry_run": True,
            "tag": "trade-confirmation",
            "subject": subject,
            "body_preview": text_body[:4000],
            "would_send_to": [email],
            "dedupe_key": dedupe_key,
        }
    coro = _send_trade_confirmation(
        user_id=user_id, email=email,
        subject=subject, text_body=text_body, html_body=html_body,
        dedupe_key=dedupe_key, dry_run=False,
    )
    try:
        loop = asyncio.get_running_loop()
        return loop.create_task(coro)
    except RuntimeError:
        try:
            return asyncio.run(coro)
        except Exception as e:  # pragma: no cover
            log.warning("inline trade_confirmation send failed: %s", e)
            return {"sent": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Halt / kill switch
# ---------------------------------------------------------------------------


def halt(
    *,
    reason: str | None,
    actor: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    subject = "[axiom][HALT] Trading kill-switch tripped"
    body = (
        "The Kalshi trading kill switch was tripped. Live order placement "
        "is now blocked until an operator resumes.\n\n"
        + _format_lines(
            [
                ("reason", reason or "(unspecified)"),
                ("actor", actor or "(api)"),
                ("at", _utc_now_iso()),
            ]
        )
        + "\n"
    )
    result = _dispatch(subject, body, tag="halt", dry_run=dry_run)
    if not dry_run:
        try:
            from . import push as _push
            _push.push_halt(reason=reason)
        except Exception as e:  # pragma: no cover — push must not break alerting
            log.warning("halt push dispatch failed: %s", e)
        try:
            from ..telegram import notify as _tg_notify
            _tg_notify.broadcast_in_background(f"{subject}\n\n{body}")
        except Exception as e:  # pragma: no cover
            log.warning("halt telegram broadcast failed: %s", e)
    return result


# ---------------------------------------------------------------------------
# Daily-loss kill switch tripped
# ---------------------------------------------------------------------------


def daily_loss_halt(
    *,
    date_utc: str,
    loss_usd: float,
    realized_pnl_usd: float,
    unrealized_pnl_usd: float,
    threshold_usd: float | None,
    threshold_pct: float | None,
    threshold_pct_usd: float | None,
    start_of_day_bankroll_usd: float | None,
    reason: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Operator alert for the daily-loss auto-halt.

    Raised by :mod:`app.risk.daily_loss` when today's realized +
    unrealized P&L draws down past either the absolute or percent cap.
    Once tripped, only an operator can re-enable trading — the gate
    never auto-untrips on P&L recovery.
    """
    def _fmt(v: float | None, pre: str = "$") -> str:
        if v is None:
            return "(n/a)"
        return f"{pre}{v:,.2f}"

    subject = (
        f"[axiom][HALT] Daily loss cap breached :: -${loss_usd:,.2f} on {date_utc}"
    )
    rows: list[tuple[str, Any]] = [
        ("date_utc", date_utc),
        ("loss", _fmt(-loss_usd) if loss_usd else "$0.00"),
        ("realized_pnl", _fmt(realized_pnl_usd)),
        ("unrealized_pnl", _fmt(unrealized_pnl_usd)),
        ("threshold_abs", _fmt(threshold_usd)),
        (
            "threshold_pct",
            "(n/a)"
            if threshold_pct is None
            else f"{threshold_pct:.2f}% ({_fmt(threshold_pct_usd)})",
        ),
        ("start_of_day_bankroll", _fmt(start_of_day_bankroll_usd)),
        ("reason", reason or "(unspecified)"),
        ("at", _utc_now_iso()),
    ]
    body = (
        "The daily-loss kill switch tripped. Live order placement is now "
        "blocked until an operator re-enables trading from "
        "/system -> controls.\n\n"
        + _format_lines(rows)
        + "\n"
    )
    result = _dispatch(subject, body, tag="daily-loss-halt", dry_run=dry_run)
    # Parity with alerts.halt(): fan out to the halt push channel so
    # operators get the same cross-channel notification as any other
    # halt-class incident.  Push failures must never break the email
    # dispatch contract.
    if not dry_run:
        try:
            from . import push as _push
            _push.push_halt(
                reason=reason or f"daily-loss cap breached (-${loss_usd:,.2f})"
            )
        except Exception as e:  # pragma: no cover — push must not break alerting
            log.warning("daily_loss_halt push dispatch failed: %s", e)
        try:
            from ..telegram import notify as _tg_notify
            _tg_notify.broadcast_in_background(f"{subject}\n\n{body}")
        except Exception as e:  # pragma: no cover
            log.warning("daily_loss_halt telegram broadcast failed: %s", e)
    return result


# ---------------------------------------------------------------------------
# Emergency stop alert (operator hit the kill-all button)
# ---------------------------------------------------------------------------


def emergency_stop(
    *,
    actor: str | None,
    canceled_count: int,
    reason: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    subject = "[axiom][EMERGENCY STOP] Trading halted, all orders canceled"
    body = (
        "An operator triggered the emergency-stop endpoint. Every resting "
        "Kalshi order has been canceled and trading_mode is now `halt`.\n\n"
        + _format_lines(
            [
                ("actor", actor or "(unknown)"),
                ("canceled_orders", canceled_count),
                ("reason", reason or "(unspecified)"),
                ("at", _utc_now_iso()),
            ]
        )
        + "\n"
    )
    result = _dispatch(subject, body, tag="emergency_stop", dry_run=dry_run)
    if not dry_run:
        try:
            from ..telegram import notify as _tg_notify
            _tg_notify.broadcast_in_background(f"{subject}\n\n{body}")
        except Exception as e:  # pragma: no cover
            log.warning("emergency_stop telegram broadcast failed: %s", e)
    return result


# ---------------------------------------------------------------------------
# Drift alert (raised by settlement loop)
# ---------------------------------------------------------------------------


def drift(
    *,
    vertical: str,
    metric: str,
    value: float | None,
    threshold: float | None,
    note: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    subject = f"[axiom][DRIFT] {vertical} :: {metric} crossed threshold"
    body = (
        "Calibration / drift threshold exceeded.\n\n"
        + _format_lines(
            [
                ("vertical", vertical),
                ("metric", metric),
                ("value", value if value is not None else "(n/a)"),
                ("threshold", threshold if threshold is not None else "(n/a)"),
                ("note", note or ""),
                ("at", _utc_now_iso()),
            ]
        )
        + "\n"
    )
    return _dispatch(subject, body, tag="drift", dry_run=dry_run)


# ---------------------------------------------------------------------------
# Per-persona daily Brier degradation
# ---------------------------------------------------------------------------


def persona_brier_drift(
    *,
    persona_id: str,
    date_utc: str,
    brier: float,
    debates: int,
    abs_threshold: float,
    jump_threshold: float,
    severity: str,
    reason: str,
    baseline: float | None = None,
    jump: float | None = None,
    note: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Operator alert: a single persona's daily Brier crossed one of the
    configured thresholds.

    Caller (`notifications.persona_brier_alerts`) is responsible for
    dedupe (use `dedupe_or_skip` keyed on
    ``f"{persona_id}:{severity}:{date_utc}"`` with a 24h TTL) so a
    persona that flaps in/out of the window only pages once per day.
    """
    sev = (severity or "warn").lower()
    sev_tag = "HIGH" if sev == "high" else "WARN"
    subject = (
        f"[axiom][PERSONA-BRIER][{sev_tag}] {persona_id} :: "
        f"daily Brier {brier:.4f} on {date_utc}"
    )
    body = (
        "A swarm persona's daily Brier degraded sharply. "
        "Investigate the persona's recent verdicts before the "
        "weighted-vote impact compounds.\n\n"
        + _format_lines(
            [
                ("persona_id", persona_id),
                ("date_utc", date_utc),
                ("debates", debates),
                ("brier", f"{brier:.4f}"),
                (
                    "all_time_brier",
                    f"{baseline:.4f}" if baseline is not None else "(n/a)",
                ),
                (
                    "jump",
                    f"+{jump:.4f}" if jump is not None else "(n/a)",
                ),
                ("abs_threshold", f"{abs_threshold:.2f}"),
                ("jump_threshold", f"+{jump_threshold:.2f}"),
                ("trigger", reason),
                ("severity", sev),
                ("at", _utc_now_iso()),
            ]
        )
        + "\n"
    )
    if note:
        body += f"\nDetails:\n  {note}\n"
    return _dispatch(subject, body, tag="persona-brier", dry_run=dry_run)


# ---------------------------------------------------------------------------
# Model integrity verification failure (manifest mismatch on boot)
# ---------------------------------------------------------------------------


def model_integrity_failure(
    *,
    manifest_version: str | None,
    missing: list[str] | None = None,
    mismatch: list[str] | None = None,
    error: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Operator alert: the on-boot manifest verification refused readiness.

    Fired from `app.models_loader.ensure_artifacts` when the scanned
    predictors don't match the published manifest (missing required
    files or sha256 mismatch). The trader stays in degraded mode until
    the deployment is fixed, so paging the operator immediately is the
    whole point of this alert.
    """
    miss = list(missing or [])
    mis = list(mismatch or [])
    bad_count = len(miss) + len(mis)
    subject = (
        f"[axiom][MODEL INTEGRITY] Manifest verification failed "
        f"({bad_count} bad file(s), version={manifest_version or 'unknown'})"
    )

    def _bullets(label: str, items: list[str]) -> str:
        if not items:
            return f"  {label}: (none)\n"
        return f"  {label}:\n" + "".join(f"    - {n}\n" for n in items)

    body = (
        "The model loader refused to mark itself ready: the predictor "
        "files on disk do not match the published manifest. The trader "
        "is in degraded mode and will not place orders until the "
        "deployment is repaired.\n\n"
        + _format_lines(
            [
                ("manifest_version", manifest_version or "(unknown)"),
                ("missing_count", len(miss)),
                ("mismatch_count", len(mis)),
                ("at", _utc_now_iso()),
            ]
        )
        + "\n\n"
        + _bullets("missing files", miss)
        + _bullets("sha256 mismatch", mis)
    )
    if error:
        body += f"\nLoader error:\n  {error}\n"
    return _dispatch(
        subject, body, tag="model-integrity-failure", dry_run=dry_run
    )


# ---------------------------------------------------------------------------
# WebSocket subscriber stuck / silent
# ---------------------------------------------------------------------------


def ws_subscriber_silent(
    *,
    hub: str,
    label: str,
    qsize: int,
    maxsize: int | None,
    dropped: int,
    overflowed: bool,
    consecutive_snapshots: int,
    age_seconds: float | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Operator alert: a WebSocket subscriber's queue has stayed >=90%
    full (or tripped close-on-overflow) for ``consecutive_snapshots``
    health snapshots in a row.

    The body identifies the offending hub + label so the operator knows
    which page / endpoint to investigate.
    """
    pct = (
        f"{(qsize / maxsize) * 100:.0f}% ({qsize}/{maxsize})"
        if maxsize else f"{qsize}"
    )
    state = "overflowed (closed)" if overflowed else f"{pct} full"
    subject = (
        f"[axiom] WS subscriber stuck: {hub}/{label} ({state})"
    )
    body = (
        "A WebSocket subscriber has been pinned at >=90% queue depth "
        "(or tripped the close-on-overflow policy) for "
        f"{consecutive_snapshots} consecutive health snapshots.\n\n"
        "This usually means a client (operator browser tab, mobile app, "
        "or downstream consumer) stopped reading from the hub but the "
        "TCP socket is still open — investigate the page tied to the "
        "hub label below.\n\n"
        + _format_lines(
            [
                ("hub", hub),
                ("label", label),
                ("qsize", qsize),
                ("maxsize", maxsize if maxsize is not None else "(n/a)"),
                ("dropped", dropped),
                ("overflowed", overflowed),
                ("consecutive_snapshots", consecutive_snapshots),
                (
                    "age_seconds",
                    f"{age_seconds:.1f}" if age_seconds is not None else "(n/a)",
                ),
                ("at", _utc_now_iso()),
            ]
        )
        + "\n"
    )
    return _dispatch(subject, body, tag="ws-subscriber-silent", dry_run=dry_run)


def ws_hub_overflowing(
    *,
    hub: str,
    maxsize: int,
    env_var: str,
    overflowed_subscribers: int,
    near_full_subscribers: int,
    total_subscribers: int,
    max_qsize: int,
    consecutive_snapshots: int,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Operator alert: a whole WS fan-out hub keeps pinning at its
    queue ceiling.

    Distinct from :func:`ws_subscriber_silent`, which fingerprints a
    single stuck connection by its subscription id. The per-hub view
    catches the misconfigured-ceiling case (typically kalshi /
    polymarket orderbooks) where a bursty hub sees subscribers
    overflow and reconnect faster than the per-id counter can
    accumulate. The body advertises the resolved ``maxsize`` and the
    env var that overrides it so the on-call knows whether to bump
    the ceiling or hunt for a stuck consumer.
    """
    if overflowed_subscribers > 0:
        state = (
            f"{overflowed_subscribers}/{total_subscribers} "
            "subscriber(s) overflowed (closed)"
        )
    else:
        state = (
            f"{near_full_subscribers}/{total_subscribers} "
            f"subscriber(s) >=90% full (max qsize {max_qsize}/{maxsize})"
        )
    subject = f"[axiom] WS hub overflowing: {hub} ({state})"
    body = (
        f"WebSocket fan-out hub '{hub}' has had at least one subscriber "
        "overflowed or pinned at >=90% queue depth for "
        f"{consecutive_snapshots} consecutive health snapshots.\n\n"
        "If this hub is bursty by design (e.g. kalshi/polymarket "
        f"orderbooks), bump the ceiling via the {env_var} env var. "
        "Otherwise, investigate which subscriber stopped reading from "
        "the hub.\n\n"
        + _format_lines(
            [
                ("hub", hub),
                ("resolved_maxsize", maxsize),
                ("override_env", env_var),
                ("overflowed_subscribers", overflowed_subscribers),
                ("near_full_subscribers", near_full_subscribers),
                ("total_subscribers", total_subscribers),
                ("max_qsize_observed", max_qsize),
                ("consecutive_snapshots", consecutive_snapshots),
                ("at", _utc_now_iso()),
            ]
        )
        + "\n"
    )
    return _dispatch(subject, body, tag="ws-hub-overflowing", dry_run=dry_run)


# ---------------------------------------------------------------------------
# Crypto OHLCV feed freshness
# ---------------------------------------------------------------------------


def crypto_feed_silent(
    *,
    symbol: str,
    granularity: str,
    expected_seconds: int,
    age_seconds: float | None,
    max_ts: str | None,
    red_threshold_seconds: int,
    consecutive_snapshots: int,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Operator alert: a (symbol, granularity) crypto bar pair has been
    `down` (no fresh bar within the red threshold) for
    ``consecutive_snapshots`` consecutive health snapshots.

    The body identifies the offending pair so the operator knows which
    feed/provider to investigate before the predictor starts trading
    on stale prices.
    """
    age_label = (
        f"{age_seconds:.0f}s old" if age_seconds is not None else "no bars"
    )
    subject = (
        f"[axiom] Crypto feed stale: {symbol}/{granularity} ({age_label})"
    )
    body = (
        f"The crypto bar feed for '{symbol}/{granularity}' has been stale\n"
        "(older than the red freshness threshold, or missing entirely) for "
        f"{consecutive_snapshots} consecutive health snapshots.\n\n"
        "The crypto-OHLCV poller is likely erroring or rate-limited; "
        "investigate the feed registry and provider keys before the "
        "predictor starts trading on stale prices.\n\n"
        + _format_lines(
            [
                ("symbol", symbol),
                ("granularity", granularity),
                ("expected_cadence_seconds", expected_seconds),
                ("red_threshold_seconds", red_threshold_seconds),
                (
                    "age_seconds",
                    f"{age_seconds:.0f}" if age_seconds is not None else "(no bars)",
                ),
                ("max_ts", max_ts or "(none)"),
                ("consecutive_snapshots", consecutive_snapshots),
                ("at", _utc_now_iso()),
            ]
        )
        + "\n"
    )
    return _dispatch(subject, body, tag="crypto-feed-silent", dry_run=dry_run)


# ---------------------------------------------------------------------------
# RevenueCat (mobile IAP) webhook health
# ---------------------------------------------------------------------------


def rc_webhook_failure_rate(
    *,
    window_minutes: int,
    total: int,
    failures: int,
    pct: float,
    threshold_pct: float,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Fire when the per-hour RevenueCat webhook 5xx rate crosses
    `threshold_pct`. A silent webhook outage means paying mobile
    customers stop being upgraded, so this is treated as an operator
    alert (no user fan-out).
    """
    subject = (
        f"[axiom][RC-WEBHOOK] 5xx rate {pct:.0f}% "
        f"({failures}/{total} in {window_minutes}m)"
    )
    body = (
        "RevenueCat webhook deliveries are failing at an elevated rate.\n"
        "Mobile in-app purchases will not flip subscription tiers until\n"
        "the handler is healthy again.\n\n"
        + _format_lines(
            [
                ("window_minutes", window_minutes),
                ("deliveries_total", total),
                ("deliveries_5xx", failures),
                ("failure_rate_pct", f"{pct:.2f}"),
                ("threshold_pct", f"{threshold_pct:.2f}"),
                ("at", _utc_now_iso()),
            ]
        )
        + "\n"
    )
    return _dispatch(subject, body, tag="rc-webhook-failures", dry_run=dry_run)


def rc_webhook_silence(
    *,
    silence_seconds: int,
    threshold_seconds: int,
    last_event_at: str | None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Fire when no RevenueCat events have arrived during business hours
    for longer than `threshold_seconds`. Catches dashboard pause/resume
    misconfiguration and upstream outages.
    """
    minutes = silence_seconds // 60
    subject = (
        f"[axiom][RC-WEBHOOK] No events for {minutes}m "
        f"(threshold {threshold_seconds // 60}m)"
    )
    body = (
        "No RevenueCat webhook deliveries have arrived during business\n"
        "hours for longer than the configured silence threshold. Check\n"
        "the RevenueCat dashboard webhook config (paused?) and the\n"
        "api-server logs.\n\n"
        + _format_lines(
            [
                ("silence_seconds", silence_seconds),
                ("threshold_seconds", threshold_seconds),
                ("last_event_at", last_event_at or "(never)"),
                ("at", _utc_now_iso()),
            ]
        )
        + "\n"
    )
    return _dispatch(subject, body, tag="rc-webhook-silence", dry_run=dry_run)


# ---------------------------------------------------------------------------
# LLM provider fallback-rate spike (task #145)
# ---------------------------------------------------------------------------


def provider_fallback_spike(
    *,
    provider: str,
    fallback_rate: float,
    threshold: float,
    fallback_count: int,
    sample_count: int,
    window_minutes: int,
    sample_reasons: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Operator alert: one LLM provider's fallback rate has spiked.

    Fired by the swarm fallback-rate monitor when, over the trailing
    `window_minutes`, more than `threshold` fraction of persona calls
    whose primary provider == `provider` had to fall back to the backup.
    Caller is responsible for dedupe (use `dedupe_or_skip` keyed on
    `provider` with a TTL >= window so we don't page once per debate).
    """
    pct = fallback_rate * 100.0
    threshold_pct = threshold * 100.0
    subject = (
        f"[axiom][LLM-FALLBACK] {provider} fallback rate {pct:.1f}% "
        f"(>{threshold_pct:.0f}% over {window_minutes}m)"
    )
    reasons_block = ""
    if sample_reasons:
        reasons_block = "\nRecent fallback reasons:\n" + "".join(
            f"  - {r}\n" for r in sample_reasons[:5]
        )
    body = (
        f"The swarm has been failing over from {provider} to its backup "
        "provider on a sustained basis. Debates are still completing "
        "(transparently, on the backup), so this would otherwise be "
        "invisible. Investigate provider health, rate limits, and "
        "credentials before the bias / cost impact compounds.\n\n"
        + _format_lines(
            [
                ("provider", provider),
                ("fallback_rate", f"{pct:.2f}%"),
                ("threshold", f"{threshold_pct:.2f}%"),
                ("fallback_calls", fallback_count),
                ("total_calls", sample_count),
                ("window_minutes", window_minutes),
                ("at", _utc_now_iso()),
            ]
        )
        + "\n"
        + reasons_block
    )
    return _dispatch(
        subject, body, tag="llm-fallback-spike", dry_run=dry_run
    )


# ---------------------------------------------------------------------------
# Pending teammate approval
# ---------------------------------------------------------------------------


def pending_approval(
    *,
    user_id: str,
    user_email: str | None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Notify the operator that a signed-in teammate is awaiting approval.

    Caller is responsible for dedupe (use `dedupe_or_skip` with a 24h TTL)
    so a single user pinging `/api/me/access` repeatedly doesn't spam the
    operator inbox.

    The recipient resolves from `OPERATOR_NOTIFY_EMAIL` first (this is the
    address explicitly intended for new-signup notifications) and falls
    back to the standard operator alert list when that env var is unset.
    """
    import os

    subject = (
        f"[axiom] Teammate awaiting approval: {user_email or user_id}"
    )
    body = (
        "A signed-in user is on the \"Awaiting approval\" screen and has "
        "no operator allow-list entry yet. Approve or revoke them from "
        "the operator console (Admin → Users).\n\n"
        + _format_lines(
            [
                ("user_email", user_email or "(unknown)"),
                ("user_id", user_id),
                ("at", _utc_now_iso()),
            ]
        )
        + "\n"
    )

    notify_to_raw = (os.environ.get("OPERATOR_NOTIFY_EMAIL") or "").strip()
    explicit_to: list[str] | None = None
    if notify_to_raw:
        explicit_to = [
            t.strip() for t in notify_to_raw.split(",") if t.strip() and "@" in t
        ] or None

    if dry_run:
        log.info("alert dry_run subject=%r tag=pending-approval", subject)
        return {
            "dry_run": True,
            "tag": "pending-approval",
            "subject": subject,
            "body_preview": body[:4000],
            "would_send_to": explicit_to or postmark.alert_recipients(),
            "configured": postmark.is_configured(),
        }
    postmark.send_in_background(
        subject=subject,
        text_body=body,
        tag="pending-approval",
        to=explicit_to,
    )
    return {
        "dry_run": False,
        "tag": "pending-approval",
        "subject": subject,
        "queued": True,
        "configured": postmark.is_configured(),
        "recipients": explicit_to or postmark.alert_recipients(),
    }


# ---------------------------------------------------------------------------
# Daily P&L summary
# ---------------------------------------------------------------------------


def daily_pnl_summary(
    *,
    date_utc: str,
    realized_pnl_cents: int,
    unrealized_pnl_cents: int,
    trades_settled: int,
    fills_today: int,
    open_positions: int,
    cycles_completed: int,
    halts_today: int,
    drift_alerts_today: int,
    slippage_guard_rejects_today: int = 0,
    top_movers: list[dict[str, Any]] | None = None,
    by_vertical: list[dict[str, Any]] | None = None,
    by_persona: list[dict[str, Any]] | None = None,
    slippage_guard_offenders: list[dict[str, Any]] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    fmt_cents = lambda c: f"{'+' if c >= 0 else '-'}${abs(c) / 100.0:,.2f}"
    subject = (
        f"[axiom] Daily summary {date_utc} :: "
        f"realized {fmt_cents(realized_pnl_cents)} "
        f"({trades_settled} settled)"
    )
    rows = [
        ("date_utc", date_utc),
        ("realized_pnl", fmt_cents(realized_pnl_cents)),
        ("unrealized_pnl", fmt_cents(unrealized_pnl_cents)),
        ("trades_settled", trades_settled),
        ("fills_today", fills_today),
        ("open_positions", open_positions),
        ("cycles_completed", cycles_completed),
        ("halts_today", halts_today),
        ("drift_alerts_today", drift_alerts_today),
        ("slippage_guard_rejects_today", slippage_guard_rejects_today),
    ]
    body = "Daily Axiom-Predict summary.\n\n" + _format_lines(rows) + "\n"
    if top_movers:
        body += "\nTop position movers:\n"
        for m in top_movers[:10]:
            body += (
                f"  {m.get('market_ticker','?'):28s} "
                f"realized={fmt_cents(int(m.get('realized_pnl_cents') or 0))} "
                f"position={m.get('position','?')}\n"
            )
    body += _render_by_vertical(by_vertical, fmt_cents)
    body += _render_by_persona(by_persona)
    body += _render_slippage_guard_offenders(slippage_guard_offenders)
    html_body = _render_daily_pnl_summary_html(
        date_utc=date_utc,
        summary_rows=rows,
        top_movers=top_movers,
        by_vertical=by_vertical,
        by_persona=by_persona,
        slippage_guard_offenders=slippage_guard_offenders,
        fmt_cents=fmt_cents,
    )
    return _dispatch(
        subject, body, tag="daily-summary", dry_run=dry_run, html_body=html_body
    )


def _render_by_vertical(
    rows: list[dict[str, Any]] | None,
    fmt_cents,
) -> str:
    out = "\nBy vertical:\n"
    if not rows:
        out += "  (no settled trades by vertical today)\n"
        return out
    header = (
        f"  {'vertical':<10} {'trades':>6} {'W/L':>7} "
        f"{'pnl':>11} {'win%':>6} {'edge':>6} {'brier':>6}\n"
    )
    out += header
    for r in rows:
        wl = f"{int(r.get('wins') or 0)}/{int(r.get('losses') or 0)}"
        brier = r.get("brier")
        brier_s = f"{brier:.3f}" if isinstance(brier, (int, float)) else "  -  "
        out += (
            f"  {str(r.get('vertical') or '?'):<10} "
            f"{int(r.get('trades') or 0):>6} "
            f"{wl:>7} "
            f"{fmt_cents(int(r.get('pnl_cents') or 0)):>11} "
            f"{float(r.get('win_rate_pct') or 0.0):>5.1f}% "
            f"{float(r.get('avg_edge') or 0.0):>+6.3f} "
            f"{brier_s:>6}\n"
        )
    return out


def _render_slippage_guard_offenders(rows: list[dict[str, Any]] | None) -> str:
    out = "\nSlippage-guard repeat offenders (markets rejected today):\n"
    if not rows:
        out += "  (no slippage-guard rejections today)\n"
        return out
    header = (
        f"  {'market':<28} {'rejects':>7} "
        f"{'max_slip':>9} {'max_erased%':>11}\n"
    )
    out += header
    for r in rows:
        slip = r.get("max_slippage_cents")
        slip_s = f"{int(slip)}¢" if isinstance(slip, (int, float)) else "  -  "
        erased = r.get("max_edge_erased_pct")
        erased_s = f"{float(erased):.1f}" if isinstance(erased, (int, float)) else "  -  "
        out += (
            f"  {str(r.get('market_ticker') or '?'):<28} "
            f"{int(r.get('rejects') or 0):>7} "
            f"{slip_s:>9} {erased_s:>11}\n"
        )
    return out


def _render_by_persona(rows: list[dict[str, Any]] | None) -> str:
    out = "\nBy persona (debates opened today that have since settled):\n"
    if not rows:
        out += "  (no settled swarm debates from today)\n"
        return out
    header = (
        f"  {'persona':<20} {'debates':>7} {'brier':>6} "
        f"{'acc%':>5} {'conf':>5}  {'leans (Y/N/PASS)':<18}\n"
    )
    out += header
    for r in rows:
        conf = r.get("avg_confidence")
        conf_s = f"{conf:.2f}" if isinstance(conf, (int, float)) else "  -  "
        leans = (
            f"{int(r.get('lean_yes') or 0)}/"
            f"{int(r.get('lean_no') or 0)}/"
            f"{int(r.get('lean_pass') or 0)}"
        )
        out += (
            f"  {str(r.get('persona_id') or '?'):<20} "
            f"{int(r.get('debates') or 0):>7} "
            f"{float(r.get('brier') or 0.0):>6.3f} "
            f"{float(r.get('accuracy_pct') or 0.0):>5.1f} "
            f"{conf_s:>5}  {leans:<18}\n"
        )
    return out


# ---------------------------------------------------------------------------
# Daily P&L summary — HTML rendering
# ---------------------------------------------------------------------------


def _html_escape(s: Any) -> str:
    import html as _html
    return _html.escape(str(s), quote=True)


_HTML_MONO = (
    "font-family:'SFMono-Regular',Menlo,Consolas,'Liberation Mono',monospace;"
)
_HTML_SANS = (
    "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Inter,Arial,sans-serif;"
)
_TH_STYLE = (
    "padding:6px 10px;text-align:left;font-size:11px;text-transform:uppercase;"
    "letter-spacing:0.06em;color:#6B7280;border-bottom:1px solid #E5E7EB;"
    "background:#F9FAFB;"
)
_TD_STYLE = (
    "padding:6px 10px;font-size:13px;color:#111827;"
    "border-bottom:1px solid #F3F4F6;"
)


def _html_table(
    headers: list[tuple[str, str]],  # (label, align: "left"|"right")
    rows: list[list[str]],
    *,
    mono: bool = True,
) -> str:
    head_cells = "".join(
        f'<th style="{_TH_STYLE}text-align:{align};">{_html_escape(label)}</th>'
        for label, align in headers
    )
    body_cells = []
    for row in rows:
        cells = []
        for (_, align), value in zip(headers, row):
            cells.append(
                f'<td style="{_TD_STYLE}text-align:{align};'
                f'{_HTML_MONO if mono else ""}">{value}</td>'
            )
        body_cells.append(f"<tr>{''.join(cells)}</tr>")
    return (
        '<table cellpadding="0" cellspacing="0" '
        'style="border-collapse:collapse;width:100%;'
        'border:1px solid #E5E7EB;border-radius:6px;overflow:hidden;">'
        f"<thead><tr>{head_cells}</tr></thead>"
        f"<tbody>{''.join(body_cells)}</tbody>"
        "</table>"
    )


def _render_by_vertical_html(
    rows: list[dict[str, Any]] | None,
    fmt_cents,
) -> str:
    title = '<h3 style="margin:24px 0 8px 0;font-size:14px;color:#111827;">By vertical</h3>'
    if not rows:
        return (
            title
            + '<p style="margin:0;color:#6B7280;font-size:13px;font-style:italic;">'
              "No settled trades by vertical today.</p>"
        )
    headers = [
        ("Vertical", "left"),
        ("Trades", "right"),
        ("W/L", "right"),
        ("PnL", "right"),
        ("Win%", "right"),
        ("Edge", "right"),
        ("Brier", "right"),
    ]
    body_rows = []
    for r in rows:
        wl = f"{int(r.get('wins') or 0)}/{int(r.get('losses') or 0)}"
        brier = r.get("brier")
        brier_s = f"{brier:.3f}" if isinstance(brier, (int, float)) else "—"
        pnl_c = int(r.get("pnl_cents") or 0)
        pnl_color = "#047857" if pnl_c >= 0 else "#B91C1C"
        body_rows.append([
            _html_escape(r.get("vertical") or "?"),
            _html_escape(int(r.get("trades") or 0)),
            _html_escape(wl),
            f'<span style="color:{pnl_color};">{_html_escape(fmt_cents(pnl_c))}</span>',
            _html_escape(f"{float(r.get('win_rate_pct') or 0.0):.1f}%"),
            _html_escape(f"{float(r.get('avg_edge') or 0.0):+.3f}"),
            _html_escape(brier_s),
        ])
    return title + _html_table(headers, body_rows)


def _render_by_persona_html(rows: list[dict[str, Any]] | None) -> str:
    title = (
        '<h3 style="margin:24px 0 8px 0;font-size:14px;color:#111827;">'
        "By persona</h3>"
        '<p style="margin:0 0 8px 0;color:#6B7280;font-size:12px;">'
        "Debates opened today that have since settled.</p>"
    )
    if not rows:
        return (
            title
            + '<p style="margin:0;color:#6B7280;font-size:13px;font-style:italic;">'
              "No settled swarm debates from today.</p>"
        )
    headers = [
        ("Persona", "left"),
        ("Debates", "right"),
        ("Brier", "right"),
        ("Acc%", "right"),
        ("Conf", "right"),
        ("Leans (Y/N/PASS)", "right"),
    ]
    body_rows = []
    for r in rows:
        conf = r.get("avg_confidence")
        conf_s = f"{conf:.2f}" if isinstance(conf, (int, float)) else "—"
        leans = (
            f"{int(r.get('lean_yes') or 0)}/"
            f"{int(r.get('lean_no') or 0)}/"
            f"{int(r.get('lean_pass') or 0)}"
        )
        body_rows.append([
            _html_escape(r.get("persona_id") or "?"),
            _html_escape(int(r.get("debates") or 0)),
            _html_escape(f"{float(r.get('brier') or 0.0):.3f}"),
            _html_escape(f"{float(r.get('accuracy_pct') or 0.0):.1f}"),
            _html_escape(conf_s),
            _html_escape(leans),
        ])
    return title + _html_table(headers, body_rows)


def _render_slippage_guard_offenders_html(
    rows: list[dict[str, Any]] | None,
) -> str:
    title = (
        '<h3 style="margin:24px 0 8px 0;font-size:14px;color:#111827;">'
        "Slippage-guard offenders</h3>"
        '<p style="margin:0 0 8px 0;color:#6B7280;font-size:12px;">'
        "Markets where the pre-submit guard rejected at least one order today.</p>"
    )
    if not rows:
        return (
            title
            + '<p style="margin:0;color:#6B7280;font-size:13px;font-style:italic;">'
              "No slippage-guard rejections today.</p>"
        )
    headers = [
        ("Market", "left"),
        ("Rejects", "right"),
        ("Max slip", "right"),
        ("Max edge erased", "right"),
    ]
    body_rows = []
    for r in rows:
        slip = r.get("max_slippage_cents")
        slip_s = f"{int(slip)}¢" if isinstance(slip, (int, float)) else "—"
        erased = r.get("max_edge_erased_pct")
        erased_s = f"{float(erased):.1f}%" if isinstance(erased, (int, float)) else "—"
        body_rows.append([
            _html_escape(r.get("market_ticker") or "?"),
            _html_escape(int(r.get("rejects") or 0)),
            _html_escape(slip_s),
            _html_escape(erased_s),
        ])
    return title + _html_table(headers, body_rows)


def _render_daily_pnl_summary_html(
    *,
    date_utc: str,
    summary_rows: list[tuple[str, Any]],
    top_movers: list[dict[str, Any]] | None,
    by_vertical: list[dict[str, Any]] | None,
    by_persona: list[dict[str, Any]] | None,
    slippage_guard_offenders: list[dict[str, Any]] | None = None,
    fmt_cents,
) -> str:
    overview_headers = [("Metric", "left"), ("Value", "right")]
    overview_rows = []
    for k, v in summary_rows:
        if k in ("realized_pnl", "unrealized_pnl") and isinstance(v, str):
            color = "#047857" if v.startswith("+") else "#B91C1C"
            value_html = f'<span style="color:{color};">{_html_escape(v)}</span>'
        else:
            value_html = _html_escape(v)
        overview_rows.append([_html_escape(k), value_html])
    overview_html = _html_table(overview_headers, overview_rows)

    movers_html = ""
    if top_movers:
        mover_headers = [
            ("Market", "left"),
            ("Realized", "right"),
            ("Position", "right"),
        ]
        mover_rows = []
        for m in top_movers[:10]:
            pnl_c = int(m.get("realized_pnl_cents") or 0)
            color = "#047857" if pnl_c >= 0 else "#B91C1C"
            mover_rows.append([
                _html_escape(m.get("market_ticker", "?")),
                f'<span style="color:{color};">{_html_escape(fmt_cents(pnl_c))}</span>',
                _html_escape(m.get("position", "?")),
            ])
        movers_html = (
            '<h3 style="margin:24px 0 8px 0;font-size:14px;color:#111827;">'
            "Top position movers</h3>"
            + _html_table(mover_headers, mover_rows)
        )

    by_vertical_html = _render_by_vertical_html(by_vertical, fmt_cents)
    by_persona_html = _render_by_persona_html(by_persona)
    slippage_html = _render_slippage_guard_offenders_html(slippage_guard_offenders)

    return f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#F9FAFB;{_HTML_SANS}">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#F9FAFB;padding:24px 0;">
    <tr><td align="center">
      <table width="640" cellpadding="0" cellspacing="0" style="background:#FFFFFF;border:1px solid #E5E7EB;border-radius:8px;overflow:hidden;max-width:640px;width:100%;">
        <tr>
          <td style="background:#111827;color:#F9FAFB;padding:20px 24px;">
            <div style="font-size:11px;letter-spacing:0.16em;text-transform:uppercase;color:#9CA3AF;">Axiom Predict</div>
            <div style="font-size:20px;font-weight:700;margin-top:4px;">Daily summary &middot; {_html_escape(date_utc)}</div>
          </td>
        </tr>
        <tr>
          <td style="padding:20px 24px;">
            {overview_html}
            {movers_html}
            {by_vertical_html}
            {by_persona_html}
            {slippage_html}
          </td>
        </tr>
        <tr>
          <td style="padding:12px 24px 20px 24px;border-top:1px solid #E5E7EB;color:#6B7280;font-size:11px;line-height:1.5;">
            Sent automatically once per UTC day. A plain-text copy is included for clients that don't render HTML.
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Dispatch helper
# ---------------------------------------------------------------------------


def _dispatch(
    subject: str,
    text_body: str,
    *,
    tag: str,
    dry_run: bool,
    html_body: str | None = None,
) -> dict[str, Any]:
    if dry_run:
        log.info("alert dry_run subject=%r tag=%s", subject, tag)
        return {
            "dry_run": True,
            "tag": tag,
            "subject": subject,
            "body_preview": text_body[:4000],
            "html_preview": (html_body[:32000] if html_body else None),
            "would_send_to": postmark.alert_recipients(),
            "user_pref_column": _TAG_TO_USER_PREF.get(tag),
            "configured": postmark.is_configured(),
        }
    postmark.send_in_background(
        subject=subject, text_body=text_body, html_body=html_body, tag=tag
    )
    _schedule_user_email_fanout(
        subject=subject, text_body=text_body, tag=tag, html_body=html_body
    )
    return {
        "dry_run": False,
        "tag": tag,
        "subject": subject,
        "queued": True,
        "configured": postmark.is_configured(),
        "recipients": postmark.alert_recipients(),
        "user_pref_column": _TAG_TO_USER_PREF.get(tag),
    }


def _schedule_user_email_fanout(
    *, subject: str, text_body: str, tag: str, html_body: str | None = None
) -> None:
    """Send a copy of the alert to each end-user who's opted in for
    this alert type.

    We send these as a *separate* Postmark batch (not appended to the
    operator recipient list) so opted-out operators or users keep their
    independent mailing decisions.  Falls back to a no-op when no users
    have opted in or the schema isn't initialised yet.
    """
    pref_col = _TAG_TO_USER_PREF.get(tag)
    if not pref_col:
        return
    from . import push as _push

    async def _runner() -> None:
        try:
            emails = await _push.opted_in_emails(pref_col)
        except Exception as e:  # pragma: no cover
            log.warning("opted_in_emails(%s) failed: %s", pref_col, e)
            return
        # Strip out anyone who's already on the operator alert list so
        # we don't double-mail an operator who's also a paying user.
        ops = {e.lower() for e in postmark.alert_recipients()}
        targets = [e for e in emails if e.lower() not in ops]
        if not targets:
            return
        try:
            await postmark.send(
                subject=subject,
                text_body=text_body,
                html_body=html_body,
                to=targets,
                tag=tag,
            )
        except Exception as e:  # pragma: no cover
            log.warning("user-pref email fanout (%s) failed: %s", tag, e)

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_runner())
    except RuntimeError:
        try:
            asyncio.run(_runner())
        except Exception as e:  # pragma: no cover
            log.warning("inline user-pref fanout failed: %s", e)


# ---------------------------------------------------------------------------
# Weekly P&L roundup
# ---------------------------------------------------------------------------


def weekly_roundup(
    *,
    week_ending_utc: str,
    realized_pnl_cents: int,
    unrealized_pnl_cents: int,
    trades_settled: int,
    win_rate_pct: float | None = None,
    notes: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Per-user weekly P&L digest.

    Operators receive a copy on the standard alert list; subscribers
    who opted in via ``email_weekly_roundup`` get one too. Tag is
    ``weekly-roundup`` so Postmark dashboards can split the metric out
    from the daily summary.
    """
    fmt_cents = lambda c: f"{'+' if c >= 0 else '-'}${abs(c) / 100.0:,.2f}"
    subject = (
        f"[axiom] Weekly roundup w/e {week_ending_utc} :: "
        f"realized {fmt_cents(realized_pnl_cents)} ({trades_settled} settled)"
    )
    rows: list[tuple[str, Any]] = [
        ("week_ending_utc", week_ending_utc),
        ("realized_pnl", fmt_cents(realized_pnl_cents)),
        ("unrealized_pnl", fmt_cents(unrealized_pnl_cents)),
        ("trades_settled", trades_settled),
    ]
    if win_rate_pct is not None:
        rows.append(("win_rate_pct", f"{win_rate_pct:.1f}%"))
    body = (
        "Weekly Axiom-Predict P&L roundup.\n\n"
        + _format_lines(rows)
        + "\n"
    )
    if notes:
        body += f"\nNotes:\n  {notes}\n"
    html_body = _render_weekly_roundup_html(
        week_ending_utc=week_ending_utc,
        summary_rows=rows,
        notes=notes,
    )
    return _dispatch(
        subject, body, tag="weekly-roundup", dry_run=dry_run, html_body=html_body
    )


def _render_weekly_roundup_html(
    *,
    week_ending_utc: str,
    summary_rows: list[tuple[str, Any]],
    notes: str | None,
) -> str:
    overview_headers = [("Metric", "left"), ("Value", "right")]
    overview_rows = []
    for k, v in summary_rows:
        if k in ("realized_pnl", "unrealized_pnl") and isinstance(v, str):
            color = "#047857" if v.startswith("+") else "#B91C1C"
            value_html = f'<span style="color:{color};">{_html_escape(v)}</span>'
        else:
            value_html = _html_escape(v)
        overview_rows.append([_html_escape(k), value_html])
    overview_html = _html_table(overview_headers, overview_rows)

    notes_html = ""
    if notes:
        notes_html = (
            '<h3 style="margin:24px 0 8px 0;font-size:14px;color:#111827;">Notes</h3>'
            f'<p style="margin:0;color:#374151;font-size:13px;line-height:1.5;'
            f'white-space:pre-wrap;">{_html_escape(notes)}</p>'
        )

    return f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#F9FAFB;{_HTML_SANS}">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#F9FAFB;padding:24px 0;">
    <tr><td align="center">
      <table width="640" cellpadding="0" cellspacing="0" style="background:#FFFFFF;border:1px solid #E5E7EB;border-radius:8px;overflow:hidden;max-width:640px;width:100%;">
        <tr>
          <td style="background:#111827;color:#F9FAFB;padding:20px 24px;">
            <div style="font-size:11px;letter-spacing:0.16em;text-transform:uppercase;color:#9CA3AF;">Axiom Predict</div>
            <div style="font-size:20px;font-weight:700;margin-top:4px;">Weekly roundup &middot; w/e {_html_escape(week_ending_utc)}</div>
          </td>
        </tr>
        <tr>
          <td style="padding:20px 24px;">
            {overview_html}
            {notes_html}
          </td>
        </tr>
        <tr>
          <td style="padding:12px 24px 20px 24px;border-top:1px solid #E5E7EB;color:#6B7280;font-size:11px;line-height:1.5;">
            Sent automatically once per ISO week. A plain-text copy is included for clients that don't render HTML.
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""
