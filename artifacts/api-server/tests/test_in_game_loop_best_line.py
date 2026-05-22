"""In-game loop hook for the cross-book best-line gate (task #835).

The live loop must run ``gate_best_line`` against every market it
re-scores so the operator catches the short-lived cross-book deltas
that open mid-game.  These tests stub the gate so we can assert the
loop fires it for each live-eligible row and aggregates the decision
counters onto ``InGameCycleResult``.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.learning import in_game_loop as _loop


@pytest.fixture(autouse=True)
def _isolate_event_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _loop._reset_state_for_tests()
    yield
    loop.close()
    _loop._reset_state_for_tests()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _FakeDB:
    def __init__(self, rows: list[dict[str, Any]], flipped: int = 0) -> None:
        self.rows = rows
        self.flipped = flipped

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "live_loop_enabled_" in args[0]:
            return {"value": "true"}
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "sportradar_live_state" in query:
            return self.rows
        return []

    async def execute(self, query: str, *args: Any) -> str:
        if "UPDATE predictions" in query:
            return f"UPDATE {self.flipped}"
        return "OK"


def test_loop_fires_best_line_check_per_live_row(monkeypatch):
    rows = [
        {
            "market_id": "KX-NBA-A", "league": "NBA",
            "game_id": "g1", "game_time": None, "is_live": False,
            "model_yes_prob": 0.6, "kalshi_price": 0.55,
            "matchup": "LAL vs BOS",
            "home_team": "Boston Celtics",
            "away_team": "Los Angeles Lakers",
            "status": "inprogress", "state_age_seconds": 5.0,
        },
        {
            "market_id": "KX-NBA-B", "league": "NBA",
            "game_id": "g2", "game_time": None, "is_live": True,
            "model_yes_prob": 0.58, "kalshi_price": 0.60,
            "matchup": "GSW vs MIA",
            "home_team": "Miami Heat",
            "away_team": "Golden State Warriors",
            "status": "inprogress", "state_age_seconds": 5.0,
        },
        # Skipped — missing home_team makes the gate ctx unbuildable.
        {
            "market_id": "KX-NBA-C", "league": "NBA",
            "game_id": "g3", "game_time": None, "is_live": True,
            "model_yes_prob": 0.5, "kalshi_price": 0.5,
            "matchup": None,
            "home_team": None,
            "away_team": None,
            "status": "inprogress", "state_age_seconds": 5.0,
        },
    ]
    monkeypatch.setattr(_loop, "db", _FakeDB(rows=rows, flipped=1))

    seen: list[str] = []

    async def _fake_gate(ctx):
        seen.append(ctx.market_id)
        # Mimic gate_best_line's return shape.
        class _R:
            details = {"decision": "block_traditional_better"}
        if ctx.market_id == "KX-NBA-A":
            _R.details = {"decision": "reroute_polymarket"}
        return _R()

    # Patch the symbol the loop imports lazily inside the helper.
    from app.risk import gates as _gates_mod
    monkeypatch.setattr(_gates_mod, "gate_best_line", _fake_gate)

    result = _run(_loop.run_in_game_cycle("nba"))
    assert result.enabled is True
    assert result.candidates_live == 3
    # The first two rows had complete data; the third was skipped.
    assert set(seen) == {"KX-NBA-A", "KX-NBA-B"}
    assert result.best_line_checks == 2
    assert result.best_line_reroutes == 1
    assert result.best_line_blocks == 1


def test_best_line_runs_on_every_rescore_even_when_tagging_dedupes(monkeypatch):
    """Same-minute duplicate rows still get a fresh best-line check.

    The minute-bucket idempotency cache only suppresses the dashboard-
    tile UPDATE — the cross-book best-line gate has its own 30s
    freshness window and (kind, market_id) alert dedupe, so re-running
    it every cycle is the entire point of the live loop.  A previous
    version of this code skipped best-line on duplicate-tag rows,
    which silently disabled the gate for any market re-evaluated more
    than once per minute.  This test guards against that regression.
    """
    rows = [{
        "market_id": "KX-NBA-A", "league": "NBA",
        "game_id": "g1", "game_time": None, "is_live": False,
        "model_yes_prob": 0.6, "kalshi_price": 0.55,
        "matchup": "LAL vs BOS",
        "home_team": "Boston Celtics",
        "away_team": "Los Angeles Lakers",
        "status": "inprogress", "state_age_seconds": 5.0,
    }]
    # Cadence basically nil so the second run re-enters the work path
    # but still inside the same minute bucket — exactly the in-game
    # 30s re-score window the task describes.
    fake = _FakeDB(rows=rows, flipped=1)
    monkeypatch.setattr(_loop, "db", fake)

    # Override cadence to a tiny value so the second call doesn't
    # short-circuit on the cadence floor.
    original_cadence = _loop.cadence_seconds

    async def _tiny_cadence(_sport):
        return 0.01

    monkeypatch.setattr(_loop, "cadence_seconds", _tiny_cadence)

    seen: list[str] = []

    async def _fake_gate(ctx):
        seen.append(ctx.market_id)
        class _R:
            details = {"decision": "pass"}
        return _R()

    from app.risk import gates as _gates_mod
    monkeypatch.setattr(_gates_mod, "gate_best_line", _fake_gate)

    r1 = _run(_loop.run_in_game_cycle("nba"))
    assert r1.best_line_checks == 1
    assert r1.candidates_tagged == 1

    import time as _t
    _t.sleep(0.05)
    fake.flipped = 0  # row is already live now → tag is a no-op

    r2 = _run(_loop.run_in_game_cycle("nba"))
    assert r2.candidates_skipped_duplicate == 1, (
        "second run inside same minute must mark the tagging path as "
        "duplicate"
    )
    assert r2.best_line_checks == 1, (
        "best-line check MUST run on every re-score — decoupled from "
        "the minute-bucket tagging dedupe"
    )
    assert seen == ["KX-NBA-A", "KX-NBA-A"], (
        "the gate must be invoked once per cycle for the live market"
    )

    # Reference original_cadence so the import isn't flagged unused
    # in linters that track local assignments.
    assert callable(original_cadence)


def test_loop_marks_ctx_is_live_true(monkeypatch):
    rows = [{
        "market_id": "KX-NBA-A", "league": "NBA",
        "game_id": "g1", "game_time": None, "is_live": False,
        "model_yes_prob": 0.6, "kalshi_price": 0.55,
        "matchup": "LAL vs BOS",
        "home_team": "Boston Celtics",
        "away_team": "Los Angeles Lakers",
        "status": "inprogress", "state_age_seconds": 5.0,
    }]
    monkeypatch.setattr(_loop, "db", _FakeDB(rows=rows, flipped=1))

    captured: dict[str, Any] = {}

    async def _fake_gate(ctx):
        captured["is_live"] = ctx.is_live
        captured["sportradar_game_id"] = ctx.sportradar_game_id
        captured["vertical"] = ctx.vertical
        captured["side"] = ctx.side
        class _R:
            details = {"decision": "pass"}
        return _R()

    from app.risk import gates as _gates_mod
    monkeypatch.setattr(_gates_mod, "gate_best_line", _fake_gate)

    _run(_loop.run_in_game_cycle("nba"))
    assert captured.get("is_live") is True, (
        "live-loop ctx must carry is_live=True so gate_best_line "
        "picks the 30s freshness window"
    )
    assert captured.get("sportradar_game_id") == "g1"
    assert captured.get("vertical") == "sports"
    assert captured.get("side") == "YES"
