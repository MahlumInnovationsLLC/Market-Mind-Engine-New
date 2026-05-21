"""Tests for gate_best_line — cross-book line-shopping router (task #828).

Scenarios from the task spec:
  (a) Kalshi wins → trade ships (PASS, no reroute).
  (b) DraftKings wins by 3% → trade blocked + alert (FAIL, decision=block_traditional_better).
  (c) Polymarket wins by 1.5% → re-routed to polymarket
      (PASS with ctx.venue_override=['polymarket']).
  (d) odds_api stale > 5min → gate passes (fail-open) — emulated as
      ``lookup_best_line`` returning an empty quotes list.

The gate is patched at the import-time symbol it resolves inside the
function via ``monkeypatch`` of ``app.risk.best_line_lookup``.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from app.risk import gates as _gates
from app.risk import best_line_lookup as _bll


@pytest.fixture(autouse=True)
def _isolate_event_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield
    loop.close()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_ctx(
    *,
    league: str = "NBA",
    market_type: str = "moneyline",
    current_price: float = 0.55,
    overrides: dict | None = None,
) -> _gates.RiskContext:
    return _gates.RiskContext(
        market_id="KX-NBA-LAL-BOS-ML",
        side="YES",
        vertical="sports",
        league=league,
        market_type=market_type,
        matchup="LAL vs BOS",
        current_price=current_price,
        model_probability=0.60,
        edge=0.05,
        confidence_band_low=0.55,
        confidence_band_high=0.65,
        agreement_rate=0.7,
        swarm_approved=True,
        swarm_recommended_action="BUY_YES",
        debate_id=1,
        intended_quantity=100,
        intended_notional_usd=55.0,
        bankroll_usd=1_000.0,
        open_positions_count=0,
        open_positions_notional_usd=0.0,
        open_positions_per_vertical_usd={},
        operator_overrides=overrides or {},
        market_cooldown_until=None,
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
    )


@dataclass
class _FakeQuote:
    book: str
    implied: float


@dataclass
class _FakeResult:
    quotes: list
    best_book: str | None
    best_implied: float | None
    best_traditional_book: str | None
    best_traditional_implied: float | None
    polymarket_implied: float | None
    error: str | None = None


def _install_lookup(monkeypatch, result):
    async def _fake_lookup(**_kwargs):
        return result
    monkeypatch.setattr(_bll, "lookup_best_line", _fake_lookup)


def _disable_alert_dispatch(monkeypatch):
    async def _noop(**_kw):
        return None
    monkeypatch.setattr(_gates, "_emit_best_line_alert", _noop)


# ---------------------------------------------------------------------------
# (a) Kalshi wins → PASS without reroute.
# ---------------------------------------------------------------------------


def test_kalshi_wins_passes(monkeypatch):
    _disable_alert_dispatch(monkeypatch)
    _install_lookup(monkeypatch, _FakeResult(
        quotes=[_FakeQuote("draftkings", 0.58)],
        best_book="draftkings",
        best_implied=0.58,
        best_traditional_book="draftkings",
        best_traditional_implied=0.58,
        polymarket_implied=None,
    ))
    ctx = _make_ctx(current_price=0.55)
    res = _run(_gates.gate_best_line(ctx))
    assert res.passed is True
    assert ctx.venue_override is None
    assert res.details.get("decision") == "pass"


# ---------------------------------------------------------------------------
# (b) DraftKings 3% better → BLOCK + alert.
# ---------------------------------------------------------------------------


def test_traditional_book_better_blocks(monkeypatch):
    _disable_alert_dispatch(monkeypatch)
    _install_lookup(monkeypatch, _FakeResult(
        quotes=[_FakeQuote("draftkings", 0.52)],
        best_book="draftkings",
        best_implied=0.52,
        best_traditional_book="draftkings",
        best_traditional_implied=0.52,
        polymarket_implied=None,
    ))
    ctx = _make_ctx(current_price=0.55)  # 3% worse than DK's 0.52
    res = _run(_gates.gate_best_line(ctx))
    assert res.passed is False
    assert res.details.get("decision") == "block_traditional_better"
    assert res.details.get("best_book") == "draftkings"
    assert res.details.get("delta_bps") >= 200
    assert ctx.venue_override is None


# ---------------------------------------------------------------------------
# (c) Polymarket 1.5% better → reroute (PASS + venue_override).
#     Uses per-league override knocking threshold down to 100 bps.
# ---------------------------------------------------------------------------


def test_polymarket_better_reroutes(monkeypatch):
    _disable_alert_dispatch(monkeypatch)
    _install_lookup(monkeypatch, _FakeResult(
        quotes=[_FakeQuote("polymarket", 0.535)],
        best_book="polymarket",
        best_implied=0.535,
        best_traditional_book=None,
        best_traditional_implied=None,
        polymarket_implied=0.535,
    ))
    ctx = _make_ctx(
        current_price=0.55,
        overrides={"best_line_threshold_bps_nba": 100},
    )
    res = _run(_gates.gate_best_line(ctx))
    assert res.passed is True
    assert ctx.venue_override == ["polymarket"]
    assert res.details.get("decision") == "reroute_polymarket"
    assert res.details.get("delta_bps") >= 100


# ---------------------------------------------------------------------------
# (d) Stale / empty data → fail-open (PASS with reason).
# ---------------------------------------------------------------------------


def test_stale_data_fails_open(monkeypatch):
    _disable_alert_dispatch(monkeypatch)
    _install_lookup(monkeypatch, _FakeResult(
        quotes=[],
        best_book=None,
        best_implied=None,
        best_traditional_book=None,
        best_traditional_implied=None,
        polymarket_implied=None,
    ))
    ctx = _make_ctx(current_price=0.55)
    res = _run(_gates.gate_best_line(ctx))
    assert res.passed is True
    assert res.details.get("fail_open") == "no_recent_quotes"
    assert ctx.venue_override is None


# ---------------------------------------------------------------------------
# Scope guards.
# ---------------------------------------------------------------------------


def test_non_sports_vertical_scoped_out(monkeypatch):
    _disable_alert_dispatch(monkeypatch)
    ctx = _make_ctx()
    ctx.vertical = "weather"
    res = _run(_gates.gate_best_line(ctx))
    assert res.passed is True
    assert res.details.get("scoped_out") == "non-sports"


def test_unsupported_market_type_scoped_out(monkeypatch):
    _disable_alert_dispatch(monkeypatch)
    ctx = _make_ctx(market_type="player_props")
    res = _run(_gates.gate_best_line(ctx))
    assert res.passed is True
    assert res.details.get("scoped_out") == "unsupported_market_type"


# ---------------------------------------------------------------------------
# Latency budget — lookup that hangs longer than 50ms fails-open.
# ---------------------------------------------------------------------------


def test_lookup_timeout_fails_open(monkeypatch):
    _disable_alert_dispatch(monkeypatch)

    async def _slow_lookup(**_kw):
        await asyncio.sleep(0.5)
        return _FakeResult([], None, None, None, None, None)

    monkeypatch.setattr(_bll, "lookup_best_line", _slow_lookup)
    ctx = _make_ctx()
    res = _run(_gates.gate_best_line(ctx))
    assert res.passed is True
    assert res.details.get("fail_open") == "lookup_timeout"


# ---------------------------------------------------------------------------
# Implied delta helper sanity check.
# ---------------------------------------------------------------------------


def test_implied_delta_bps_basic():
    # 55% → 52% = 300 bps absolute delta.
    assert _bll.implied_delta_bps(0.55, 0.52) == 300
    # Helper returns absolute magnitude — gate guards direction itself.
    assert _bll.implied_delta_bps(0.50, 0.55) == 500
