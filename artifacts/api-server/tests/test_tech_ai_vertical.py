"""Tests for the tech / AI trading vertical (task #199)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app import verticals as vertical_registry
from app.risk.gates import GATES, RiskContext, gate_kelly_fraction
from app.swarm import profiles as swarm_profiles
from app.tech_ai import promotion as promotion_mod
from app.tech_ai.evidence import EvidenceRow
from app.tech_ai.model import bayesian_probability
from app.tech_ai.tickers import (
    InvalidTechAITicker,
    SUPPORTED_SYMBOLS,
    TechAITicker,
    parse_tech_ai_ticker,
)


# ---------------------------------------------------------------------------
# Ticker parser
# ---------------------------------------------------------------------------


def test_parse_llm_release_ticker():
    t = parse_tech_ai_ticker("KXLLM-26JUN-T1")
    assert t.symbol == "LLM"
    assert t.direction == "above"


def test_parse_below_direction():
    t = parse_tech_ai_ticker("KXNVDA-26AUG-B1000")
    assert t.symbol == "NVDA"
    assert t.direction == "below"
    assert t.threshold == 1000.0


def test_parse_supported_symbols_cover_ten():
    assert len(SUPPORTED_SYMBOLS) == 10


def test_parse_rejects_blank():
    with pytest.raises(InvalidTechAITicker):
        parse_tech_ai_ticker("")


def test_parse_rejects_unknown_symbol():
    with pytest.raises(InvalidTechAITicker):
        parse_tech_ai_ticker("KXFOO-26JUN-T1")


# ---------------------------------------------------------------------------
# Vertical registration
# ---------------------------------------------------------------------------


def test_vertical_registered_with_kelly_006():
    spec = vertical_registry.get_spec("tech-ai")
    assert spec is not None
    assert spec.predictor_hook == "tech_ai_evidence"
    assert spec.kelly_multiplier == pytest.approx(0.06)
    assert "TECH_AI" in spec.leagues
    assert "KXLLM" in spec.ticker_prefixes


def test_calibrators_includes_tech_ai():
    from app.calibrators import SUPPORTED_LEAGUES
    assert "TECH_AI" in SUPPORTED_LEAGUES


# ---------------------------------------------------------------------------
# Persona profile (5 baseline + 3 layered = 8)
# ---------------------------------------------------------------------------


def test_tech_ai_profile_has_eight_personas_including_layered():
    profile = swarm_profiles.PROFILES["tech-ai"]
    ids = [p.id for p in profile.personas]
    assert len(ids) == 8, ids
    for required in ("industry_calendar", "hype_contrarian", "bench_analyst"):
        assert required in ids, f"missing layered persona {required!r}"


# ---------------------------------------------------------------------------
# Evidence resolver (DB-monkey-patched)
# ---------------------------------------------------------------------------


def test_evidence_resolver_scopes_by_symbol_and_window(monkeypatch):
    from app.tech_ai import evidence as ev_mod

    captured: dict[str, Any] = {}

    async def fake_fetch(query, *args):
        captured["query"] = query
        captured["args"] = args
        now = datetime.now(timezone.utc)
        return [
            {
                "source": "github_releases",
                "signal_id": "openai/whisper@v3",
                "symbol": args[0],
                "kind": "release",
                "observed_at": now - timedelta(days=2),
                "weight": 1.0,
                "payload": {"repo": "openai/whisper"},
            }
        ]

    monkeypatch.setattr(ev_mod.db, "fetch", fake_fetch)
    ticker = parse_tech_ai_ticker("KXGPT-26JUN-T1")
    rows = asyncio.run(ev_mod.resolve_evidence(ticker, lookback_days=14))
    assert len(rows) == 1
    assert rows[0].symbol == "GPT"
    assert captured["args"][0] == "GPT"
    cutoff = captured["args"][1]
    assert isinstance(cutoff, datetime)


# ---------------------------------------------------------------------------
# Bayesian model
# ---------------------------------------------------------------------------


def _ticker_above() -> TechAITicker:
    return parse_tech_ai_ticker("KXGPT-26JUN-T1")


def test_bayesian_prior_with_no_evidence():
    res = bayesian_probability(_ticker_above(), [])
    assert res.probability == pytest.approx(0.5)
    assert res.contributing_rows == 0


def test_bayesian_monotone_in_supportive_weight():
    now = datetime.now(timezone.utc)
    light = [EvidenceRow("github_releases", "a", "GPT", "release", now, 0.2, {})]
    heavy = [
        EvidenceRow("github_releases", "a", "GPT", "release", now, 1.0, {}),
        EvidenceRow("hf_trending", "b", "GPT", "trending", now, 1.0, {}),
    ]
    p_light = bayesian_probability(_ticker_above(), light, now=now).probability
    p_heavy = bayesian_probability(_ticker_above(), heavy, now=now).probability
    assert p_heavy > p_light > 0.5


def test_bayesian_clamped_at_upper_bound():
    now = datetime.now(timezone.utc)
    overwhelming = [
        EvidenceRow("github_releases", f"r{i}", "GPT", "release", now, 1.0, {})
        for i in range(50)
    ]
    res = bayesian_probability(_ticker_above(), overwhelming, now=now)
    assert res.probability <= 0.95
    assert res.probability >= 0.5


def test_bayesian_below_direction_inverts_evidence():
    """A 'below threshold' market resolves YES when the symbol is
    inactive — supportive evidence should DROP YES, not raise it."""
    now = datetime.now(timezone.utc)
    rows = [EvidenceRow("github_releases", "a", "NVDA", "release", now, 1.0, {})]
    below_t = parse_tech_ai_ticker("KXNVDA-26AUG-B1000")
    p = bayesian_probability(below_t, rows, now=now).probability
    assert p < 0.5


# ---------------------------------------------------------------------------
# Per-vertical Kelly multiplier override (gate)
# ---------------------------------------------------------------------------


def _tech_ctx(**overrides) -> RiskContext:
    base: dict[str, Any] = dict(
        market_id="KXGPT-26JUN-T1",
        side="YES",
        vertical="tech-ai",
        league="TECH_AI",
        market_type="binary",
        matchup="GPT",
        current_price=0.50,
        model_probability=0.65,
        edge=0.15,
        confidence_band_low=0.60,
        confidence_band_high=0.70,
        agreement_rate=0.80,
        swarm_approved=True,
        swarm_recommended_action="BUY_YES",
        debate_id=1,
        intended_quantity=1,
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


def test_per_vertical_kelly_override_beats_spec_default():
    """An override of `kelly_multiplier_tech_ai` should raise the cap
    above the default 0.06 even though no global `kelly_multiplier`
    operator override is set."""
    # With spec default (0.06), Kelly cap is small enough to reject
    # a $1500 intended notional: bankroll=10_000, kelly=0.30,
    # fraction_cap=0.25, multiplier=0.06 -> max=$45.  A $50 ticket
    # is just over the cap.
    ctx_default = _tech_ctx(intended_notional_usd=50.0)
    res_default = asyncio.run(gate_kelly_fraction(ctx_default))
    assert not res_default.passed
    assert res_default.details["kelly_multiplier"] == pytest.approx(0.06)

    # With per-vertical override 0.10, the same ticket fits.
    ctx_promoted = _tech_ctx(
        intended_notional_usd=50.0,
        operator_overrides={"kelly_multiplier_tech_ai": 0.10},
    )
    res_promoted = asyncio.run(gate_kelly_fraction(ctx_promoted))
    assert res_promoted.details["kelly_multiplier"] == pytest.approx(0.10)


def test_twelve_gate_invariant_unchanged():
    assert len(GATES) == 12


# ---------------------------------------------------------------------------
# End-to-end synthetic candidate -> predictor cold path
# ---------------------------------------------------------------------------


def test_end_to_end_predictor_cold_path(monkeypatch):
    """Predictor returns a Prediction even when the evidence cache is
    empty (cold path) — falls through to the prior probability."""
    from app.tech_ai import predictor as tp
    from app.tech_ai import evidence as ev_mod

    async def empty_evidence(ticker, *, lookback_days=30, limit=50):
        return []

    monkeypatch.setattr(tp, "resolve_evidence", empty_evidence)
    pred = asyncio.run(tp.predict_tech_ai_binary("KXGPT-26JUN-T1"))
    assert pred.probability == pytest.approx(0.5)
    assert pred.model_name == "tech_ai_bayesian"
    assert pred.model_type == "BayesianLogOdds"
    assert "ticker" in pred.raw


# ---------------------------------------------------------------------------
# Promotion eligibility
# ---------------------------------------------------------------------------


def test_promotion_ineligible_when_no_settled_trades(monkeypatch):
    async def fake_fetchval(query, *args):
        return None  # no override row -> default 0.06

    async def fake_fetchrow(query, *args):
        return {"n": 0, "brier": None}

    monkeypatch.setattr(promotion_mod.db, "fetchval", fake_fetchval)
    monkeypatch.setattr(promotion_mod.db, "fetchrow", fake_fetchrow)
    status = asyncio.run(promotion_mod.evaluate_promotion())
    assert not status.eligible
    assert status.settled_count == 0
    assert status.current_multiplier == pytest.approx(0.06)
    assert "30 settled" in status.reason


def test_promotion_ineligible_when_brier_too_high(monkeypatch):
    async def fake_fetchval(query, *args):
        return None

    async def fake_fetchrow(query, *args):
        return {"n": 50, "brier": 0.25}

    monkeypatch.setattr(promotion_mod.db, "fetchval", fake_fetchval)
    monkeypatch.setattr(promotion_mod.db, "fetchrow", fake_fetchrow)
    status = asyncio.run(promotion_mod.evaluate_promotion())
    assert not status.eligible
    assert status.brier == pytest.approx(0.25)
    assert "Brier" in status.reason


def test_promotion_eligible_when_thresholds_met(monkeypatch):
    async def fake_fetchval(query, *args):
        return None

    async def fake_fetchrow(query, *args):
        return {"n": 30, "brier": 0.18}

    monkeypatch.setattr(promotion_mod.db, "fetchval", fake_fetchval)
    monkeypatch.setattr(promotion_mod.db, "fetchrow", fake_fetchrow)
    status = asyncio.run(promotion_mod.evaluate_promotion())
    assert status.eligible
    assert status.reason == "eligible"


def test_promotion_skipped_when_already_at_target(monkeypatch):
    async def fake_fetchval(query, *args):
        return "0.10"

    async def fake_fetchrow(query, *args):
        return {"n": 100, "brier": 0.10}

    monkeypatch.setattr(promotion_mod.db, "fetchval", fake_fetchval)
    monkeypatch.setattr(promotion_mod.db, "fetchrow", fake_fetchrow)
    status = asyncio.run(promotion_mod.evaluate_promotion())
    assert not status.eligible
    assert status.current_multiplier == pytest.approx(0.10)
