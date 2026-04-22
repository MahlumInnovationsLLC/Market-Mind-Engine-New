"""Tests for the elections trading vertical (task #201)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import verticals as vertical_registry
from app.elections import promotion as promotion_mod
from app.elections.arb import detect_arb
from app.elections.model import poll_of_polls_probability
from app.elections.polls import PollRow
from app.elections.tickers import (
    InvalidElectionTicker,
    SUPPORTED_OFFICES,
    parse_election_ticker,
)
from app.risk.gates import RiskContext, gate_kelly_fraction
from app.swarm import profiles as swarm_profiles


# ---------------------------------------------------------------------------
# Ticker parser
# ---------------------------------------------------------------------------


def test_parse_us_pres_ticker():
    t = parse_election_ticker("KXPRES-28-DEM")
    assert t.office == "PRES"
    assert t.candidate == "DEM"
    assert t.scope == "US_PRES_2028"


def test_parse_state_senate_with_region():
    t = parse_election_ticker("KXSEN-26OH-VANCE")
    assert t.office == "SEN"
    assert t.region == "OH"
    assert t.scope == "US_SEN_OH_2026"
    assert t.candidate == "VANCE"


def test_parse_governor_with_state():
    t = parse_election_ticker("KXGOV-26CA-DEM")
    assert t.office == "GOV"
    assert t.region == "CA"
    assert t.scope == "US_GOV_CA_2026"


def test_parse_house_district():
    t = parse_election_ticker("KXHOUSE-26NY03-DEM")
    assert t.office == "HOUSE"
    assert t.region == "NY_03"


def test_parse_foreign_election():
    t = parse_election_ticker("KXFRPRES-27APR-LEPEN")
    assert t.office == "FRPRES"
    assert "FRPRES" in t.scope


def test_parse_supported_offices_present():
    for office in ("PRES", "GOV", "SEN", "HOUSE"):
        assert office in SUPPORTED_OFFICES


def test_parse_rejects_blank():
    with pytest.raises(InvalidElectionTicker):
        parse_election_ticker("")


def test_parse_rejects_unknown_office():
    with pytest.raises(InvalidElectionTicker):
        parse_election_ticker("KXMAYOR-26NY-DEM")


def test_parse_case_insensitive():
    t = parse_election_ticker("kxpres-28-trump")
    assert t.office == "PRES"
    assert t.candidate == "TRUMP"


# ---------------------------------------------------------------------------
# Vertical registration
# ---------------------------------------------------------------------------


def test_vertical_registered_with_kelly_005():
    spec = vertical_registry.get_spec("elections")
    assert spec is not None
    assert spec.predictor_hook == "elections_poll_of_polls"
    assert spec.kelly_multiplier == pytest.approx(0.05)
    assert "ELECTIONS" in spec.leagues
    assert "KXPRES" in spec.ticker_prefixes
    assert "KXSEN" in spec.ticker_prefixes
    assert "KXFRPRES" in spec.ticker_prefixes


def test_calibrators_includes_elections():
    from app.calibrators import SUPPORTED_LEAGUES
    assert "ELECTIONS" in SUPPORTED_LEAGUES


# ---------------------------------------------------------------------------
# Persona profile (5 baseline + 3 layered = 8)
# ---------------------------------------------------------------------------


def test_elections_profile_has_eight_personas_including_layered():
    profile = swarm_profiles.get_profile("elections")
    assert profile is not None
    ids = {p.id for p in profile.personas}
    # Five baseline (from #194)
    for baseline in (
        "poll_aggregator", "fundamentals_modeler",
        "early_vote_pulse", "ground_game", "risk_guardian",
    ):
        assert baseline in ids, f"missing baseline persona {baseline}"
    # Three layered (task #201 spec)
    for layered in (
        "pollster_aggregator",
        "historical_correction",
        "prediction_market_arbitrage",
    ):
        assert layered in ids, f"missing layered persona {layered}"
    assert len(profile.personas) == 8


# ---------------------------------------------------------------------------
# Poll-of-polls model
# ---------------------------------------------------------------------------


def _row(
    *,
    candidate: str = "DEM",
    kind: str = "poll",
    pct: float | None = 50.0,
    n: int | None = 1000,
    days_ago: float = 1.0,
    weight: float = 1.0,
    source: str = "rcp_polling_rss",
    scope: str = "US_PRES_2028",
) -> PollRow:
    now = datetime.now(timezone.utc)
    return PollRow(
        source=source,
        signal_id=f"{source}/{candidate}/{days_ago}",
        scope=scope,
        candidate=candidate,
        kind=kind,
        observed_at=now - timedelta(days=days_ago),
        sample_size=n,
        support_pct=pct,
        weight=weight,
        payload={},
    )


def test_no_polls_returns_prior_05():
    t = parse_election_ticker("KXPRES-28-DEM")
    res = poll_of_polls_probability(t, [])
    assert res.probability == pytest.approx(0.5)
    assert res.contributing_polls == 0


def test_more_supportive_polls_pushes_probability_higher():
    t = parse_election_ticker("KXPRES-28-DEM")
    low = poll_of_polls_probability(t, [_row(pct=51.0)])
    high = poll_of_polls_probability(t, [_row(pct=58.0)])
    assert high.probability > low.probability
    assert low.probability > 0.5  # 51% support => > 0.5


def test_clamps_into_band():
    t = parse_election_ticker("KXPRES-28-DEM")
    extreme = poll_of_polls_probability(t, [_row(pct=99.0)])
    assert 0.05 <= extreme.probability <= 0.95


def test_state_bias_correction_applies_to_known_state():
    # Wisconsin polls historically over-state Democrats by ~2.5pp.
    # A DEM candidate with raw 50% support in WI should see a
    # corrected support strictly below 50%.
    t = parse_election_ticker("KXSEN-26WI-DEM")
    row = _row(scope=t.scope, pct=50.0)
    res = poll_of_polls_probability(t, [row])
    assert res.raw_support_pct == pytest.approx(50.0)
    assert res.weighted_support_pct < 50.0
    assert res.state_bias_pp < 0.0


def test_polymarket_xref_pulls_estimate_toward_market():
    # No polls but an explicit Polymarket cross-ref at 70% YES.
    t = parse_election_ticker("KXPRES-28-DEM")
    xref = _row(kind="polymarket_xref", candidate="DEM", pct=70.0,
                source="polymarket_xref", n=None)
    res = poll_of_polls_probability(t, [xref])
    assert res.probability > 0.5
    assert res.polymarket_implied_prob is not None
    assert res.polymarket_implied_prob == pytest.approx(0.70, abs=0.05)


def test_recency_decay_outweighs_old_polls():
    t = parse_election_ticker("KXPRES-28-DEM")
    fresh = _row(pct=58.0, days_ago=1.0, candidate="DEM")
    stale = _row(pct=42.0, days_ago=120.0, candidate="DEM")
    res = poll_of_polls_probability(t, [fresh, stale])
    # Fresh 58% should dominate stale 42% under 14-day half-life
    assert res.weighted_support_pct > 50.0


# ---------------------------------------------------------------------------
# Arbitrage detector
# ---------------------------------------------------------------------------


def test_arb_no_arb_when_within_threshold():
    arb = detect_arb(
        market_id="KXPRES-28-DEM",
        candidate="DEM",
        kalshi_price=0.50,
        polymarket_price=0.52,
        threshold_pp=4.0,
    )
    assert arb.arb is False
    assert arb.spread_pp == pytest.approx(2.0)
    assert arb.direction == "none"


def test_arb_kalshi_buy_when_kalshi_cheaper():
    arb = detect_arb(
        market_id="KXPRES-28-DEM",
        candidate="DEM",
        kalshi_price=0.45,
        polymarket_price=0.55,
        threshold_pp=4.0,
    )
    assert arb.arb is True
    assert arb.direction == "kalshi_buy"


def test_arb_polymarket_buy_when_polymarket_cheaper():
    arb = detect_arb(
        market_id="KXPRES-28-DEM",
        candidate="DEM",
        kalshi_price=0.60,
        polymarket_price=0.50,
        threshold_pp=4.0,
    )
    assert arb.arb is True
    assert arb.direction == "polymarket_buy"


def test_arb_missing_leg_disables_signal():
    arb = detect_arb(
        market_id="KXPRES-28-DEM",
        candidate="DEM",
        kalshi_price=None,
        polymarket_price=0.55,
    )
    assert arb.arb is False
    assert arb.spread_pp is None
    assert arb.direction == "none"


# ---------------------------------------------------------------------------
# Per-vertical Kelly multiplier override (gate)
# ---------------------------------------------------------------------------


def _ctx(**overrides) -> RiskContext:
    base = dict(
        market_id="KXPRES-28-DEM",
        side="YES",
        league="ELECTIONS",
        vertical="elections",
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


def test_kelly_default_uses_spec_005():
    # No per-vertical override; gate falls through to spec=0.05.
    res = asyncio.run(gate_kelly_fraction(_ctx(intended_notional_usd=200.0)))
    assert res.passed is True or res.details.get("kelly_multiplier") == pytest.approx(0.05)


def test_kelly_per_vertical_override_beats_spec_default():
    res = asyncio.run(gate_kelly_fraction(_ctx(
        intended_notional_usd=200.0,
        operator_overrides={"kelly_multiplier_elections": 0.20},
    )))
    assert res.details.get("kelly_multiplier") == pytest.approx(0.20)


# ---------------------------------------------------------------------------
# Promotion thresholds
# ---------------------------------------------------------------------------


def test_promotion_constants_match_spec():
    assert promotion_mod.SETTLED_THRESHOLD == 30
    assert promotion_mod.BRIER_THRESHOLD == 0.20
    assert promotion_mod.DEFAULT_MULTIPLIER == pytest.approx(0.05)
    assert promotion_mod.PROMOTED_MULTIPLIER == pytest.approx(0.08)


def test_promotion_ineligible_with_few_settled(monkeypatch):
    async def fake_current() -> float:
        return promotion_mod.DEFAULT_MULTIPLIER

    async def fake_fetchrow(_q, *_a):
        return {"n": 5, "brier": 0.10}

    monkeypatch.setattr(promotion_mod, "_read_current_multiplier", fake_current)
    from app import db as _db
    monkeypatch.setattr(_db, "fetchrow", fake_fetchrow)
    status = asyncio.run(promotion_mod.evaluate_promotion())
    assert status.eligible is False
    assert status.settled_count == 5


def test_promotion_eligible_when_thresholds_met(monkeypatch):
    async def fake_current() -> float:
        return promotion_mod.DEFAULT_MULTIPLIER

    async def fake_fetchrow(_q, *_a):
        return {"n": 50, "brier": 0.15}

    monkeypatch.setattr(promotion_mod, "_read_current_multiplier", fake_current)
    from app import db as _db
    monkeypatch.setattr(_db, "fetchrow", fake_fetchrow)
    status = asyncio.run(promotion_mod.evaluate_promotion())
    assert status.eligible is True
    assert status.brier == pytest.approx(0.15)


def test_party_label_ticker_matches_named_candidate_polls():
    """Architect-flagged regression: a `KXPRES-28-DEM` ticker must
    aggregate poll rows whose candidate field is a known Democratic
    person name (e.g. HARRIS, NEWSOM), not silently drop them."""
    ticker = parse_election_ticker("KXPRES-28-DEM")
    now = datetime.now(timezone.utc)
    polls = [
        PollRow(source="rcp", signal_id="a", scope="US_PRES_2028",
                candidate="HARRIS", kind="poll", observed_at=now,
                sample_size=1000, support_pct=51.0, weight=1.0, payload={}),
        PollRow(source="538", signal_id="b", scope="US_PRES_2028",
                candidate="NEWSOM", kind="poll", observed_at=now,
                sample_size=900, support_pct=53.0, weight=1.0, payload={}),
    ]
    res = poll_of_polls_probability(ticker, polls, now=now)
    assert res.contributing_polls == 2
    assert res.raw_support_pct > 50.0


def test_named_rep_ticker_matches_party_label_polls():
    """Architect-flagged regression: a `TRUMP` ticker must accept REP-
    labelled rows from the Cook ratings poller."""
    ticker = parse_election_ticker("KXPRES-28-TRUMP")
    now = datetime.now(timezone.utc)
    polls = [
        PollRow(source="cook", signal_id="r1", scope="US_PRES_2028",
                candidate="REP", kind="rating", observed_at=now,
                sample_size=None, support_pct=46.0, weight=1.0, payload={}),
    ]
    res = poll_of_polls_probability(ticker, polls, now=now)
    assert res.contributing_polls == 1


def test_state_bias_inverts_for_named_rep_candidate():
    """Bias inversion must trigger for any REP-mapped candidate name,
    not only the small hardcoded set."""
    from app.elections.model import _bias_for_region
    # WI bias is -2.5 pp on the Dem side -> +2.5 for REP.
    assert _bias_for_region("WI", "DEM") == -2.5
    assert _bias_for_region("WI", "REP") == 2.5
    assert _bias_for_region("WI", "TRUMP") == 2.5
    assert _bias_for_region("WI", "RAMASWAMY") == 2.5
    assert _bias_for_region("WI", "HARRIS") == -2.5  # named Dem


def test_polymarket_xref_payload_envelope_handled(monkeypatch):
    """Architect-flagged regression: poll_polymarket_xref must accept
    the actual CLOB payload shape (`{data: [...]}`) and not crash on
    the real `list_markets(limit=...)` signature."""
    from app.feeds import election_polls as ep

    class _FakePoly:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *exc):
            return False
        async def list_markets(self, *, limit: int = 50,
                               next_cursor: str | None = None):
            assert "category" not in (limit, next_cursor)  # signature
            return {
                "data": [
                    {
                        "condition_id": "0xabc",
                        "question": "Will the Democratic nominee win the 2028 US presidential election?",
                        "scope": "US_PRES_2028",
                        "outcomes": [
                            {"name": "DEM", "token_id": "t1", "price": "0.52"},
                            {"name": "REP", "token_id": "t2", "price": "0.48"},
                        ],
                    },
                ],
                "next_cursor": None,
            }

    captured = []

    async def _fake_upsert(rows):
        rows = list(rows)
        captured.extend(rows)
        return len(rows)

    monkeypatch.setattr(ep, "upsert_polls", _fake_upsert)
    from app.polymarket import client as _poly_mod
    monkeypatch.setattr(_poly_mod, "PolymarketClient", _FakePoly)

    n = asyncio.run(ep.poll_polymarket_xref())
    assert n == 2, f"expected 2 outcomes parsed, got {n}; rows={captured}"
    assert {r["candidate"] for r in captured} == {"DEM", "REP"}
    assert all(r["kind"] == ep.KIND_POLYMARKET_XREF for r in captured)


def test_promotion_ineligible_when_already_promoted(monkeypatch):
    async def fake_current() -> float:
        return promotion_mod.PROMOTED_MULTIPLIER

    async def fake_fetchrow(_q, *_a):
        return {"n": 100, "brier": 0.10}

    monkeypatch.setattr(promotion_mod, "_read_current_multiplier", fake_current)
    from app import db as _db
    monkeypatch.setattr(_db, "fetchrow", fake_fetchrow)
    status = asyncio.run(promotion_mod.evaluate_promotion())
    assert status.eligible is False
    assert "already" in status.reason
