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

import logging
from datetime import datetime, timezone
from typing import Any, Iterable

from . import postmark


log = logging.getLogger("axiom.notifications.alerts")


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
    return _dispatch(subject, body, tag="live-trade", dry_run=dry_run)


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
    return _dispatch(subject, body, tag="halt", dry_run=dry_run)


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
    return _dispatch(subject, body, tag="daily-summary", dry_run=dry_run)


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
            "body_preview": text_body[:600],
            "would_send_to": postmark.alert_recipients(),
            "configured": postmark.is_configured(),
        }
    postmark.send_in_background(subject=subject, text_body=text_body, tag=tag)
    return {
        "dry_run": False,
        "tag": tag,
        "subject": subject,
        "queued": True,
        "configured": postmark.is_configured(),
        "recipients": postmark.alert_recipients(),
    }
