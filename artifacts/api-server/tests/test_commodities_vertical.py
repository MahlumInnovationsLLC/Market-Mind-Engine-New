"""Tests for the commodities trading vertical (task #198)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import verticals as vertical_registry
from app.commodities import (
    InvalidCommodityTicker,
    SUPPORTED_SYMBOLS,
    correlated_exposure,
    correlation_for,
    parse_commodity_ticker,
)
from app.commodities.correlations import CORRELATED_PAIRS
from app.commodities.features import (
    FEATURE_COUNT,
    FEATURE_NAMES,
    build_features_from_bars,
)
from app.commodities.models import BaselineLGBXGBStack
from app.risk.gates import GATES, RiskContext, gate_per_vertical_cap


# ---------------------------------------------------------------------------
# Ticker parser
# ---------------------------------------------------------------------------


def test_parse_simple_oil_ticker():
    t = parse_commodity_ticker("KXOIL-26APR15-T75")
    assert t.symbol == "WTI"
    assert t.timeframe == "1d"
    assert t.threshold == 75.0
    assert t.direction == "above"


def test_parse_below_direction():
    t = parse_commodity_ticker("KXGOLD-26MAY15-B2400")
    assert t.symbol == "GOLD"
    assert t.threshold == 2400.0
    assert t.direction == "below"


def test_parse_intraday_natgas():
    t = parse_commodity_ticker("KXNATGAS1H-26APR1517-T2.50")
    assert t.symbol == "NG"
    assert t.timeframe == "1h"
    assert t.threshold == pytest.approx(2.50)


def test_parse_grains_and_softs():
    assert parse_commodity_ticker("KXCORN-26MAY-T450").symbol == "CORN"
    assert parse_commodity_ticker("KXSOY-26MAY-T1100").symbol == "SOY"
    assert parse_commodity_ticker("KXWHEAT-26JUN-T550").symbol == "WHEAT"
    assert parse_commodity_ticker("KXCOFFEE-26MAR-T200").symbol == "COFFEE"
    assert parse_commodity_ticker("KXSUGAR-26MAR-T22").symbol == "SUGAR"


def test_parse_rejects_unknown_symbol():
    with pytest.raises(InvalidCommodityTicker):
        parse_commodity_ticker("KXFOO-26APR-T100")


def test_parse_rejects_blank():
    with pytest.raises(InvalidCommodityTicker):
        parse_commodity_ticker("")


def test_parse_rejects_malformed_shape():
    with pytest.raises(InvalidCommodityTicker):
        parse_commodity_ticker("KXOIL_26APR")


def test_supported_symbols_cover_ten():
    assert len(SUPPORTED_SYMBOLS) == 10
    assert "WTI" in SUPPORTED_SYMBOLS and "SUGAR" in SUPPORTED_SYMBOLS


# ---------------------------------------------------------------------------
# Vertical registry
# ---------------------------------------------------------------------------


def test_commodities_vertical_registered():
    spec = vertical_registry.get_spec("commodities")
    assert spec is not None
    assert spec.predictor_hook == "commodities_binary"
    assert spec.persona_profile == "commodities"
    assert spec.kelly_multiplier == pytest.approx(0.15)
    assert spec.default_allocation_pct == pytest.approx(0.10)
    assert "KXOIL" in spec.ticker_prefixes
    assert "KXSUGAR" in spec.ticker_prefixes
    assert spec.duplicate_trade_exempt is True


def test_commodities_listed_in_calibrators_supported():
    from app.calibrators import SUPPORTED_LEAGUES

    assert "COMMODITIES" in SUPPORTED_LEAGUES


# ---------------------------------------------------------------------------
# Correlation matrix
# ---------------------------------------------------------------------------


def test_correlation_matrix_has_eleven_pairs():
    assert len(CORRELATED_PAIRS) == 11


def test_correlation_lookup_symmetric():
    assert correlation_for("GOLD", "SILVER") == pytest.approx(0.78)
    assert correlation_for("SILVER", "GOLD") == pytest.approx(0.78)
    assert correlation_for("GOLD", "GOLD") == 1.0
    assert correlation_for("GOLD", "COFFEE") == 0.0


def test_correlated_exposure_aggregates():
    # 100 USD open on SILVER → contributes 0.78 * 100 to a GOLD candidate.
    # 50 USD open on COFFEE → uncorrelated to GOLD (rho=0), excluded.
    report = correlated_exposure(
        {"SILVER": 100.0, "COFFEE": 50.0},
        intended_symbol="GOLD",
        intended_notional=20.0,
    )
    assert report["correlated_open"] == pytest.approx(78.0)
    assert report["projected"] == pytest.approx(98.0)
    assert "SILVER" in report["pair_contributions"]
    assert "COFFEE" not in report["pair_contributions"]


def test_correlated_exposure_self_pair_full_weight():
    # Re-buying GOLD when 200 USD is already open should count fully.
    report = correlated_exposure(
        {"GOLD": 200.0},
        intended_symbol="GOLD",
        intended_notional=10.0,
    )
    assert report["correlated_open"] == pytest.approx(200.0)
    assert report["projected"] == pytest.approx(210.0)


# ---------------------------------------------------------------------------
# Feature builder + baseline predictor
# ---------------------------------------------------------------------------


def _synthetic_bars(n: int = 60, *, base: float = 75.0,
                    trend: float = 0.001) -> list[dict]:
    """Slightly trending bars for deterministic feature output."""
    now = datetime.now(timezone.utc)
    bars = []
    price = base
    for i in range(n):
        price *= 1.0 + trend
        bars.append({
            "ts": now - timedelta(hours=(n - i)),
            "o": price * 0.999, "h": price * 1.002,
            "l": price * 0.997, "c": price,
            "v": 1000.0 + i,
        })
    return bars


def test_feature_builder_produces_expected_width():
    t = parse_commodity_ticker("KXOIL-26APR-T80")
    fv = build_features_from_bars(_synthetic_bars(), t)
    assert len(fv.values) == FEATURE_COUNT
    assert set(fv.as_dict().keys()) == set(FEATURE_NAMES)
    assert fv.last_price > 0


def test_baseline_stack_returns_well_formed_proba():
    t = parse_commodity_ticker("KXOIL-26APR-T80")
    fv = build_features_from_bars(_synthetic_bars(trend=0.002), t)
    stack = BaselineLGBXGBStack(timeframe="1d")
    import numpy as np

    proba = stack.predict_proba(np.asarray([fv.values]))
    assert proba.shape == (1, 2)
    p_yes = float(proba[0, 1])
    assert 0.0 <= p_yes <= 1.0
    # Each row must sum to 1.0.
    assert float(proba[0].sum()) == pytest.approx(1.0)


def test_baseline_stack_directionality_above_vs_below():
    """A market that's already above the strike, with bullish momentum,
    should have HIGHER YES probability for an `above` direction than for
    the same setup re-cast as a `below` direction."""
    bars = _synthetic_bars(trend=0.003)
    above = parse_commodity_ticker("KXOIL-26APR-T75")  # bars trend through 75
    below = parse_commodity_ticker("KXOIL-26APR-B75")
    stack = BaselineLGBXGBStack(timeframe="1d")
    import numpy as np

    fv_above = build_features_from_bars(bars, above)
    fv_below = build_features_from_bars(bars, below)
    p_above = stack.predict_proba(np.asarray([fv_above.values]))[0, 1]
    p_below = stack.predict_proba(np.asarray([fv_below.values]))[0, 1]
    assert p_above > p_below


# ---------------------------------------------------------------------------
# 12-gate invariant + commodities correlation sub-check
# ---------------------------------------------------------------------------


def test_twelve_gate_invariant_unchanged():
    assert len(GATES) == 12


def _commodities_ctx(**overrides) -> RiskContext:
    base = dict(
        market_id="KXGOLD-26APR15-T2400",
        side="YES",
        vertical="commodities",
        league="COMMODITIES",
        market_type="binary",
        matchup="GOLD",
        current_price=0.50,
        model_probability=0.60,
        edge=0.10,
        confidence_band_low=0.55,
        confidence_band_high=0.65,
        agreement_rate=0.80,
        swarm_approved=True,
        swarm_recommended_action="BUY_YES",
        debate_id=999,
        intended_quantity=10,
        intended_notional_usd=50.0,
        bankroll_usd=10_000.0,
        open_positions_count=0,
        open_positions_notional_usd=0.0,
        open_positions_per_vertical_usd={},
        operator_overrides={},
        market_cooldown_until=None,
    )
    base.update(overrides)
    return RiskContext(**base)


def test_per_vertical_cap_passes_under_correlation_cap():
    ctx = _commodities_ctx(
        intended_notional_usd=50.0,
        open_positions_per_matchup_usd={"COMMODITIES:SILVER": 100.0},
    )
    res = asyncio.run(gate_per_vertical_cap(ctx))
    assert res.passed, res.reason


def test_per_vertical_cap_rejects_correlated_overflow():
    """Tight correlation cap + heavy SILVER exposure should reject a
    new GOLD bet via the commodities_correlation sub-check."""
    ctx = _commodities_ctx(
        intended_notional_usd=200.0,
        # 0.78 * 3000 + 200 = 2540 → cap of 0.20 * 10_000 = 2000 fires.
        operator_overrides={"commodities_correlation_cap_pct": 0.20},
        open_positions_per_matchup_usd={"COMMODITIES:SILVER": 3000.0},
    )
    res = asyncio.run(gate_per_vertical_cap(ctx))
    assert not res.passed
    assert res.details["sub_check"] == "commodities_correlation"
    assert res.details["intended_symbol"] == "GOLD"
    assert "SILVER" in res.details["pair_contributions"]


def test_per_vertical_cap_correlation_ignores_uncorrelated_sym():
    """Heavy COFFEE exposure must NOT trip a GOLD candidate (rho=0)."""
    ctx = _commodities_ctx(
        intended_notional_usd=10.0,
        operator_overrides={"commodities_correlation_cap_pct": 0.05},
        open_positions_per_matchup_usd={"COMMODITIES:COFFEE": 9000.0},
    )
    res = asyncio.run(gate_per_vertical_cap(ctx))
    assert res.passed, res.reason
