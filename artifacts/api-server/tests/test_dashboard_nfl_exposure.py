"""Roundtrip test for the NFL exposure dashboard helper.

Asserts that ``dashboard.get_nfl_exposure()`` is sourced from the same
``_open_position_aggregates`` snapshot the orchestrator is about to feed
into the risk gates -- if those buckets ever drift, the operator panel
would silently lie about cap headroom.
"""
from __future__ import annotations

import asyncio


def test_get_nfl_exposure_roundtrips_open_position_aggregates(monkeypatch):
    from app import dashboard
    from app.orchestrator import cycle as cycle_mod
    from app.sports.leagues import LEAGUE_CONFIGS

    nfl = LEAGUE_CONFIGS["NFL"]
    bankroll = 10_000.0

    # Fake snapshot: NFL has $400 open across the league, of which $300
    # is in BUF@MIA (active matchup) and $100 in KC@DEN; $250 of the
    # league total falls in the Sunday window.  A non-NFL row is mixed in
    # to prove the helper filters by league prefix.
    per_league = {"NFL": 400.0, "NBA": 50.0}
    per_matchup = {
        "NFL:BUF@MIA": 300.0,
        "NFL:KC@DEN": 100.0,
        "NBA:LAL@BOS": 50.0,
    }
    per_sunday = {"NFL": 250.0}

    async def _fake_aggregates():
        return (
            5,                # open_n
            450.0,            # open_notional
            {"sports": 450.0},
            per_league,
            per_matchup,
            per_sunday,
            {},               # per_event
        )

    async def _fake_bankroll():
        return bankroll

    monkeypatch.setattr(cycle_mod, "_open_position_aggregates", _fake_aggregates)
    monkeypatch.setattr(cycle_mod, "_latest_bankroll", _fake_bankroll)

    res = asyncio.run(dashboard.get_nfl_exposure())

    # Top-level shape.
    assert res["league"] == "NFL"
    assert res["bankroll_usd"] == bankroll
    assert res["warn_threshold"] == 0.80

    # Per-league bucket: open=400, cap=5% of 10k = 500, headroom=100, util=0.8 (warn).
    pl = res["per_league"]
    assert pl["open_usd"] == 400.0
    assert pl["cap_pct"] == nfl.allocation_pct
    assert pl["cap_usd"] == bankroll * nfl.allocation_pct
    assert abs(pl["headroom_usd"] - 100.0) < 1e-6
    assert abs(pl["utilization"] - 0.8) < 1e-9
    assert pl["warn"] is True

    # Sunday-window: open=250, cap=3% of 10k = 300, headroom=50, util≈0.833 (warn).
    sw = res["sunday_window"]
    assert sw["open_usd"] == 250.0
    assert sw["cap_pct"] == nfl.sunday_window_cap_pct
    assert sw["cap_usd"] == bankroll * nfl.sunday_window_cap_pct
    assert abs(sw["headroom_usd"] - 50.0) < 1e-6
    assert sw["utilization"] > 0.80
    assert sw["warn"] is True

    # Per-matchup: sorted heaviest-first, only NFL prefixed entries.
    matchups = res["per_matchup"]
    assert [m["matchup"] for m in matchups] == ["BUF@MIA", "KC@DEN"]
    game_cap_usd = bankroll * nfl.per_game_cap_pct  # 1.5% of 10k = 150
    # BUF@MIA: open=300 vs cap 150 => util=2.0 (overflow), headroom 0.
    top = matchups[0]
    assert top["open_usd"] == 300.0
    assert top["cap_pct"] == nfl.per_game_cap_pct
    assert top["cap_usd"] == game_cap_usd
    assert top["headroom_usd"] == 0.0
    assert abs(top["utilization"] - 2.0) < 1e-9
    assert top["warn"] is True
    # KC@DEN: open=100 vs cap 150 => util≈0.667, headroom 50, no warn.
    second = matchups[1]
    assert second["open_usd"] == 100.0
    assert abs(second["headroom_usd"] - 50.0) < 1e-6
    assert second["warn"] is False

    # Raw aggregates roundtrip the source snapshot exactly so a regression
    # introducing a parallel/duplicate SQL fan-out would fail this check.
    raw = res["raw"]
    assert raw["per_league_usd"] == per_league["NFL"]
    assert raw["per_sunday_window_usd"] == per_sunday["NFL"]
    assert raw["per_matchup_usd"] == {
        "BUF@MIA": 300.0,
        "KC@DEN": 100.0,
    }


def test_get_nfl_exposure_handles_no_bankroll(monkeypatch):
    """When the bankroll snapshot is missing, cap_usd / utilization /
    headroom degrade to None rather than dividing by zero."""
    from app import dashboard
    from app.orchestrator import cycle as cycle_mod

    async def _fake_aggregates():
        return (
            1, 50.0, {"sports": 50.0},
            {"NFL": 50.0}, {"NFL:BUF@MIA": 50.0}, {}, {},
        )

    async def _fake_bankroll():
        return None

    monkeypatch.setattr(cycle_mod, "_open_position_aggregates", _fake_aggregates)
    monkeypatch.setattr(cycle_mod, "_latest_bankroll", _fake_bankroll)

    res = asyncio.run(dashboard.get_nfl_exposure())
    assert res["bankroll_usd"] is None
    assert res["per_league"]["cap_usd"] is None
    assert res["per_league"]["utilization"] is None
    assert res["per_league"]["headroom_usd"] is None
    assert res["per_league"]["warn"] is False
