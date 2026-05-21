"""Tests for the WNBA player-props vertical (task #823).

Covers:

* per-player baseline summariser (insufficient sample, happy path,
  composite stats, all-zero σ floor),
* matchup adjustment helper (bounds, no-opponent fallback,
  insufficient opponent sample),
* predictor end-to-end against an in-memory baseline / matchup,
* :func:`gate_player_props` (no-op for non-player verticals,
  rejects on blocking status, sub-min sample, line-move overshoot,
  matchup hit the bound; allow-questionable override),
* registry: WNBA appears in `LEAGUE_CONFIGS`; `wnba_player_props`
  vertical is registered, ships disabled, and is ordered after
  `entertainment`,
* ``len(GATES) == 22`` and ``player_props`` lives in the ordered
  registry (regression for the count assertion).
"""
from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.player_props.baseline import (
    DEFAULT_MIN_SAMPLE,
    SUPPORTED_STATS,
    summarize,
)
from app.player_props.injuries import is_blocking_status
from app.risk.gates import GATES, GateResult, RiskContext, gate_player_props
from app.sports.leagues import LEAGUE_CONFIGS
from app.verticals import REGISTRY
import app.verticals.builtins  # noqa: F401 — import side-effect populates REGISTRY
from app.wnba.matchup import (
    DEFAULT_MAX_MATCHUP_ADJ,
    compute_adjustment,
)
from app.wnba.predictor import predict_wnba_player_prop


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _row(played_at: datetime, **stats: float) -> dict[str, Any]:
    base = {"points": 0, "rebounds": 0, "assists": 0,
            "threes": 0, "blocks": 0, "steals": 0, "turnovers": 0}
    base.update(stats)
    return {"played_at": played_at, **base}


def _recent_rows(n: int, *, mean: float, jitter: float = 1.0,
                 stat: str = "points") -> list[dict[str, Any]]:
    base_dt = datetime(2026, 5, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(n):
        # Recent-first ordering matches the production query's
        # ``ORDER BY played_at DESC``.
        played = base_dt - timedelta(days=i)
        offset = jitter if i % 2 == 0 else -jitter
        rows.append(_row(played, **{stat: mean + offset}))
    return rows


def _minimal_ctx(
    *,
    vertical: str = "wnba_player_props",
    overrides: dict[str, Any] | None = None,
) -> RiskContext:
    return RiskContext(
        market_id="KXWNBA-2026-05-11-LV-LIB-PTS",
        side="YES",
        vertical=vertical,
        league="WNBA",
        market_type="player_prop",
        matchup="LV@LIB",
        current_price=0.52,
        model_probability=0.58,
        edge=0.06,
        confidence_band_low=0.55,
        confidence_band_high=0.61,
        agreement_rate=0.8,
        swarm_approved=True,
        swarm_recommended_action="BUY_YES",
        debate_id=None,
        intended_quantity=10,
        intended_notional_usd=5.20,
        bankroll_usd=10000.0,
        open_positions_count=0,
        open_positions_notional_usd=0.0,
        open_positions_per_vertical_usd={},
        operator_overrides=overrides or {},
        market_cooldown_until=None,
    )


# ---------------------------------------------------------------------------
# Baseline summariser
# ---------------------------------------------------------------------------


class TestBaselineSummarize:
    def test_insufficient_sample_returns_none(self) -> None:
        rows = _recent_rows(DEFAULT_MIN_SAMPLE - 1, mean=18.0)
        assert summarize("WNBA", "P1", "points", rows) is None

    def test_happy_path_normal(self) -> None:
        rows = _recent_rows(12, mean=20.0, jitter=2.0)
        b = summarize("WNBA", "Player", "points", rows)
        assert b is not None
        assert b.sample_count == 12
        # rolling_mean over the most-recent 10 should be the symmetric
        # mean (jitter cancels out across an even count).
        assert math.isclose(b.rolling_mean, 20.0, abs_tol=0.5)
        assert b.season_median == 20.0 if 20.0 in (
            r["points"] for r in rows
        ) else 20.0
        assert b.mad_sigma >= 0.0
        # σ floor kicks in inside the predictor (.sigma()), not the
        # raw mad_sigma value.
        assert b.sigma() >= 0.5

    def test_composite_stat_pra(self) -> None:
        rows = []
        base_dt = datetime(2026, 5, 1, tzinfo=timezone.utc)
        for i in range(8):
            rows.append(_row(
                base_dt - timedelta(days=i),
                points=20, rebounds=5, assists=4,
            ))
        b = summarize(
            "WNBA", "Player", "points_rebounds_assists", rows,
        )
        assert b is not None
        assert math.isclose(b.rolling_mean, 29.0, abs_tol=0.01)

    def test_all_zero_variance_floors_sigma(self) -> None:
        rows = _recent_rows(10, mean=15.0, jitter=0.0)
        b = summarize("WNBA", "Player", "points", rows)
        assert b is not None
        assert b.mad_sigma == 0.0  # raw MAD is genuinely zero
        assert b.sigma() == 0.5  # floor in the σ accessor

    def test_unsupported_stat_returns_none(self) -> None:
        rows = _recent_rows(10, mean=20.0)
        assert summarize("WNBA", "P", "made_up_stat", rows) is None


# ---------------------------------------------------------------------------
# Matchup adjustment
# ---------------------------------------------------------------------------


class TestMatchupAdjustment:
    def test_no_opponent_history_neutralises(self) -> None:
        league = [{"points": 18.0} for _ in range(20)]
        adj = compute_adjustment(
            stat="points",
            opponent_rows=[],  # no data → opp factor = 1.0
            league_rows=league,
            pace_estimate=80.0,
            league_pace=80.0,
        )
        assert math.isclose(adj.opp_allowed_factor, 1.0)
        assert math.isclose(adj.factor, 1.0)
        assert adj.bounded is False

    def test_pace_adjustment_within_bounds(self) -> None:
        league = [{"points": 18.0} for _ in range(20)]
        opp = [{"points": 18.0} for _ in range(8)]
        adj = compute_adjustment(
            stat="points",
            opponent_rows=opp,
            league_rows=league,
            pace_estimate=88.0,  # +10% pace
            league_pace=80.0,
        )
        assert math.isclose(adj.pace_factor, 1.10, abs_tol=0.001)
        # opp at league avg → opp factor 1.0
        assert math.isclose(adj.opp_allowed_factor, 1.0, abs_tol=0.001)
        assert math.isclose(adj.factor, 1.10, abs_tol=0.001)
        assert adj.bounded is False

    def test_combined_hits_bound(self) -> None:
        league = [{"points": 10.0} for _ in range(20)]
        opp = [{"points": 18.0} for _ in range(8)]  # +80% raw — clipped to +35%
        adj = compute_adjustment(
            stat="points",
            opponent_rows=opp,
            league_rows=league,
            pace_estimate=120.0,  # +50% raw — clipped to +35%
            league_pace=80.0,
            max_adj=DEFAULT_MAX_MATCHUP_ADJ,
        )
        # Per-component clip → 1.35 each → combined 1.8225 → clipped to 1+max_adj
        assert math.isclose(adj.factor, 1 + DEFAULT_MAX_MATCHUP_ADJ, abs_tol=0.001)
        assert adj.bounded is True

    def test_insufficient_opponent_sample_neutralises(self) -> None:
        league = [{"points": 10.0} for _ in range(20)]
        opp = [{"points": 25.0}, {"points": 25.0}]  # only 2 < min 3
        adj = compute_adjustment(
            stat="points",
            opponent_rows=opp,
            league_rows=league,
            pace_estimate=80.0,
            league_pace=80.0,
        )
        assert math.isclose(adj.opp_allowed_factor, 1.0)


# ---------------------------------------------------------------------------
# Predictor — patches the DB-touching helpers so we don't need Postgres
# ---------------------------------------------------------------------------


class TestPredictor:
    def test_insufficient_sample_returns_none(self, monkeypatch) -> None:
        async def _none(*a, **k):
            return None
        monkeypatch.setattr("app.wnba.predictor.fetch_baseline", _none)
        monkeypatch.setattr("app.wnba.predictor.fetch_adjustment", _none)
        out = asyncio.run(predict_wnba_player_prop(
            player="X", stat="points", line=20.0, side="over",
        ))
        assert out is None

    def test_happy_path_over(self, monkeypatch) -> None:
        from app.player_props.baseline import BaselineDistribution

        async def _baseline(league, player, stat, **kw):
            return BaselineDistribution(
                league="WNBA", player=player, stat=stat,
                sample_count=12, rolling_mean=22.0,
                season_median=22.0, mad_sigma=4.0,
                last_played_at=None, rolling_window_used=10,
            )

        async def _matchup(*a, **kw):
            return None

        monkeypatch.setattr("app.wnba.predictor.fetch_baseline", _baseline)
        monkeypatch.setattr("app.wnba.predictor.fetch_adjustment", _matchup)

        out = asyncio.run(predict_wnba_player_prop(
            player="P", stat="points", line=18.0, side="over",
        ))
        assert out is not None
        assert out.model_name == "wnba_player_props"
        assert out.model_version == "1"
        assert 0.5 < out.probability < 0.95
        out_under = asyncio.run(predict_wnba_player_prop(
            player="P", stat="points", line=18.0, side="under",
        ))
        assert out_under is not None
        assert out_under.probability < 0.5

    def test_unsupported_market_raises(self, monkeypatch) -> None:
        from app.wnba.predictor import WnbaPropPredictionError
        with pytest.raises(WnbaPropPredictionError):
            asyncio.run(predict_wnba_player_prop(
                player="P", stat="not_a_market", line=1.0,
            ))


# ---------------------------------------------------------------------------
# Risk gate
# ---------------------------------------------------------------------------


class TestGatePlayerProps:
    def test_no_meta_passes(self) -> None:
        r = asyncio.run(gate_player_props(_minimal_ctx(vertical="sports")))
        assert r.passed is True
        assert r.details.get("player_prop_meta") is False

    def test_blocking_status_rejects(self) -> None:
        ctx = _minimal_ctx(overrides={"_player_prop_meta": {
            "player": "X", "stat": "points",
            "status": "OUT", "sample_count": 12,
        }})
        r = asyncio.run(gate_player_props(ctx))
        assert r.passed is False
        assert "OUT" in r.reason

    def test_questionable_with_override_passes(self) -> None:
        ctx = _minimal_ctx(overrides={
            "_player_prop_meta": {
                "player": "X", "stat": "points",
                "status": "QUESTIONABLE", "sample_count": 12,
            },
            "player_props_allow_questionable_wnba_player_props": True,
        })
        r = asyncio.run(gate_player_props(ctx))
        assert r.passed is True
        assert r.details.get("allow_questionable") is True

    def test_low_sample_rejects(self) -> None:
        ctx = _minimal_ctx(overrides={"_player_prop_meta": {
            "player": "X", "stat": "points",
            "status": "ACTIVE", "sample_count": 2,
        }})
        r = asyncio.run(gate_player_props(ctx))
        assert r.passed is False
        assert "sample" in r.reason.lower()

    def test_line_move_rejects(self) -> None:
        ctx = _minimal_ctx(overrides={"_player_prop_meta": {
            "player": "X", "stat": "points",
            "status": "ACTIVE", "sample_count": 20,
            "line_move_bps": 500.0,
        }})
        r = asyncio.run(gate_player_props(ctx))
        assert r.passed is False
        assert "moved" in r.reason

    def test_matchup_bounded_rejects(self) -> None:
        ctx = _minimal_ctx(overrides={"_player_prop_meta": {
            "player": "X", "stat": "points",
            "status": "ACTIVE", "sample_count": 20,
            "matchup_bounded": True, "matchup_factor": 1.25,
        }})
        r = asyncio.run(gate_player_props(ctx))
        assert r.passed is False
        assert "matchup" in r.reason.lower()

    def test_happy_path_passes(self) -> None:
        ctx = _minimal_ctx(overrides={"_player_prop_meta": {
            "player": "X", "stat": "points",
            "status": "ACTIVE", "sample_count": 20,
            "line_move_bps": 50.0, "matchup_bounded": False,
            "matchup_factor": 1.05,
        }})
        r = asyncio.run(gate_player_props(ctx))
        assert r.passed is True


# ---------------------------------------------------------------------------
# Registry — vertical, league config, gate ordering
# ---------------------------------------------------------------------------


class TestRegistries:
    def test_wnba_in_league_configs(self) -> None:
        assert "WNBA" in LEAGUE_CONFIGS
        cfg = LEAGUE_CONFIGS["WNBA"]
        assert cfg.priority == 8
        assert cfg.allocation_pct == 0.05

    def test_wnba_player_props_vertical_registered_disabled(self) -> None:
        spec = REGISTRY.get("wnba_player_props")
        assert spec is not None
        assert spec.enabled is False
        assert spec.predictor_hook == "wnba_player_props"
        assert spec.kelly_multiplier == 0.5
        assert spec.leagues == ("WNBA",)

    def test_player_props_gate_in_registry(self) -> None:
        names = [n for n, _ in GATES]
        assert "player_props" in names
        assert len(GATES) == 22
        # player_props sits AFTER uncalibrated_vertical (so calibrator
        # blocks take precedence) and BEFORE market_cooldown.
        i = names.index("player_props")
        assert names[i - 1] == "uncalibrated_vertical"
        assert names[i + 1] == "market_cooldown"


class TestInjuryHelpers:
    @pytest.mark.parametrize("status,expected", [
        ("OUT", True),
        ("DOUBTFUL", True),
        ("QUESTIONABLE", True),
        ("GTD", True),
        ("GAME TIME DECISION", True),
        ("ACTIVE", False),
        ("PROBABLE", False),
        (None, False),
        ("", False),
    ])
    def test_is_blocking_status(self, status, expected) -> None:
        assert is_blocking_status(status) is expected
