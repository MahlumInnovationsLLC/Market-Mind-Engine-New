"""Regression coverage for the soccer lineup_rotation persona wiring.

Task #441 wires the cached `sportradar_rosters` + `sportradar_injuries`
("missing players") feeds — populated by task #211 — into the swarm
prompt path so the soccer ``lineup_rotation`` persona reasons on real
squad availability instead of just being told to "think about" team
news.

Hermetic — no network, no DB.  Stubs ``app.db`` and the swarm runner
helpers so the whole prompt path is exercised end-to-end.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.sports import (
    SOCCER_LEAGUES,
    build_soccer_game_context,
    fetch_soccer_lineup_status,
    is_soccer_league,
    persona_context_augment,
)
from app.sports import soccer_context as ctx_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_soccer_db(
    monkeypatch: pytest.MonkeyPatch,
    *,
    home_missing: list[dict[str, Any]] | None = None,
    away_missing: list[dict[str, Any]] | None = None,
    home_team_match: str = "Arsenal",
    away_team_match: str = "Chelsea",
    home_roster_n: int = 28,
    away_roster_n: int = 27,
) -> None:
    """Pin the two ``app.db`` helpers ``soccer_context`` reads from to
    deterministic per-team payloads."""

    async def fake_fetch(query, *args):  # noqa: ANN001
        if "FROM sportradar_injuries" not in query:
            return []
        league_arg, team_pattern = args
        team_pattern_l = team_pattern.lower()
        if home_team_match.lower() in team_pattern_l:
            return list(home_missing or [])
        if away_team_match.lower() in team_pattern_l:
            return list(away_missing or [])
        return []

    async def fake_fetchval(query, *args):  # noqa: ANN001
        if "FROM sportradar_rosters" not in query:
            return 0
        league_arg, team_pattern = args
        team_pattern_l = team_pattern.lower()
        if home_team_match.lower() in team_pattern_l:
            return home_roster_n
        if away_team_match.lower() in team_pattern_l:
            return away_roster_n
        return 0

    monkeypatch.setattr(ctx_mod.db, "fetch", fake_fetch)
    monkeypatch.setattr(ctx_mod.db, "fetchval", fake_fetchval)


def _missing_player(
    name: str, position: str, status: str = "missing", reason: str = ""
) -> dict[str, Any]:
    """Mimic the row shape ``sportradar_injuries`` returns to the reader."""
    return {
        "player_name": name,
        "position": position,
        "status": status,
        "description": reason,
    }


# ---------------------------------------------------------------------------
# is_soccer_league
# ---------------------------------------------------------------------------


def test_is_soccer_league_covers_every_traded_competition():
    expected = {"EPL", "LALIGA", "SERIEA", "BUNDES", "LIGUE1", "MLS", "UCL"}
    assert SOCCER_LEAGUES == expected
    for lg in expected:
        assert is_soccer_league(lg) is True
        assert is_soccer_league(lg.lower()) is True
    assert is_soccer_league("NFL") is False
    assert is_soccer_league("") is False
    assert is_soccer_league(None) is False


# ---------------------------------------------------------------------------
# Severity scoring
# ---------------------------------------------------------------------------


def test_lineup_severity_zero_with_no_missing_players():
    assert ctx_mod._compute_lineup_severity_pp([]) == 0.0


def test_lineup_severity_weights_goalkeeper_highest():
    """A missing GK is the single biggest single-player swing (~3pp)."""
    gk_only = ctx_mod._compute_lineup_severity_pp(
        [{"position": "G", "status": "missing"}]
    )
    fb_only = ctx_mod._compute_lineup_severity_pp(
        [{"position": "RB", "status": "missing"}]
    )
    assert gk_only > fb_only
    assert gk_only == pytest.approx(0.030)


def test_lineup_severity_doubtful_downweighted_vs_missing():
    out_row = [{"position": "F", "status": "missing"}]
    doubtful_row = [{"position": "F", "status": "doubtful"}]
    out_sev = ctx_mod._compute_lineup_severity_pp(out_row)
    doubt_sev = ctx_mod._compute_lineup_severity_pp(doubtful_row)
    assert out_sev > doubt_sev
    assert doubt_sev == pytest.approx(out_sev * 0.5)


def test_lineup_severity_caps_at_max_so_outage_does_not_runaway():
    huge = [
        {"position": pos, "status": "missing"}
        for pos in ("G", "F", "F", "M", "M", "D", "D", "D", "F", "M", "G")
    ]
    sev = ctx_mod._compute_lineup_severity_pp(huge)
    assert sev == pytest.approx(ctx_mod._MAX_LINEUP_SEVERITY_PP)


def test_lineup_severity_skips_unknown_status():
    """Status strings outside the notable set (e.g. 'active') must
    contribute zero — otherwise routine roster rows would inflate the
    severity for an otherwise healthy side."""
    sev = ctx_mod._compute_lineup_severity_pp(
        [
            {"position": "G", "status": "active"},
            {"position": "F", "status": "available"},
        ]
    )
    assert sev == 0.0


# ---------------------------------------------------------------------------
# fetch_lineup_status (DB readers)
# ---------------------------------------------------------------------------


def test_fetch_lineup_status_skips_non_soccer_league(monkeypatch):
    async def boom(*a, **kw):  # noqa: ANN001
        raise AssertionError("must not hit DB for non-soccer league")

    monkeypatch.setattr(ctx_mod.db, "fetch", boom)
    monkeypatch.setattr(ctx_mod.db, "fetchval", boom)
    assert asyncio.run(fetch_soccer_lineup_status("NFL", "Packers", "Lions")) is None


def test_fetch_lineup_status_db_failure_degrades(monkeypatch):
    """A DB outage on either side collapses to empty lists / zero
    severity rather than blocking the debate."""

    async def boom(*a, **kw):  # noqa: ANN001
        raise RuntimeError("DB down")

    monkeypatch.setattr(ctx_mod.db, "fetch", boom)
    monkeypatch.setattr(ctx_mod.db, "fetchval", boom)
    out = asyncio.run(fetch_soccer_lineup_status("EPL", "Arsenal", "Chelsea"))
    assert out is not None
    assert out["home"]["missing_players"] == []
    assert out["home"]["lineup_severity_pp"] == 0.0
    assert out["away"]["missing_players"] == []
    assert out["away"]["lineup_severity_pp"] == 0.0
    assert out["net_home_swing_pp"] == 0.0


def test_fetch_lineup_status_filters_out_active_players(monkeypatch):
    """Cached rows tagged 'active' must not show up as missing — that
    would make a healthy side look depleted."""
    _stub_soccer_db(
        monkeypatch,
        home_missing=[
            _missing_player("Bukayo Saka", "F", status="missing", reason="knee"),
            _missing_player("Reserve Man", "M", status="active", reason=""),
        ],
        away_missing=[],
    )
    out = asyncio.run(fetch_soccer_lineup_status("EPL", "Arsenal", "Chelsea"))
    assert out is not None
    home_missing = out["home"]["missing_players"]
    assert len(home_missing) == 1
    assert home_missing[0]["player"] == "Bukayo Saka"
    assert home_missing[0]["status"] == "missing"


def test_fetch_lineup_status_stamps_roster_size(monkeypatch):
    _stub_soccer_db(
        monkeypatch,
        home_missing=[],
        away_missing=[],
        home_roster_n=27,
        away_roster_n=24,
    )
    out = asyncio.run(fetch_soccer_lineup_status("EPL", "Arsenal", "Chelsea"))
    assert out["home"]["roster_size"] == 27
    assert out["away"]["roster_size"] == 24


# ---------------------------------------------------------------------------
# build_game_context
# ---------------------------------------------------------------------------


def test_build_soccer_game_context_returns_lineup_status(monkeypatch):
    _stub_soccer_db(
        monkeypatch,
        home_missing=[_missing_player("Saka", "F")],
        away_missing=[],
    )
    out = asyncio.run(
        build_soccer_game_context("EPL", "Arsenal", "Chelsea")
    )
    assert "lineup_status" in out
    assert out["lineup_status"]["home"]["team"] == "Arsenal"


def test_build_soccer_game_context_empty_for_non_soccer_league(monkeypatch):
    async def boom(*a, **kw):  # noqa: ANN001
        raise AssertionError("must not hit DB")

    monkeypatch.setattr(ctx_mod.db, "fetch", boom)
    monkeypatch.setattr(ctx_mod.db, "fetchval", boom)
    out = asyncio.run(build_soccer_game_context("NFL", "Packers", "Lions"))
    assert out == {}


# ---------------------------------------------------------------------------
# persona_context_augment renders the lineup_status block
# ---------------------------------------------------------------------------


def test_persona_context_renders_missing_players_and_severity():
    """The rendered prompt must surface per-side severity, the missing
    player list, and the explicit signed swing so the
    ``lineup_rotation`` persona has a numeric anchor."""
    aug = persona_context_augment(
        "EPL",
        game_context={
            "lineup_status": {
                "league": "EPL",
                "home": {
                    "team": "Arsenal",
                    "missing_players": [
                        {"player": "Bukayo Saka", "position": "F",
                         "status": "missing", "description": "knee"},
                        {"player": "David Raya", "position": "G",
                         "status": "missing", "description": "back"},
                    ],
                    "roster_size": 27,
                    "lineup_severity_pp": 0.05,
                },
                "away": {
                    "team": "Chelsea",
                    "missing_players": [],
                    "roster_size": 26,
                    "lineup_severity_pp": 0.0,
                },
                "net_home_swing_pp": -0.05,
            },
        },
    )
    assert "OBSERVED CONDITIONS" in aug
    assert "Arsenal" in aug and "Chelsea" in aug
    assert "MISSING G David Raya" in aug
    assert "MISSING F Bukayo Saka" in aug
    assert "lineup severity 5.0pp" in aug
    assert "lineup severity 0.0pp" in aug
    assert "squad depth 27" in aug
    # Net swing is rendered with sign + direction so the persona knows
    # which way to nudge the win share.
    assert "Net lineup swing" in aug
    assert "toward AWAY" in aug
    # Base SOCCER signals prose still present alongside the structured
    # block (back-compat with callers that don't pass game_context).
    assert "SOCCER-SPECIFIC SIGNALS" in aug


def test_persona_context_keeps_legacy_block_without_game_context():
    """No game_context kwarg → original prose-only block (back-compat).

    The legacy prose persona context mentions the phrase OBSERVED
    CONDITIONS as a directive ("when OBSERVED CONDITIONS carries..."),
    so we anchor on the actual rendered-block header to verify we
    haven't accidentally appended a structured render.
    """
    aug = persona_context_augment("EPL")
    assert "OBSERVED CONDITIONS for this matchup:" not in aug
    assert "SOCCER-SPECIFIC SIGNALS" in aug


# ---------------------------------------------------------------------------
# REGRESSION: meaningful EV adjustment when key players are missing
# ---------------------------------------------------------------------------


def test_lineup_severity_drives_meaningful_ev_adjustment():
    """A soccer candidate with several "missing" key players must
    produce a meaningfully different lineup signal than the same
    matchup with no injuries.

    "Meaningful" is concretely:
      * the depleted-side severity is at least 5 probability points,
      * the net home-vs-away swing is at least 5pp (≥1/2 the typical
        home-field edge in soccer, large enough that the moderator
        notices),
      * the prompt-facing OBSERVED CONDITIONS block actually surfaces
        the missing names (so the persona LLM can quote them when
        justifying the EV adjustment),
      * the no-injury baseline keeps net-swing at exactly 0pp.
    """
    healthy = asyncio.run(
        _build_lineup_only({"home_missing": [], "away_missing": []})
    )
    depleted = asyncio.run(
        _build_lineup_only({
            "home_missing": [
                _missing_player("Bukayo Saka", "F",
                                status="missing", reason="knee"),
                _missing_player("David Raya", "G",
                                status="missing", reason="back"),
                _missing_player("Martin Odegaard", "M",
                                status="missing", reason="ankle"),
            ],
            "away_missing": [],
        })
    )

    # Healthy baseline: zero on both sides.
    assert healthy["home"]["lineup_severity_pp"] == 0.0
    assert healthy["away"]["lineup_severity_pp"] == 0.0
    assert healthy["net_home_swing_pp"] == 0.0

    # Depleted home side: meaningful severity AND a meaningful net
    # swing toward the away team.
    home_sev = depleted["home"]["lineup_severity_pp"]
    net_swing = depleted["net_home_swing_pp"]
    assert home_sev >= 0.05, f"expected ≥5pp severity, got {home_sev}"
    assert abs(net_swing) >= 0.05, f"expected ≥5pp swing, got {net_swing}"
    assert net_swing < 0  # away is healthier → win-share drifts away

    # Prompt-facing rendering must surface the names so the persona's
    # reasoning can quote them.  This is the integration contract: the
    # numeric severity and the human-readable list both ride the same
    # OBSERVED CONDITIONS block.
    healthy_aug = persona_context_augment(
        "EPL", game_context={"lineup_status": healthy}
    )
    depleted_aug = persona_context_augment(
        "EPL", game_context={"lineup_status": depleted}
    )
    assert "MISSING G David Raya" in depleted_aug
    assert "MISSING F Bukayo Saka" in depleted_aug
    assert "MISSING M Martin Odegaard" in depleted_aug
    assert "David Raya" not in healthy_aug
    assert depleted_aug != healthy_aug


async def _build_lineup_only(payload: dict[str, Any]) -> dict[str, Any]:
    """Test helper that builds a lineup_status dict with a stubbed DB
    via monkeypatch-free injection — keeps the regression test focused
    on the severity contract rather than the DB plumbing."""

    async def fake_fetch_missing(league, team_name):
        if "Arsenal" in team_name:
            return [
                {
                    "player": r["player_name"],
                    "position": (r["position"] or "").upper(),
                    "status": (r["status"] or "missing").lower(),
                    "description": r["description"],
                }
                for r in payload["home_missing"]
            ]
        if "Chelsea" in team_name:
            return [
                {
                    "player": r["player_name"],
                    "position": (r["position"] or "").upper(),
                    "status": (r["status"] or "missing").lower(),
                    "description": r["description"],
                }
                for r in payload["away_missing"]
            ]
        return []

    async def fake_roster_size(league, team_name):
        return 26

    # Manually call the same path fetch_lineup_status takes — but with
    # the dependency-free stubs above so we don't need monkeypatch at
    # the module level.  This keeps the regression test self-contained.
    home_missing, away_missing, home_roster, away_roster = await asyncio.gather(
        fake_fetch_missing("EPL", "Arsenal FC"),
        fake_fetch_missing("EPL", "Chelsea FC"),
        fake_roster_size("EPL", "Arsenal FC"),
        fake_roster_size("EPL", "Chelsea FC"),
    )
    home_severity = ctx_mod._compute_lineup_severity_pp(home_missing)
    away_severity = ctx_mod._compute_lineup_severity_pp(away_missing)
    return {
        "league": "EPL",
        "home": {
            "team": "Arsenal",
            "missing_players": home_missing,
            "roster_size": home_roster,
            "lineup_severity_pp": round(home_severity, 4),
        },
        "away": {
            "team": "Chelsea",
            "missing_players": away_missing,
            "roster_size": away_roster,
            "lineup_severity_pp": round(away_severity, 4),
        },
        "net_home_swing_pp": round(away_severity - home_severity, 4),
    }


# ---------------------------------------------------------------------------
# Runner integration: soccer candidate triggers the soccer game_context
# build, the OBSERVED CONDITIONS block lands in the persona prompt, and
# the resulting lineup_status payload is stashed back on the candidate
# so `_persist_debate` can audit it.
# ---------------------------------------------------------------------------


def test_runner_attaches_soccer_lineup_context_to_prompt(monkeypatch):
    from app.swarm import runner

    async def fake_build_ctx(league, home, away):  # noqa: ANN001
        return {
            "lineup_status": {
                "league": (league or "").upper(),
                "home": {
                    "team": home or "",
                    "missing_players": [
                        {"player": "Bukayo Saka", "position": "F",
                         "status": "missing", "description": "knee"},
                    ],
                    "roster_size": 27,
                    "lineup_severity_pp": 0.020,
                },
                "away": {
                    "team": away or "",
                    "missing_players": [],
                    "roster_size": 26,
                    "lineup_severity_pp": 0.0,
                },
                "net_home_swing_pp": -0.020,
            }
        }

    captured: dict[str, Any] = {}

    async def fake_call_persona(persona, prompt):  # noqa: ANN001
        captured.setdefault("prompt", prompt)
        return {
            "persona": persona.id, "provider": persona.provider,
            "model": persona.model_id, "probability": 0.55,
            "confidence": 0.6, "lean": "YES", "reasoning": "ok",
            "raw_text": '{"approved":true,"recommended_action":"BUY_YES"}',
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
        return 7

    monkeypatch.setattr(
        "app.sports.build_soccer_game_context", fake_build_ctx
    )
    monkeypatch.setattr(runner, "_call_persona", fake_call_persona)
    monkeypatch.setattr(runner, "get_persona_weight", fake_weight)
    monkeypatch.setattr(runner, "_publish", fake_publish)
    monkeypatch.setattr(runner, "disabled_persona_ids", fake_disabled)
    monkeypatch.setattr(runner, "_persist_debate", fake_persist)

    out = asyncio.run(
        runner.run_debate({
            "game_id": "g-soccer-1",
            "matchup": "Arsenal vs Chelsea",
            "league": "EPL",
            "market_type": "moneyline_3way",
            "side": "HOME",
            "current_price": 0.45,
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "context": "edge=0.04",
            "vertical": "soccer",
        })
    )
    prompt = captured["prompt"]
    assert "OBSERVED CONDITIONS" in prompt
    assert "Arsenal" in prompt and "Chelsea" in prompt
    assert "MISSING F Bukayo Saka" in prompt
    assert "Net lineup swing" in prompt
    persisted = captured["persisted_candidate"]
    assert persisted.get("soccer_context", {}).get("lineup_status", {}) \
        .get("home", {}).get("team") == "Arsenal"
    assert out["debate_id"] == 7


def test_runner_skips_soccer_context_for_non_soccer_league(monkeypatch):
    """Non-soccer leagues must not invoke the soccer builder — keeps the
    NFL path pristine and avoids a wasted DB round-trip on every
    NFL/NBA/MLB candidate."""
    from app.swarm import runner

    async def boom_build(*a, **kw):  # noqa: ANN001
        raise AssertionError("must not call build_soccer_game_context for NFL")

    async def fake_call_persona(persona, prompt):  # noqa: ANN001
        return {
            "persona": persona.id, "provider": persona.provider,
            "model": persona.model_id, "probability": 0.55,
            "confidence": 0.6, "lean": "YES", "reasoning": "ok",
            "raw_text": '{"approved":true,"recommended_action":"BUY_YES"}',
            "input_tokens": 1, "output_tokens": 1,
            "cost": 0.0, "latency_ms": 1, "error": None,
            "primary_provider": persona.provider,
            "primary_model": persona.model_id,
            "fallback_used": False, "fallback_reason": None,
        }

    async def fake_nfl_ctx(home, away):  # noqa: ANN001
        return {}

    async def fake_weight(*a, **kw):  # noqa: ANN001
        return 1.0

    async def fake_publish(*a, **kw):  # noqa: ANN001
        return None

    async def fake_disabled(*a, **kw):  # noqa: ANN001
        return frozenset()

    async def fake_persist(**kw):  # noqa: ANN001
        return 1

    monkeypatch.setattr(
        "app.sports.build_soccer_game_context", boom_build
    )
    monkeypatch.setattr(
        "app.sports.build_nfl_game_context", fake_nfl_ctx
    )
    monkeypatch.setattr(runner, "_call_persona", fake_call_persona)
    monkeypatch.setattr(runner, "get_persona_weight", fake_weight)
    monkeypatch.setattr(runner, "_publish", fake_publish)
    monkeypatch.setattr(runner, "disabled_persona_ids", fake_disabled)
    monkeypatch.setattr(runner, "_persist_debate", fake_persist)

    asyncio.run(
        runner.run_debate({
            "game_id": "g-nfl-1",
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
