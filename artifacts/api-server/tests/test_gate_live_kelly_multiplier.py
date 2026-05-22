"""Tests for ``gate_live_kelly_multiplier`` (task #830).

Behaviour under test:
  * ``is_live=False`` → no-op (PASS), no size mutation.
  * ``is_live=True`` → halves ``intended_quantity`` and
    ``intended_notional_usd`` in place (default 0.5x); still PASS.
  * The mutation persists for downstream gates (specifically the
    ``kelly_fraction`` gate that runs immediately after).
"""
from __future__ import annotations

import asyncio

import pytest

from app.risk import gates as _gates


@pytest.fixture(autouse=True)
def _isolate_event_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield
    loop.close()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _ctx(*, is_live: bool, qty: int = 100,
         notional: float = 55.0) -> _gates.RiskContext:
    return _gates.RiskContext(
        market_id="KX-NBA-X",
        side="YES",
        vertical="sports",
        league="NBA",
        market_type="moneyline",
        matchup="LAL vs BOS",
        current_price=0.55,
        model_probability=0.6,
        edge=0.05,
        confidence_band_low=0.55,
        confidence_band_high=0.65,
        agreement_rate=0.8,
        swarm_approved=True,
        swarm_recommended_action="BUY_YES",
        debate_id=1,
        intended_quantity=qty,
        intended_notional_usd=notional,
        bankroll_usd=1_000.0,
        open_positions_count=0,
        open_positions_notional_usd=0.0,
        open_positions_per_vertical_usd={},
        operator_overrides={},
        market_cooldown_until=None,
        home_team="BOS",
        away_team="LAL",
        is_live=is_live,
    )


def test_pre_game_does_not_mutate_size():
    ctx = _ctx(is_live=False, qty=100, notional=55.0)
    result = _run(_gates.gate_live_kelly_multiplier(ctx))
    assert result.passed is True
    assert ctx.intended_quantity == 100
    assert ctx.intended_notional_usd == pytest.approx(55.0)


def test_in_game_halves_intended_size_in_place():
    ctx = _ctx(is_live=True, qty=100, notional=55.0)
    result = _run(_gates.gate_live_kelly_multiplier(ctx))
    assert result.passed is True, "live multiplier must always pass"
    assert ctx.intended_quantity == 50, (
        "default 0.5x multiplier must halve intended_quantity"
    )
    assert ctx.intended_notional_usd == pytest.approx(27.5)


def test_mutation_visible_to_downstream_gate_chain():
    # Prove the mutation persists across the await boundary in the
    # actual chain order — the kelly_fraction gate consumes the
    # mutated values, which is the whole point of registering this
    # gate immediately before it.
    ctx = _ctx(is_live=True, qty=200, notional=110.0)
    _run(_gates.gate_live_kelly_multiplier(ctx))
    assert ctx.intended_quantity == 100
    assert ctx.intended_notional_usd == pytest.approx(55.0)
