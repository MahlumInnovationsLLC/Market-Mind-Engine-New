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

    async def _fake_fetchrow(query: str, *args: Any) -> dict[str, Any] | None:
        # Task #752 step 5 (architect follow-up): the trade UPDATE was
        # converted to ``UPDATE ... RETURNING id`` so settle can detect
        # whether it actually transitioned the row (and therefore
        # whether to drain a canary slot).  Record the UPDATE here for
        # the test's `trade_updates` assertions and return a row to
        # signal "first settle wins" — there is no double-settle in
        # these tests.
        if "UPDATE trades" in query and "RETURNING id" in query:
            captured["trade_updates"].append({"id": args[0], "pnl": args[1]})
            return {"id": args[0]}
        return None

    async def _fake_fetchval(*_a: Any, **_kw: Any) -> Any:
        return None

    async def _fake_execute(query: str, *args: Any) -> str:
        if "INSERT INTO trade_postmortems" in query:
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


def test_sportradar_soccer_match_summary_returns_teams_and_score(monkeypatch):
    """The new helper underpins the operator-console UI: trade-detail
    + recent-settlements both render the home/away team names and the
    final scoreline.  Verify it joins through swarm_debates ->
    sportradar_live_state + sportradar_schedules and returns the full
    summary once the match has finalised.
    """
    async def _fake_fetchrow(query: str, *args):
        if "FROM swarm_debates" in query:
            return {"league": "EPL", "game_id": "sr:match:42"}
        if "FROM sportradar_live_state" in query:
            return {"status": "closed", "home_score": 2, "away_score": 1}
        if "FROM sportradar_schedules" in query:
            return {"home_team": "Arsenal", "away_team": "Chelsea"}
        return None

    monkeypatch.setattr(_pipeline.db, "fetchrow", _fake_fetchrow)

    summary = asyncio.run(
        _pipeline._sportradar_soccer_match_summary("KX-EPL-1")
    )

    assert summary is not None
    assert summary["result"] == "home"
    assert summary["home_team"] == "Arsenal"
    assert summary["away_team"] == "Chelsea"
    assert summary["home_score"] == 2
    assert summary["away_score"] == 1
    assert summary["status"] == "closed"


def test_sportradar_soccer_match_summary_tolerates_missing_schedule(monkeypatch):
    """If the schedules cache has no row yet, the helper must still
    surface the result + scoreline — the UI falls back to "Home"/
    "Away" placeholders rather than failing.
    """
    async def _fake_fetchrow(query: str, *args):
        if "FROM swarm_debates" in query:
            return {"league": "EPL", "game_id": "sr:match:99"}
        if "FROM sportradar_live_state" in query:
            return {"status": "ended", "home_score": 0, "away_score": 0}
        if "FROM sportradar_schedules" in query:
            return None
        return None

    monkeypatch.setattr(_pipeline.db, "fetchrow", _fake_fetchrow)

    summary = asyncio.run(
        _pipeline._sportradar_soccer_match_summary("KX-EPL-2")
    )

    assert summary is not None
    assert summary["result"] == "draw"
    assert summary["home_team"] is None
    assert summary["away_team"] is None
    assert summary["home_score"] == 0
    assert summary["away_score"] == 0


def test_run_cycle_passes_sportradar_three_way_result_to_settle(monkeypatch):
    """End-to-end cycle integration: a soccer kalshi market with NO
    Kalshi resolution yet but a final Sportradar match score must
    invoke ``settle_market`` with ``result_3way`` derived from the
    Sportradar h/d/a result.

    This is the wiring task #445 specifies — without it the cycle
    would skip the market entirely (Kalshi parse returns None) and
    soccer trades would never settle on the per-leg path until the
    exchange catches up.
    """
    # Stub the candidate-market discovery so we run exactly one
    # ticker through the loop.
    async def _fake_open_ids(*_a, **_kw):
        return ["KX-EPL-DRAW-1"]
    monkeypatch.setattr(_pipeline, "_open_kalshi_market_ids", _fake_open_ids)

    # Wire a fake Settings + KalshiClient so the cycle reaches the
    # per-ticker branch.  KalshiClient is a context manager whose
    # `get_market` returns a still-open market so the binary parse
    # path returns None — forcing the cycle to consult Sportradar.
    class _FakeKalshiClient:
        def __init__(self, *a, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get_market(self, ticker):
            return {"market": {"ticker": ticker, "status": "open"}}

    class _FakeKalshiAPIError(RuntimeError):
        def __init__(self, status, body=None):
            super().__init__(f"kalshi {status}")
            self.status = status
    # Note: the cycle imports KalshiAPIError lazily; we just need the
    # name to exist on the module for the `except` block.
    import sys, types
    fake_kalshi_mod = types.ModuleType("app.kalshi.client")
    fake_kalshi_mod.KalshiClient = _FakeKalshiClient
    fake_kalshi_mod.KalshiAPIError = _FakeKalshiAPIError
    monkeypatch.setitem(sys.modules, "app.kalshi.client", fake_kalshi_mod)

    fake_config_mod = types.ModuleType("app.config")
    class _S:
        kalshi_api_key_id = "k"
        kalshi_private_key = "p"
    fake_config_mod.get_settings = lambda: _S()
    monkeypatch.setitem(sys.modules, "app.config", fake_config_mod)

    # Stub DB lookups: league = EPL, sportradar score = 1-1 (draw).
    captured: dict = {"settle_calls": []}

    async def _fake_fetchrow(query: str, *args):
        if "FROM trades" in query and "WHERE market_id" in query:
            return {"league": "EPL"}
        if "FROM swarm_debates" in query:
            # Used by both _league_for_market fallback and
            # _sportradar_soccer_match_summary. Returning a debate row
            # carrying the game_id is enough for both.
            return {"league": "EPL", "game_id": "sr:match:42"}
        if "FROM sportradar_live_state" in query:
            return {"status": "closed", "home_score": 1, "away_score": 1}
        if "FROM sportradar_schedules" in query:
            return {"home_team": "Arsenal", "away_team": "Chelsea"}
        return None

    async def _fake_settle(market_id, *, outcome_yes, settle_price=None,
                           result_3way=None, match_summary=None):
        captured["settle_calls"].append({
            "market_id": market_id,
            "outcome_yes": outcome_yes,
            "settle_price": settle_price,
            "result_3way": result_3way,
            "match_summary": match_summary,
        })
        return {
            "trades_settled": 1,
            "persona_metrics_updated": 1,
            "alerts_raised": 0,
        }

    monkeypatch.setattr(_pipeline.db, "fetchrow", _fake_fetchrow)
    monkeypatch.setattr(_pipeline, "settle_market", _fake_settle)

    cr = asyncio.run(_pipeline.run_cycle(market_limit=10))

    assert cr.markets_examined == 1
    assert cr.markets_settled == 1
    assert len(captured["settle_calls"]) == 1
    call = captured["settle_calls"][0]
    assert call["market_id"] == "KX-EPL-DRAW-1"
    # The cycle must pass the Sportradar-derived 3-way result through
    # so settle_market can drive the per-leg outcome path.
    assert call["result_3way"] == "draw"
    # Binary fallback is the safe 0 — settle_market overrides it
    # per-trade based on each row's `three_way_leg` metadata.
    assert call["outcome_yes"] == 0
    # The full match summary must also be threaded through so that
    # settle_market can publish home/away team names + scoreline onto
    # the recent-settlements feed for the operator console.
    summary = call["match_summary"]
    assert summary is not None
    assert summary["result"] == "draw"
    assert summary["home_team"] == "Arsenal"
    assert summary["away_team"] == "Chelsea"
    assert summary["home_score"] == 1
    assert summary["away_score"] == 1


def test_run_cycle_skips_market_when_sportradar_has_no_final_score(monkeypatch):
    """Soccer market with no Kalshi resolution AND no Sportradar
    match-final-status yet: cycle must NOT call settle_market and
    must report 0 markets settled (preserves legacy "wait for the
    feed" behaviour)."""
    async def _fake_open_ids(*_a, **_kw):
        return ["KX-EPL-INPLAY-2"]
    monkeypatch.setattr(_pipeline, "_open_kalshi_market_ids", _fake_open_ids)

    class _FakeKalshiClient:
        def __init__(self, *a, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get_market(self, ticker):
            return {"market": {"ticker": ticker, "status": "open"}}

    class _FakeKalshiAPIError(RuntimeError):
        def __init__(self, status, body=None):
            super().__init__("kalshi error")
            self.status = status

    import sys, types
    fake_kalshi_mod = types.ModuleType("app.kalshi.client")
    fake_kalshi_mod.KalshiClient = _FakeKalshiClient
    fake_kalshi_mod.KalshiAPIError = _FakeKalshiAPIError
    monkeypatch.setitem(sys.modules, "app.kalshi.client", fake_kalshi_mod)

    fake_config_mod = types.ModuleType("app.config")
    class _S:
        kalshi_api_key_id = "k"
        kalshi_private_key = "p"
    fake_config_mod.get_settings = lambda: _S()
    monkeypatch.setitem(sys.modules, "app.config", fake_config_mod)

    async def _fake_fetchrow(query: str, *args):
        if "FROM trades" in query and "WHERE market_id" in query:
            return {"league": "EPL"}
        if "FROM swarm_debates" in query:
            return {"league": "EPL", "game_id": "sr:match:99"}
        if "FROM sportradar_live_state" in query:
            # Match still in progress — no final score available.
            return {"status": "live", "home_score": 0, "away_score": 1}
        return None

    settle_calls: list = []
    async def _boom_settle(*a, **kw):
        settle_calls.append((a, kw))
        return {}
    monkeypatch.setattr(_pipeline.db, "fetchrow", _fake_fetchrow)
    monkeypatch.setattr(_pipeline, "settle_market", _boom_settle)

    cr = asyncio.run(_pipeline.run_cycle(market_limit=10))

    assert cr.markets_examined == 1
    assert cr.markets_settled == 0
    assert settle_calls == []


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
