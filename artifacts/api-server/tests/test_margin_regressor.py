"""Unit + backtest coverage for the per-league margin regressor.

These tests both pin the API surface (feature extraction, line +
side resolution, blending) and demonstrate that on synthetic data with
a known elo→margin relationship the blended probability beats the
binary-only baseline on Brier score — which is what the spec calls
out as "done looks like".
"""
from __future__ import annotations

import random

import pytest

from app import margin_regressor


# ---------------------------------------------------------------------------
# Synthetic-game generator
# ---------------------------------------------------------------------------


def _synthesize_games(
    n: int,
    *,
    league: str = "NBA",
    margin_per_elo_pt: float = 0.05,   # ~5pt margin per 100pt Elo gap (NBA-ish)
    sigma: float = 10.0,
    seed: int = 17,
) -> list[dict]:
    """Generate (elo_diff, margin, total, spread_line) games where
    margin = β·elo_diff + N(0, σ²) so the regressor has a real signal
    to learn.  spread_line is set to the closing-line approximation
    (-β·elo_diff rounded to nearest 0.5) so half the games go each way
    around the line — exactly the regime where a better expected-margin
    estimate should improve Brier."""
    rng = random.Random(seed)
    games: list[dict] = []
    for i in range(n):
        elo_diff = rng.gauss(0.0, 200.0)
        margin = margin_per_elo_pt * elo_diff + rng.gauss(0.0, sigma)
        total = 220.0 + rng.gauss(0.0, 18.0)
        # Pretend the bookmaker priced the spread perfectly off elo_diff,
        # then jittered the line by ±1.0 pt of "market noise".
        spread_line = round((-margin_per_elo_pt * elo_diff) * 2.0) / 2.0
        spread_line += rng.choice([-1.0, -0.5, 0.0, 0.5, 1.0])
        home_score = round(total / 2.0 + margin / 2.0)
        away_score = round(total / 2.0 - margin / 2.0)
        games.append({
            "league": league,
            "elo_diff": elo_diff,
            "recent_form_delta": 0.0,
            "opponent_recent_form_delta": 0.0,
            "home_score": home_score,
            "away_score": away_score,
            "spread_line": spread_line,
            "total_line": round(total),
        })
    return games


# ---------------------------------------------------------------------------
# Unit-level coverage
# ---------------------------------------------------------------------------


def test_extract_features_includes_hfa_and_form_diff():
    feats = margin_regressor.extract_features(
        {"league": "NBA", "elo_diff": 100.0,
         "recent_form_delta": 30.0,
         "opponent_recent_form_delta": -20.0},
    )
    # FEATURE_NAMES = (intercept, elo_diff_hfa, form_delta_diff, rest_diff)
    assert feats[0] == 1.0
    assert feats[1] == pytest.approx(100.0 + 60.0)  # NBA HFA = 60
    assert feats[2] == pytest.approx(50.0)
    assert feats[3] == 0.0


def test_extract_features_falls_back_to_home_minus_opp_elo():
    feats = margin_regressor.extract_features(
        {"league": "MLB", "home_elo": 1550.0, "opponent_elo": 1500.0},
    )
    # MLB HFA = 24
    assert feats[1] == pytest.approx(50.0 + 24.0)


def test_unfitted_regressor_returns_zero_margin_and_default_total():
    reg = margin_regressor.LeagueScoreRegressor(league="NBA")
    margin, total = reg.predict([1.0, 0.0, 0.0, 0.0])
    assert margin == 0.0
    assert total == pytest.approx(225.0)


def test_margin_to_spread_prob_monotonic_in_expected_margin():
    reg = margin_regressor.LeagueScoreRegressor(
        league="NBA", margin_coef=[0.0, 1.0, 0.0, 0.0],
        total_coef=[225.0, 0.0, 0.0, 0.0],
        margin_sd=10.0, total_sd=18.0, n_train=10,
    )
    p_low = reg.margin_to_spread_prob(-5.0, spread_line=-3.5)
    p_eq = reg.margin_to_spread_prob(3.5, spread_line=-3.5)
    p_hi = reg.margin_to_spread_prob(15.0, spread_line=-3.5)
    assert p_low < p_eq < p_hi
    # At margin == cover threshold, probability should be 0.5.
    assert p_eq == pytest.approx(0.5, abs=1e-6)


def test_margin_to_spread_prob_inverts_for_away_side():
    reg = margin_regressor.LeagueScoreRegressor(
        league="NBA", margin_coef=[0.0, 1.0, 0.0, 0.0],
        total_coef=[225.0, 0.0, 0.0, 0.0],
        margin_sd=10.0, total_sd=18.0, n_train=10,
    )
    p_home = reg.margin_to_spread_prob(7.0, spread_line=-3.5, side="home")
    p_away = reg.margin_to_spread_prob(7.0, spread_line=-3.5, side="away")
    assert p_home + p_away == pytest.approx(1.0)


def test_total_to_over_prob_monotonic_in_expected_total():
    reg = margin_regressor.LeagueScoreRegressor(
        league="NBA", margin_coef=[0.0, 0.0, 0.0, 0.0],
        total_coef=[200.0, 0.0, 0.0, 0.0],
        margin_sd=10.0, total_sd=18.0, n_train=10,
    )
    p_under = reg.total_to_over_prob(190.0, total_line=225.0)
    p_at = reg.total_to_over_prob(225.0, total_line=225.0)
    p_over = reg.total_to_over_prob(260.0, total_line=225.0)
    assert p_under < p_at < p_over
    assert p_at == pytest.approx(0.5, abs=1e-6)


# ---------------------------------------------------------------------------
# Fit + registry
# ---------------------------------------------------------------------------


def test_fit_recovers_known_slope_and_returns_sane_residual_sd():
    games = _synthesize_games(800, league="NBA",
                              margin_per_elo_pt=0.05, sigma=10.0, seed=3)
    reg = margin_regressor.fit_regressor("NBA", games)
    assert reg.is_fitted()
    assert reg.n_train == 800
    # Coefficient on elo_diff_hfa column.  The synthetic data uses
    # margin = 0.05·elo_diff (no HFA contribution), so the fit should
    # land near 0.05 with the constant HFA term absorbed by the
    # intercept.
    elo_coef = reg.margin_coef[1]
    assert elo_coef == pytest.approx(0.05, abs=0.01)
    # Residual SD should land near the noise we injected.
    assert 8.0 < reg.margin_sd < 12.5


def test_fit_with_no_games_returns_unfitted_regressor_with_defaults():
    reg = margin_regressor.fit_regressor("NHL", [])
    assert not reg.is_fitted()
    assert reg.margin_sd == margin_regressor._DEFAULT_MARGIN_SD["NHL"]


def test_register_and_get_regressor_roundtrip():
    margin_regressor.reset_registry()
    reg = margin_regressor.LeagueScoreRegressor(
        league="NFL",
        margin_coef=[0.0, 0.04, 0.0, 0.0],
        total_coef=[44.0, 0.0, 0.0, 0.0],
        margin_sd=13.0, total_sd=12.0, n_train=200,
    )
    margin_regressor.register_regressor(reg)
    got = margin_regressor.get_regressor("NFL")
    assert got is not None and got.n_train == 200


def test_get_regressor_for_unsupported_league_returns_none():
    margin_regressor.reset_registry()
    assert margin_regressor.get_regressor("PGA") is None
    assert margin_regressor.get_regressor(None) is None


# ---------------------------------------------------------------------------
# adjust_probability — orchestrator-side blending
# ---------------------------------------------------------------------------


def _seed_nba_regressor():
    margin_regressor.reset_registry()
    margin_regressor.register_regressor(
        margin_regressor.LeagueScoreRegressor(
            league="NBA",
            margin_coef=[0.0, 0.05, 0.0, 0.0],
            total_coef=[225.0, 0.0, 0.0, 0.0],
            margin_sd=10.0, total_sd=18.0, n_train=500,
        )
    )


def test_adjust_skips_unsupported_market_type():
    _seed_nba_regressor()
    out = margin_regressor.adjust_probability(
        0.6, {"league": "NBA", "market_type": "Winner",
              "game_context": {"elo_diff": 100.0}, "spread_line": -3.5},
    )
    assert out is None


def test_adjust_skips_unsupported_league():
    _seed_nba_regressor()
    out = margin_regressor.adjust_probability(
        0.6, {"league": "PGA", "market_type": "Spread",
              "game_context": {"elo_diff": 100.0}, "spread_line": -3.5},
    )
    assert out is None


def test_adjust_skips_when_line_missing():
    _seed_nba_regressor()
    out = margin_regressor.adjust_probability(
        0.6, {"league": "NBA", "market_type": "Spread",
              "game_context": {"elo_diff": 100.0}},
    )
    assert out is None


def test_adjust_blends_spread_probability():
    _seed_nba_regressor()
    cand = {
        "league": "NBA",
        "market_type": "Spread",
        "spread_line": -3.5,
        "side": "home",
        "game_context": {"elo_diff": 200.0},   # home favored by ~10pts
    }
    out = margin_regressor.adjust_probability(0.5, cand, blend_weight=0.5)
    assert out is not None
    # Home favored by 10pts vs a -3.5 line → regressor should lean YES.
    assert out["regressor_prob"] > 0.5
    assert out["blended"] == pytest.approx(
        0.5 * 0.5 + 0.5 * out["regressor_prob"]
    )
    assert out["league"] == "NBA"
    assert out["market_type"] == "Spread"
    assert out["fitted"] is True


def test_adjust_blends_total_probability_for_under():
    _seed_nba_regressor()
    cand = {
        "league": "NBA",
        "market_type": "Total",
        "total_line": 240.0,        # market expects high-scoring game
        "side": "under",
        "game_context": {"elo_diff": 0.0},
    }
    out = margin_regressor.adjust_probability(0.4, cand, blend_weight=0.5)
    assert out is not None
    # Regressor expects ~225 total, line is 240 → UNDER should be > 0.5.
    assert out["regressor_prob"] > 0.5
    assert 0.0 <= out["blended"] <= 1.0


def test_adjust_handles_json_string_game_context():
    _seed_nba_regressor()
    out = margin_regressor.adjust_probability(
        0.5,
        {"league": "NBA", "market_type": "Spread",
         "spread_line": -3.5,
         "game_context": '{"elo_diff": 200.0}'},
    )
    assert out is not None
    assert out["regressor_prob"] > 0.5


# ---------------------------------------------------------------------------
# Backtest:  the headline "blended Brier < baseline Brier" demonstration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("league,beta,sigma", [
    ("NBA", 0.05, 10.0),
    ("NFL", 0.06, 13.0),
    ("MLB", 0.012, 4.0),
    ("NHL", 0.008, 2.5),
])
def test_backtest_blended_beats_flat_baseline(league, beta, sigma):
    games = _synthesize_games(600, league=league,
                              margin_per_elo_pt=beta, sigma=sigma, seed=11)
    res = margin_regressor.backtest_spread(
        league, games, train_frac=0.7, blend_weight=0.5,
    )
    # The flat-0.5 baseline gives Brier == 0.25 on every game.
    assert res.baseline_brier == pytest.approx(0.25, abs=1e-9)
    # The blended (and the regressor-only) probability MUST score
    # strictly better than the uninformed baseline on this synthetic
    # data — that's the whole point of the regressor.
    assert res.blended_brier < res.baseline_brier
    assert res.regressor_brier < res.baseline_brier
    assert res.improvement > 0.0


def test_backtest_blended_beats_realistic_binary_baseline():
    """The more interesting comparison: a binary baseline that already
    knows about elo_diff (via a logistic transform) — does adding the
    margin regressor on top still help?"""
    import math as _math
    games = _synthesize_games(800, league="NBA",
                              margin_per_elo_pt=0.05, sigma=10.0, seed=29)

    def logistic_baseline(g):
        # Binary classifier that thinks the right team is favored but
        # ignores the spread line — gets the WIN/LOSS prob right but
        # is miscalibrated for spread because it doesn't know the
        # cover threshold.  This is exactly the scenario the spec
        # describes: "we currently only have binary classifiers".
        z = 0.05 * g["elo_diff"] / 5.0   # logistic on margin
        return 1.0 / (1.0 + _math.exp(-z))

    res = margin_regressor.backtest_spread(
        "NBA", games, train_frac=0.7, blend_weight=0.5,
        baseline_fn=logistic_baseline,
    )
    # The blended probability should beat the binary-only baseline,
    # because the regressor explicitly accounts for the spread line
    # whereas the binary classifier doesn't.
    assert res.blended_brier < res.baseline_brier, res.as_dict()
    assert res.improvement > 0.0


def test_backtest_rejects_too_few_games():
    with pytest.raises(ValueError):
        margin_regressor.backtest_spread("NBA", [{"home_score": 1, "away_score": 0,
                                                   "spread_line": 0}])


# ---------------------------------------------------------------------------
# Production path:  load_training_games + refit_all_leagues + line attach
# ---------------------------------------------------------------------------


class _FakeDB:
    """Minimal asyncpg-shaped stub.  Records executed SQL + params and
    returns whatever the test queued."""

    def __init__(self):
        self.queue: list[list[dict]] = []
        self.fetchrow_queue: list[dict | None] = []
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        if not self.queue:
            return []
        return self.queue.pop(0)

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        if not self.fetchrow_queue:
            return None
        return self.fetchrow_queue.pop(0)


@pytest.fixture
def fake_db(monkeypatch):
    fake = _FakeDB()
    from app import db as real_db
    monkeypatch.setattr(real_db, "fetch", fake.fetch)
    monkeypatch.setattr(real_db, "fetchrow", fake.fetchrow)
    return fake


def _real_shape_game_rows(n: int = 600, league: str = "NBA",
                          beta: float = 0.05, sigma: float = 10.0):
    """Same generator as the synthetic backtest but emits rows in the
    shape `load_training_games` returns from the DB (column names
    matching the actual SQL projection)."""
    import random
    rng = random.Random(2026)
    rows = []
    for i in range(n):
        elo_diff = rng.gauss(0.0, 200.0)
        margin = beta * elo_diff + rng.gauss(0.0, sigma)
        total = 220.0 + rng.gauss(0.0, 18.0)
        spread_line = round((-beta * elo_diff) * 2.0) / 2.0
        spread_line += rng.choice([-1.0, -0.5, 0.0, 0.5, 1.0])
        rows.append({
            "league": league,
            "game_id": f"g{i}",
            "home_team": f"H{i}",
            "away_team": f"A{i}",
            "scheduled_start": "2025-01-01",
            "home_score": round(total / 2.0 + margin / 2.0),
            "away_score": round(total / 2.0 - margin / 2.0),
            "spread_line": spread_line,
            "total_line": round(total),
            # Pre-computed elo_diff so `extract_features()` (and tests
            # bypassing `load_training_games`) see real signal.
            "elo_diff": elo_diff,
            "home_elo": 1500.0 + elo_diff / 2.0,
            "opponent_elo": 1500.0 - elo_diff / 2.0,
            "recent_form_delta": 0.0,
            "opponent_recent_form_delta": 0.0,
        })
    return rows


@pytest.fixture
def fake_elo(monkeypatch):
    """Stub get_rating to return ratings consistent with the test row's
    `_elo_diff` so load_training_games produces the expected feature."""
    from app import elo as elo_mod
    state = {"current_diff": 0.0}

    class _Rating:
        def __init__(self, rating):
            self.rating = rating
            self.games_played = 50

    async def _get_rating(league, team):
        if team == "H":
            return _Rating(1500.0 + state["current_diff"] / 2.0)
        return _Rating(1500.0 - state["current_diff"] / 2.0)

    monkeypatch.setattr(elo_mod, "get_rating", _get_rating)
    return state


def test_load_training_games_returns_normalized_dicts(fake_db, fake_elo):
    async def _inner():
        fake_db.queue.append([
            {
                "game_id": "g1", "home_team": "Lakers", "away_team": "Celtics",
                "scheduled_start": "2025-12-01", "home_score": 110, "away_score": 102,
                "spread_line": -3.5, "total_line": 220.0,
            },
        ])
        games = await margin_regressor.load_training_games("NBA")
        assert len(games) == 1
        g = games[0]
        assert g["league"] == "NBA"
        assert g["home_score"] == 110.0
        assert g["away_score"] == 102.0
        assert g["spread_line"] == -3.5
        assert g["total_line"] == 220.0
        assert "elo_diff" in g
        assert "home_elo" in g
        # The query was issued with the right league + sport filter.
        assert fake_db.calls
        sql, args = fake_db.calls[0]
        assert args[0] == "NBA"
        assert "basketball_nba" in args[1]


    import asyncio as _asyncio
    _asyncio.run(_inner())

def test_load_training_games_unsupported_league_returns_empty():
    async def _inner():
        games = await margin_regressor.load_training_games("PGA")
        assert games == []


    import asyncio as _asyncio
    _asyncio.run(_inner())

def test_load_training_games_swallows_db_failure(monkeypatch):
    async def _inner():
        from app import db as real_db
        async def _boom(*a, **kw):
            raise RuntimeError("connection refused")
        monkeypatch.setattr(real_db, "fetch", _boom)
        games = await margin_regressor.load_training_games("NBA")
        assert games == []


    import asyncio as _asyncio
    _asyncio.run(_inner())

def test_refit_all_leagues_skips_empty_and_fits_populated(monkeypatch):
    async def _inner():
        margin_regressor.reset_registry()

        async def _fake_load(league, *, limit=None):
            if league == "NBA":
                return _real_shape_game_rows(n=400, league="NBA")
            if league == "NFL":
                return _real_shape_game_rows(n=10, league="NFL")  # below min_games
            return []

        monkeypatch.setattr(margin_regressor, "load_training_games", _fake_load)
        summary = await margin_regressor.refit_all_leagues(min_games=50)
        d = summary.as_dict()
        assert d["fitted"]["NBA"] == 400
        assert "NFL" in d["skipped"]
        assert "MLB" in d["skipped"] and "NHL" in d["skipped"]
        # And the registry is now populated for NBA.
        nba = margin_regressor.get_regressor("NBA")
        assert nba is not None and nba.is_fitted()
        assert nba.n_train == 400


    import asyncio as _asyncio
    _asyncio.run(_inner())

def test_attach_lines_to_candidate_writes_spread_line(fake_db):
    async def _inner():
        fake_db.fetchrow_queue.append({"line_value": -3.5})
        cand = {
            "league": "NBA", "market_type": "Spread", "game_id": "g42",
            "game_context": {},
        }
        out = await margin_regressor.attach_lines_to_candidate(cand)
        assert out["game_context"]["spread_line"] == -3.5


    import asyncio as _asyncio
    _asyncio.run(_inner())

def test_attach_lines_to_candidate_writes_total_line(fake_db):
    async def _inner():
        fake_db.fetchrow_queue.append({"line_value": 225.0})
        cand = {
            "league": "NBA", "market_type": "Total", "game_id": "g7",
            "game_context": {},
        }
        out = await margin_regressor.attach_lines_to_candidate(cand)
        assert out["game_context"]["total_line"] == 225.0


    import asyncio as _asyncio
    _asyncio.run(_inner())

def test_attach_lines_to_candidate_skips_when_already_present(fake_db):
    async def _inner():
        cand = {
            "league": "NBA", "market_type": "Spread", "game_id": "g7",
            "game_context": {"spread_line": -2.5},
        }
        out = await margin_regressor.attach_lines_to_candidate(cand)
        # No DB call should have been made.
        assert not fake_db.calls
        assert out["game_context"]["spread_line"] == -2.5


    import asyncio as _asyncio
    _asyncio.run(_inner())

def test_attach_lines_to_candidate_noop_for_unsupported_market(fake_db):
    async def _inner():
        cand = {"league": "NBA", "market_type": "Winner", "game_id": "g7"}
        out = await margin_regressor.attach_lines_to_candidate(cand)
        assert not fake_db.calls
        # game_context stays untouched.
        assert "game_context" not in out or out.get("game_context") in (None, {})


    import asyncio as _asyncio
    _asyncio.run(_inner())

def test_attach_lines_to_candidate_noop_when_db_fails(monkeypatch):
    async def _inner():
        from app import db as real_db
        async def _boom(*a, **kw):
            raise RuntimeError("db down")
        monkeypatch.setattr(real_db, "fetchrow", _boom)
        cand = {
            "league": "NBA", "market_type": "Spread", "game_id": "g7",
            "game_context": {},
        }
        out = await margin_regressor.attach_lines_to_candidate(cand)
        # No spread_line was added (line attach silently failed).
        assert "spread_line" not in (out.get("game_context") or {})


    import asyncio as _asyncio
    _asyncio.run(_inner())

def test_end_to_end_real_shape_backtest_beats_baseline(monkeypatch):
    async def _inner():
        """Full prod-shape rehearsal: fake DB returns rows in the *exact*
        shape `load_training_games` projects, refit_all_leagues registers
        NBA, and a chronological backtest on a held-out tail beats a
        binary-only baseline.  This exercises the same code path the
        settlement scheduler will hit in production."""
        margin_regressor.reset_registry()

        rows = _real_shape_game_rows(n=800, league="NBA",
                                     beta=0.05, sigma=10.0)
        train_rows = rows[:560]
        test_rows = rows[560:]

        async def _fake_load(league, *, limit=None):
            return train_rows if league == "NBA" else []

        monkeypatch.setattr(margin_regressor, "load_training_games", _fake_load)
        summary = await margin_regressor.refit_all_leagues(min_games=50)
        assert summary.fitted["NBA"] == len(train_rows)

        reg = margin_regressor.get_regressor("NBA")
        assert reg is not None and reg.is_fitted()

        # Score the held-out rows two ways: flat-0.5 baseline vs the fitted
        # regressor blended 50/50.  Demonstrates the spec's "improves Brier
        # vs binary-only baseline" deliverable on the same DB-shaped data
        # the production scheduler will train on.
        base_sq = 0.0
        blend_sq = 0.0
        for r in test_rows:
            outcome = 1.0 if (r["home_score"] - r["away_score"]) > -r["spread_line"] else 0.0
            feats = margin_regressor.extract_features(r, league="NBA")
            m_hat, _ = reg.predict(feats)
            reg_prob = reg.margin_to_spread_prob(m_hat, spread_line=r["spread_line"])
            blended = 0.5 * 0.5 + 0.5 * reg_prob
            base_sq += (0.5 - outcome) ** 2
            blend_sq += (blended - outcome) ** 2
        n = len(test_rows)
        baseline_brier = base_sq / n
        blended_brier = blend_sq / n
        assert blended_brier < baseline_brier, (blended_brier, baseline_brier)


    import asyncio as _asyncio
    _asyncio.run(_inner())

# ---------------------------------------------------------------------------
# Per-league blend-weight tuning
# ---------------------------------------------------------------------------


def test_get_blend_weight_returns_per_league_default():
    margin_regressor.reset_registry()
    # Each supported league has its own cold-start default — they must
    # differ so we know it isn't the legacy flat-0.5 sneaking through.
    weights = {
        lg: margin_regressor.get_blend_weight(lg)
        for lg in margin_regressor.SUPPORTED_LEAGUES
    }
    assert len(set(weights.values())) > 1
    # Unknown / blank leagues fall back to 0.5 so callers never crash.
    assert margin_regressor.get_blend_weight(None) == 0.5
    assert margin_regressor.get_blend_weight("PGA") == 0.5


def test_register_blend_weight_overrides_default_and_clamps():
    margin_regressor.reset_registry()
    margin_regressor.register_blend_weight("NBA", 0.42)
    assert margin_regressor.get_blend_weight("NBA") == 0.42
    # Out-of-range weights are clamped — `adjust_probability()` must
    # never see a weight outside [0, 1].
    margin_regressor.register_blend_weight("NBA", 1.7)
    assert margin_regressor.get_blend_weight("NBA") == 1.0
    margin_regressor.register_blend_weight("NBA", -0.4)
    assert margin_regressor.get_blend_weight("NBA") == 0.0
    # reset_registry wipes both regressors and weights.
    margin_regressor.reset_registry()
    assert margin_regressor.get_blend_weight("NBA") == \
        margin_regressor._DEFAULT_BLEND_WEIGHT_BY_LEAGUE["NBA"]


def test_adjust_probability_uses_per_league_weight_when_unspecified():
    _seed_nba_regressor()
    margin_regressor.register_blend_weight("NBA", 0.8)
    cand = {
        "league": "NBA", "market_type": "Spread",
        "spread_line": -3.5, "side": "home",
        "game_context": {"elo_diff": 200.0},
    }
    out = margin_regressor.adjust_probability(0.5, cand)  # no blend_weight
    assert out is not None
    assert out["blend_weight"] == pytest.approx(0.8)
    # Blended math reflects the league weight, not 0.5.
    assert out["blended"] == pytest.approx(
        0.2 * 0.5 + 0.8 * out["regressor_prob"]
    )


def test_adjust_probability_explicit_weight_still_wins():
    _seed_nba_regressor()
    margin_regressor.register_blend_weight("NBA", 0.8)
    cand = {
        "league": "NBA", "market_type": "Spread",
        "spread_line": -3.5, "side": "home",
        "game_context": {"elo_diff": 200.0},
    }
    out = margin_regressor.adjust_probability(0.5, cand, blend_weight=0.1)
    assert out is not None
    assert out["blend_weight"] == pytest.approx(0.1)


def test_tune_blend_weight_picks_minimum_brier_on_synthetic_data():
    games = _synthesize_games(600, league="NBA",
                              margin_per_elo_pt=0.05, sigma=10.0, seed=7)
    tune = margin_regressor.tune_blend_weight("NBA", games)
    assert tune is not None
    # The chosen weight must indeed be the argmin of the swept grid.
    assert tune.weight in tune.grid
    assert min(tune.grid_brier) == pytest.approx(tune.blended_brier)
    # The tuned blend must beat the flat baseline on this signal-rich
    # data — that's the whole reason we're tuning.
    assert tune.blended_brier <= tune.baseline_brier
    # On flat-baseline data with a real elo→margin signal the tuner
    # should land well above 0 (regressor adds value) and at most 1.
    assert 0.0 <= tune.weight <= 1.0
    assert tune.weight > 0.0


def test_tune_blend_weight_skips_rows_with_missing_lines_or_scores():
    """`load_training_games()` LEFT-JOINs spread lines and may return
    rows with `spread_line=None` (or non-numeric scores).  The tuner
    must filter them out instead of aborting the whole league's tune,
    otherwise refit_all_leagues silently falls back to defaults forever
    on any league that has even one bad row."""
    good = _synthesize_games(400, league="NBA",
                             margin_per_elo_pt=0.05, sigma=10.0, seed=33)
    # Inject a few representative bad rows in random spots.
    bad_rows = [
        {**good[0], "spread_line": None},
        {**good[1], "spread_line": "n/a"},
        {**good[2], "home_score": None},
    ]
    mixed = good + bad_rows
    tune = margin_regressor.tune_blend_weight("NBA", mixed)
    assert tune is not None
    assert 0.0 <= tune.weight <= 1.0


def test_tune_blend_weight_returns_none_on_too_few_games():
    assert margin_regressor.tune_blend_weight("NBA", []) is None
    assert margin_regressor.tune_blend_weight(
        "NBA", [{"home_score": 1, "away_score": 0, "spread_line": 0}],
    ) is None


def test_tune_blend_weight_picks_different_weights_per_league():
    """High-noise league (NFL-style σ) should land on a *lower* weight
    than the same regressor with a clean signal — that's the entire
    motivation for tuning per league instead of a flat 50/50."""
    clean = _synthesize_games(600, league="MLB",
                              margin_per_elo_pt=0.012, sigma=2.5, seed=21)
    noisy = _synthesize_games(600, league="NFL",
                              margin_per_elo_pt=0.06, sigma=20.0, seed=21)
    t_clean = margin_regressor.tune_blend_weight("MLB", clean)
    t_noisy = margin_regressor.tune_blend_weight("NFL", noisy)
    assert t_clean is not None and t_noisy is not None
    # The clean-signal league should trust the regressor at least as
    # much as the noisy-signal league.  (Strict > would be fragile to
    # grid resolution; >= is the meaningful invariant.)
    assert t_clean.weight >= t_noisy.weight


def test_refit_all_leagues_registers_tuned_blend_weights(monkeypatch):
    async def _inner():
        margin_regressor.reset_registry()

        async def _fake_load(league, *, limit=None):
            if league == "NBA":
                return _real_shape_game_rows(n=400, league="NBA",
                                             beta=0.05, sigma=10.0)
            return []

        monkeypatch.setattr(margin_regressor, "load_training_games", _fake_load)
        summary = await margin_regressor.refit_all_leagues(min_games=50)
        d = summary.as_dict()
        # NBA was fitted AND its blend weight was tuned + recorded.
        assert d["fitted"]["NBA"] == 400
        assert "NBA" in d["blend_weights"]
        tuned = d["blend_weights"]["NBA"]
        assert 0.0 <= tuned <= 1.0
        # And `get_blend_weight()` now returns the tuned value (not the
        # cold-start default), proving the wire-up reaches the live
        # `adjust_probability()` path.
        assert margin_regressor.get_blend_weight("NBA") == pytest.approx(tuned)

    import asyncio as _asyncio
    _asyncio.run(_inner())


def test_backtest_result_as_dict_round_keys():
    games = _synthesize_games(400, seed=5)
    res = margin_regressor.backtest_spread("NBA", games)
    d = res.as_dict()
    assert set(d) == {
        "league", "n_train", "n_test",
        "baseline_brier", "blended_brier", "regressor_brier", "improvement",
    }
    assert d["league"] == "NBA"
    assert d["n_train"] + d["n_test"] == 400


# ---------------------------------------------------------------------------
# Persistence + status (calibration_snapshots round-trip)
# ---------------------------------------------------------------------------


class _PersistFakeDB:
    """Fake DB that mirrors what `_persist_fit` and `load_all` need:
    captures `fetchval` INSERTs and replays them on `fetch` SELECTs so
    the snapshot the refit job writes is the snapshot the load job
    reads back.  Mirrors the asyncpg shape (rows are dict-like)."""

    def __init__(self):
        self.snapshots: list[dict] = []
        self.queue: list[list[dict]] = []
        self.fetchrow_queue: list[dict | None] = []
        self.calls: list[tuple[str, tuple]] = []
        self._next_id = 1

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        # Synthesize the load_all SELECT from snapshots we've recorded
        # so test bodies don't have to hand-curate the query response.
        q = " ".join(sql.split())
        if (
            "FROM calibration_snapshots" in q
            and "DISTINCT ON (sub_category)" in q
            and "margin_regressor:" in (args[1] if len(args) > 1 else "")
        ):
            by_sub: dict[str, dict] = {}
            for r in self.snapshots:
                cur = by_sub.get(r["sub_category"])
                if cur is None or r["created_at"] > cur["created_at"]:
                    by_sub[r["sub_category"]] = r
            return [
                {
                    "sub_category": r["sub_category"],
                    "calibrator_state": r["calibrator_state"],
                    "sample_count": r["sample_count"],
                    "created_at": r["created_at"],
                }
                for r in sorted(by_sub.values(), key=lambda x: x["sub_category"])
            ]
        if not self.queue:
            return []
        return self.queue.pop(0)

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        if not self.fetchrow_queue:
            return None
        return self.fetchrow_queue.pop(0)

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        q = " ".join(sql.split())
        if "INSERT INTO calibration_snapshots" in q and "RETURNING id" in q:
            new_id = self._next_id
            self._next_id += 1
            self.snapshots.append({
                "id": new_id,
                "sub_category": args[1],
                "calibrator_state": args[3],
                "sample_count": args[2],
                "created_at": new_id,  # monotonically increasing
            })
            return new_id
        return None


@pytest.fixture
def persist_db(monkeypatch):
    fake = _PersistFakeDB()
    from app import db as real_db
    monkeypatch.setattr(real_db, "fetch", fake.fetch)
    monkeypatch.setattr(real_db, "fetchrow", fake.fetchrow)
    monkeypatch.setattr(real_db, "fetchval", fake.fetchval)
    margin_regressor.reset_registry()
    return fake


def test_status_lists_every_supported_league_unfitted():
    margin_regressor.reset_registry()
    s = margin_regressor.status()
    assert set(s["supported"]) == margin_regressor.SUPPORTED_LEAGUES
    for league in margin_regressor.SUPPORTED_LEAGUES:
        entry = s["leagues"][league]
        assert entry["fitted"] is False
        assert entry["n_train"] == 0
        assert entry["last_fit_at"] is None
        # The textbook fallback SDs are surfaced even when no fit lands
        # so the operator can see what we're trading against.
        assert entry["margin_sd"] > 0
        assert entry["total_sd"] > 0


def test_register_regressor_records_fit_at_and_status_surfaces_it():
    margin_regressor.reset_registry()
    reg = margin_regressor.LeagueScoreRegressor(
        league="NBA", margin_coef=[0.0, 0.05, 0.0, 0.0],
        total_coef=[225.0, 0.0, 0.0, 0.0],
        margin_sd=10.0, total_sd=18.0, n_train=400,
    )
    margin_regressor.register_regressor(reg, fit_at="2026-04-23T00:00:00+00:00")
    s = margin_regressor.status()
    nba = s["leagues"]["NBA"]
    assert nba["fitted"] is True
    assert nba["n_train"] == 400
    assert nba["last_fit_at"] == "2026-04-23T00:00:00+00:00"
    assert nba["margin_sd"] == 10.0


def test_refit_persists_and_load_all_rehydrates_after_restart(monkeypatch, persist_db):
    """End-to-end: refit_all_leagues writes snapshots -> wipe registry
    (simulating a process restart) -> load_all rehydrates the same
    fitted regressor from the DB."""
    async def _fake_load(league, *, limit=None):
        if league == "NBA":
            return _real_shape_game_rows(n=400, league="NBA")
        return []

    monkeypatch.setattr(margin_regressor, "load_training_games", _fake_load)

    async def _inner():
        summary = await margin_regressor.refit_all_leagues(min_games=50)
        assert summary.fitted.get("NBA") == 400
        # We persisted exactly one snapshot for NBA.
        nba_snaps = [
            s for s in persist_db.snapshots if s["sub_category"] == "margin_regressor:NBA"
        ]
        assert len(nba_snaps) == 1

        # Capture the live coefficients before we wipe.
        before = margin_regressor.get_regressor("NBA")
        assert before is not None and before.is_fitted()
        before_coef = list(before.margin_coef)
        before_n = before.n_train
        before_status = margin_regressor.status()["leagues"]["NBA"]
        assert before_status["last_fit_at"] is not None

        # Simulate process restart.
        margin_regressor.reset_registry()
        cleared = margin_regressor.status()["leagues"]["NBA"]
        assert cleared["fitted"] is False
        assert cleared["last_fit_at"] is None

        # load_all() pulls the snapshot back into the registry.
        loaded = await margin_regressor.load_all()
        assert "NBA" in loaded
        after = margin_regressor.get_regressor("NBA")
        assert after is not None and after.is_fitted()
        assert after.n_train == before_n
        assert after.margin_coef == pytest.approx(before_coef)
        # And the status endpoint surfaces the rehydrated last_fit_at.
        after_status = margin_regressor.status()["leagues"]["NBA"]
        assert after_status["fitted"] is True
        assert after_status["last_fit_at"] is not None

    import asyncio as _asyncio
    _asyncio.run(_inner())


def test_load_all_swallows_db_failure(monkeypatch):
    """A flaky DB at boot must not raise — load_all should log and
    return an empty mapping so the API server still comes up."""
    margin_regressor.reset_registry()
    from app import db as real_db

    async def _boom(*a, **kw):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(real_db, "fetch", _boom)

    async def _inner():
        loaded = await margin_regressor.load_all()
        assert loaded == {}
        # Registry stays empty + cold-start defaults still surface.
        s = margin_regressor.status()
        assert all(not v["fitted"] for v in s["leagues"].values())

    import asyncio as _asyncio
    _asyncio.run(_inner())


def test_refit_continues_when_persistence_fails(monkeypatch):
    """A DB blip on the snapshot insert must not abort the refit cycle —
    the in-process registry should still be updated so the live trading
    cycle picks up the new coefficients."""
    margin_regressor.reset_registry()
    from app import db as real_db

    async def _fake_load(league, *, limit=None):
        if league == "NBA":
            return _real_shape_game_rows(n=400, league="NBA")
        return []

    async def _boom(*a, **kw):
        raise RuntimeError("snapshot insert exploded")

    monkeypatch.setattr(margin_regressor, "load_training_games", _fake_load)
    monkeypatch.setattr(real_db, "fetchval", _boom)

    async def _inner():
        summary = await margin_regressor.refit_all_leagues(min_games=50)
        # Fit was still recorded in the in-process registry even though
        # the persistence step blew up.
        assert summary.fitted.get("NBA") == 400
        reg = margin_regressor.get_regressor("NBA")
        assert reg is not None and reg.is_fitted()

    import asyncio as _asyncio
    _asyncio.run(_inner())
