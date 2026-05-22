"""Tests for ``gate_stale_live_state`` — the in-game loop's stale-feed
guard (task #830).

Behaviour under test:
  * ``is_live=False`` → no-op (PASS), no DB lookup attempted.
  * ``is_live=True`` + fresh sportradar_live_state row → PASS.
  * ``is_live=True`` + stale row (older than per-league budget) → FAIL.
  * ``is_live=True`` + missing row → FAIL-CLOSED (the gate cannot
    confirm the feed is fresh so it must reject).
  * Registered in the GATES tuple BEFORE ``kelly_fraction`` so the
    per-trade Kelly cap applies to the halved (live-multiplier) size.
"""
from __future__ import annotations

import asyncio
from typing import Any

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


def _ctx(*, is_live: bool, league: str = "NBA",
         game_id: str = "nba-game-1") -> _gates.RiskContext:
    return _gates.RiskContext(
        market_id="KX-NBA-X",
        side="YES",
        vertical="sports",
        league=league,
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
        intended_quantity=100,
        intended_notional_usd=55.0,
        bankroll_usd=1_000.0,
        open_positions_count=0,
        open_positions_notional_usd=0.0,
        open_positions_per_vertical_usd={},
        operator_overrides={},
        market_cooldown_until=None,
        home_team="BOS",
        away_team="LAL",
        is_live=is_live,
        sportradar_game_id=game_id,
    )


class _FakeDB:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row
        self.calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.calls.append((query, args))
        return self.row


def test_pre_game_skips_db_lookup(monkeypatch):
    fake = _FakeDB(row=None)
    monkeypatch.setattr(_gates, "db", fake)
    ctx = _ctx(is_live=False)
    result = _run(_gates.gate_stale_live_state(ctx))
    assert result.passed is True
    assert fake.calls == [], (
        "gate must not touch the DB when ctx.is_live=False"
    )


def test_fresh_live_state_passes(monkeypatch):
    fake = _FakeDB(row={"age_seconds": 5.0, "status": "inprogress"})
    monkeypatch.setattr(_gates, "db", fake)
    ctx = _ctx(is_live=True, league="NBA")
    result = _run(_gates.gate_stale_live_state(ctx))
    assert result.passed is True
    assert len(fake.calls) == 1


def test_stale_live_state_rejects(monkeypatch):
    # NBA budget is small (< 120s) so 9999s old definitely trips.
    fake = _FakeDB(row={"age_seconds": 9999.0, "status": "inprogress"})
    monkeypatch.setattr(_gates, "db", fake)
    ctx = _ctx(is_live=True, league="NBA")
    result = _run(_gates.gate_stale_live_state(ctx))
    assert result.passed is False
    assert "stale" in (result.reason or "").lower()


def test_missing_row_fail_closed(monkeypatch):
    fake = _FakeDB(row=None)
    monkeypatch.setattr(_gates, "db", fake)
    ctx = _ctx(is_live=True, league="NBA")
    result = _run(_gates.gate_stale_live_state(ctx))
    assert result.passed is False, (
        "missing sportradar_live_state row must fail-closed: the gate "
        "cannot confirm the feed is fresh and live trades must not "
        "ship on an unverified state"
    )


def test_missing_sportradar_game_id_fail_closed(monkeypatch):
    # Architect-flagged regression: a live candidate that arrives
    # without a resolved sportradar_game_id must NOT pass on the back
    # of any unrelated fresh row in the same league.  The gate must
    # short-circuit to a rejection without even consulting the DB.
    fake = _FakeDB(row={"age_seconds": 1.0, "status": "inprogress"})
    monkeypatch.setattr(_gates, "db", fake)
    ctx = _ctx(is_live=True, league="NBA", game_id="")  # type: ignore[arg-type]
    # _ctx sets sportradar_game_id from `game_id` arg; force empty so
    # the gate sees a candidate with no resolvable game.
    ctx.sportradar_game_id = None
    result = _run(_gates.gate_stale_live_state(ctx))
    assert result.passed is False
    assert "sportradar_game_id" in (result.reason or "")
    assert fake.calls == [], (
        "fail-closed branch must short-circuit without a DB lookup so "
        "an unrelated fresh row can't accidentally pass the gate"
    )


def test_registered_before_kelly_fraction():
    names = [name for name, _fn in _gates.GATES]
    assert "stale_live_state" in names
    assert "live_kelly_multiplier" in names
    assert "kelly_fraction" in names
    i_stale = names.index("stale_live_state")
    i_mult = names.index("live_kelly_multiplier")
    i_kelly = names.index("kelly_fraction")
    assert i_stale < i_kelly
    assert i_mult < i_kelly, (
        "live_kelly_multiplier MUST run before kelly_fraction so the "
        "Kelly cap applies to the halved size, not the original"
    )
