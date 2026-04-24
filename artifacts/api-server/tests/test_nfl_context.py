"""Coverage for the NFL game_context wiring (task #209).

Hermetic — no network, no DB.  Stubs the Open-Meteo HTTP client and
the DB read helpers so the tests assert prompt assembly and the
augmentation contract end-to-end.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.sports import (
    persona_context_augment,
    resolve_nfl_stadium,
    fetch_nfl_player_status,
    fetch_nfl_weather,
    build_nfl_game_context,
)
from app.sports import nfl_context as ctx_mod


# ---------------------------------------------------------------------------
# Stadium table
# ---------------------------------------------------------------------------


def test_stadium_lookup_resolves_by_market_or_mascot():
    s1 = resolve_nfl_stadium("Packers")
    s2 = resolve_nfl_stadium("Green Bay")
    s3 = resolve_nfl_stadium("Green Bay Packers")
    assert s1 is s2 is s3
    assert s1 is not None
    assert s1.indoor is False
    assert "Lambeau" in s1.name


def test_stadium_lookup_indoor_flag_for_dome():
    s = resolve_nfl_stadium("Saints")
    assert s is not None and s.indoor is True
    s = resolve_nfl_stadium("Cowboys")
    assert s is not None and s.indoor is True  # AT&T retractable, plays closed


def test_stadium_lookup_unknown_returns_none():
    assert resolve_nfl_stadium("") is None
    assert resolve_nfl_stadium(None) is None
    assert resolve_nfl_stadium("Atlantis Mermaids") is None


# ---------------------------------------------------------------------------
# Weather
# ---------------------------------------------------------------------------


def test_fetch_weather_indoor_short_circuits_no_http(monkeypatch):
    """Domes never hit Open-Meteo — they're flagged weather_irrelevant."""

    async def boom(*a, **kw):  # noqa: ANN001
        raise AssertionError("must not call Open-Meteo for indoor venues")

    monkeypatch.setattr(ctx_mod, "_fetch_open_meteo_current", boom)
    out = asyncio.run(fetch_nfl_weather("Saints"))
    assert out is not None
    assert out["venue_type"] == "indoor"
    assert out["weather_irrelevant"] is True
    assert "Superdome" in out["venue"]


def test_fetch_weather_outdoor_returns_open_meteo_block(monkeypatch):
    ctx_mod._weather_cache.clear()

    async def fake(lat, lon, timeout_s=6.0):  # noqa: ANN001
        return {
            "temperature_2m": 38.0,
            "wind_speed_10m": 18.0,
            "wind_gusts_10m": 28.0,
            "precipitation": 0.04,
            "relative_humidity_2m": 81.0,
            "weather_code": 61,
        }

    monkeypatch.setattr(ctx_mod, "_fetch_open_meteo_current", fake)
    out = asyncio.run(fetch_nfl_weather("Packers"))
    assert out is not None
    assert out["venue_type"] == "outdoor"
    assert out["temperature_f"] == 38.0
    assert out["wind_mph"] == 18.0
    assert out["wind_gust_mph"] == 28.0
    assert out["precip_in_per_hr"] == 0.04
    assert out["humidity_pct"] == 81.0


def test_fetch_weather_outdoor_handles_feed_outage(monkeypatch):
    ctx_mod._weather_cache.clear()

    async def fake(lat, lon, timeout_s=6.0):  # noqa: ANN001
        return None

    monkeypatch.setattr(ctx_mod, "_fetch_open_meteo_current", fake)
    out = asyncio.run(fetch_nfl_weather("Packers"))
    assert out is not None
    assert out["venue_type"] == "outdoor"
    assert out["forecast_unavailable"] is True


def test_fetch_weather_unknown_team_returns_none():
    assert asyncio.run(fetch_nfl_weather("Atlantis Mermaids")) is None
    assert asyncio.run(fetch_nfl_weather(None)) is None


# ---------------------------------------------------------------------------
# Player status
# ---------------------------------------------------------------------------


def test_fetch_player_status_filters_to_ol_dl_and_skill(monkeypatch):
    """Special-teams + kickers must be dropped; OL/DL/skill kept."""

    async def fake_fetch(query, *args):  # noqa: ANN001
        if "sportradar_injuries" in query:
            return [
                {"player_name": "Jordan Love",   "position": "QB",
                 "status": "OUT", "description": "knee"},
                {"player_name": "David Bakhtiari", "position": "LT",
                 "status": "QUESTIONABLE", "description": "back"},
                {"player_name": "Aaron Jones",  "position": "RB",
                 "status": "ACTIVE", "description": ""},
                {"player_name": "Lukas Patrick", "position": "K",
                 "status": "OUT", "description": "leg"},
            ]
        return []

    monkeypatch.setattr(ctx_mod.db, "fetch", fake_fetch)
    out = asyncio.run(fetch_nfl_player_status("Packers", "Lions"))
    assert out is not None
    home = out["home"]
    positions = {i["position"] for i in home["injuries"]}
    assert "K" not in positions  # kicker dropped
    assert "QB" in positions
    assert "LT" in positions
    # ACTIVE status filtered out (not in notable set)
    players = {i["player"] for i in home["injuries"]}
    assert "Aaron Jones" not in players


def test_fetch_player_status_db_failure_degrades(monkeypatch):
    async def boom(*a, **kw):  # noqa: ANN001
        raise RuntimeError("DB down")

    monkeypatch.setattr(ctx_mod.db, "fetch", boom)
    out = asyncio.run(fetch_nfl_player_status("Packers", "Lions"))
    assert out is not None
    assert out["home"]["injuries"] == []
    assert out["home"]["snap_counts"] == []


# ---------------------------------------------------------------------------
# build_game_context
# ---------------------------------------------------------------------------


def test_build_game_context_assembles_weather_and_player_status(monkeypatch):
    ctx_mod._weather_cache.clear()

    async def fake_om(lat, lon, timeout_s=6.0):  # noqa: ANN001
        return {
            "temperature_2m": 25.0,
            "wind_speed_10m": 22.0,
            "wind_gusts_10m": 35.0,
            "precipitation": 0.0,
            "relative_humidity_2m": 60.0,
            "weather_code": 1,
        }

    async def fake_fetch(query, *args):  # noqa: ANN001
        if "sportradar_injuries" in query:
            return [{"player_name": "X", "position": "DT",
                     "status": "OUT", "description": "knee"}]
        if "nfl_snap_counts" in query:
            return [{"player_name": "Doubs", "position": "WR",
                     "snap_pct": 78.0, "week": 18, "season": 2025}]
        return []

    monkeypatch.setattr(ctx_mod, "_fetch_open_meteo_current", fake_om)
    monkeypatch.setattr(ctx_mod.db, "fetch", fake_fetch)
    out = asyncio.run(build_nfl_game_context("Packers", "Lions"))
    assert "weather" in out
    assert "player_status" in out
    assert out["weather"]["venue_type"] == "outdoor"
    assert out["player_status"]["home"]["snap_counts"][0]["player"] == "Doubs"


# ---------------------------------------------------------------------------
# persona_context_augment with structured game_context
# ---------------------------------------------------------------------------


def test_persona_context_augment_renders_observed_conditions_block():
    aug = persona_context_augment(
        "NFL",
        game_context={
            "weather": {
                "venue": "Lambeau Field",
                "venue_type": "outdoor",
                "temperature_f": 38.0,
                "wind_mph": 18.0,
                "wind_gust_mph": 28.0,
                "precip_in_per_hr": 0.04,
                "humidity_pct": 81.0,
            },
            "player_status": {
                "home": {
                    "team": "Packers",
                    "injuries": [
                        {"player": "Jordan Love", "position": "QB",
                         "status": "OUT", "description": "knee"},
                    ],
                    "snap_counts": [
                        {"player": "Doubs", "position": "WR", "snap_pct": 78},
                    ],
                },
                "away": {
                    "team": "Lions",
                    "injuries": [],
                    "snap_counts": [],
                },
            },
        },
    )
    assert "OBSERVED CONDITIONS" in aug
    assert "Lambeau Field" in aug
    assert "wind 18 mph (gust 28)" in aug
    assert "OUT QB Jordan Love" in aug
    assert "WR Doubs 78%" in aug
    # Empty side still emits a row with the "no notable" / "no snap"
    # fallback so the prompt makes the absence explicit.
    assert "no notable" in aug
    assert "no snap-count data" in aug
    # Base NFL prose still present so the original prompt guidance
    # isn't lost when the structured block is added.
    assert "NFL-SPECIFIC SIGNALS" in aug


def test_persona_context_augment_indoor_short_circuits_weather_block():
    aug = persona_context_augment(
        "NFL",
        game_context={
            "weather": {
                "venue": "Caesars Superdome",
                "venue_type": "indoor",
                "weather_irrelevant": True,
            },
        },
    )
    assert "indoor / dome" in aug
    assert "weather is not a factor" in aug


def test_persona_context_augment_no_game_context_keeps_legacy_block():
    """Back-compat: existing callers that don't pass game_context still
    receive the original prose-only NFL block."""
    aug = persona_context_augment("NFL")
    assert "OBSERVED CONDITIONS" not in aug
    assert "NFL-SPECIFIC SIGNALS" in aug


def test_persona_context_augment_unknown_league_is_noop():
    assert persona_context_augment(
        "UNKNOWN", game_context={"weather": {"venue": "x"}}
    ) == ""


# ---------------------------------------------------------------------------
# Runner integration: NFL candidates inject game_context into prompt
# ---------------------------------------------------------------------------


def test_runner_attaches_nfl_game_context_to_prompt(monkeypatch):
    """Asserts the integration: when league=NFL the swarm runner builds
    the game_context dict, appends the OBSERVED CONDITIONS block to the
    user prompt, and stashes the dict back on the candidate so it gets
    persisted.  We monkeypatch every external dependency so the test
    is hermetic."""
    from app.swarm import runner

    async def fake_build_ctx(home, away):  # noqa: ANN001
        return {
            "weather": {
                "venue": "Lambeau Field",
                "venue_type": "outdoor",
                "temperature_f": 38.0,
                "wind_mph": 18.0,
            },
            "player_status": {
                "home": {"team": home or "", "injuries": [], "snap_counts": []},
                "away": {"team": away or "", "injuries": [], "snap_counts": []},
            },
        }

    captured: dict[str, Any] = {}

    async def fake_call_persona(persona, prompt):  # noqa: ANN001
        captured.setdefault("prompt", prompt)
        return {
            "persona": persona.id, "provider": persona.provider,
            "model": persona.model_id, "probability": 0.6,
            "confidence": 0.7, "lean": "YES",
            "reasoning": "ok", "raw_text": '{"approved":true,"recommended_action":"BUY_YES"}',
            "input_tokens": 1, "output_tokens": 1,
            "cost": 0.0, "latency_ms": 1, "error": None,
            "primary_provider": persona.provider,
            "primary_model": persona.model_id,
            "fallback_used": False, "fallback_reason": None,
        }

    async def fake_weight(*a, **kw):  # noqa: ANN001
        return 1.0

    async def fake_publish(*a, **kw):  # noqa: ANN001
        return None

    async def fake_disabled(*a, **kw):  # noqa: ANN001
        return frozenset()

    async def fake_persist(**kw):  # noqa: ANN001
        captured["persisted_candidate"] = kw["candidate"]
        return 1

    monkeypatch.setattr(
        "app.sports.build_nfl_game_context", fake_build_ctx
    )
    monkeypatch.setattr(runner, "_call_persona", fake_call_persona)
    monkeypatch.setattr(runner, "get_persona_weight", fake_weight)
    monkeypatch.setattr(runner, "_publish", fake_publish)
    monkeypatch.setattr(runner, "disabled_persona_ids", fake_disabled)
    monkeypatch.setattr(runner, "_persist_debate", fake_persist)

    out = asyncio.run(
        runner.run_debate({
            "game_id": "g1",
            "matchup": "GB @ DET",
            "league": "NFL",
            "market_type": "moneyline",
            "side": "YES",
            "current_price": 0.5,
            "home_team": "Packers",
            "away_team": "Lions",
            "context": "edge=0.05",
            "vertical": "sports",
        })
    )
    prompt = captured["prompt"]
    assert "OBSERVED CONDITIONS" in prompt
    assert "Lambeau Field" in prompt
    assert "wind 18 mph" in prompt
    persisted = captured["persisted_candidate"]
    assert persisted.get("nfl_context", {}).get("weather", {}).get("venue") == "Lambeau Field"
    assert out["debate_id"] == 1


# ---------------------------------------------------------------------------
# Snap-counts feed (parser + poller registration + end-to-end flow)
# ---------------------------------------------------------------------------


def test_extended_box_score_parser_extracts_snap_pcts():
    from app.feeds.nfl_snap_counts import parse_extended_box_score

    payload = {
        "season": {"year": 2025},
        "week": {"sequence": 7},
        "home": {
            "id": "T-PACK",
            "name": "Packers",
            "players": [
                {
                    "id": "P-LOVE",
                    "name": "Jordan Love",
                    "position": "QB",
                    "statistics": {
                        "participation": {
                            "offense_snaps_pct": 100.0,
                            "offense_snaps": 65,
                            "total_snaps": 65,
                        }
                    },
                },
                {
                    "id": "P-OL1",
                    "name": "Tackle One",
                    "position": "OT",
                    "offense_snaps_pct": 0.93,  # 0..1 form, must normalise
                    "offense_snaps": 60,
                },
                {
                    # Player with no participation block — must drop.
                    "id": "P-INACTIVE",
                    "name": "Inactive Guy",
                    "position": "WR",
                },
            ],
        },
        "away": {
            "id": "T-LIONS",
            "name": "Lions",
            "players": [
                {
                    "id": "P-DL1",
                    "name": "Dline One",
                    "position": "DT",
                    "participation": {
                        "defense_snaps_pct": 75.0,
                        "defense_snaps": 50,
                        "st_snaps_pct": 10.0,
                        "st_snaps": 7,
                    },
                },
            ],
        },
    }
    rows = parse_extended_box_score(payload)
    by_pid = {r["player_id"]: r for r in rows}
    assert "P-INACTIVE" not in by_pid
    assert by_pid["P-LOVE"]["snap_pct"] == 100.0
    assert by_pid["P-LOVE"]["team_name"] == "Packers"
    assert by_pid["P-LOVE"]["season"] == 2025 and by_pid["P-LOVE"]["week"] == 7
    # 0..1 form normalised to 0..100
    assert by_pid["P-OL1"]["snap_pct"] == pytest.approx(93.0)
    # Highest pct wins (defense > st)
    assert by_pid["P-DL1"]["snap_pct"] == 75.0
    assert by_pid["P-DL1"]["snaps"] == 50


def test_register_nfl_snap_counts_feed_disabled_without_key(monkeypatch):
    """Without a Sportradar key the feed must register DISABLED with a
    skipped_reason so the operator console can flag the gap."""
    from app.feeds.registry import registry
    from app.feeds import nfl_snap_counts as snap_mod

    monkeypatch.delenv("SPORTRADAR_NFL_KEY", raising=False)
    monkeypatch.delenv("SPORTRADAR_API_KEY", raising=False)

    registry._feeds.pop("sportradar_nfl_snap_counts", None)
    registry._status.pop("sportradar_nfl_snap_counts", None)
    snap_mod.register_nfl_snap_counts_feed()
    status = registry.status_of("sportradar_nfl_snap_counts")
    assert status is not None
    assert status.enabled is False
    assert "SPORTRADAR_NFL_KEY" in (status.skipped_reason or "")


def test_register_nfl_snap_counts_feed_enabled_with_key(monkeypatch):
    from app.feeds.registry import registry
    from app.feeds import nfl_snap_counts as snap_mod

    monkeypatch.setenv("SPORTRADAR_NFL_KEY", "test-key")
    registry._feeds.pop("sportradar_nfl_snap_counts", None)
    registry._status.pop("sportradar_nfl_snap_counts", None)
    snap_mod.register_nfl_snap_counts_feed()
    status = registry.status_of("sportradar_nfl_snap_counts")
    assert status is not None
    assert status.enabled is True
    assert status.skipped_reason is None


def test_poll_snap_counts_end_to_end_populates_player_status(monkeypatch):
    """End-to-end: stubbed Sportradar HTTP -> parsed rows -> upsert
    captured -> nfl_context reader returns the same rows.

    Asserts that with feed data present, ``game_context.player_status``
    actually contains non-empty snap_counts entries (the gap the code
    review flagged).
    """
    from app.feeds import nfl_snap_counts as snap_mod
    from app.sports import nfl_context as ctx

    monkeypatch.setenv("SPORTRADAR_NFL_KEY", "test-key")

    box_payload = {
        "season": {"year": 2025},
        "week": {"sequence": 7},
        "home": {
            "id": "T-PACK", "name": "Packers",
            "players": [
                {
                    "id": "P-LOVE", "name": "Jordan Love", "position": "QB",
                    "offense_snaps_pct": 100.0, "offense_snaps": 65,
                },
                {
                    "id": "P-JACOBS", "name": "Josh Jacobs", "position": "RB",
                    "offense_snaps_pct": 78.0, "offense_snaps": 51,
                },
            ],
        },
        "away": {
            "id": "T-LIONS", "name": "Lions",
            "players": [
                {
                    "id": "P-GIBBS", "name": "Jahmyr Gibbs", "position": "RB",
                    "offense_snaps_pct": 62.0, "offense_snaps": 40,
                },
            ],
        },
    }

    async def fake_recent(limit: int):  # noqa: ANN001
        return ["g-pack-lions"]

    async def fake_get_json(url, key):  # noqa: ANN001
        assert "extended_box_score" in url
        assert "g-pack-lions" in url
        return box_payload

    captured: list[dict[str, Any]] = []

    async def fake_upsert(rows):  # noqa: ANN001
        captured.extend(rows)
        return len(rows)

    monkeypatch.setattr(snap_mod, "_recent_closed_nfl_game_ids", fake_recent)
    monkeypatch.setattr(snap_mod, "_get_json", fake_get_json)
    monkeypatch.setattr(snap_mod, "upsert_snap_counts", fake_upsert)
    monkeypatch.setattr(snap_mod, "ensure_schema", lambda: _noop())

    async def _noop():
        return None

    written = asyncio.run(snap_mod.poll_snap_counts())
    assert written == 3
    assert {r["player_id"] for r in captured} == {"P-LOVE", "P-JACOBS", "P-GIBBS"}

    # Now wire those upserted rows into the nfl_context reader and
    # assert the prompt-facing player_status block sees them.
    pack_rows = [r for r in captured if r["team_name"] == "Packers"]
    lion_rows = [r for r in captured if r["team_name"] == "Lions"]

    async def fake_snap_lookup(team_name):  # noqa: ANN001
        if "Packers" in team_name or team_name == "Packers":
            return pack_rows
        if "Lions" in team_name or team_name == "Lions":
            return lion_rows
        return []

    async def fake_inj_lookup(team_name):  # noqa: ANN001
        return []

    monkeypatch.setattr(ctx, "_fetch_team_snap_counts", fake_snap_lookup)
    monkeypatch.setattr(ctx, "_fetch_team_injuries", fake_inj_lookup)

    status = asyncio.run(ctx.fetch_player_status("Packers", "Lions"))
    assert status["home"]["snap_counts"], "home snap counts must be non-empty"
    assert status["away"]["snap_counts"], "away snap counts must be non-empty"
    home_pids = {p["player_id"] for p in status["home"]["snap_counts"]}
    assert home_pids == {"P-LOVE", "P-JACOBS"}
