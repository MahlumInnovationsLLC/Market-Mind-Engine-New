"""End-to-end coverage for the international soccer trading vertical
(task #196).

Hermetic — no network, no DB.  Mirrors `test_nfl_vertical.py` so a
future vertical can use the same template.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from app import verticals
from app.calibrators import SUPPORTED_LEAGUES
from app.feeds import odds_api
from app.risk.gates import (
    GATES,
    RiskContext,
    gate_per_vertical_cap,
)
from app.sports import (
    LEAGUE_CONFIGS,
    get_league_config,
    persona_context_augment,
)
from app.swarm.profiles import PROFILES, SOCCER_PERSONAS, get_profile


SOCCER_LEAGUES = ("EPL", "LALIGA", "SERIEA", "BUNDES", "LIGUE1", "MLS", "UCL")


# ---------------------------------------------------------------------------
# Spec-anchored constants
# ---------------------------------------------------------------------------


def test_default_odds_sports_default_includes_all_soccer():
    """Every spec-listed soccer league must be in the env-unset default
    so The-Odds-API poller pulls 3-way h2h moneylines without manual
    operator override."""
    default = os.environ.get(
        "AXIOM_ODDS_SPORTS",
        "basketball_nba,baseball_mlb,icehockey_nhl,basketball_ncaab,"
        "americanfootball_nfl,soccer_epl,soccer_spain_la_liga,"
        "soccer_italy_serie_a,soccer_germany_bundesliga,"
        "soccer_france_ligue_one,soccer_usa_mls,soccer_uefa_champs_league",
    ).split(",")
    expected = {
        "soccer_epl", "soccer_spain_la_liga", "soccer_italy_serie_a",
        "soccer_germany_bundesliga", "soccer_france_ligue_one",
        "soccer_usa_mls", "soccer_uefa_champs_league",
    }
    assert expected.issubset(set(default))


def test_calibrator_supports_every_soccer_league():
    """Per-league calibrator registry must list every soccer league
    so refit_all() trains and load_all() picks up artifacts at boot."""
    for lg in SOCCER_LEAGUES:
        assert lg in SUPPORTED_LEAGUES, f"calibrator missing {lg}"


def test_sportradar_schedule_paths_include_every_soccer_league():
    """Sportradar soccer schedule paths must be wired so the daily
    poller can fetch fixtures for each competition."""
    for sport in ("epl", "laliga", "seriea", "bundes", "ligue1", "mls", "ucl"):
        assert sport in odds_api._SPORTRADAR_SCHEDULE_PATHS
        assert "tournament" in odds_api._SPORTRADAR_SCHEDULE_PATHS[sport]


def test_sportradar_league_map_round_trips_soccer_codes():
    for sport, league in (
        ("epl", "EPL"), ("laliga", "LALIGA"), ("seriea", "SERIEA"),
        ("bundes", "BUNDES"), ("ligue1", "LIGUE1"), ("mls", "MLS"),
        ("ucl", "UCL"),
    ):
        assert odds_api._SPORTRADAR_LEAGUE.get(sport) == league


# ---------------------------------------------------------------------------
# League configs — 3-way market shape + per-league caps
# ---------------------------------------------------------------------------


def test_every_soccer_league_has_three_way_market_flag():
    for lg in SOCCER_LEAGUES:
        cfg = get_league_config(lg)
        assert cfg is not None, f"missing LeagueConfig for {lg}"
        assert cfg.three_way_market is True, (
            f"{lg} must be flagged 3-way (home/draw/away)"
        )


def test_na_leagues_are_two_way():
    for lg in ("NFL", "NBA", "MLB", "NHL", "NCAAB", "NCAAF"):
        cfg = get_league_config(lg)
        assert cfg is not None
        assert cfg.three_way_market is False, (
            f"{lg} must remain 2-way moneyline"
        )


def test_epl_priority_and_allocation():
    """Per task #196 spec: EPL priority=5, allocation=3%, 10 trades/day."""
    cfg = get_league_config("epl")
    assert cfg is not None
    assert cfg.league == "EPL"
    assert cfg.priority == 5
    assert cfg.allocation_pct == pytest.approx(0.03)
    assert cfg.per_game_cap_pct == pytest.approx(0.01)
    assert cfg.max_daily_trades == 10


def test_top4_european_leagues_use_2pct_allocation():
    """La Liga / Serie A / Bundesliga / Ligue 1 ship at 2% — half of
    EPL — because liquidity is thinner outside England."""
    for lg in ("LALIGA", "SERIEA", "BUNDES", "LIGUE1"):
        cfg = get_league_config(lg)
        assert cfg is not None
        assert cfg.allocation_pct == pytest.approx(0.02), (
            f"{lg} allocation should be 2%, got {cfg.allocation_pct}"
        )
        assert cfg.max_daily_trades == 8


def test_ucl_is_lowest_allocation_highest_priority():
    """UEFA Champions League: 1% / priority=4 — high signal but
    inherently small slate (~8 fixtures matchnight)."""
    cfg = get_league_config("ucl")
    assert cfg is not None
    assert cfg.allocation_pct == pytest.approx(0.01)
    assert cfg.priority == 4
    assert cfg.max_daily_trades == 6


def test_persona_context_carries_soccer_signals():
    aug = persona_context_augment("EPL")
    lower = aug.lower()
    assert "3-way" in lower or "draw" in lower
    assert "xg" in lower
    assert "lineup" in lower or "rotation" in lower
    # Same context applies to every soccer league.
    for lg in SOCCER_LEAGUES:
        assert persona_context_augment(lg) == aug


# ---------------------------------------------------------------------------
# Persona profile
# ---------------------------------------------------------------------------


def test_soccer_profile_registered_with_5_personas():
    assert "soccer" in PROFILES
    profile = get_profile("soccer")
    assert profile.name == "soccer"
    assert len(profile.personas) == 5


def test_soccer_profile_persona_ids_match_spec():
    ids = set(p.id for p in SOCCER_PERSONAS)
    assert ids == {
        "xg_modeler",
        "lineup_rotation",
        "tactical_matchup",
        "market_microstructure",
        "risk_guardian",
    }


def test_soccer_profile_provider_mix_balanced():
    """Provider failures must not blackout the swarm — at least one
    OpenAI and one Anthropic persona in the soccer stack."""
    providers = {p.provider for p in SOCCER_PERSONAS}
    assert "openai" in providers
    assert "anthropic" in providers


def test_soccer_personas_mention_three_way_and_xg_in_prompts():
    """Verbatim contract: prompts must instruct personas to reason
    about 3-way outcomes + xG so the swarm trades the right shape."""
    prompts = " ".join(p.system_prompt for p in SOCCER_PERSONAS)
    lower = prompts.lower()
    assert "3" in lower and "outcome" in lower
    assert "xg" in lower
    assert "draw" in lower


# ---------------------------------------------------------------------------
# Vertical registry
# ---------------------------------------------------------------------------


def test_soccer_vertical_registered():
    assert verticals.has("soccer")
    spec = verticals.require_spec("soccer")
    assert spec.persona_profile == "soccer"
    assert set(spec.leagues) == set(SOCCER_LEAGUES)


def test_soccer_ticker_prefixes_cover_every_league():
    spec = verticals.require_spec("soccer")
    expected = {
        "KXEPL", "KXLALIGA", "KXSERIEA", "KXBUNDES",
        "KXLIGUE1", "KXMLS", "KXUCL",
    }
    assert expected.issubset(set(spec.ticker_prefixes))


def test_soccer_vertical_routes_through_soccer_persona_profile():
    """The runner reads `verticals.persona_profile_for(...)` to pick
    which persona stack to run for a candidate's vertical."""
    assert verticals.persona_profile_for("soccer") == "soccer"


def test_legacy_sports_vertical_unchanged():
    """Adding the soccer vertical must NOT silently rewrite the
    legacy sports spec — its leagues / persona_profile are pinned by
    test_verticals_registry's baseline fixture."""
    spec = verticals.require_spec("sports")
    assert spec.persona_profile == "sports"
    assert spec.leagues == ()


# ---------------------------------------------------------------------------
# Risk gates — per-league cap + max_daily_trades sub-check
# ---------------------------------------------------------------------------


def _ctx(**over) -> RiskContext:
    base = dict(
        market_id="KXEPL-26APR25LIVMCI-LIV",
        side="YES",
        vertical="soccer",
        league="EPL",
        market_type="moneyline",
        matchup="Liverpool vs Manchester City",
        current_price=0.50,
        model_probability=0.60,
        edge=0.10,
        confidence_band_low=0.55,
        confidence_band_high=0.65,
        agreement_rate=0.66,
        swarm_approved=True,
        swarm_recommended_action="BUY_YES",
        debate_id=42,
        intended_quantity=10,
        intended_notional_usd=5.0,
        bankroll_usd=10_000.0,
        open_positions_count=0,
        open_positions_notional_usd=0.0,
        open_positions_per_vertical_usd={},
        operator_overrides={},
        market_cooldown_until=None,
    )
    base.update(over)
    return RiskContext(**base)


def test_gate_max_daily_trades_blocks_at_cap():
    """EPL ships at 10 trades/day — the 11th candidate must be rejected."""
    ctx = _ctx(trades_today_per_league={"EPL": 10})
    res = asyncio.run(gate_per_vertical_cap(ctx))
    assert res.passed is False
    assert res.details.get("sub_check") == "max_daily_trades"
    assert res.details["cap"] == 10
    assert res.details["trades_today"] == 10


def test_gate_max_daily_trades_passes_below_cap():
    ctx = _ctx(trades_today_per_league={"EPL": 9})
    res = asyncio.run(gate_per_vertical_cap(ctx))
    assert res.passed is True


def test_gate_max_daily_trades_operator_override_wins():
    """Operator can tighten/loosen the per-league daily cap — override
    key mirrors the per_league_cap_pct / per_game_cap_pct convention."""
    ctx = _ctx(
        trades_today_per_league={"EPL": 5},
        operator_overrides={"max_daily_trades.epl": 3},
    )
    res = asyncio.run(gate_per_vertical_cap(ctx))
    assert res.passed is False
    assert res.details["cap"] == 3


def test_gate_max_daily_trades_no_op_when_unset():
    """A league without `max_daily_trades` (NBA / MLB / etc) must not
    trip even with arbitrarily many trades booked today."""
    ctx = _ctx(
        league="NBA",
        market_id="KXNBA-1",
        matchup="Lakers vs Celtics",
        trades_today_per_league={"NBA": 9_999},
    )
    res = asyncio.run(gate_per_vertical_cap(ctx))
    assert res.passed is True


def test_gate_per_league_cap_enforces_3pct_for_epl():
    """EPL cap = 3% of $10k = $300.  Already $290 open, +$30 trips."""
    ctx = _ctx(
        intended_notional_usd=30.0,
        open_positions_per_league_usd={"EPL": 290.0},
    )
    res = asyncio.run(gate_per_vertical_cap(ctx))
    assert res.passed is False
    assert res.details.get("sub_check") == "per_league_cap"
    assert res.details["cap_usd"] == pytest.approx(300.0)


def test_gate_per_league_cap_enforces_2pct_for_laliga():
    """La Liga cap = 2% of $10k = $200.  Already $190, +$30 trips."""
    ctx = _ctx(
        league="LALIGA",
        market_id="KXLALIGA-1",
        matchup="Real Madrid vs Barcelona",
        intended_notional_usd=30.0,
        open_positions_per_league_usd={"LALIGA": 190.0},
    )
    res = asyncio.run(gate_per_vertical_cap(ctx))
    assert res.passed is False
    assert res.details["cap_usd"] == pytest.approx(200.0)


def test_gate_per_league_cap_enforces_1pct_for_ucl():
    """UCL cap = 1% of $10k = $100.  Already $90, +$30 trips."""
    ctx = _ctx(
        league="UCL",
        market_id="KXUCL-1",
        matchup="Arsenal vs Bayern Munich",
        intended_notional_usd=30.0,
        open_positions_per_league_usd={"UCL": 90.0},
    )
    res = asyncio.run(gate_per_vertical_cap(ctx))
    assert res.passed is False
    assert res.details["cap_usd"] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# 12-gate invariant + LeagueConfig coverage
# ---------------------------------------------------------------------------


def test_gate_count_invariant_preserved():
    """Spec requires the soccer daily-trade sub-check stay bundled
    inside per_vertical_cap rather than adding a new gate.  Cross-
    vertical risk plumbing is the only thing allowed to grow this
    number (14 → 17 in task #730: sharpe_ratio + ruin_monte_carlo +
    segment_throttler audit gates)."""
    assert len(GATES) == 22


def test_league_configs_table_covers_every_supported_league():
    """Drift guard: every calibrator-listed league must have a config."""
    for lg in SUPPORTED_LEAGUES:
        assert lg in LEAGUE_CONFIGS, f"missing LeagueConfig for {lg}"


# ---------------------------------------------------------------------------
# Sportradar soccer-extended payload normalizer (task #196)
# ---------------------------------------------------------------------------


def test_normalize_sportradar_payload_filters_by_competition():
    """A single soccer-extended global daily endpoint feeds 7 per-league
    pollers; each must keep ONLY rows whose tournament matches its
    competition or we'd cross-attribute EPL events to MLS, etc.
    """
    from app.feeds.odds_api import _normalize_sportradar_payload

    payload = {
        "schedules": [
            {
                "sport_event": {
                    "id": "sr:sport_event:111",
                    "start_time": "2026-04-22T15:00:00Z",
                    "tournament": {"id": "sr:tournament:17", "name": "Premier League"},
                    "competitors": [
                        {"qualifier": "home", "name": "Arsenal"},
                        {"qualifier": "away", "name": "Chelsea"},
                    ],
                },
                "sport_event_status": {"status": "scheduled"},
            },
            {
                "sport_event": {
                    "id": "sr:sport_event:222",
                    "start_time": "2026-04-22T18:30:00Z",
                    "tournament": {"id": "sr:tournament:35", "name": "Bundesliga"},
                    "competitors": [
                        {"qualifier": "home", "name": "Bayern Munich"},
                        {"qualifier": "away", "name": "Borussia Dortmund"},
                    ],
                },
                "sport_event_status": {
                    "status": "closed", "home_score": 2, "away_score": 1,
                },
            },
        ]
    }

    epl_rows = _normalize_sportradar_payload("epl", "EPL", payload)
    assert len(epl_rows) == 1
    assert epl_rows[0]["id"] == "sr:sport_event:111"
    assert epl_rows[0]["home"]["name"] == "Arsenal"
    assert epl_rows[0]["away"]["name"] == "Chelsea"

    bundes_rows = _normalize_sportradar_payload("bundes", "BUNDES", payload)
    assert len(bundes_rows) == 1
    assert bundes_rows[0]["id"] == "sr:sport_event:222"
    assert bundes_rows[0]["status"] == "closed"
    assert bundes_rows[0]["home_points"] == 2
    assert bundes_rows[0]["away_points"] == 1

    # MLS shares the global feed but has no rows in this payload.
    mls_rows = _normalize_sportradar_payload("mls", "MLS", payload)
    assert mls_rows == []


def test_normalize_sportradar_payload_preserves_na_league_shape():
    """Adding the soccer branch must not regress NA-league parsing."""
    from app.feeds.odds_api import _normalize_sportradar_payload

    nba_payload = {"games": [
        {"id": "g_a", "home": {"name": "Lakers"}, "away": {"name": "Celtics"},
         "scheduled": "2026-04-22T19:00:00Z", "status": "scheduled"},
    ]}
    rows = _normalize_sportradar_payload("nba", "NBA", nba_payload)
    assert len(rows) == 1 and rows[0]["id"] == "g_a"

    nfl_payload = {"week": {"games": [
        {"id": "g_b", "home": {"name": "Chiefs"}, "away": {"name": "49ers"},
         "scheduled": "2026-04-22T20:00:00Z", "status": "scheduled"},
    ]}}
    rows = _normalize_sportradar_payload("nfl", "NFL", nfl_payload)
    assert len(rows) == 1 and rows[0]["id"] == "g_b"
