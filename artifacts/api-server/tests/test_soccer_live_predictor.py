"""Tests for the soccer in-game live predictor (task #857).

Covers:
  * Feature builder degrades cleanly when there's no live_state row
    (predictor falls back to the pre-game prior).
  * The blend pulls the result from the pre-game prior at kickoff
    toward the Kalshi mid as minutes elapse.
  * The dispatcher in ``in_game_loop._predict_live_for_market``
    routes soccer leagues to the soccer module and other leagues
    fall through to a None probability.
  * The in-game cycle persists a fresh ``model_yes_prob`` for
    soccer rows when the loop is enabled.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.learning import in_game_loop as _loop
from app.learning import soccer_live as _sl


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


# ---------------------------------------------------------------------------
# Pure feature-builder + blend tests (no DB)
# ---------------------------------------------------------------------------


def test_is_soccer_league_canonical_names():
    for lg in ("EPL", "LALIGA", "SERIEA", "BUNDES", "LIGUE1", "MLS", "UCL"):
        assert _sl.is_soccer_league(lg)
        assert _sl.is_soccer_league(lg.lower())
    assert not _sl.is_soccer_league("NBA")
    assert not _sl.is_soccer_league(None)
    assert not _sl.is_soccer_league("")


def test_sport_leagues_soccer_matches_canonical_names():
    # The fetch query joins on upper(league) ANY($1) — if these names
    # drift from the canonical SOCCER_LEAGUES the query never matches
    # an actual soccer row and the loop runs empty forever.
    assert set(_loop.SPORT_LEAGUES["soccer"]) == set(_sl.SOCCER_LEAGUES)


def test_build_features_no_live_state_falls_back_to_pregame():
    row = {
        "model_yes_prob": 0.62, "kalshi_price": 0.55,
        "league": "EPL",
    }
    feats = _sl.build_live_features(row=row, live_state=None)
    assert feats["pregame_prob"] == pytest.approx(0.62)
    assert feats["kalshi_price"] == pytest.approx(0.55)
    assert feats["has_live_state"] == 0.0
    assert feats["minutes_elapsed"] == 0.0


def test_build_features_with_live_state_carries_score_and_clock():
    row = {"model_yes_prob": 0.4, "kalshi_price": 0.3, "league": "EPL"}
    live = {
        "status": "2nd_half", "home_score": 2, "away_score": 0,
        "period": "2nd_half", "clock": "30:00",
        "state_age_seconds": 12.0,
    }
    feats = _sl.build_live_features(row=row, live_state=live)
    assert feats["home_score"] == 2.0
    assert feats["away_score"] == 0.0
    assert feats["score_diff"] == 2.0
    # 2nd_half base = 45, clock = 30:00 → 75 minutes elapsed.
    assert feats["minutes_elapsed"] == pytest.approx(75.0)
    assert feats["minutes_fraction"] == pytest.approx(75.0 / 90.0)
    assert feats["has_live_state"] == 1.0


def test_raw_probability_at_kickoff_is_pregame_prior():
    feats = _sl.build_live_features(
        row={"model_yes_prob": 0.7, "kalshi_price": 0.5, "league": "EPL"},
        live_state={
            "home_score": 0, "away_score": 0,
            "period": "1st_half", "clock": "00:00",
            "state_age_seconds": 5.0,
        },
    )
    p = _sl._raw_live_probability(feats)
    # At minute 0 the blend weight is 0 → posterior == pre-game prior.
    assert p == pytest.approx(0.7)


def test_raw_probability_late_match_pulls_toward_market():
    feats = _sl.build_live_features(
        row={"model_yes_prob": 0.7, "kalshi_price": 0.2, "league": "EPL"},
        live_state={
            "home_score": 0, "away_score": 1,
            "period": "2nd_half", "clock": "44:00",
            "state_age_seconds": 5.0,
        },
    )
    p = _sl._raw_live_probability(feats)
    # 89 minutes elapsed → blend weight ~ 0.99 * 0.85 = 0.84.
    # 0.16 * 0.7 + 0.84 * 0.2 = 0.112 + 0.168 = 0.28.
    # Score gap kicker: |diff|=1, market=0.2 (<0.5) → push DOWN by
    # min(0.06, 0.02 * 1) * fraction ≈ 0.0198.
    # Expected ~ 0.28 - 0.02 ≈ 0.26.
    assert 0.20 <= p <= 0.32
    assert p < 0.7  # market pulled the posterior way down from the prior


def test_raw_probability_no_signal_returns_none():
    # No pre-game prior AND no market price → caller should treat as
    # "no live update this cycle".
    feats = _sl.build_live_features(
        row={"model_yes_prob": None, "kalshi_price": None, "league": "EPL"},
        live_state=None,
    )
    assert _sl._raw_live_probability(feats) is None


def test_leg_orientation_parsed_from_metadata_label():
    feats = _sl.build_live_features(
        row={
            "model_yes_prob": 0.5, "kalshi_price": 0.4, "league": "EPL",
            "home_team": "Manchester City", "away_team": "Arsenal",
            "metadata": {"leg_orientation": "HOME"},
        },
        live_state={
            "home_score": 1, "away_score": 0,
            "period": "2nd_half", "clock": "30:00",
            "state_age_seconds": 5.0,
        },
    )
    assert feats["has_leg_orientation"] == 1.0
    assert feats["leg_is_home"] == 1.0
    assert feats["has_leg_dc"] == 1.0
    # Leading 1-0 in the 75th minute on a HOME leg should make the DC
    # posterior comfortably > 50%.
    assert feats["leg_dc_prob"] > 0.65


def test_leg_orientation_parsed_from_title_fallback():
    # Producer didn't set leg_orientation but the title is parseable.
    feats = _sl.build_live_features(
        row={
            "model_yes_prob": 0.5, "kalshi_price": 0.4, "league": "EPL",
            "home_team": "Manchester City", "away_team": "Arsenal",
            "metadata": {"title": "Will Arsenal beat Manchester City?"},
        },
        live_state={
            "home_score": 1, "away_score": 0,
            "period": "2nd_half", "clock": "30:00",
            "state_age_seconds": 5.0,
        },
    )
    assert feats["leg_is_away"] == 1.0
    # Arsenal trails 0-1 in the 75th minute — AWAY leg DC posterior
    # should be quite low.
    assert feats["leg_dc_prob"] < 0.20


def test_leg_orientation_draw_keyword():
    feats = _sl.build_live_features(
        row={
            "model_yes_prob": 0.3, "kalshi_price": 0.3, "league": "EPL",
            "home_team": "Manchester City", "away_team": "Arsenal",
            "metadata": {"title": "Manchester City vs Arsenal - Draw"},
        },
        live_state={
            "home_score": 0, "away_score": 0,
            "period": "2nd_half", "clock": "30:00",
            "state_age_seconds": 5.0,
        },
    )
    assert feats["leg_is_draw"] == 1.0
    # 0-0 in the 75th minute → draw posterior should be the dominant
    # outcome (well above the league-average ~25% draw share).
    assert feats["leg_dc_prob"] > 0.45


def test_leg_aware_branch_uses_dc_not_score_kicker():
    # HOME leg, 2-0 lead at minute 80 — leg-aware branch should pull
    # the posterior toward the HIGH DC probability even though the
    # market mid is low (e.g. an under-reactive book).
    row = {
        "model_yes_prob": 0.5, "kalshi_price": 0.30, "league": "EPL",
        "home_team": "Manchester City", "away_team": "Arsenal",
        "metadata": {"leg_orientation": "HOME"},
    }
    feats = _sl.build_live_features(
        row=row,
        live_state={
            "home_score": 2, "away_score": 0,
            "period": "2nd_half", "clock": "35:00",
            "state_age_seconds": 5.0,
        },
    )
    leg_aware = _sl._raw_live_probability(feats)
    # Leg-agnostic counterpart: same row with the leg dropped.
    row_no_leg = dict(row)
    row_no_leg["metadata"] = {}
    feats_no_leg = _sl.build_live_features(
        row=row_no_leg,
        live_state={
            "home_score": 2, "away_score": 0,
            "period": "2nd_half", "clock": "35:00",
            "state_age_seconds": 5.0,
        },
    )
    leg_agnostic = _sl._raw_live_probability(feats_no_leg)
    # The leg-aware branch should produce a meaningfully higher
    # posterior because it knows the live 2-0 scoreline makes the
    # HOME leg very likely, where the leg-agnostic blend can only
    # drift toward the (low) market mid.
    assert leg_aware > leg_agnostic + 0.05
    # Leg-aware posterior should sit above the (low) market mid because
    # the leg-aware core estimate knows a 2-0 HOME lead at minute 80 is
    # overwhelmingly likely to hold, dragging the blended result back up
    # past where the market alone would settle it.
    assert leg_aware > 0.40


def test_dc_full_time_collapses_to_indicator():
    # At minute 90 (or beyond) the DC posterior should reflect the
    # final score exactly: HOME wins 1-0 → P(HOME)=1, P(DRAW)=0,
    # P(AWAY)=0.
    assert _sl._dc_leg_probability("HOME", 1, 90.0) == 1.0
    assert _sl._dc_leg_probability("DRAW", 1, 90.0) == 0.0
    assert _sl._dc_leg_probability("AWAY", 1, 90.0) == 0.0
    assert _sl._dc_leg_probability("DRAW", 0, 95.0) == 1.0


def test_dc_kickoff_returns_full_match_prior():
    # At minute 0 the DC posterior reduces to the unconditional prior
    # for the configured baseline lambdas.  HOME advantage > AWAY, and
    # both should each exceed the DRAW share.
    p_home = _sl._dc_leg_probability("HOME", 0, 0.0)
    p_draw = _sl._dc_leg_probability("DRAW", 0, 0.0)
    p_away = _sl._dc_leg_probability("AWAY", 0, 0.0)
    assert abs((p_home + p_draw + p_away) - 1.0) < 1e-6
    assert p_home > p_away > 0.0
    # League-average ~25% draw rate for top-five leagues — wide check.
    assert 0.20 < p_draw < 0.32


def test_baseline_lambdas_per_league_canonical_values():
    # Each canonical league has its own hand-set (home, away) pair.
    # Pin them down so an accidental edit to the table is caught.
    expected = {
        "EPL":    (1.55, 1.25),
        "LALIGA": (1.40, 1.10),
        "SERIEA": (1.50, 1.20),
        "BUNDES": (1.70, 1.45),
        "LIGUE1": (1.35, 1.05),
        "MLS":    (1.55, 1.30),
        "UCL":    (1.65, 1.40),
    }
    for league, pair in expected.items():
        assert _sl._baseline_lambdas_for_league(league) == pair
        # Case-insensitive: the resolver upper-cases before lookup.
        assert _sl._baseline_lambdas_for_league(league.lower()) == pair


def test_baseline_lambdas_unknown_league_falls_back_to_blend():
    blended = (_sl._BASELINE_LAMBDA_HOME, _sl._BASELINE_LAMBDA_AWAY)
    # None / empty / unrecognised labels all degrade to the blended
    # baseline rather than raising — the predictor must keep working
    # on rows missing a canonical league.
    assert _sl._baseline_lambdas_for_league(None) == blended
    assert _sl._baseline_lambdas_for_league("") == blended
    assert _sl._baseline_lambdas_for_league("NBA") == blended
    assert _sl._baseline_lambdas_for_league("  not-a-league  ") == blended


def test_dc_home_leg_higher_for_bundes_than_ligue1_at_0_0_min_70():
    # A 0-0 at minute 70 leaves ~20 minutes of remaining scoring.
    # BUNDES baselines (1.70 / 1.45) are materially higher than
    # LIGUE1 (1.35 / 1.05) so the HOME leg's posterior should be
    # measurably larger under BUNDES — if the per-league lookup
    # ever regresses to the blended baseline this gap disappears.
    p_home_bundes = _sl._dc_leg_probability("HOME", 0, 70.0, league="BUNDES")
    p_home_ligue1 = _sl._dc_leg_probability("HOME", 0, 70.0, league="LIGUE1")
    assert p_home_bundes is not None and p_home_ligue1 is not None
    assert p_home_bundes > p_home_ligue1 + 0.01


def test_parse_clock_minutes_robust_to_garbage():
    assert _sl._parse_clock_minutes(None) == 0.0
    assert _sl._parse_clock_minutes("") == 0.0
    assert _sl._parse_clock_minutes("not_a_clock") == 0.0
    assert _sl._parse_clock_minutes("12") == 12.0
    assert _sl._parse_clock_minutes("12:30") == pytest.approx(12.5)


# ---------------------------------------------------------------------------
# Dispatcher tests
# ---------------------------------------------------------------------------


class _PredictorFakeDB:
    """Fake DB exposing only the fetchrow path soccer_live needs."""

    def __init__(self, live_state: dict[str, Any] | None) -> None:
        self.live_state = live_state
        self.fetchrow_calls = 0

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "sportradar_live_state" in query:
            self.fetchrow_calls += 1
            return self.live_state
        return None


def test_dispatcher_routes_soccer_through_soccer_live(monkeypatch):
    fake = _PredictorFakeDB(
        live_state={
            "status": "2nd_half", "home_score": 1, "away_score": 0,
            "period": "2nd_half", "clock": "10:00",
            "state_age_seconds": 4.0,
        }
    )
    monkeypatch.setattr(_sl, "db", fake)

    prob, meta = _run(_loop._predict_live_for_market({
        "market_id": "KX-EPL-A", "league": "EPL", "game_id": "g1",
        "model_yes_prob": 0.6, "kalshi_price": 0.55, "is_live": True,
    }))
    assert prob is not None
    assert 0.0 <= prob <= 1.0
    assert meta["source"] == "soccer_live"
    assert meta["league"] == "EPL"
    assert "features" in meta
    assert fake.fetchrow_calls == 1


def test_dispatcher_skips_when_no_signal(monkeypatch):
    fake = _PredictorFakeDB(live_state=None)
    monkeypatch.setattr(_sl, "db", fake)
    prob, meta = _run(_loop._predict_live_for_market({
        "market_id": "KX-EPL-A", "league": "EPL", "game_id": "g1",
        "model_yes_prob": None, "kalshi_price": None,
    }))
    assert prob is None
    assert meta["source"] == "soccer_live"
    assert "skipped_reason" in meta


# ---------------------------------------------------------------------------
# Integration: in_game_loop cycle persists soccer live predictions
# ---------------------------------------------------------------------------


class _CycleFakeDB:
    """Fake DB for the full cycle integration test."""

    def __init__(
        self,
        *,
        rows: list[dict[str, Any]],
        live_state: dict[str, Any] | None,
        flipped: int,
    ) -> None:
        self.rows = rows
        self.live_state = live_state
        self.flipped = flipped
        self.update_calls: list[tuple[str, ...]] = []
        self.persist_calls: list[tuple[Any, ...]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "runtime_settings" in query and "live_loop_enabled_" in args[0]:
            return {"value": "true"}
        if "runtime_settings" in query and "live_loop_cadence_" in args[0]:
            return None
        if "sportradar_live_state" in query:
            return self.live_state
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "sportradar_live_state" in query and "predictions" in query:
            return self.rows
        return []

    async def execute(self, query: str, *args: Any) -> str:
        if "UPDATE predictions" in query and "is_live = true" in query:
            self.update_calls.append(args)
            return f"UPDATE {self.flipped}"
        if "UPDATE predictions" in query and "model_yes_prob" in query:
            self.persist_calls.append(args)
            return "UPDATE 1"
        return "OK"


def test_cycle_persists_live_prediction_for_soccer(monkeypatch):
    fake = _CycleFakeDB(
        rows=[{
            "market_id": "KX-EPL-A", "league": "EPL",
            "game_id": "g1", "game_time": None, "is_live": False,
            "model_yes_prob": 0.6, "kalshi_price": 0.45,
            "status": "2nd_half", "state_age_seconds": 5.0,
        }],
        live_state={
            "status": "2nd_half", "home_score": 1, "away_score": 0,
            "period": "2nd_half", "clock": "30:00",
            "state_age_seconds": 5.0,
        },
        flipped=1,
    )
    monkeypatch.setattr(_loop, "db", fake)
    monkeypatch.setattr(_sl, "db", fake)

    r = _run(_loop.run_in_game_cycle("soccer"))
    assert r.enabled is True
    assert r.candidates_scanned == 1
    assert r.candidates_live == 1
    assert r.candidates_tagged == 1
    assert r.live_predictions_computed == 1
    assert r.live_predictions_persisted == 1
    assert len(fake.persist_calls) == 1
    # First UPDATE arg is the market_id, second is the new probability.
    args = fake.persist_calls[0]
    assert args[0] == "KX-EPL-A"
    assert 0.0 <= float(args[1]) <= 1.0


def test_cycle_does_not_persist_for_unwired_sport(monkeypatch):
    # MLB has since been wired onto both the meta-model dispatcher
    # (task #881) and the registry feature-builder path, so to keep
    # exercising the "tag is_live but no model update" branch we stub
    # both predictor entrypoints to yield nothing.  This also keeps the
    # test hermetic: the wired MLB path otherwise reaches the live gate
    # / registry predictor, which read module-level ``db`` state that is
    # NOT routed through the ``_loop.db`` / ``_sl.db`` fakes below — in
    # the full suite that real pool is bound to a torn-down event loop,
    # which is the "Event loop is closed" leak this test used to hit.
    fake = _CycleFakeDB(
        rows=[{
            "market_id": "KX-MLB-A", "league": "MLB",
            "game_id": "g1", "game_time": None, "is_live": False,
            "model_yes_prob": 0.6, "kalshi_price": 0.5,
            "status": "inprogress", "state_age_seconds": 5.0,
        }],
        live_state=None,
        flipped=1,
    )
    monkeypatch.setattr(_loop, "db", fake)
    monkeypatch.setattr(_sl, "db", fake)

    calls = {"meta": 0, "registry": 0}

    async def _no_meta_prediction(_row):
        calls["meta"] += 1
        return None

    async def _no_registry_prediction(_row, **_kwargs):
        calls["registry"] += 1
        return None

    monkeypatch.setattr(
        _loop, "_predict_live_for_market_meta", _no_meta_prediction
    )
    monkeypatch.setattr(
        _loop, "_predict_live_via_registry", _no_registry_prediction
    )

    r = _run(_loop.run_in_game_cycle("mlb"))
    assert r.candidates_tagged == 1
    assert r.live_predictions_computed == 0
    assert r.live_predictions_persisted == 0
    assert fake.persist_calls == []
    # Guard against the wiring silently regressing back to "unwired": the
    # MLB path must actually reach a predictor entrypoint (which we stubbed
    # to no-op), otherwise the no-persist result above would be meaningless.
    assert calls["meta"] + calls["registry"] >= 1
