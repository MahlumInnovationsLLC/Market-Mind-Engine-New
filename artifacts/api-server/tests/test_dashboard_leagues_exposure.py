"""Roundtrip tests for the multi-league exposure dashboard helper.

Asserts that ``dashboard.get_leagues_exposure()`` is sourced from the
same ``_open_position_aggregates`` snapshot the orchestrator feeds the
risk gates, and that it generalises the NFL panel to every league
configured in ``LEAGUE_CONFIGS`` (per-league + per-game caps for all,
plus Sunday-window for NFL and per-event for combat / individual /
event-shaped leagues).
"""
from __future__ import annotations

import asyncio


def test_get_leagues_exposure_covers_multiple_leagues(monkeypatch):
    from app import dashboard
    from app.orchestrator import cycle as cycle_mod
    from app.sports.leagues import LEAGUE_CONFIGS

    nfl = LEAGUE_CONFIGS["NFL"]
    nba = LEAGUE_CONFIGS["NBA"]
    ufc = LEAGUE_CONFIGS["UFC"]
    bankroll = 10_000.0

    # Mixed snapshot: NFL has Sunday-window exposure, NBA has only
    # per-league + per-matchup, UFC has per-event concentration too.
    per_league = {
        "NFL": 400.0,
        "NBA": 600.0,
        "UFC": 80.0,
    }
    per_matchup = {
        "NFL:BUF@MIA": 300.0,
        "NFL:KC@DEN": 100.0,
        "NBA:LAL@BOS": 600.0,
        "UFC:ADESANYA@PEREIRA": 80.0,
    }
    per_sunday = {"NFL": 250.0}
    per_event = {"UFC:UFC300": 80.0}

    async def _fake_aggregates(*, trading_mode="live"):
        return (
            8,
            1080.0,
            {"sports": 1080.0},
            per_league,
            per_matchup,
            per_sunday,
            per_event,
        )

    async def _fake_bankroll(*, trading_mode="live"):
        return bankroll

    monkeypatch.setattr(cycle_mod, "_open_position_aggregates", _fake_aggregates)
    monkeypatch.setattr(cycle_mod, "_latest_bankroll", _fake_bankroll)

    res = asyncio.run(dashboard.get_leagues_exposure())

    assert res["bankroll_usd"] == bankroll
    assert res["warn_threshold"] == 0.80

    leagues = {entry["league"]: entry for entry in res["leagues"]}
    # Every league with open exposure must be present, leagues without
    # any open exposure (e.g. MLB, EPL) must be filtered out.
    assert set(leagues.keys()) == {"NFL", "NBA", "UFC"}

    # NFL: per-league + Sunday window + per-matchup, no per-event.
    nfl_entry = leagues["NFL"]
    assert nfl_entry["per_league"]["open_usd"] == 400.0
    assert nfl_entry["per_league"]["cap_pct"] == nfl.allocation_pct
    assert nfl_entry["per_league"]["cap_usd"] == bankroll * nfl.allocation_pct
    assert abs(nfl_entry["per_league"]["utilization"] - 0.8) < 1e-9
    assert nfl_entry["per_league"]["warn"] is True
    assert "sunday_window" in nfl_entry
    assert nfl_entry["sunday_window"]["open_usd"] == 250.0
    assert nfl_entry["sunday_window"]["cap_pct"] == nfl.sunday_window_cap_pct
    assert "per_event" not in nfl_entry
    assert [m["matchup"] for m in nfl_entry["per_matchup"]] == [
        "BUF@MIA",
        "KC@DEN",
    ]
    # Per-game cap is wired from the league config, not a magic constant.
    assert nfl_entry["per_matchup"][0]["cap_pct"] == nfl.per_game_cap_pct
    assert nfl_entry["per_matchup"][0]["warn"] is True  # 300 > 150 cap

    # NBA: per-league + per-matchup, no Sunday window, no per-event.
    nba_entry = leagues["NBA"]
    assert "sunday_window" not in nba_entry
    assert "per_event" not in nba_entry
    assert nba_entry["per_league"]["cap_pct"] == nba.allocation_pct
    assert nba_entry["per_league"]["cap_usd"] == bankroll * nba.allocation_pct
    # 600 vs cap 1000 = 0.6 utilization, no warn.
    assert abs(nba_entry["per_league"]["utilization"] - 0.6) < 1e-9
    assert nba_entry["per_league"]["warn"] is False
    assert [m["matchup"] for m in nba_entry["per_matchup"]] == ["LAL@BOS"]
    assert nba_entry["per_matchup"][0]["cap_pct"] == nba.per_game_cap_pct

    # UFC: per-league + per-matchup + per-event (combat sport).
    ufc_entry = leagues["UFC"]
    assert "sunday_window" not in ufc_entry
    assert "per_event" in ufc_entry
    assert [e["event_id"] for e in ufc_entry["per_event"]] == ["UFC300"]
    assert ufc_entry["per_event"][0]["cap_pct"] == ufc.per_event_cap_pct
    assert ufc_entry["per_event"][0]["cap_usd"] == bankroll * ufc.per_event_cap_pct
    # 80 vs cap 150 = ~0.533, no warn.
    assert ufc_entry["per_event"][0]["utilization"] < 0.80
    assert ufc_entry["per_event"][0]["warn"] is False

    # Sort order: NFL (util=0.80) > NBA (util=0.60) > UFC (util=0.40).
    ordered = [entry["league"] for entry in res["leagues"]]
    assert ordered == ["NFL", "NBA", "UFC"]


def test_get_leagues_exposure_filters_zero_exposure_leagues(monkeypatch):
    """Leagues with no open positions are filtered out entirely so the
    panel doesn't render 20 empty cards on a quiet day."""
    from app import dashboard
    from app.orchestrator import cycle as cycle_mod

    async def _fake_aggregates(*, trading_mode="live"):
        return (
            1, 50.0, {"sports": 50.0},
            {"NFL": 50.0}, {"NFL:BUF@MIA": 50.0}, {}, {},
        )

    async def _fake_bankroll(*, trading_mode="live"):
        return 10_000.0

    monkeypatch.setattr(cycle_mod, "_open_position_aggregates", _fake_aggregates)
    monkeypatch.setattr(cycle_mod, "_latest_bankroll", _fake_bankroll)

    res = asyncio.run(dashboard.get_leagues_exposure())
    assert [e["league"] for e in res["leagues"]] == ["NFL"]


def test_get_leagues_exposure_handles_no_bankroll(monkeypatch):
    """When the bankroll snapshot is missing, cap_usd / utilization /
    headroom degrade to None rather than dividing by zero — same
    behaviour as :func:`get_nfl_exposure`."""
    from app import dashboard
    from app.orchestrator import cycle as cycle_mod

    async def _fake_aggregates(*, trading_mode="live"):
        return (
            1, 50.0, {"sports": 50.0},
            {"NFL": 50.0}, {"NFL:BUF@MIA": 50.0}, {}, {},
        )

    async def _fake_bankroll(*, trading_mode="live"):
        return None

    monkeypatch.setattr(cycle_mod, "_open_position_aggregates", _fake_aggregates)
    monkeypatch.setattr(cycle_mod, "_latest_bankroll", _fake_bankroll)

    res = asyncio.run(dashboard.get_leagues_exposure())
    assert res["bankroll_usd"] is None
    nfl_entry = next(e for e in res["leagues"] if e["league"] == "NFL")
    assert nfl_entry["per_league"]["cap_usd"] is None
    assert nfl_entry["per_league"]["utilization"] is None
    assert nfl_entry["per_league"]["headroom_usd"] is None
    assert nfl_entry["per_league"]["warn"] is False


def test_get_leagues_exposure_surfaces_unconfigured_league(monkeypatch):
    """A league with open positions but no LEAGUE_CONFIGS entry should
    still show up — with cap_pct / cap_usd as None — so operators can
    see exposure they would otherwise miss.

    Uses ``VOLLEYBALL`` as a stand-in for any league that may appear on
    legacy rows but has never been promoted into ``LEAGUE_CONFIGS``.
    (WNBA was the historical placeholder here but became a configured
    league in task #823.)
    """
    from app import dashboard
    from app.orchestrator import cycle as cycle_mod

    async def _fake_aggregates(*, trading_mode="live"):
        return (
            1, 25.0, {"sports": 25.0},
            {"VOLLEYBALL": 25.0},        # no LEAGUE_CONFIGS entry
            {"VOLLEYBALL:LV@NY": 25.0},
            {},
            {},
        )

    async def _fake_bankroll(*, trading_mode="live"):
        return 5_000.0

    monkeypatch.setattr(cycle_mod, "_open_position_aggregates", _fake_aggregates)
    monkeypatch.setattr(cycle_mod, "_latest_bankroll", _fake_bankroll)

    res = asyncio.run(dashboard.get_leagues_exposure())
    entry = next(
        (e for e in res["leagues"] if e["league"] == "VOLLEYBALL"), None,
    )
    assert entry is not None
    assert entry["per_league"]["open_usd"] == 25.0
    assert entry["per_league"]["cap_pct"] is None
    assert entry["per_league"]["cap_usd"] is None
    assert entry["per_league"]["warn"] is False
    # Per-matchup carries open exposure but no cap either.
    assert [m["matchup"] for m in entry["per_matchup"]] == ["LV@NY"]
    assert entry["per_matchup"][0]["cap_pct"] is None
