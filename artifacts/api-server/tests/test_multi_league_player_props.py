"""Task #829 — per-league player-props rollout invariants.

Locks in the four-vertical / four-calibrator-bucket / four-feed /
four-dashboard-route registration so a future refactor can't
silently drop a league or change its calibrator key without an
explicit test update.

These are static-registration tests; no DB connection required.
"""
from __future__ import annotations

import pytest

from app import calibrators
from app.player_props import (
    LEAGUE_SPECS,
    SUPPORTED_STATS,
    calibrator_buckets,
    compute_adjustment,
    get_league_spec,
    get_vertical_spec,
)
from app.player_props.matchup import _PER_COMPONENT_CAP
from app.sports.leagues import LEAGUE_CONFIGS
from app.v1_archive.league_inference import (
    SUPPORTED_LEAGUES as ARCHIVE_SUPPORTED,
)
from app.verticals.builtins import REGISTRY


_NEW_LEAGUES = ("NBA", "NHL", "MLB", "NFL")
_NEW_VERTICALS = (
    "nba_player_props",
    "nhl_player_props",
    "mlb_player_props",
    "nfl_player_props",
)
_NEW_BUCKETS = ("NBA_PROPS", "NHL_PROPS", "MLB_PROPS", "NFL_PROPS")


@pytest.mark.parametrize("league", _NEW_LEAGUES)
def test_league_spec_registered(league: str) -> None:
    spec = get_league_spec(league)
    assert spec is not None, f"missing league spec for {league}"
    assert spec.calibrator_bucket == f"{league}_PROPS"
    assert spec.vertical == f"{league.lower()}_player_props"
    assert len(spec.markets) >= 5
    assert all(m in SUPPORTED_STATS for m in spec.markets)


@pytest.mark.parametrize("vertical", _NEW_VERTICALS)
def test_vertical_registered_and_disabled(vertical: str) -> None:
    spec = REGISTRY.get(vertical)
    assert spec is not None, f"vertical {vertical} not registered"
    assert spec.enabled is False, (
        f"{vertical} must ship disabled (task #829)"
    )
    assert spec.predictor_hook == vertical


@pytest.mark.parametrize("bucket", _NEW_BUCKETS)
def test_calibrator_bucket_registered(bucket: str) -> None:
    assert bucket in calibrators.SUPPORTED_LEAGUES
    assert bucket in ARCHIVE_SUPPORTED


def test_archive_mirror_matches_calibrators() -> None:
    """Drift guard: V1-archive league_inference mirror must equal the
    canonical calibrator set (otherwise the inference cascade silently
    drops the new buckets)."""
    assert ARCHIVE_SUPPORTED == frozenset(calibrators.SUPPORTED_LEAGUES)


@pytest.mark.parametrize("bucket", _NEW_BUCKETS)
def test_league_config_table_covers_new_buckets(bucket: str) -> None:
    cfg = LEAGUE_CONFIGS.get(bucket)
    assert cfg is not None, (
        f"LEAGUE_CONFIGS missing pseudo-league {bucket} — risk gates"
        f" cannot prioritise / size player-prop trades without it"
    )
    assert cfg.allocation_pct > 0
    assert cfg.per_game_cap_pct > 0


def test_calibrator_buckets_helper_matches_registry() -> None:
    assert set(calibrator_buckets()) == set(_NEW_BUCKETS)


def test_best_line_excludes_player_props_by_construction() -> None:
    """Cross-book best_line gate is scoped to vertical='sports' so
    player-prop verticals are auto-excluded.  Lock the convention
    here so a future broadening of best_line's scope must update
    this test."""
    for vertical in _NEW_VERTICALS:
        spec = REGISTRY.get(vertical)
        assert spec is not None
        # Player-prop verticals are NOT the generic 'sports' vertical
        # — keeping the name distinct is what causes the gate to skip.
        assert spec.name != "sports"


def test_get_vertical_spec_roundtrip() -> None:
    for vertical in _NEW_VERTICALS:
        spec = get_vertical_spec(vertical)
        assert spec is not None
        assert spec.vertical == vertical


def test_compute_adjustment_bounded() -> None:
    """Bounded matchup adjustment respects ±25% combined cap even when
    pace + opp-allowed both push the same direction."""
    # Synthesise opp-allowed 50% above league mean + pace 50% above.
    opp_rows = [{"points": 60.0} for _ in range(10)]
    league_rows = [{"points": 40.0} for _ in range(50)]
    adj = compute_adjustment(
        stat="points",
        opponent_rows=opp_rows,
        league_rows=league_rows,
        pace_estimate=150.0,
        league_pace=100.0,
        pace_fallback=100.0,
    )
    # Combined factor must respect the 25% cap (default).
    assert 0.75 <= adj.factor <= 1.25
    assert adj.bounded is True
    # Each component must respect the per-component 35% cap.
    assert (1 - _PER_COMPONENT_CAP) <= adj.pace_factor <= (1 + _PER_COMPONENT_CAP)
    assert (1 - _PER_COMPONENT_CAP) <= adj.opp_allowed_factor <= (1 + _PER_COMPONENT_CAP)


def test_supported_stats_covers_every_league_market() -> None:
    """Every market every league prices must be in SUPPORTED_STATS."""
    for spec in LEAGUE_SPECS.values():
        for m in spec.markets:
            assert m in SUPPORTED_STATS, (
                f"{spec.league} market {m!r} missing from "
                f"SUPPORTED_STATS"
            )


def test_inactive_window_minutes_per_league() -> None:
    """Generalised inactive-list cooldown: NFL keeps its 90-min
    window from task #823; NBA stays at 30; NHL/MLB at 60."""
    assert get_league_spec("NFL").inactive_window_minutes == 90
    assert get_league_spec("NBA").inactive_window_minutes == 30
    assert get_league_spec("NHL").inactive_window_minutes == 60
    assert get_league_spec("MLB").inactive_window_minutes == 60
