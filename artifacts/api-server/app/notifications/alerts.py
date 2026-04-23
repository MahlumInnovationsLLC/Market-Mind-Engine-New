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


_dedupe_table_ready = False
_dedupe_lock = asyncio.Lock()


async def _ensure_dedupe_table() -> None:
    """Schema is now owned by the Alembic baseline migration; no-op."""
    global _dedupe_table_ready
    _dedupe_table_ready = True


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
    await _ensure_dedupe_table()
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
        return bool(row)
    except Exception as e:
        log.warning("dedupe_or_skip claim failed for %s: %s", full_key, e)
        return True


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
    dry_run: bool = False,
) -> dict[str, Any]:
    px = f"{price_cents}¢" if price_cents is not None else "(market)"
    subject = (
        f"[axiom][LIVE] {action.upper()} {side.upper()} {contracts}x "
        f"{market_ticker} @ {px}"
    )
    body = (
        "A LIVE Kalshi order filled (not paper).\n\n"
        + _format_lines(
            [
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
                f"LIVE {action.upper()} {side.upper()} {contracts}x "
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
    return _dispatch(subject, body, tag="emergency_stop", dry_run=dry_run)


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
    top_movers: list[dict[str, Any]] | None = None,
    by_vertical: list[dict[str, Any]] | None = None,
    by_persona: list[dict[str, Any]] | None = None,
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
    return _dispatch(subject, body, tag="daily-summary", dry_run=dry_run)


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
# Dispatch helper
# ---------------------------------------------------------------------------


def _dispatch(
    subject: str,
    text_body: str,
    *,
    tag: str,
    dry_run: bool,
) -> dict[str, Any]:
    if dry_run:
        log.info("alert dry_run subject=%r tag=%s", subject, tag)
        return {
            "dry_run": True,
            "tag": tag,
            "subject": subject,
            "body_preview": text_body[:4000],
            "would_send_to": postmark.alert_recipients(),
            "user_pref_column": _TAG_TO_USER_PREF.get(tag),
            "configured": postmark.is_configured(),
        }
    postmark.send_in_background(subject=subject, text_body=text_body, tag=tag)
    _schedule_user_email_fanout(subject=subject, text_body=text_body, tag=tag)
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
    *, subject: str, text_body: str, tag: str
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
    return _dispatch(subject, body, tag="weekly-roundup", dry_run=dry_run)
