"""Read-only data formatters for the Telegram concierge.

Each function returns a Telegram-friendly text block (plain text, ~4 KB
ceiling). Markdown / HTML formatting is intentionally avoided so the
bot can ship messages with ``parse_mode`` unset and never crash on a
stray ``_`` or ``*`` in a market title.

All queries are best-effort: a failing sub-query falls back to a
neutral placeholder rather than raising, so the operator always gets a
response — even if the dashboard is partially degraded.
"""
from __future__ import annotations

import logging
from typing import Any

from .. import dashboard, db
from ..kalshi import lifecycle as kalshi_lifecycle


log = logging.getLogger(__name__)


def _fmt_money(cents_or_dollars: float | None, *, is_cents: bool = False) -> str:
    if cents_or_dollars is None:
        return "n/a"
    val = float(cents_or_dollars)
    if is_cents:
        val = val / 100.0
    sign = "-" if val < 0 else ""
    return f"{sign}${abs(val):,.2f}"


def _fmt_pct(p: float | None) -> str:
    if p is None:
        return "n/a"
    return f"{float(p) * 100:.1f}%"


async def _today_realized_cents() -> int | None:
    try:
        v = await db.fetchval(
            """
            SELECT coalesce(sum(coalesce(realized_pnl, pnl, 0)), 0)::float
              FROM trades
             WHERE settled_at IS NOT NULL
               AND date_trunc('day', settled_at AT TIME ZONE 'UTC')
                   = date_trunc('day', (now() AT TIME ZONE 'UTC'))
            """,
        )
        return int(round(float(v or 0.0) * 100))
    except Exception as e:
        log.warning("today_realized failed: %s", e)
        return None


async def _today_fills() -> int | None:
    try:
        v = await db.fetchval(
            """
            SELECT count(*)::int FROM order_lifecycle
             WHERE event_type = 'filled'
               AND date_trunc('day', created_at AT TIME ZONE 'UTC')
                   = date_trunc('day', (now() AT TIME ZONE 'UTC'))
            """,
        )
        return int(v or 0)
    except Exception as e:
        log.warning("today_fills failed: %s", e)
        return None


async def status_text() -> str:
    """Same shape the dashboard renders at the top of the home page."""
    overview = await dashboard.get_overview()
    today_realized = await _today_realized_cents()
    today_fills = await _today_fills()
    ks = kalshi_lifecycle.kill_switch_state()
    halted = bool(ks.get("halted"))
    open_positions = overview.get("open_positions")
    realized_pnl = overview.get("realized_pnl")
    win_rate = overview.get("win_rate")
    latest_cycle = overview.get("latest_cycle_at") or "—"
    latest_trade = overview.get("latest_trade_at") or "—"

    lines = [
        "Axiom status",
        f"  trading: {'HALTED' if halted else 'live'}",
    ]
    if halted:
        reason = ks.get("reason") or "operator halt"
        since = ks.get("since") or "?"
        lines.append(f"  halt reason: {reason}")
        lines.append(f"  halted since: {since}")
    lines += [
        f"  today realized: {_fmt_money(today_realized, is_cents=True)}",
        f"  today fills: {today_fills if today_fills is not None else 'n/a'}",
        f"  open positions: {open_positions if open_positions is not None else 'n/a'}",
        f"  lifetime realized: {_fmt_money(realized_pnl)}",
        f"  lifetime win-rate: {_fmt_pct(win_rate)}",
        f"  latest cycle: {latest_cycle}",
        f"  latest trade: {latest_trade}",
    ]
    return "\n".join(lines)


async def positions_text(limit: int = 15) -> str:
    """Snapshot of currently-open positions across exchanges.

    Reads the same ``positions`` table the dashboard uses so values
    line up with the operator console exactly.
    """
    try:
        rows = await db.fetch(
            """
            SELECT exchange,
                   market_id,
                   market_title,
                   side,
                   quantity::float       AS quantity,
                   avg_price::float      AS avg_price,
                   unrealized_pnl::float AS unrealized
              FROM positions
             WHERE coalesce(status,'open') = 'open'
             ORDER BY abs(coalesce(unrealized_pnl, 0)) DESC NULLS LAST,
                      market_id ASC
             LIMIT $1
            """,
            limit,
        )
    except Exception as e:
        log.warning("positions_text query failed: %s", e)
        return "Open positions: query failed (DB degraded)"

    if not rows:
        return "Open positions: none"

    lines = [f"Open positions ({len(rows)} shown):"]
    for r in rows:
        title = r.get("market_title") or r.get("market_id") or "?"
        if len(title) > 60:
            title = title[:57] + "..."
        side = (r.get("side") or "?").upper()
        qty = r.get("quantity")
        avg = r.get("avg_price")
        unr = r.get("unrealized")
        lines.append(
            f"  {title}\n"
            f"    {side} qty={qty} @ {avg if avg is not None else '?'}"
            f"  unrealized={_fmt_money(unr)}  exch={r.get('exchange') or '?'}"
        )
    return "\n".join(lines)


async def halts_text(limit: int = 5) -> str:
    """Current kill-switch state plus the most recent emergency-stop events.

    Pulls ``emergency_stop`` rows from the admin audit log so the
    operator can see who tripped the halt, when, and why — same source
    of truth as the operator console history view.
    """
    ks = kalshi_lifecycle.kill_switch_state()
    halted = bool(ks.get("halted"))
    lines = ["Halts"]
    if halted:
        lines.append(f"  current: HALTED — {ks.get('reason') or 'operator halt'}")
        if ks.get("since"):
            lines.append(f"  since: {ks.get('since')}")
    else:
        lines.append("  current: trading is live (kill switch clear)")

    try:
        rows = await db.fetch(
            """
            SELECT actor_email,
                   actor_user_id,
                   after_value,
                   to_char(created_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS at
              FROM admin_audit_log
             WHERE action = 'emergency_stop'
             ORDER BY created_at DESC
             LIMIT $1
            """,
            limit,
        )
    except Exception as e:
        log.warning("halts_text audit query failed: %s", e)
        rows = []

    if rows:
        lines.append("")
        lines.append(f"Recent emergency stops ({len(rows)}):")
        for r in rows:
            after = r.get("after_value") or {}
            if isinstance(after, str):
                # asyncpg returns jsonb as str when no codec is registered;
                # try a best-effort decode but tolerate failure.
                try:
                    import json as _json
                    after = _json.loads(after)
                except Exception:
                    after = {}
            reason = (after.get("reason") if isinstance(after, dict) else None) or "?"
            canceled = (
                after.get("canceled_orders") if isinstance(after, dict) else None
            )
            actor = r.get("actor_email") or r.get("actor_user_id") or "?"
            lines.append(
                f"  {r.get('at')} by {actor}\n"
                f"    reason: {reason}\n"
                f"    canceled orders: {canceled if canceled is not None else 'n/a'}"
            )
    else:
        lines.append("")
        lines.append("Recent emergency stops: none")
    return "\n".join(lines)
