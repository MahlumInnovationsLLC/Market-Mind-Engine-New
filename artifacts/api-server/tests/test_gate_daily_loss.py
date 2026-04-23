"""Tests for the daily-loss kill-switch gate (task #268).

The gate wraps ``app.risk.daily_loss`` — we stub the three DB reads
(realized, unrealized, start-of-day bankroll) plus the trading-mode
lookup so no asyncpg pool is required, then drive the full gate chain
via :func:`app.risk.gates.gate_daily_loss`.
"""
from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import patch

from app.risk import daily_loss as _dl
from app.risk import gates as _gates


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(overrides: dict | None = None):
    return _gates.RiskContext(
        market_id="TEST-T1",
        side="YES",
        vertical="sports",
        league=None,
        market_type=None,
        matchup=None,
        current_price=0.5,
        model_probability=0.6,
        edge=0.1,
        confidence_band_low=None,
        confidence_band_high=None,
        agreement_rate=None,
        swarm_approved=True,
        swarm_recommended_action="BUY_YES",
        debate_id=None,
        intended_quantity=10,
        intended_notional_usd=5.0,
        bankroll_usd=1000.0,
        open_positions_count=0,
        open_positions_notional_usd=0.0,
        open_positions_per_vertical_usd={},
        operator_overrides=overrides or {},
        market_cooldown_until=None,
    )


class _Patcher:
    """Context manager wiring mock returns for every DB path the daily-loss
    module touches.  Each test tweaks a couple of knobs instead of hand-rolling
    full monkey-patches."""

    def __init__(
        self,
        *,
        realized: float = 0.0,
        unrealized: float = 0.0,
        bankroll: float | None = 1000.0,
        trading_mode: str | None = "live",
        today: date | None = None,
    ) -> None:
        self.realized = realized
        self.unrealized = unrealized
        self.bankroll = bankroll
        self.trading_mode = trading_mode
        self.today = today or date(2026, 4, 23)
        self.persist_calls: list[tuple] = []
        self.halt_calls: list[str] = []
        self.alerts: list[dict] = []

    async def _fake_sum_realized(self, today, tz_name="UTC"):
        self.last_tz_realized = tz_name
        self.last_date_realized = today
        return float(self.realized)

    async def _fake_sum_unrealized(self):
        return float(self.unrealized)

    async def _fake_start_of_day(self, today, tz_name="UTC"):
        self.last_tz_bankroll = tz_name
        self.last_date_bankroll = today
        return self.bankroll

    async def _fake_current_mode(self):
        return self.trading_mode

    async def _fake_persist(self):
        self.persist_calls.append(("halt",))
        # Persisting the halt reflects in subsequent reads in the real DB.
        self.trading_mode = "halt"

    async def _fake_dedupe(self, alert_type, dedupe_key, ttl_seconds=0):
        # First call wins; replay returns False.
        key = (alert_type, dedupe_key)
        if key in self._dedupe_seen:
            return False
        self._dedupe_seen.add(key)
        return True

    def _fake_alert(self, **kwargs):
        self.alerts.append(kwargs)
        return {"dispatched": True}

    def _fake_exchange_halt(self, reason):
        self.halt_calls.append(reason)

    def __enter__(self):
        self._dedupe_seen: set[tuple] = set()
        _dl.invalidate_cache()
        self._patches = [
            patch.object(_dl, "_today_utc", lambda: self.today),
            patch.object(_dl, "_today_in_tz", lambda tz: self.today),
            patch.object(_dl, "_sum_realized_today", self._fake_sum_realized),
            patch.object(_dl, "_sum_unrealized", self._fake_sum_unrealized),
            patch.object(_dl, "_start_of_day_bankroll", self._fake_start_of_day),
            patch.object(_dl, "_current_trading_mode", self._fake_current_mode),
            patch.object(_dl, "_persist_halt", self._fake_persist),
            patch(
                "app.notifications.alerts.dedupe_or_skip", self._fake_dedupe
            ),
            patch(
                "app.notifications.alerts.daily_loss_halt", self._fake_alert
            ),
            patch(
                "app.exchange_kill_switch.halt", self._fake_exchange_halt
            ),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        _dl.invalidate_cache()


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


def test_gate_no_thresholds_is_noop(monkeypatch):
    """With neither env var nor override set, gate_daily_loss must pass
    through without tripping anything — no DB writes, no alerts."""
    monkeypatch.delenv("AXIOM_DAILY_LOSS_HALT_USD", raising=False)
    monkeypatch.delenv("AXIOM_DAILY_LOSS_HALT_PCT", raising=False)
    with _Patcher(realized=-500.0, unrealized=-500.0) as p:
        res = asyncio.run(_gates.gate_daily_loss(_make_ctx()))
    assert res.passed is True
    assert res.details["loss_usd"] == 1000.0
    assert p.persist_calls == []
    assert p.alerts == []


def test_gate_under_threshold_passes(monkeypatch):
    """Loss under both caps: passes, details carry the loss snapshot."""
    monkeypatch.setenv("AXIOM_DAILY_LOSS_HALT_USD", "500")
    monkeypatch.setenv("AXIOM_DAILY_LOSS_HALT_PCT", "10")
    with _Patcher(realized=-100.0, unrealized=-50.0, bankroll=2000.0) as p:
        res = asyncio.run(_gates.gate_daily_loss(_make_ctx()))
    assert res.passed is True
    assert res.details["loss_usd"] == 150.0
    assert res.details["threshold_usd"] == 500.0
    assert res.details["threshold_pct_usd"] == 200.0  # 10% of 2000
    assert p.persist_calls == []
    assert p.alerts == []


def test_gate_tripped_by_absolute_cap(monkeypatch):
    """Breaching the absolute dollar cap trips the halt side-effect
    (persist + exchange kill + one alert) and fails the gate."""
    monkeypatch.setenv("AXIOM_DAILY_LOSS_HALT_USD", "250")
    monkeypatch.delenv("AXIOM_DAILY_LOSS_HALT_PCT", raising=False)
    with _Patcher(realized=-300.0, unrealized=0.0, bankroll=5000.0) as p:
        res = asyncio.run(_gates.gate_daily_loss(_make_ctx()))
    assert res.passed is False
    assert "absolute cap" in (res.reason or "")
    assert len(p.persist_calls) == 1
    assert len(p.halt_calls) == 1
    assert len(p.alerts) == 1
    assert p.alerts[0]["loss_usd"] == 300.0
    assert p.alerts[0]["threshold_usd"] == 250.0


def test_gate_tripped_by_percent_cap(monkeypatch):
    """Absolute cap unset, pct cap breached — uses start-of-day bankroll
    as the denominator and reports the pct branch."""
    monkeypatch.delenv("AXIOM_DAILY_LOSS_HALT_USD", raising=False)
    monkeypatch.setenv("AXIOM_DAILY_LOSS_HALT_PCT", "5")  # 5% of 1000 = $50
    with _Patcher(realized=-60.0, unrealized=0.0, bankroll=1000.0) as p:
        res = asyncio.run(_gates.gate_daily_loss(_make_ctx()))
    assert res.passed is False
    assert res.details["threshold_pct_usd"] == 50.0
    assert "5.00%" in (res.reason or "") or "of start" in (res.reason or "")
    assert len(p.alerts) == 1


def test_already_halted_skips_state_change_and_alert(monkeypatch):
    """A gate evaluation while ``trading_mode`` is already ``halt`` must
    not re-persist halt (no-op) and must not fire a duplicate alert —
    the manual/kalshi halt alert already went out."""
    monkeypatch.setenv("AXIOM_DAILY_LOSS_HALT_USD", "100")
    with _Patcher(
        realized=-500.0, unrealized=0.0, bankroll=1000.0, trading_mode="halt"
    ) as p:
        res = asyncio.run(_gates.gate_daily_loss(_make_ctx()))
    # Gate still reports fail (loss is past the cap), but side-effects are
    # suppressed because the system is already halted.
    assert res.passed is False
    assert p.persist_calls == []
    assert p.alerts == []


def test_operator_resume_while_tripped_rehalts_without_double_alert(monkeypatch):
    """If an operator flips runtime mode back to paper/live while the
    day's loss budget is still blown, the gate MUST force the mode
    back to halt on the next evaluation — but dedupe keeps the alert
    at one-per-UTC-day so the inbox doesn't get spammed."""
    monkeypatch.setenv("AXIOM_DAILY_LOSS_HALT_USD", "100")
    with _Patcher(realized=-500.0, unrealized=0.0, bankroll=1000.0) as p:
        # First call: tripped, mode flips to halt, first alert fires.
        r1 = asyncio.run(_gates.gate_daily_loss(_make_ctx()))
        _dl.invalidate_cache()
        # Simulate operator re-arming paper while the loss is still breached.
        p.trading_mode = "paper"
        r2 = asyncio.run(_gates.gate_daily_loss(_make_ctx()))
    assert r1.passed is False and r2.passed is False
    # State transition ran TWICE (first trip + re-halt after operator resume).
    assert len(p.persist_calls) == 2
    # But the alert fired ONCE — UTC-day dedupe suppresses the second one.
    assert len(p.alerts) == 1


def test_dedupe_key_uses_utc_date_not_local(monkeypatch):
    """Dedupe must key on UTC calendar date regardless of the configured
    trading-day timezone, so an operator in America/New_York doesn't
    get two alerts bridging the UTC midnight rollover."""
    monkeypatch.setenv("AXIOM_DAILY_LOSS_HALT_USD", "100")
    monkeypatch.setenv("AXIOM_DAILY_LOSS_TZ", "America/New_York")
    captured: list[tuple] = []

    async def _spy_dedupe(alert_type, dedupe_key, ttl_seconds=0):
        captured.append((alert_type, dedupe_key, ttl_seconds))
        return True

    with _Patcher(realized=-500.0, unrealized=0.0, bankroll=1000.0) as p:
        with patch(
            "app.notifications.alerts.dedupe_or_skip", _spy_dedupe
        ):
            asyncio.run(_gates.gate_daily_loss(_make_ctx()))
    # The dedupe key must be the UTC date, not the NY-local date.
    from datetime import datetime as _dt, timezone as _tz
    expected_utc_date = _dt.now(_tz.utc).date().isoformat()
    assert captured, "dedupe_or_skip was never called"
    assert captured[0][0] == "daily_loss_halt"
    assert captured[0][1] == expected_utc_date


def test_operator_override_beats_env(monkeypatch):
    """Operator-override thresholds must take precedence over env vars."""
    monkeypatch.setenv("AXIOM_DAILY_LOSS_HALT_USD", "10000")  # loose env
    # override says $50 — should trip at $60 loss.
    overrides = {"daily_loss_halt_usd": 50}
    with _Patcher(realized=-60.0, unrealized=0.0, bankroll=1000.0) as p:
        res = asyncio.run(_gates.gate_daily_loss(_make_ctx(overrides)))
    assert res.passed is False
    assert res.details["threshold_usd"] == 50.0
    assert len(p.alerts) == 1


def test_configurable_timezone_shifts_day_boundary(monkeypatch):
    """Setting AXIOM_DAILY_LOSS_TZ must change the trading-day boundary:
    the SQL filter binds the configured zone, and the snapshot's
    ``timezone`` + ``date_local`` fields reflect it."""
    monkeypatch.setenv("AXIOM_DAILY_LOSS_TZ", "America/New_York")
    monkeypatch.setenv("AXIOM_DAILY_LOSS_HALT_USD", "500")
    _dl.invalidate_cache()
    # Intentionally don't patch _today_in_tz so the real TZ lookup runs.
    with _Patcher(realized=-100.0, unrealized=0.0, bankroll=1000.0) as p:
        # Unpatch the _today_in_tz patcher set in __enter__ so the real
        # zoneinfo lookup runs against the env var.
        for patcher in p._patches:
            if getattr(patcher, "attribute", None) == "_today_in_tz":
                patcher.stop()
        snap = asyncio.run(_dl.compute_snapshot(None))
    assert snap.timezone == "America/New_York"
    assert p.last_tz_realized == "America/New_York"
    assert p.last_tz_bankroll == "America/New_York"
    # date_local must be an ISO date string in that zone (today in NY)
    import re as _re
    assert _re.fullmatch(r"\d{4}-\d{2}-\d{2}", snap.date_local)


def test_unknown_timezone_falls_back_to_utc(monkeypatch):
    """A typo in the TZ name must not silently shift the halt boundary —
    fall back to UTC with a warning."""
    monkeypatch.setenv("AXIOM_DAILY_LOSS_TZ", "Not/A_Real_Zone")
    _dl.invalidate_cache()
    tz_name, _tz = _dl._resolve_tz(None)
    assert tz_name == "UTC"


def test_operator_override_tz_beats_env(monkeypatch):
    """operator_override 'daily_loss_tz' must beat the env var."""
    monkeypatch.setenv("AXIOM_DAILY_LOSS_TZ", "America/New_York")
    name, _tz = _dl._resolve_tz({"daily_loss_tz": "Asia/Tokyo"})
    assert name == "Asia/Tokyo"


def test_tz_rollover_invalidates_cache(monkeypatch):
    """When the UTC day ticks over the snapshot cache must NOT return the
    previous day's numbers — that would mis-report yesterday's loss as
    today's budget usage."""
    monkeypatch.setenv("AXIOM_DAILY_LOSS_HALT_USD", "1000")
    # Day 1 — heavy loss, no trip.
    with _Patcher(
        realized=-900.0,
        unrealized=0.0,
        bankroll=5000.0,
        today=date(2026, 4, 23),
    ) as p:
        snap1 = asyncio.run(_dl.compute_snapshot(None))
        assert snap1.date_local == "2026-04-23"
        assert snap1.loss_usd == 900.0

    # Day 2 — same process, fresh day, realized resets to 0, snapshot
    # must reflect the new date + 0 loss rather than the cached day-1 snap.
    with _Patcher(
        realized=0.0,
        unrealized=0.0,
        bankroll=5000.0,
        today=date(2026, 4, 24),
    ) as p:
        snap2 = asyncio.run(_dl.compute_snapshot(None))
        assert snap2.date_local == "2026-04-24"
        assert snap2.loss_usd == 0.0
        assert snap2.tripped is False


def test_gates_registry_includes_daily_loss():
    """Registry invariant — daily_loss must be gate #2 (after kill_switch),
    so it runs before any of the expensive DB-backed checks."""
    names = [name for (name, _fn) in _gates.GATES]
    assert names[0] == "kill_switch"
    assert names[1] == "daily_loss"
    assert len(_gates.GATES) == 13


def test_snapshot_green_day_reports_zero_loss(monkeypatch):
    """A green P&L day must report loss=0 (not a negative number) so the
    UI gauge shows 0% used rather than something nonsensical."""
    with _Patcher(realized=250.0, unrealized=100.0, bankroll=1000.0):
        snap = asyncio.run(_dl.compute_snapshot(None))
    assert snap.net_pnl_usd == 350.0
    assert snap.loss_usd == 0.0
    assert snap.tripped is False
