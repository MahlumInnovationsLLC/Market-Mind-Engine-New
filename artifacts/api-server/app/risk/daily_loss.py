"""Daily-loss kill switch.

Computes today's realized + unrealized P&L from the ``trades`` and
``positions`` tables, compares against the operator-configured
thresholds (``AXIOM_DAILY_LOSS_HALT_USD`` absolute and
``AXIOM_DAILY_LOSS_HALT_PCT`` percent of starting-of-day bankroll) and,
when tripped, flips ``system_settings.risk.trading_mode`` to ``halt`` —
identical to the EMERGENCY STOP path so an existing halt workflow (alert
dedupe, dashboard indicator, audit log) handles the rest.

The gate wraps this module so a risk-check candidate never blocks on a
DB round-trip for more than a cache window (default 5s).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Any

from .. import db
from ..notifications import alerts as notif_alerts


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Snapshot + cache
# ---------------------------------------------------------------------------


CACHE_TTL_SECONDS = 5.0


@dataclass
class DailyLossSnapshot:
    date_utc: str
    realized_pnl_usd: float
    unrealized_pnl_usd: float
    net_pnl_usd: float
    start_of_day_bankroll_usd: float | None
    threshold_usd: float | None
    threshold_pct: float | None
    threshold_pct_usd: float | None  # pct converted to USD at start-of-day bankroll
    loss_usd: float  # positive dollars of loss (0 when flat/green)
    tripped_abs: bool
    tripped_pct: bool
    tripped: bool
    tripped_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_cache_lock = asyncio.Lock()
_cache: tuple[float, DailyLossSnapshot] | None = None


def _today_utc() -> date:
    return datetime.now(timezone.utc).date()


def _read_threshold_usd(overrides: dict[str, Any] | None) -> float | None:
    if overrides and overrides.get("daily_loss_halt_usd") is not None:
        try:
            v = float(overrides["daily_loss_halt_usd"])
            return v if v > 0 else None
        except (TypeError, ValueError):
            pass
    raw = os.environ.get("AXIOM_DAILY_LOSS_HALT_USD")
    if raw is None or not raw.strip():
        return None
    try:
        v = float(raw)
        return v if v > 0 else None
    except ValueError:
        log.warning("AXIOM_DAILY_LOSS_HALT_USD is not a number: %r", raw)
        return None


def _read_threshold_pct(overrides: dict[str, Any] | None) -> float | None:
    if overrides and overrides.get("daily_loss_halt_pct") is not None:
        try:
            v = float(overrides["daily_loss_halt_pct"])
            return v if v > 0 else None
        except (TypeError, ValueError):
            pass
    raw = os.environ.get("AXIOM_DAILY_LOSS_HALT_PCT")
    if raw is None or not raw.strip():
        return None
    try:
        v = float(raw)
        return v if v > 0 else None
    except ValueError:
        log.warning("AXIOM_DAILY_LOSS_HALT_PCT is not a number: %r", raw)
        return None


async def _sum_realized_today(today: date) -> float:
    try:
        row = await db.fetchrow(
            """
            SELECT coalesce(sum(realized_pnl), 0)::float AS realized
              FROM trades
             WHERE settled_at IS NOT NULL
               AND ((settled_at at time zone 'UTC')::date) = $1
            """,
            today,
        )
        return float(row["realized"] or 0.0) if row else 0.0
    except Exception as e:
        log.warning("daily_loss realized query failed: %s", e)
        return 0.0


async def _sum_unrealized() -> float:
    try:
        row = await db.fetchrow(
            """
            SELECT coalesce(sum(unrealized_pnl), 0)::float AS unr
              FROM positions
             WHERE coalesce(status, 'open') = 'open'
            """,
        )
        return float(row["unr"] or 0.0) if row else 0.0
    except Exception as e:
        log.warning("daily_loss unrealized query failed: %s", e)
        return 0.0


async def _start_of_day_bankroll(today: date) -> float | None:
    """Balance before the first bankroll_events row of ``today`` (UTC).

    Falls back to the latest bankroll_events balance, which is correct
    on a quiet day — if there were no bankroll changes yet today, the
    latest balance IS the start-of-day balance.
    """
    try:
        v = await db.fetchval(
            """
            SELECT balance_cents FROM bankroll_events
             WHERE ((created_at at time zone 'UTC')::date) < $1
             ORDER BY id DESC
             LIMIT 1
            """,
            today,
        )
        if v is not None:
            return float(int(v)) / 100.0
    except Exception as e:
        log.debug("daily_loss start-of-day query failed: %s", e)
    # No rows before today — use the earliest row as a proxy (first ever
    # recorded balance) rather than nothing, so the pct threshold still
    # has a denominator on day-1 deploys.
    try:
        v = await db.fetchval(
            "SELECT balance_cents FROM bankroll_events ORDER BY id ASC LIMIT 1"
        )
        if v is not None:
            return float(int(v)) / 100.0
    except Exception as e:
        log.debug("daily_loss fallback bankroll query failed: %s", e)
    return None


async def compute_snapshot(
    overrides: dict[str, Any] | None = None,
    *,
    use_cache: bool = True,
) -> DailyLossSnapshot:
    """Build a fresh (or cached) snapshot of today's P&L and thresholds."""
    global _cache
    now_m = time.monotonic()
    if use_cache and _cache is not None:
        expires, snap = _cache
        if now_m < expires and snap.date_utc == _today_utc().isoformat():
            return snap
    async with _cache_lock:
        # Re-check inside lock so concurrent callers share one DB hit.
        if use_cache and _cache is not None:
            expires, snap = _cache
            if time.monotonic() < expires and snap.date_utc == _today_utc().isoformat():
                return snap
        today = _today_utc()
        realized, unrealized, bankroll = await asyncio.gather(
            _sum_realized_today(today),
            _sum_unrealized(),
            _start_of_day_bankroll(today),
        )
        net = float(realized) + float(unrealized)
        loss = -net if net < 0 else 0.0
        thr_usd = _read_threshold_usd(overrides)
        thr_pct = _read_threshold_pct(overrides)
        thr_pct_usd: float | None = None
        if thr_pct is not None and bankroll is not None and bankroll > 0:
            thr_pct_usd = bankroll * (thr_pct / 100.0)
        tripped_abs = thr_usd is not None and loss >= thr_usd
        tripped_pct = thr_pct_usd is not None and loss >= thr_pct_usd
        reason: str | None = None
        if tripped_abs and tripped_pct:
            reason = (
                f"daily loss ${loss:.2f} breaches both ${thr_usd:.2f} absolute "
                f"and {thr_pct:.2f}% (${thr_pct_usd:.2f}) caps"
            )
        elif tripped_abs:
            reason = f"daily loss ${loss:.2f} >= absolute cap ${thr_usd:.2f}"
        elif tripped_pct:
            reason = (
                f"daily loss ${loss:.2f} >= {thr_pct:.2f}% of "
                f"start-of-day bankroll (${thr_pct_usd:.2f})"
            )
        snap = DailyLossSnapshot(
            date_utc=today.isoformat(),
            realized_pnl_usd=realized,
            unrealized_pnl_usd=unrealized,
            net_pnl_usd=net,
            start_of_day_bankroll_usd=bankroll,
            threshold_usd=thr_usd,
            threshold_pct=thr_pct,
            threshold_pct_usd=thr_pct_usd,
            loss_usd=loss,
            tripped_abs=bool(tripped_abs),
            tripped_pct=bool(tripped_pct),
            tripped=bool(tripped_abs or tripped_pct),
            tripped_reason=reason,
        )
        _cache = (now_m + CACHE_TTL_SECONDS, snap)
        return snap


def invalidate_cache() -> None:
    """Clear the snapshot cache — used by tests and the ``/halt`` endpoint
    so the next read reflects the freshly-applied mode change."""
    global _cache
    _cache = None


# ---------------------------------------------------------------------------
# Halt side-effect
# ---------------------------------------------------------------------------


async def _current_trading_mode() -> str | None:
    try:
        v = await db.fetchval(
            "SELECT value FROM system_settings WHERE key = 'risk.trading_mode'"
        )
    except Exception as e:
        log.debug("daily_loss trading_mode lookup failed: %s", e)
        return None
    return str(v).lower() if v is not None else None


async def _persist_halt() -> None:
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
        log.warning("daily_loss persist halt failed: %s", e)


async def apply_halt(snap: DailyLossSnapshot) -> bool:
    """Idempotently trip the kill-switch for ``snap``.

    Returns True when this caller performed the trip (wrote system_settings
    and dispatched the alert), False when a prior trip within the same UTC
    day already did it.  The ``daily_loss_halt:<utc_date>`` dedupe key
    suppresses the second alert; callers that want the snapshot updated
    should call :func:`invalidate_cache`.
    """
    # Short-circuit when already halted — no alert, no write.
    current = await _current_trading_mode()
    if current == "halt":
        return False
    # Claim the dedupe key; only the winner performs side effects.
    dedupe_key = f"daily_loss_halt:{snap.date_utc}"
    try:
        ok = await notif_alerts.dedupe_or_skip(
            "daily_loss_halt", snap.date_utc, ttl_seconds=86400
        )
    except Exception as e:
        log.warning("daily_loss dedupe claim failed: %s", e)
        ok = True
    if not ok:
        log.info("daily_loss halt suppressed by dedupe: %s", dedupe_key)
        return False
    await _persist_halt()
    # Trip the in-memory kill switch so this process stops submitting
    # orders immediately, even before the next cycle re-reads the DB.
    try:
        from ..exchange_kill_switch import halt as _halt
        _halt(snap.tripped_reason or "daily loss cap breached")
    except Exception as e:
        log.warning("daily_loss exchange_kill_switch trip failed: %s", e)
    # Fire the operator alert via the standard notification path.
    try:
        notif_alerts.daily_loss_halt(
            date_utc=snap.date_utc,
            loss_usd=snap.loss_usd,
            realized_pnl_usd=snap.realized_pnl_usd,
            unrealized_pnl_usd=snap.unrealized_pnl_usd,
            threshold_usd=snap.threshold_usd,
            threshold_pct=snap.threshold_pct,
            threshold_pct_usd=snap.threshold_pct_usd,
            start_of_day_bankroll_usd=snap.start_of_day_bankroll_usd,
            reason=snap.tripped_reason,
        )
    except Exception as e:
        log.warning("daily_loss alert dispatch failed: %s", e)
    return True
