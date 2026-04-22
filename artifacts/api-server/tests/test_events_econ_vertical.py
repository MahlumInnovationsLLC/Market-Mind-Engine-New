"""Tests for the events / macro-economics trading vertical (task #202)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app import verticals as vertical_registry
from app.events_econ import promotion as promotion_mod
from app.events_econ.calendar import ReleaseRow
from app.events_econ.model import consensus_prior_probability
from app.events_econ.tickers import (
    InvalidEventsEconTicker,
    SUPPORTED_RELEASES,
    parse_events_econ_ticker,
)
from app.risk.gates import RiskContext, gate_kelly_fraction, gate_market_cooldown
from app.swarm import profiles as swarm_profiles


# ---------------------------------------------------------------------------
# Ticker parser
# ---------------------------------------------------------------------------


def test_parse_cpi_ticker():
    t = parse_events_econ_ticker("KXCPI-24DEC-ABOVE3")
    assert t.release_type == "CPI"
    assert t.when == "24DEC"
    assert t.threshold == "ABOVE3"


def test_parse_decimal_threshold_with_underscore():
    t = parse_events_econ_ticker("KXPPI-25JAN-ABOVE0_3")
    assert t.release_type == "PPI"
    assert t.threshold == "ABOVE0_3"


def test_parse_fomc_ticker():
    t = parse_events_econ_ticker("KXFOMC-25MAR-HIKE25")
    assert t.release_type == "FOMC"
    assert t.threshold == "HIKE25"


def test_parse_jobless_weekly_when():
    t = parse_events_econ_ticker("KXJOBLESS-25W08-ABOVE220")
    assert t.release_type == "JOBLESS"
    assert t.when == "25W08"


def test_parse_case_insensitive():
    t = parse_events_econ_ticker("kxnfp-25feb-above200")
    assert t.release_type == "NFP"
    assert t.when == "25FEB"


def test_supported_releases_present():
    for rel in ("CPI", "PPI", "GDP", "FOMC", "NFP", "JOBLESS", "RETAIL"):
        assert rel in SUPPORTED_RELEASES


def test_parse_rejects_blank():
    with pytest.raises(InvalidEventsEconTicker):
        parse_events_econ_ticker("")


def test_parse_rejects_unrecognised_shape():
    with pytest.raises(InvalidEventsEconTicker):
        parse_events_econ_ticker("NOT-A-MACRO-TICKER-AT-ALL!!!")


# ---------------------------------------------------------------------------
# Vertical registration
# ---------------------------------------------------------------------------


def test_vertical_registered_with_kelly_015():
    spec = vertical_registry.get_spec("events-econ")
    assert spec is not None
    assert spec.predictor_hook == "events_econ_consensus_prior"
    assert spec.kelly_multiplier == pytest.approx(0.15)
    assert "EVENTS_ECON" in spec.leagues
    assert "KXCPI" in spec.ticker_prefixes
    assert "KXFOMC" in spec.ticker_prefixes
    assert "KXNFP" in spec.ticker_prefixes


def test_calibrators_includes_events_econ():
    from app.calibrators import SUPPORTED_LEAGUES
    assert "EVENTS_ECON" in SUPPORTED_LEAGUES


def test_league_config_present():
    from app.sports.leagues import get_league_config
    cfg = get_league_config("EVENTS_ECON")
    assert cfg is not None
    assert cfg.live_action_blackout is False


# ---------------------------------------------------------------------------
# Persona profile (5 baseline + 3 layered = 8)
# ---------------------------------------------------------------------------


def test_events_econ_profile_has_eight_personas_including_layered():
    profile = swarm_profiles.get_profile("events-econ")
    assert profile is not None
    ids = {p.id for p in profile.personas}
    for layered in ("macro_economist", "fed_watcher", "print_revisions"):
        assert layered in ids, f"missing layered persona {layered}"
    assert len(profile.personas) == 8


# ---------------------------------------------------------------------------
# Consensus-prior model
# ---------------------------------------------------------------------------


def _row(
    *,
    release_type: str = "CPI",
    consensus: float | None = None,
    actual: float | None = None,
    previous: float | None = None,
    days_ago: float = 30.0,
    source: str = "trading_economics_rss",
) -> ReleaseRow:
    now = datetime.now(timezone.utc)
    return ReleaseRow(
        source=source,
        signal_id=f"{source}/{release_type}/{days_ago}",
        release_type=release_type,
        release_at=now - timedelta(days=days_ago),
        consensus=consensus,
        previous=previous,
        actual=actual,
        revised_previous=None,
        payload={},
    )


def test_no_data_returns_prior_05_when_no_threshold():
    # Ticker with non-numeric threshold token + no history => prior 0.5.
    t = parse_events_econ_ticker("KXCPI-24DEC-NEUTRAL")
    res = consensus_prior_probability(t, [])
    assert res.probability == pytest.approx(0.5)


def test_above_threshold_lower_than_consensus_yields_high_prob():
    # Consensus 3.0, threshold 2.0 -> P(actual >= 2.0) should be HIGH.
    t = parse_events_econ_ticker("KXCPI-24DEC-ABOVE2")
    upcoming = _row(release_type="CPI", consensus=3.0, days_ago=-1.0)
    history = [_row(release_type="CPI", consensus=2.5 + i*0.1, actual=2.5 + i*0.1)
               for i in range(10)]
    res = consensus_prior_probability(t, [upcoming, *history])
    assert res.probability > 0.7


def test_above_threshold_higher_than_consensus_yields_low_prob():
    # Consensus 2.0, threshold 4.0 -> P(actual >= 4.0) should be LOW.
    t = parse_events_econ_ticker("KXCPI-24DEC-ABOVE4")
    upcoming = _row(release_type="CPI", consensus=2.0, days_ago=-1.0)
    history = [_row(release_type="CPI", consensus=2.0, actual=2.0 + (i-5)*0.1)
               for i in range(10)]
    res = consensus_prior_probability(t, [upcoming, *history])
    assert res.probability < 0.3


def test_clamps_into_band():
    t = parse_events_econ_ticker("KXCPI-24DEC-ABOVE100")  # absurd threshold
    upcoming = _row(release_type="CPI", consensus=2.0, days_ago=-1.0)
    res = consensus_prior_probability(t, [upcoming])
    assert 0.05 <= res.probability <= 0.95


def test_more_supportive_consensus_pushes_probability_higher():
    t = parse_events_econ_ticker("KXCPI-24DEC-ABOVE3")
    low_consensus = _row(release_type="CPI", consensus=2.5, days_ago=-1.0)
    high_consensus = _row(release_type="CPI", consensus=3.5, days_ago=-1.0)
    low = consensus_prior_probability(t, [low_consensus])
    high = consensus_prior_probability(t, [high_consensus])
    assert high.probability > low.probability


# ---------------------------------------------------------------------------
# Per-vertical Kelly multiplier override (gate)
# ---------------------------------------------------------------------------


def _ctx(**overrides) -> RiskContext:
    base = dict(
        market_id="KXCPI-24DEC-ABOVE3",
        side="YES",
        league="EVENTS_ECON",
        vertical="events-econ",
        market_type=None,
        matchup=None,
        current_price=0.50,
        model_probability=0.65,
        edge=0.15,
        confidence_band_low=None,
        confidence_band_high=None,
        agreement_rate=None,
        swarm_approved=True,
        swarm_recommended_action="BUY_YES",
        debate_id=None,
        intended_quantity=20,
        intended_notional_usd=20.0,
        bankroll_usd=10_000.0,
        open_positions_count=0,
        open_positions_notional_usd=0.0,
        open_positions_per_vertical_usd={},
        operator_overrides={},
        market_cooldown_until=None,
    )
    base.update(overrides)
    return RiskContext(**base)


def test_kelly_per_vertical_override_beats_spec_default():
    res = asyncio.run(gate_kelly_fraction(_ctx(
        intended_notional_usd=200.0,
        operator_overrides={"kelly_multiplier_events_econ": 0.20},
    )))
    assert res.details.get("kelly_multiplier") == pytest.approx(0.20)


# ---------------------------------------------------------------------------
# Pre-release blackout (gate_market_cooldown sub-check)
# ---------------------------------------------------------------------------


def test_pre_release_blackout_rejects_within_window():
    """5-min before the release prints, the gate rejects new entries."""
    next_release = datetime.now(timezone.utc) + timedelta(minutes=2)

    async def _fake_next_release_at(_release_type: str):
        return next_release

    with patch(
        "app.events_econ.calendar.next_release_at",
        side_effect=_fake_next_release_at,
    ):
        res = asyncio.run(gate_market_cooldown(_ctx()))
    assert res.passed is False
    assert res.details.get("reason") == "pre_release_blackout"
    assert res.details.get("release_type") == "CPI"


def test_pre_release_blackout_passes_outside_window():
    """Outside the 5-minute window, the gate falls through."""
    next_release = datetime.now(timezone.utc) + timedelta(hours=4)

    async def _fake_next_release_at(_release_type: str):
        return next_release

    with patch(
        "app.events_econ.calendar.next_release_at",
        side_effect=_fake_next_release_at,
    ):
        res = asyncio.run(gate_market_cooldown(_ctx()))
    # Either the gate passed, or it was rejected for some other reason
    # (but NOT the pre-release blackout).
    if not res.passed:
        assert res.details.get("reason") != "pre_release_blackout"


# ---------------------------------------------------------------------------
# Promotion thresholds
# ---------------------------------------------------------------------------


def test_promotion_constants():
    assert promotion_mod.SETTLED_THRESHOLD == 30
    assert promotion_mod.BRIER_THRESHOLD == pytest.approx(0.20)
    assert promotion_mod.DEFAULT_MULTIPLIER == pytest.approx(0.15)
    assert promotion_mod.PROMOTED_MULTIPLIER == pytest.approx(0.20)
    assert promotion_mod.SETTING_KEY == "risk.kelly_multiplier_events_econ"
    assert promotion_mod.VERTICAL == "events-econ"


def test_promotion_ineligible_when_under_settled_threshold():
    async def _fake_read_mult():
        return promotion_mod.DEFAULT_MULTIPLIER

    async def _fake_fetchrow(_q, _vertical):
        return {"n": 5, "brier": 0.1}

    with patch.object(promotion_mod, "_read_current_multiplier",
                      side_effect=_fake_read_mult), \
         patch.object(promotion_mod.db, "fetchrow",
                      side_effect=_fake_fetchrow):
        status = asyncio.run(promotion_mod.evaluate_promotion())
    assert status.eligible is False
    assert "need 30 settled trades" in status.reason


def test_promotion_ineligible_when_brier_too_high():
    async def _fake_read_mult():
        return promotion_mod.DEFAULT_MULTIPLIER

    async def _fake_fetchrow(_q, _vertical):
        return {"n": 50, "brier": 0.30}

    with patch.object(promotion_mod, "_read_current_multiplier",
                      side_effect=_fake_read_mult), \
         patch.object(promotion_mod.db, "fetchrow",
                      side_effect=_fake_fetchrow):
        status = asyncio.run(promotion_mod.evaluate_promotion())
    assert status.eligible is False
    assert "Brier" in status.reason


def test_promotion_eligible_when_thresholds_met():
    async def _fake_read_mult():
        return promotion_mod.DEFAULT_MULTIPLIER

    async def _fake_fetchrow(_q, _vertical):
        return {"n": 50, "brier": 0.15}

    with patch.object(promotion_mod, "_read_current_multiplier",
                      side_effect=_fake_read_mult), \
         patch.object(promotion_mod.db, "fetchrow",
                      side_effect=_fake_fetchrow):
        status = asyncio.run(promotion_mod.evaluate_promotion())
    assert status.eligible is True
    assert status.reason == "eligible"
