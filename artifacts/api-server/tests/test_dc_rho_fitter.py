"""Tests for the per-league Dixon-Coles rho fitter (task #900).

Covers:
  * The rho MLE recovers the sign / approximate magnitude of a
    negative rho on a synthetic match sample drawn from the
    DC-corrected grid.
  * ``_fit_rho`` falls back to a near-zero rho on an independent-
    Poisson sample (no low-score correlation to detect).
  * ``run_cycle`` integrates: stub DB layer returns synthetic
    scorelines per league, fitter persists the vector and refreshes
    the predictor's in-memory override cache.
  * ``_resolve_dc_rho`` consults the in-memory override before
    falling back to the hand-set defaults.
  * Cadence gate in ``maybe_refit`` skips repeat runs inside the
    refit window.
"""
from __future__ import annotations

import asyncio
import json
import math
import random
from typing import Any

import pytest

from app.learning import dc_rho_fitter as _fitter
from app.learning import soccer_live as _sl


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _isolate_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _sl.set_dc_rho_overrides({})
    _fitter._reset_state_for_tests()
    yield
    _sl.set_dc_rho_overrides({})
    _fitter._reset_state_for_tests()
    loop.close()


# ---------------------------------------------------------------------------
# Override cache + resolver
# ---------------------------------------------------------------------------


def test_resolver_prefers_override_over_default():
    _sl.set_dc_rho_overrides({"EPL": -0.05})
    assert _sl._resolve_dc_rho("EPL") == pytest.approx(-0.05)
    # Other leagues still see the hand-set default.
    assert _sl._resolve_dc_rho("LALIGA") == pytest.approx(_sl._DC_RHO_BY_LEAGUE["LALIGA"])
    # Unknown league still falls through to the blended default.
    assert _sl._resolve_dc_rho("MADEUP") == pytest.approx(_sl._DC_RHO_DEFAULT)


def test_set_overrides_normalizes_keys_and_values():
    _sl.set_dc_rho_overrides({" epl ": "-0.07", "laliga": -0.11, "": -0.5, "bad": "x"})
    assert _sl._resolve_dc_rho("EPL") == pytest.approx(-0.07)
    assert _sl._resolve_dc_rho("LALIGA") == pytest.approx(-0.11)
    # Empty-key entry was dropped; "bad" was non-numeric so dropped too.
    snap = _sl.get_dc_rho_overrides()
    assert "BAD" not in snap
    assert "" not in snap


def test_set_overrides_clear_with_none_or_empty():
    _sl.set_dc_rho_overrides({"EPL": -0.05})
    _sl.set_dc_rho_overrides(None)
    assert _sl.get_dc_rho_overrides() == {}
    _sl.set_dc_rho_overrides({"EPL": -0.05})
    _sl.set_dc_rho_overrides({})
    assert _sl.get_dc_rho_overrides() == {}


# ---------------------------------------------------------------------------
# Likelihood / MLE on synthetic data
# ---------------------------------------------------------------------------


def _sample_from_dc_grid(
    n: int, lam_h: float, lam_a: float, rho: float, seed: int = 0,
) -> list[tuple[int, int]]:
    """Inverse-CDF sample n scorelines from the renormalised DC grid.

    Uses the same 8x8 truncated grid the fitter scores against — keeps
    the synthetic samples on the same support as the likelihood
    evaluation, so a successful round-trip is a real signal that the
    MLE found the right rho.
    """
    rng = random.Random(seed)
    grid = _fitter._build_grid_independent(lam_h, lam_a)
    corrected, total = _fitter._corrected_grid(grid, lam_h, lam_a, rho)
    flat: list[tuple[tuple[int, int], float]] = []
    for h in range(_fitter._GRID_MAX + 1):
        for a in range(_fitter._GRID_MAX + 1):
            flat.append(((h, a), corrected[h][a] / total))
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


def test_fit_recovers_negative_rho_on_synthetic_dc_sample():
    lam_h, lam_a = 1.55, 1.25  # EPL baselines
    true_rho = -0.12
    matches = _sample_from_dc_grid(4000, lam_h, lam_a, true_rho, seed=42)
    rho_hat, ll = _fitter._fit_rho(matches, lam_h, lam_a)
    # 4000 matches → MLE comfortably inside ±0.04 of the truth on the
    # rho-only fit with fixed lambdas.
    assert rho_hat < 0.0
    assert abs(rho_hat - true_rho) < 0.04
    assert math.isfinite(ll)


def test_fit_returns_near_zero_on_independent_poisson_sample():
    lam_h, lam_a = 1.55, 1.25
    matches = _sample_from_dc_grid(4000, lam_h, lam_a, 0.0, seed=7)
    rho_hat, _ll = _fitter._fit_rho(matches, lam_h, lam_a)
    # Independent Poisson → rho should be very close to zero.
    assert abs(rho_hat) < 0.05


def test_clip_rho_enforces_safe_band():
    assert _fitter._clip_rho(-0.5) == _fitter.RHO_CLIP_MIN
    assert _fitter._clip_rho(0.5) == _fitter.RHO_CLIP_MAX
    assert _fitter._clip_rho(-0.1) == pytest.approx(-0.1)


# ---------------------------------------------------------------------------
# run_cycle integration with stubbed DB
# ---------------------------------------------------------------------------


class _StubDb:
    """Just enough of app.db to drive run_cycle's reads + writes."""

    def __init__(self, scorelines_per_league: dict[str, list[tuple[int, int]]]):
        self.scorelines_per_league = scorelines_per_league
        self.persisted_value: str | None = None
        self.persisted_key: str | None = None

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        # Used by _fetch_scorelines — first arg is league.
        league = str(args[0]).upper()
        rows = self.scorelines_per_league.get(league, [])
        return [{"home_score": h, "away_score": a} for h, a in rows]

    async def fetchval(self, sql: str, *args: Any) -> Any:
        # Used to read the existing override blob; return whatever
        # was last persisted (start: None).
        return self.persisted_value

    class _Conn:
        def __init__(self, parent: "_StubDb"):
            self.parent = parent

        async def execute(self, sql: str, *args: Any) -> None:
            # Recognise the INSERT into runtime_settings — first two
            # args are (key, value).
            if "runtime_settings" in sql and "INSERT" in sql:
                self.parent.persisted_key = str(args[0])
                self.parent.persisted_value = str(args[1])

    def acquire(self) -> Any:
        parent = self
        class _Ctx:
            async def __aenter__(self_inner):
                return parent._Conn(parent)
            async def __aexit__(self_inner, *exc):
                return False
        return _Ctx()


def test_run_cycle_fits_persists_and_refreshes_cache(monkeypatch):
    lam_epl = _sl._baseline_lambdas_for_league("EPL")
    lam_laliga = _sl._baseline_lambdas_for_league("LALIGA")
    epl_matches = _sample_from_dc_grid(3000, *lam_epl, -0.12, seed=1)
    laliga_matches = _sample_from_dc_grid(3000, *lam_laliga, -0.14, seed=2)
    # MLS gets fewer than MIN_MATCHES_FOR_FIT to exercise the skip
    # path; all other leagues get an empty list → also skipped.
    scorelines = {
        "EPL": epl_matches,
        "LALIGA": laliga_matches,
        "MLS": [(1, 1)] * (_fitter.MIN_MATCHES_FOR_FIT - 1),
    }
    stub = _StubDb(scorelines)
    # Patch the db module references both _fitter and the soccer_live
    # refresh helper look at.
    monkeypatch.setattr(_fitter, "db", stub)
    from app import db as _appdb  # noqa: F401 — module exists
    # soccer_live imports db lazily inside refresh, so no patch needed
    # there (the cycle calls set_dc_rho_overrides directly).

    cr = _run(_fitter.run_cycle())

    assert cr.leagues_examined == len(_sl.SOCCER_LEAGUES)
    assert cr.leagues_fitted == 2  # EPL + LALIGA
    assert cr.leagues_skipped == len(_sl.SOCCER_LEAGUES) - 2
    assert cr.persisted is True
    # MLS skip reason is the insufficient-samples branch.
    skipped = {r.league: r.skipped_reason for r in cr.per_league if r.skipped_reason}
    assert skipped.get("MLS") == "insufficient_samples"

    # The persisted JSON carries the fitted values…
    assert stub.persisted_key == _fitter.DC_RHO_OVERRIDES_KEY
    persisted = json.loads(stub.persisted_value or "{}")
    assert "EPL" in persisted and "LALIGA" in persisted
    assert persisted["EPL"] < 0.0
    assert persisted["LALIGA"] < 0.0

    # …and the predictor's in-memory cache was refreshed so a follow-
    # up _resolve_dc_rho call sees the fitted value.
    assert _sl._resolve_dc_rho("EPL") == pytest.approx(persisted["EPL"])
    assert _sl._resolve_dc_rho("LALIGA") == pytest.approx(persisted["LALIGA"])


def test_run_cycle_preserves_existing_override_for_skipped_league(monkeypatch):
    # Existing persisted vector covers MLS with a known value; this
    # cycle has insufficient new MLS samples so the previous fitted
    # value should survive the rewrite (we never silently revert to
    # the hand-set default).
    stub = _StubDb({"MLS": []})
    stub.persisted_value = json.dumps({"MLS": -0.07})
    monkeypatch.setattr(_fitter, "db", stub)

    cr = _run(_fitter.run_cycle())
    # All leagues skipped → no fits, but the existing override is
    # preserved in the in-memory cache.
    assert cr.leagues_fitted == 0
    assert _sl.get_dc_rho_overrides().get("MLS") == pytest.approx(-0.07)


def test_maybe_refit_respects_cadence(monkeypatch):
    stub = _StubDb({})  # all leagues empty → cycle runs but no fits
    monkeypatch.setattr(_fitter, "db", stub)

    first = _run(_fitter.maybe_refit())
    assert first is not None
    # Immediate re-call within the refit window is a no-op.
    second = _run(_fitter.maybe_refit())
    assert second is None
