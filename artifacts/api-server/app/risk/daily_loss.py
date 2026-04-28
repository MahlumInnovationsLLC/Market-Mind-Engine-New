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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .. import db
from ..notifications import alerts as notif_alerts


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Snapshot + cache
# ---------------------------------------------------------------------------


CACHE_TTL_SECONDS = 5.0
DEFAULT_TZ = "UTC"


@dataclass
class DailyLossSnapshot:
    # Trading-day boundary, expressed in whatever timezone the operator
    # has configured (``AXIOM_DAILY_LOSS_TZ`` or risk.daily_loss_tz
    # override).  The field name is kept generic (not "date_utc") so
    # the UI doesn't lie when a non-UTC zone is in effect.
    date_local: str
    timezone: str
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


def _resolve_tz(overrides: dict[str, Any] | None = None) -> tuple[str, ZoneInfo]:
    """Pick the trading-day timezone.

    Precedence: operator override ``risk.daily_loss_tz`` > env
    ``AXIOM_DAILY_LOSS_TZ`` > ``UTC``.  An unknown zone name falls
    back to UTC with a warning so a typo never silently shifts the
    halt boundary by hours.
    """
    name: str | None = None
    if overrides and overrides.get("daily_loss_tz"):
        name = str(overrides["daily_loss_tz"]).strip() or None
    if name is None:
        env = os.environ.get("AXIOM_DAILY_LOSS_TZ", "").strip()
        if env:
            name = env
    if not name:
        name = DEFAULT_TZ
    try:
        return name, ZoneInfo(name)
    except ZoneInfoNotFoundError:
        log.warning(
            "AXIOM_DAILY_LOSS_TZ / risk.daily_loss_tz = %r is not a known "
            "IANA zone; falling back to UTC", name,
        )
        return DEFAULT_TZ, ZoneInfo(DEFAULT_TZ)


def _today_in_tz(tz: ZoneInfo) -> date:
    return datetime.now(tz).date()


# Kept for backwards compatibility with the test suite + callers that
# don't need a configurable zone.
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


async def _fetch_snapshot_inputs(
    today: date, tz_name: str = DEFAULT_TZ
) -> tuple[float, float, float | None]:
    """Aggregate the three snapshot inputs in a single round-trip.

    Returns ``(realized_pnl_today_usd, unrealized_pnl_open_usd,
    start_of_day_bankroll_usd_or_None)``.

    The CTEs preserve the exact semantics of the older per-query helpers:

    * ``realized`` sums settled trades whose ``settled_at`` falls on
      ``$1`` when projected into ``$2`` (the configured trading-day TZ).
      The trades table stores realized P&L per trade in the ``pnl``
      column (there is no ``realized_pnl`` column on trades — that
      column lives on ``positions``).  Earlier revisions of this file
      referenced ``trades.realized_pnl`` and silently degraded the
      gate every cycle with "column does not exist", leaving the
      daily-loss kill switch as a no-op in production.
    * ``unrealized`` sums open positions' ``unrealized_pnl``.
    * Start-of-day bankroll is the latest ``bankroll_events.balance_cents``
      strictly before ``$1`` in the same TZ, falling back to the earliest
      row ever recorded so the pct threshold still has a denominator on
      day-1 deploys when no prior-day rows exist.

    A failure returns the same safe defaults the per-query paths used so
    the gate keeps passing rather than blocking trades on a transient DB
    error — the 5s snapshot cache absorbs a brief outage.
    """
    try:
        row = await db.fetchrow(
            """
            WITH realized AS (
                SELECT coalesce(sum(pnl), 0)::float AS v
                  FROM trades
                 WHERE settled_at IS NOT NULL
                   AND ((settled_at at time zone $2)::date) = $1
            ),
            unrealized AS (
                SELECT coalesce(sum(unrealized_pnl), 0)::float AS v
                  FROM positions
                 WHERE coalesce(status, 'open') = 'open'
            ),
            sod_primary AS (
                SELECT balance_cents
                  FROM bankroll_events
                 WHERE ((occurred_at at time zone $2)::date) < $1
                 ORDER BY id DESC
                 LIMIT 1
            ),
            sod_fallback AS (
                SELECT balance_cents
                  FROM bankroll_events
                 ORDER BY id ASC
                 LIMIT 1
            )
            SELECT
                (SELECT v FROM realized) AS realized,
                (SELECT v FROM unrealized) AS unrealized,
                COALESCE(
                    (SELECT balance_cents FROM sod_primary),
                    (SELECT balance_cents FROM sod_fallback)
                ) AS sod_balance_cents
            """,
            today,
            tz_name,
        )
    except Exception as e:
        log.warning("daily_loss snapshot aggregate query failed: %s", e)
        return 0.0, 0.0, None

    if row is None:
        return 0.0, 0.0, None
    realized = float(row["realized"] or 0.0)
    unrealized = float(row["unrealized"] or 0.0)
    sod_cents = row["sod_balance_cents"]
    bankroll = (
        float(int(sod_cents)) / 100.0 if sod_cents is not None else None
    )
    return realized, unrealized, bankroll


async def compute_snapshot(
    overrides: dict[str, Any] | None = None,
    *,
    use_cache: bool = True,
) -> DailyLossSnapshot:
    """Build a fresh (or cached) snapshot of today's P&L and thresholds.

    "Today" is the current date in the configured trading-day timezone
    (default UTC). A TZ or override change invalidates the cache on the
    next call because the cache-key comparison checks ``(date_local, tz)``.
    """
    global _cache
    tz_name, tz = _resolve_tz(overrides)
    now_m = time.monotonic()
    today_str = _today_in_tz(tz).isoformat()
    if use_cache and _cache is not None:
        expires, snap = _cache
        if (
            now_m < expires
            and snap.date_local == today_str
            and snap.timezone == tz_name
        ):
            return snap
    async with _cache_lock:
        # Re-check inside lock so concurrent callers share one DB hit.
        if use_cache and _cache is not None:
            expires, snap = _cache
            if (
                time.monotonic() < expires
                and snap.date_local == _today_in_tz(tz).isoformat()
                and snap.timezone == tz_name
            ):
                return snap
        today = _today_in_tz(tz)
        realized, unrealized, bankroll = await _fetch_snapshot_inputs(
            today, tz_name
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
            date_local=today.isoformat(),
            timezone=tz_name,
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

    The state transition (flipping ``risk.trading_mode`` to ``halt`` and
    tripping the in-process exchange kill switch) ALWAYS runs whenever
    the gate trips and the system is not already halted — including
    when an operator has re-armed paper/live earlier the same day.
    Only the operator alert is deduplicated, keyed on UTC date so a
    trading-day-timezone override can never shift the dedupe window
    off of calendar UTC.

    Returns True when an alert was dispatched on this call, False when
    the alert was suppressed (either because the system was already
    halted, or the UTC-day dedupe key was already claimed).  Callers
    that want the cached snapshot updated should call
    :func:`invalidate_cache`.
    """
    current = await _current_trading_mode()
    # Always force back to halt when we're tripped and not already halted —
    # this guards against an operator re-arming paper/live while the day's
    # loss budget is still blown.
    if current != "halt":
        await _persist_halt()
        # Trip the in-memory kill switch so this process stops submitting
        # orders immediately, even before the next cycle re-reads the DB.
        try:
            from ..exchange_kill_switch import halt as _halt
            _halt(snap.tripped_reason or "daily loss cap breached")
        except Exception as e:
            log.warning("daily_loss exchange_kill_switch trip failed: %s", e)

    # Alert is deduped on a UTC-calendar-day key (NOT snap.date_local)
    # so a non-UTC trading-day setting never drifts the dedupe window.
    utc_date = datetime.now(timezone.utc).date().isoformat()
    dedupe_key = f"daily_loss_halt:{utc_date}"
    try:
        ok = await notif_alerts.dedupe_or_skip(
            "daily_loss_halt", utc_date, ttl_seconds=86400
        )
    except Exception as e:
        log.warning("daily_loss dedupe claim failed: %s", e)
        ok = True
    if not ok:
        log.info("daily_loss alert suppressed by dedupe: %s", dedupe_key)
        return False
    # Suppress a second alert-within-the-same-day for the case where the
    # system was already halted when we arrived (we never dispatched the
    # first alert on this path either).  The kalshi/emergency-stop flow
    # fires its own `halt` alert — piling a daily-loss one on top would
    # be noise.
    if current == "halt":
        return False
    try:
        notif_alerts.daily_loss_halt(
            date_utc=utc_date,
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
