"""Tests for the per-league soccer baseline lambda refresher (task #896).

Covers:
  * The override cache + resolver in soccer_live: override takes
    precedence over the hand-set default, and clears restore the
    default.
  * The clipping band keeps degenerate samples inside the validated
    goal-grid regime.
  * ``run_cycle`` integrates: stub DB layer returns synthetic
    scorelines per league, fitter persists the JSON blob with a
    timestamp and refreshes the predictor's in-memory cache.
  * An existing override survives a cycle in which every league is
    skipped for insufficient samples (we never silently revert a
    real estimate to the hand-set default).
  * Cadence gate in ``maybe_refit`` skips repeat runs inside the
    refresh window.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.learning import soccer_baseline_fitter as _fitter
from app.learning import soccer_live as _sl


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _isolate_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _sl.set_baseline_lambda_overrides(None)
    _fitter._reset_state_for_tests()
    yield
    _sl.set_baseline_lambda_overrides(None)
    _fitter._reset_state_for_tests()
    loop.close()


# ---------------------------------------------------------------------------
# Override cache + resolver
# ---------------------------------------------------------------------------


def test_resolver_prefers_override_over_default():
    _sl.set_baseline_lambda_overrides({"EPL": (1.80, 1.40)}, refreshed_at="2026-05-01T00:00:00+00:00")
    assert _sl._baseline_lambdas_for_league("EPL") == pytest.approx((1.80, 1.40))
    # Other leagues still see the hand-set default.
    assert _sl._baseline_lambdas_for_league("LALIGA") == pytest.approx(
        _sl._BASELINE_LAMBDAS_BY_LEAGUE["LALIGA"]
    )
    # Unknown league still falls through to the blended default.
    assert _sl._baseline_lambdas_for_league("MADEUP") == pytest.approx(
        (_sl._BASELINE_LAMBDA_HOME, _sl._BASELINE_LAMBDA_AWAY)
    )
    assert _sl.get_baseline_lambdas_refreshed_at() == "2026-05-01T00:00:00+00:00"


def test_set_overrides_normalises_keys_and_drops_garbage():
    _sl.set_baseline_lambda_overrides(
        {
            " epl ": [1.6, 1.3],
            "laliga": (1.45, "1.10"),
            "": [1.0, 1.0],
            "bad": ["x", "y"],
            "neg": [-0.1, 1.0],
            "tooshort": [1.5],
        }
    )
    snap = _sl.get_baseline_lambda_overrides()
    assert snap.get("EPL") == pytest.approx((1.6, 1.3))
    assert snap.get("LALIGA") == pytest.approx((1.45, 1.10))
    for k in ("", "BAD", "NEG", "TOOSHORT"):
        assert k not in snap


def test_set_overrides_clear_with_none_or_empty():
    _sl.set_baseline_lambda_overrides({"EPL": (1.6, 1.3)})
    _sl.set_baseline_lambda_overrides(None)
    assert _sl.get_baseline_lambda_overrides() == {}
    _sl.set_baseline_lambda_overrides({"EPL": (1.6, 1.3)})
    _sl.set_baseline_lambda_overrides({})
    assert _sl.get_baseline_lambda_overrides() == {}


def test_clip_lambda_enforces_band():
    assert _fitter._clip_lambda(0.1) == _fitter.LAMBDA_CLIP_MIN
    assert _fitter._clip_lambda(10.0) == _fitter.LAMBDA_CLIP_MAX
    assert _fitter._clip_lambda(1.5) == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# JSON load shape tolerance
# ---------------------------------------------------------------------------


def test_load_existing_overrides_handles_wrapped_and_bare():
    wrapped = json.dumps(
        {"refreshed_at": "2026-05-01T00:00:00+00:00", "lambdas": {"EPL": [1.55, 1.25]}}
    )
    overs, ts = _fitter._load_existing_overrides_from_json(wrapped)
    assert overs == {"EPL": (1.55, 1.25)}
    assert ts == "2026-05-01T00:00:00+00:00"

    bare = json.dumps({"LALIGA": [1.40, 1.10]})
    overs, ts = _fitter._load_existing_overrides_from_json(bare)
    assert overs == {"LALIGA": (1.40, 1.10)}
    assert ts is None

    assert _fitter._load_existing_overrides_from_json(None) == ({}, None)
    assert _fitter._load_existing_overrides_from_json("not json") == ({}, None)


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
        league = str(args[0]).upper()
        rows = self.scorelines_per_league.get(league, [])
        return [{"home_score": h, "away_score": a} for h, a in rows]

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return self.persisted_value

    class _Conn:
        def __init__(self, parent: "_StubDb"):
            self.parent = parent

        async def execute(self, sql: str, *args: Any) -> None:
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


def test_run_cycle_refreshes_persists_and_warms_cache(monkeypatch):
    # EPL gets a sample biased a touch higher than the hand-set default
    # so we can verify the refreshed lambdas actually moved.
    epl = [(2, 1)] * 100 + [(1, 1)] * 100 + [(3, 2)] * 100  # mean ≈ (2.0, 1.33)
    # LALIGA gets a low-scoring sample.
    laliga = [(1, 0)] * 100 + [(1, 1)] * 100 + [(0, 1)] * 100  # mean ≈ (0.67, 0.67) → clipped to floor 0.67
    # MLS gets fewer than the minimum to exercise the skip path.
    mls = [(2, 1)] * (_fitter.MIN_MATCHES_FOR_REFRESH - 1)
    stub = _StubDb({"EPL": epl, "LALIGA": laliga, "MLS": mls})
    monkeypatch.setattr(_fitter, "db", stub)

    cr = _run(_fitter.run_cycle())

    assert cr.leagues_examined == len(_sl.SOCCER_LEAGUES)
    assert cr.leagues_refreshed == 2  # EPL + LALIGA
    assert cr.leagues_skipped == len(_sl.SOCCER_LEAGUES) - 2
    assert cr.persisted is True
    assert cr.refreshed_at is not None

    skipped = {r.league: r.skipped_reason for r in cr.per_league if r.skipped_reason}
    assert skipped.get("MLS") == "insufficient_samples"

    # The persisted JSON wraps a {refreshed_at, lambdas} blob.
    assert stub.persisted_key == _fitter.BASELINE_LAMBDAS_KEY
    persisted = json.loads(stub.persisted_value or "{}")
    assert "refreshed_at" in persisted
    lambdas = persisted["lambdas"]
    assert "EPL" in lambdas and "LALIGA" in lambdas
    assert lambdas["EPL"][0] == pytest.approx(2.0, abs=0.05)
    assert lambdas["EPL"][1] == pytest.approx(1.333, abs=0.05)

    # The predictor's in-memory cache was refreshed.
    assert _sl._baseline_lambdas_for_league("EPL") == pytest.approx((2.0, 1.333), abs=0.05)
    assert _sl.get_baseline_lambdas_refreshed_at() == cr.refreshed_at


def test_run_cycle_preserves_existing_override_for_skipped_league(monkeypatch):
    # An existing persisted vector covers MLS; this cycle has empty
    # samples everywhere → no refresh, but the existing override
    # should survive in the in-memory cache (we never silently revert
    # a real estimate to the hand-set default).
    stub = _StubDb({})
    stub.persisted_value = json.dumps(
        {
            "refreshed_at": "2026-04-30T00:00:00+00:00",
            "lambdas": {"MLS": [1.62, 1.34]},
        }
    )
    monkeypatch.setattr(_fitter, "db", stub)

    cr = _run(_fitter.run_cycle())
    assert cr.leagues_refreshed == 0
    assert _sl.get_baseline_lambda_overrides().get("MLS") == pytest.approx((1.62, 1.34))


def test_run_cycle_clips_extreme_means(monkeypatch):
    # A degenerate per-league sample with a wild 12-goal scoreline
    # pulls the mean above the safe band; clipping should hold the
    # persisted value at LAMBDA_CLIP_MAX.
    crazy = [(12, 12)] * _fitter.MIN_MATCHES_FOR_REFRESH
    stub = _StubDb({"EPL": crazy})
    monkeypatch.setattr(_fitter, "db", stub)

    cr = _run(_fitter.run_cycle())
    epl_row = next(r for r in cr.per_league if r.league == "EPL")
    assert epl_row.lambda_home_persisted == pytest.approx(_fitter.LAMBDA_CLIP_MAX)
    assert epl_row.lambda_away_persisted == pytest.approx(_fitter.LAMBDA_CLIP_MAX)


def test_maybe_refit_respects_cadence(monkeypatch):
    stub = _StubDb({})  # all leagues empty → cycle runs but no refreshes
    monkeypatch.setattr(_fitter, "db", stub)

    first = _run(_fitter.maybe_refit())
    assert first is not None
    # Immediate re-call within the refresh window is a no-op.
    second = _run(_fitter.maybe_refit())
    assert second is None
