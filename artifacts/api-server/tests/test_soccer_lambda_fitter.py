"""Tests for the per-league soccer baseline-lambda fitter (task #903).

Covers:
  * The independent-Poisson MLE recovers the home / away goal means on
    a synthetic sample.
  * ``_clip_lambda`` enforces the safe band.
  * ``run_cycle`` integrates: stub DB layer returns synthetic
    scorelines per league, fitter persists per-league rows and
    refreshes the predictor's in-memory override cache.
  * ``_baseline_lambdas_for_league`` consults the in-memory override
    before falling back to the hand-set defaults.
  * Cadence gate in ``maybe_refit`` skips repeat runs inside the refit
    window.
  * Override cache survives an existing-row scenario and the predictor
    keeps the previously-fitted value for leagues skipped this cycle.
"""
from __future__ import annotations

import asyncio
import random
from typing import Any

import pytest

from app.learning import soccer_lambda_fitter as _fitter
from app.learning import soccer_live as _sl


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _isolate_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _sl.set_baseline_lambda_overrides({})
    _fitter._reset_state_for_tests()
    yield
    _sl.set_baseline_lambda_overrides({})
    _fitter._reset_state_for_tests()
    loop.close()


# ---------------------------------------------------------------------------
# Override cache + resolver
# ---------------------------------------------------------------------------


def test_resolver_prefers_override_over_default():
    _sl.set_baseline_lambda_overrides({"EPL": (1.80, 1.30)})
    assert _sl._baseline_lambdas_for_league("EPL") == (1.80, 1.30)
    # Other leagues still see the hand-set default.
    assert _sl._baseline_lambdas_for_league("LALIGA") == _sl._BASELINE_LAMBDAS_BY_LEAGUE["LALIGA"]
    # Unknown league falls through to the blended default.
    assert _sl._baseline_lambdas_for_league("MADEUP") == (
        _sl._BASELINE_LAMBDA_HOME, _sl._BASELINE_LAMBDA_AWAY,
    )


def test_set_overrides_normalises_keys_and_drops_bad_values():
    _sl.set_baseline_lambda_overrides({
        " epl ": ("1.7", "1.3"),
        "laliga": (1.4, 1.1),
        "": (1.0, 1.0),
        "bad": "x",                  # non-tuple → dropped
        "alsobad": (1.0, "nope"),    # non-numeric → dropped
    })
    assert _sl._baseline_lambdas_for_league("EPL") == pytest.approx((1.7, 1.3))
    assert _sl._baseline_lambdas_for_league("LALIGA") == pytest.approx((1.4, 1.1))
    snap = _sl.get_baseline_lambda_overrides()
    assert "BAD" not in snap
    assert "ALSOBAD" not in snap
    assert "" not in snap


def test_set_overrides_clear_with_none_or_empty():
    _sl.set_baseline_lambda_overrides({"EPL": (1.7, 1.3)})
    _sl.set_baseline_lambda_overrides(None)
    assert _sl.get_baseline_lambda_overrides() == {}
    _sl.set_baseline_lambda_overrides({"EPL": (1.7, 1.3)})
    _sl.set_baseline_lambda_overrides({})
    assert _sl.get_baseline_lambda_overrides() == {}


# ---------------------------------------------------------------------------
# MLE on synthetic data
# ---------------------------------------------------------------------------


def _sample_independent_poisson(
    n: int, lam_h: float, lam_a: float, seed: int = 0,
) -> list[tuple[int, int]]:
    """Sample n independent-Poisson scorelines via inverse-CDF over the
    same 8x8 truncated grid the predictor uses.

    Drawing from the truncated grid (rather than ``random.gauss`` /
    ``numpy.random.poisson``) keeps the test sample on the support the
    fitter scores against and avoids a numpy dependency for tests.
    """
    rng = random.Random(seed)
    GRID = _sl._DC_MAX_REMAINING_GOALS
    ph = [_sl._poisson_pmf(lam_h, k) for k in range(GRID + 1)]
    pa = [_sl._poisson_pmf(lam_a, k) for k in range(GRID + 1)]
    total = sum(p * q for p in ph for q in pa)
    flat: list[tuple[tuple[int, int], float]] = []
    for h in range(GRID + 1):
        for a in range(GRID + 1):
            flat.append(((h, a), ph[h] * pa[a] / total))
    out: list[tuple[int, int]] = []
    for _ in range(n):
        u = rng.random()
        cum = 0.0
        for cell, p in flat:
            cum += p
            if u <= cum:
                out.append(cell)
                break
        else:
            out.append(flat[-1][0])
    return out


def test_fit_recovers_independent_poisson_means():
    true_h, true_a = 1.55, 1.25
    matches = _sample_independent_poisson(4000, true_h, true_a, seed=42)
    lam_h, lam_a, ll = _fitter._fit_lambdas(matches)
    # 4000 matches → sample-mean MLE comfortably inside ±0.06 of each
    # true intensity.
    assert abs(lam_h - true_h) < 0.06
    assert abs(lam_a - true_a) < 0.06
    import math
    assert math.isfinite(ll)


def test_fit_empty_input_returns_zeros():
    lam_h, lam_a, ll = _fitter._fit_lambdas([])
    assert lam_h == 0.0
    assert lam_a == 0.0
    assert ll == float("-inf")


def test_clip_lambda_enforces_safe_band():
    assert _fitter._clip_lambda(0.0) == _fitter.LAMBDA_CLIP_MIN
    assert _fitter._clip_lambda(10.0) == _fitter.LAMBDA_CLIP_MAX
    assert _fitter._clip_lambda(1.5) == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# run_cycle integration with stubbed DB
# ---------------------------------------------------------------------------


class _StubDb:
    """Just enough of app.db to drive run_cycle's reads + writes."""

    def __init__(self, scorelines_per_league: dict[str, list[tuple[int, int]]]):
        self.scorelines_per_league = scorelines_per_league
        self.persisted: dict[str, tuple[float, float, int]] = {}

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        # The lambda fitter passes league as the first positional arg,
        # the predictor's refresh helper passes no args (whole-table
        # scan) — dispatch on that shape.
        if "soccer_baseline_lambdas" in sql:
            rows = []
            for league, (lam_h, lam_a, _n) in self.persisted.items():
                rows.append(
                    {"league": league, "lambda_home": lam_h, "lambda_away": lam_a},
                )
            return rows
        league = str(args[0]).upper()
        rows = self.scorelines_per_league.get(league, [])
        return [{"home_score": h, "away_score": a} for h, a in rows]

    class _Conn:
        def __init__(self, parent: "_StubDb"):
            self.parent = parent

        async def execute(self, sql: str, *args: Any) -> None:
            if "soccer_baseline_lambdas" in sql and "INSERT" in sql:
                # (league, lambda_home, lambda_away, matches, log_likelihood)
                league = str(args[0])
                lam_h = float(args[1])
                lam_a = float(args[2])
                n = int(args[3])
                self.parent.persisted[league] = (lam_h, lam_a, n)

    def acquire(self) -> Any:
        parent = self

        class _Ctx:
            async def __aenter__(self_inner):
                return parent._Conn(parent)

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()


def test_run_cycle_fits_persists_and_refreshes_cache(monkeypatch):
    epl_matches = _sample_independent_poisson(3000, 1.55, 1.25, seed=1)
    laliga_matches = _sample_independent_poisson(3000, 1.40, 1.10, seed=2)
    # MLS gets fewer than MIN_MATCHES_FOR_FIT to exercise the skip
    # path; all other leagues get an empty list → also skipped.
    scorelines = {
        "EPL": epl_matches,
        "LALIGA": laliga_matches,
        "MLS": [(1, 1)] * (_fitter.MIN_MATCHES_FOR_FIT - 1),
    }
    stub = _StubDb(scorelines)
    monkeypatch.setattr(_fitter, "db", stub)
    # The predictor's refresh helper imports db lazily inside the
    # function body — patch the package attribute the lazy import
    # resolves against.
    from app import db as _appdb  # noqa: F401
    monkeypatch.setattr("app.db.fetch", stub.fetch)

    cr = _run(_fitter.run_cycle())

    assert cr.leagues_examined == len(_sl.SOCCER_LEAGUES)
    assert cr.leagues_fitted == 2  # EPL + LALIGA
    assert cr.leagues_skipped == len(_sl.SOCCER_LEAGUES) - 2
    assert cr.persisted is True
    skipped = {r.league: r.skipped_reason for r in cr.per_league if r.skipped_reason}
    assert skipped.get("MLS") == "insufficient_samples"

    # Two rows persisted with the fitted intensities.
    assert "EPL" in stub.persisted and "LALIGA" in stub.persisted
    lam_h_epl, lam_a_epl, n_epl = stub.persisted["EPL"]
    assert abs(lam_h_epl - 1.55) < 0.08
    assert abs(lam_a_epl - 1.25) < 0.08
    assert n_epl == len(epl_matches)

    # …and the predictor's in-memory cache was refreshed so a follow-
    # up lookup sees the fitted values.
    cached_epl = _sl._baseline_lambdas_for_league("EPL")
    assert cached_epl == pytest.approx((lam_h_epl, lam_a_epl))


def test_run_cycle_preserves_existing_override_for_skipped_league(monkeypatch):
    # The previous run persisted a value for MLS; this cycle has
    # insufficient new MLS samples (all leagues empty) so the previous
    # row should survive in the in-memory cache (we never silently
    # revert to the hand-set default while the row is still in the
    # table).
    stub = _StubDb({})
    stub.persisted["MLS"] = (1.70, 1.40, 250)
    monkeypatch.setattr(_fitter, "db", stub)
    monkeypatch.setattr("app.db.fetch", stub.fetch)

    cr = _run(_fitter.run_cycle())
    assert cr.leagues_fitted == 0
    # Refresh read the previously-persisted row back into the cache.
    assert _sl._baseline_lambdas_for_league("MLS") == pytest.approx((1.70, 1.40))


def test_maybe_refit_respects_cadence(monkeypatch):
    stub = _StubDb({})  # all leagues empty → cycle runs but no fits
    monkeypatch.setattr(_fitter, "db", stub)
    monkeypatch.setattr("app.db.fetch", stub.fetch)

    first = _run(_fitter.maybe_refit())
    assert first is not None
    # Immediate re-call within the refit window is a no-op.
    second = _run(_fitter.maybe_refit())
    assert second is None


def test_predictor_uses_fitted_lambdas_end_to_end(monkeypatch):
    """Integration check: after run_cycle persists + refreshes the
    override cache, the live predictor's leg probability computation
    consumes the FITTED intensities — not the hand-set defaults.

    Drives EPL well off its hand-set baseline (1.55 / 1.25) toward a
    very low-scoring sample so the resulting Dixon-Coles leg probability
    at minute 70 from a 0-0 scoreline is visibly different from the
    default-baseline answer.  Confirms the override flows all the way
    through `_baseline_lambdas_for_league` → `_dc_leg_probability`.
    """
    # Generate a low-scoring sample so the fitted lambdas land well
    # below the hand-set EPL defaults (1.55 / 1.25).
    low_matches = _sample_independent_poisson(2000, 0.70, 0.60, seed=9)
    stub = _StubDb({"EPL": low_matches})
    monkeypatch.setattr(_fitter, "db", stub)
    monkeypatch.setattr("app.db.fetch", stub.fetch)

    # Baseline leg-DRAW probability at minute 70, 0-0, using the
    # hand-set defaults.
    default_draw = _sl._dc_leg_probability(
        "DRAW", score_diff=0, minutes_elapsed=70.0, league="EPL",
    )

    _run(_fitter.run_cycle())

    # After the fit the cache holds the low-scoring intensities; a
    # lower per-side lambda raises the conditional DRAW probability at
    # a tied scoreline (fewer expected remaining goals = more chance
    # the score stays level).
    fitted_draw = _sl._dc_leg_probability(
        "DRAW", score_diff=0, minutes_elapsed=70.0, league="EPL",
    )

    assert default_draw is not None and fitted_draw is not None
    # Override actually moved the answer — and in the expected
    # direction (lower scoring → higher conditional draw).
    assert fitted_draw > default_draw + 0.02


def test_refresh_baseline_lambda_overrides_loads_table_rows(monkeypatch):
    stub = _StubDb({})
    stub.persisted["EPL"] = (1.65, 1.30, 400)
    stub.persisted["BUNDES"] = (1.80, 1.50, 320)
    monkeypatch.setattr("app.db.fetch", stub.fetch)

    loaded = _run(_sl.refresh_baseline_lambda_overrides())
    assert loaded["EPL"] == pytest.approx((1.65, 1.30))
    assert loaded["BUNDES"] == pytest.approx((1.80, 1.50))
    # And the resolver now returns the fitted value.
    assert _sl._baseline_lambdas_for_league("EPL") == pytest.approx((1.65, 1.30))
