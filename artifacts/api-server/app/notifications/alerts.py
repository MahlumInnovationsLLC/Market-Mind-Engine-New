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
    global _dedupe_table_ready
    if _dedupe_table_ready:
        return
    async with _dedupe_lock:
        if _dedupe_table_ready:
            return
        from .. import db  # local import to avoid cycles at module load
        try:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS notification_dedupe (
                  key  TEXT PRIMARY KEY,
                  date_utc DATE,
                  sent_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            # Make date_utc nullable so generic (non-daily) rows can omit it.
            await db.execute(
                "ALTER TABLE notification_dedupe ALTER COLUMN date_utc DROP NOT NULL"
            )
            await db.execute(
                "ALTER TABLE notification_dedupe "
                "ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ"
            )
            _dedupe_table_ready = True
        except Exception as e:
            log.warning("notification_dedupe ensure failed: %s", e)


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
