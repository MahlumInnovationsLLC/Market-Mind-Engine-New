"""3-way (soccer h/d/a) settlement tests.

Soccer matches resolve as one of three outcomes (home / draw / away),
but each Kalshi market is still a single YES/NO leg.  The settlement
pipeline must derive each leg's binary outcome from the actual match
result so that:

  * a draw correctly settles BOTH the home and away YES legs as NO
    rather than being treated as a "moneyline miss" against the YES
    side every time, and
  * the draw leg pays YES (1) without leaking that draw outcome onto
    the per-persona Brier on home/away legs.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.settlement import pipeline as _pipeline
from app.settlement.pipeline import (
    normalize_three_way_result,
    outcome_yes_for_three_way_leg,
    resolve_three_way_leg,
)


# ---------------------------------------------------------------------------
# Pure helpers — no DB, no async
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("home", "home"), ("HOME", "home"), ("h", "home"), ("1", "home"),
        ("home_win", "home"), ("Home Win", "home"),
        ("draw", "draw"), ("d", "draw"), ("tie", "draw"), ("X", "draw"),
        ("away", "away"), ("a", "away"), ("2", "away"),
        (None, None), ("", None), ("garbage", None),
    ],
)
def test_normalize_three_way_result(raw, expected):
    assert normalize_three_way_result(raw) == expected


def test_outcome_yes_for_three_way_leg_draw_settles_home_and_away_as_no():
    # The crux of the bug task #210 fixes: a draw must NOT score the
    # home / away legs as a YES win.
    assert outcome_yes_for_three_way_leg("draw", "home") == 0
    assert outcome_yes_for_three_way_leg("draw", "away") == 0
    # And the draw leg itself pays YES.
    assert outcome_yes_for_three_way_leg("draw", "draw") == 1


def test_outcome_yes_for_three_way_leg_home_win():
    assert outcome_yes_for_three_way_leg("home", "home") == 1
    assert outcome_yes_for_three_way_leg("home", "draw") == 0
    assert outcome_yes_for_three_way_leg("home", "away") == 0


def test_outcome_yes_for_three_way_leg_away_win():
    assert outcome_yes_for_three_way_leg("away", "home") == 0
    assert outcome_yes_for_three_way_leg("away", "draw") == 0
    assert outcome_yes_for_three_way_leg("away", "away") == 1


def test_outcome_yes_for_three_way_leg_rejects_garbage():
    with pytest.raises(ValueError):
        outcome_yes_for_three_way_leg("draw", "yes")
    with pytest.raises(ValueError):
        outcome_yes_for_three_way_leg("postponed", "home")


def test_resolve_three_way_leg_from_metadata_dict():
    assert resolve_three_way_leg({"metadata": {"three_way_leg": "home"}}) == "home"
    assert resolve_three_way_leg({"metadata": {"outcome_3way": "draw"}}) == "draw"
    assert resolve_three_way_leg({"metadata": {"leg": "Away"}}) == "away"


def test_resolve_three_way_leg_from_metadata_string_jsonb():
    # asyncpg returns jsonb columns as strings sometimes — make sure
    # we still parse them cleanly.
    assert (
        resolve_three_way_leg({"metadata": '{"three_way_leg": "draw"}'}) == "draw"
    )


def test_resolve_three_way_leg_falls_back_to_side():
    # When metadata is empty, side carrying a 3-way name is the
    # fallback (used by the cycle when the candidate's `side` is
    # already "home"/"draw"/"away").
    assert resolve_three_way_leg({"metadata": None, "side": "draw"}) == "draw"
    # A YES/NO side does NOT resolve to a 3-way leg.
    assert resolve_three_way_leg({"metadata": None, "side": "yes"}) is None
    assert resolve_three_way_leg({"metadata": {}, "side": None}) is None


# ---------------------------------------------------------------------------
# settle_market end-to-end with stubbed DB
# ---------------------------------------------------------------------------


def _stub_db(monkeypatch, trade_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Wire fake db.fetch / fetchrow / fetchval / execute and return a
    capture dict so tests can assert what got persisted."""
    captured: dict[str, Any] = {
        "trade_updates": [],
        "postmortems": [],
        "online_buffer": [],
    }

    async def _fake_fetch(query: str, *args: Any) -> list[dict[str, Any]]:
        if "FROM trades" in query and "settled_at IS NULL" in query:
            return trade_rows
        return []

    async def _fake_fetchrow(*_a: Any, **_kw: Any) -> dict[str, Any] | None:
        return None

    async def _fake_fetchval(*_a: Any, **_kw: Any) -> Any:
        return None

    async def _fake_execute(query: str, *args: Any) -> str:
        # Record the per-trade UPDATE so the test can assert which
        # outcome each trade was settled against.
        if "UPDATE trades" in query:
            captured["trade_updates"].append({"id": args[0], "pnl": args[1]})
        elif "INSERT INTO trade_postmortems" in query:
            # actual_outcome is the 8th positional arg per the INSERT
            # column order; details json is the 14th.
            captured["postmortems"].append({
                "trade_id": args[0],
                "actual_outcome": args[7],
                "details": args[13],
            })
        elif "INSERT INTO online_learning_buffer" in query:
            captured["online_buffer"].append({
                "trade_id": args[0],
                "actual_outcome": args[4],
            })
        return "OK"

    monkeypatch.setattr(_pipeline.db, "fetch", _fake_fetch)
    monkeypatch.setattr(_pipeline.db, "fetchrow", _fake_fetchrow)
    monkeypatch.setattr(_pipeline.db, "fetchval", _fake_fetchval)
    monkeypatch.setattr(_pipeline.db, "execute", _fake_execute)
    # Disable the push fan-out so we don't depend on the notification
    # subsystem here.
    from app.notifications import push as _push
    monkeypatch.setattr(_push, "push_settlement", lambda **_kw: None)
    return captured


def _soccer_trade(tid: int, leg: str, *, side: str = "yes",
                   model_prob: float = 0.4) -> dict[str, Any]:
    return {
        "id": tid, "trade_id": f"t{tid}",
        "market_id": f"MKT-EPL-{leg.upper()}",
        "vertical": "sports", "league": "EPL",
        "side": side, "action": "buy",
        "quantity": 10.0, "price": model_prob,
        "model_prob": model_prob, "model_edge": 0.05, "edge": 0.05,
        "exchange": "kalshi", "trading_mode": "live",
        "metadata": {"three_way_leg": leg},
    }


def test_draw_result_settles_home_leg_as_loss(monkeypatch):
    """Persona placed YES on the home leg; match ended in a draw.
    The trade must settle as a NO (outcome_yes=0) and the postmortem
    must record actual_outcome=0 — NOT inherit the binary outcome the
    caller may have passed (which a non-3-way-aware caller would set
    to 1 by mistake)."""
    rows = [_soccer_trade(1, "home", side="yes", model_prob=0.55)]
    cap = _stub_db(monkeypatch, rows)

    asyncio.run(
        _pipeline.settle_market(
            "MKT-EPL-HOME",
            outcome_yes=1,           # bogus binary fallback
            settle_price=0.0,
            result_3way="draw",
        )
    )
    # Trade settled at the per-leg outcome (NO, so cost = 10*0.55,
    # payout = 0 → pnl = -5.5).
    assert len(cap["trade_updates"]) == 1
    assert cap["trade_updates"][0]["pnl"] == pytest.approx(-5.5)
    # Postmortem records the per-leg outcome AND the 3-way context.
    assert len(cap["postmortems"]) == 1
    pm = cap["postmortems"][0]
    assert pm["actual_outcome"] == 0
    assert '"three_way_result": "draw"' in pm["details"]
    assert '"three_way_leg": "home"' in pm["details"]


def test_draw_result_settles_draw_leg_as_win(monkeypatch):
    """A persona that bought YES on the draw leg should be paid out
    when the match ends level — outcome_yes=1 even though the binary
    fallback the caller supplied was 0."""
    rows = [_soccer_trade(2, "draw", side="yes", model_prob=0.30)]
    cap = _stub_db(monkeypatch, rows)

    asyncio.run(
        _pipeline.settle_market(
            "MKT-EPL-DRAW",
            outcome_yes=0,           # bogus binary fallback
            settle_price=1.0,
            result_3way="draw",
        )
    )
    # YES paid out: cost = 10*0.30 = 3, payout = 10 → pnl = +7.0.
    assert cap["trade_updates"][0]["pnl"] == pytest.approx(7.0)
    pm = cap["postmortems"][0]
    assert pm["actual_outcome"] == 1
    assert '"three_way_result": "draw"' in pm["details"]
    assert '"three_way_leg": "draw"' in pm["details"]


def test_home_win_settles_each_leg_correctly(monkeypatch):
    """Three trades (one per leg) settled in a single sweep — only the
    home leg pays YES on a home-win match, draw and away both pay NO."""
    rows = [
        _soccer_trade(10, "home", side="yes", model_prob=0.45),
        _soccer_trade(11, "draw", side="yes", model_prob=0.28),
        _soccer_trade(12, "away", side="yes", model_prob=0.27),
    ]
    cap = _stub_db(monkeypatch, rows)

    asyncio.run(
        _pipeline.settle_market(
            "MKT-EPL-MIXED",
            outcome_yes=0,
            settle_price=0.0,
            result_3way="home",
        )
    )
    pms = {pm["trade_id"]: pm for pm in cap["postmortems"]}
    assert pms["t10"]["actual_outcome"] == 1
    assert pms["t11"]["actual_outcome"] == 0
    assert pms["t12"]["actual_outcome"] == 0


def test_non_three_way_league_ignores_result_3way(monkeypatch):
    """An NBA market is 2-way — even if a (buggy) caller passes
    result_3way="draw", the binary outcome_yes must still be honoured
    so legacy verticals don't silently flip their settlement label."""
    nba_trade = {
        "id": 99, "trade_id": "t99", "market_id": "MKT-NBA-1",
        "vertical": "sports", "league": "NBA",
        "side": "yes", "action": "buy",
        "quantity": 5.0, "price": 0.5,
        "model_prob": 0.55, "model_edge": 0.04, "edge": 0.04,
        "exchange": "kalshi", "trading_mode": "live",
        "metadata": {},
    }
    cap = _stub_db(monkeypatch, [nba_trade])

    asyncio.run(
        _pipeline.settle_market(
            "MKT-NBA-1",
            outcome_yes=1,
            settle_price=1.0,
            result_3way="draw",
        )
    )
    # NBA trade settled YES (binary path) → cost 5*0.5=2.5, payout 5 → +2.5.
    assert cap["trade_updates"][0]["pnl"] == pytest.approx(2.5)
    assert cap["postmortems"][0]["actual_outcome"] == 1


def _stub_db_with_debate(monkeypatch, trade_rows, debate_rows,
                         existing_metric=None):
    """Wire fake db.* and capture every persona metric upsert.  The
    debate_rows fake returns one swarm_debates row whose
    `analyst_verdicts` carries a single persona prediction so the test
    can assert which actual_outcome the Brier was scored against."""
    captured: dict[str, Any] = {
        "persona_inserts": [],
        "persona_updates": [],
        "debate_stamps": [],
    }

    async def _fake_fetch(query: str, *args: Any) -> list[dict[str, Any]]:
        if "FROM trades" in query and "settled_at IS NULL" in query:
            return trade_rows
        if "FROM swarm_debates" in query:
            return debate_rows
        return []

    async def _fake_fetchrow(query: str, *args: Any) -> dict[str, Any] | None:
        if "FROM swarm_analyst_metrics" in query:
            return existing_metric
        return None

    async def _fake_fetchval(*_a: Any, **_kw: Any) -> Any:
        return None

    async def _fake_execute(query: str, *args: Any) -> str:
        if "UPDATE swarm_debates SET outcome" in query:
            captured["debate_stamps"].append({"id": args[0], "outcome": args[1]})
            return "UPDATE 1"
        if "INSERT INTO swarm_analyst_metrics" in query:
            captured["persona_inserts"].append({
                "persona_id": args[0],
                "league": args[1],
                "directional": args[3],
                "sq_err": args[4],
            })
        elif "UPDATE swarm_analyst_metrics" in query:
            captured["persona_updates"].append({
                "id": args[0],
                "brier_new": args[3],
            })
        return "OK"

    monkeypatch.setattr(_pipeline.db, "fetch", _fake_fetch)
    monkeypatch.setattr(_pipeline.db, "fetchrow", _fake_fetchrow)
    monkeypatch.setattr(_pipeline.db, "fetchval", _fake_fetchval)
    monkeypatch.setattr(_pipeline.db, "execute", _fake_execute)
    from app.notifications import push as _push
    monkeypatch.setattr(_push, "push_settlement", lambda **_kw: None)
    # Stub the swarm weight refresh to avoid pulling in the swarm
    # module just for this unit test.
    import sys
    import types
    fake_swarm = types.ModuleType("app.swarm")
    async def _noop_refresh():
        return None
    fake_swarm.refresh_persona_weights = _noop_refresh  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.swarm", fake_swarm)
    return captured


def test_persona_brier_uses_per_leg_outcome_on_draw(monkeypatch):
    """Persona Brier on the HOME leg of an EPL match that ends in a
    draw must be scored against actual_outcome=0 (NO), even when the
    caller passes a stale binary `outcome_yes=1`.  A persona that
    predicted P(YES home win)=0.20 should therefore log a tiny Brier
    of (0.20 - 0)^2 = 0.04 — NOT a fat (0.20 - 1)^2 = 0.64 miss the
    pre-fix behaviour would have produced."""
    rows = [_soccer_trade(7, "home", side="yes", model_prob=0.20)]
    debate_rows = [{
        "id": "deb-1",
        "league": "EPL",
        "market_type": "moneyline",
        "analyst_verdicts": [{
            "persona": "matchup_historian",
            "probability": 0.20,
            "confidence": 0.8,
        }],
        "outcome": None,
        "game_context": {"vertical": "sports", "kalshi_market_id": "MKT-EPL-HOME"},
    }]
    cap = _stub_db_with_debate(monkeypatch, rows, debate_rows)

    asyncio.run(
        _pipeline.settle_market(
            "MKT-EPL-HOME",
            outcome_yes=1,           # bogus binary fallback
            settle_price=0.0,
            result_3way="draw",
        )
    )
    # Persona inserted (cold cache) — verify the Brier squared error
    # was computed against y=0, not y=1.
    assert len(cap["persona_inserts"]) == 1
    pi = cap["persona_inserts"][0]
    assert pi["persona_id"] == "matchup_historian"
    assert pi["sq_err"] == pytest.approx(0.04)
    # And the persona was directionally correct (P<0.5 matched NO).
    assert pi["directional"] == 1
    # Debate outcome stamp carries the richer 3-way label.
    assert cap["debate_stamps"][0]["outcome"] == "DRAW_RESULT_LOSS"


def test_persona_brier_uses_per_leg_outcome_on_draw_leg_win(monkeypatch):
    """Persona that bought YES on the draw leg with P(YES)=0.45 saw a
    draw — Brier must score against y=1, not the bogus binary 0 the
    caller supplied."""
    rows = [_soccer_trade(8, "draw", side="yes", model_prob=0.45)]
    debate_rows = [{
        "id": "deb-2",
        "league": "EPL",
        "market_type": "moneyline",
        "analyst_verdicts": [{
            "persona": "xg_analyst",
            "probability": 0.45,
            "confidence": 0.7,
        }],
        "outcome": None,
        "game_context": {"vertical": "sports", "kalshi_market_id": "MKT-EPL-DRAW"},
    }]
    cap = _stub_db_with_debate(monkeypatch, rows, debate_rows)

    asyncio.run(
        _pipeline.settle_market(
            "MKT-EPL-DRAW",
            outcome_yes=0,           # bogus binary fallback
            settle_price=1.0,
            result_3way="draw",
        )
    )
    pi = cap["persona_inserts"][0]
    # (0.45 - 1)^2 = 0.3025, and P>=0.5? No, so directional NO ≠ YES → 0.
    assert pi["sq_err"] == pytest.approx(0.3025)
    assert pi["directional"] == 0
    assert cap["debate_stamps"][0]["outcome"] == "DRAW_RESULT_WIN"


def test_margin_regressor_skips_three_way_soccer():
    """The margin/total regressor is NA-sports-only.  Even if a soccer
    candidate somehow reaches `adjust_probability` carrying a Spread
    market_type, the 3-way short-circuit must return None so the
    NA-sports regressor never tries to price a soccer line."""
    from app.margin_regressor import adjust_probability
    # Soccer EPL with a (hypothetical) Spread-style candidate.
    cand = {
        "league": "EPL",
        "market_type": "Spread",
        "spread_line": -0.5,
        "side": "home",
    }
    assert adjust_probability(0.5, cand) is None
