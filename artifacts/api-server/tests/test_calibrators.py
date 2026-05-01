"""Tests for the per-league probability calibrators.

Covers:
  * apply() bypasses cleanly when no calibrator is loaded
  * apply() routes by league + calibrator type
  * load_all() pulls the most recent per-league row from the DB
  * refit_all() picks isotonic vs. platt by sample count, persists, reloads
  * predict() applies the calibrator when a league is supplied
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from app import calibrators, db as _db, models_loader, predictor


# ---------------------------------------------------------------------------
# DB shim
# ---------------------------------------------------------------------------


class _FakeDb:
    def __init__(self) -> None:
        self.snapshots: list[dict[str, Any]] = []  # rows we'd find in calibration_snapshots
        self.training_rows: list[dict[str, Any]] = []  # league/p_side/y_side
        self._next_id = 1

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        q = " ".join(query.split())
        if "FROM calibration_snapshots" in q and "DISTINCT ON (sub_category)" in q:
            # Return latest per sub_category for sports/per_league:*
            by_sub: dict[str, dict[str, Any]] = {}
            for r in self.snapshots:
                sub = r["sub_category"]
                if not sub.startswith("per_league:"):
                    continue
                if r["calibrator_state"] in (None, "{}"):
                    continue
                cur = by_sub.get(sub)
                if cur is None or r["created_at"] > cur["created_at"]:
                    by_sub[sub] = r
            out = []
            for r in sorted(by_sub.values(), key=lambda x: x["sub_category"]):
                out.append({
                    "sub_category": r["sub_category"],
                    "calibrator_state": r["calibrator_state"],
                    "sample_count": r["sample_count"],
                    "fit_at": float(r["created_at"]),
                })
            return out
        if "FROM trades t" in q and "trade_postmortems" in q:
            return [dict(r) for r in self.training_rows]
        return []

    async def fetchval(self, query: str, *args: Any):
        q = " ".join(query.split())
        if "INSERT INTO calibration_snapshots" in q and "RETURNING id" in q:
            new_id = self._next_id
            self._next_id += 1
            sub_cat = args[0]
            n = int(args[4])
            state_json = args[5]
            self.snapshots.append({
                "id": new_id,
                "sub_category": sub_cat,
                "calibrator_state": state_json,
                "sample_count": n,
                "created_at": time.time(),
            })
            return new_id
        return None

    async def execute(self, query: str, *args: Any) -> str:
        return ""


@pytest.fixture
def fake_db(monkeypatch):
    fake = _FakeDb()
    monkeypatch.setattr(_db, "fetch", fake.fetch)
    monkeypatch.setattr(_db, "fetchval", fake.fetchval)
    monkeypatch.setattr(_db, "execute", fake.execute)
    calibrators.invalidate()
    return fake


# ---------------------------------------------------------------------------
# apply()
# ---------------------------------------------------------------------------


def test_apply_bypasses_without_league():
    calibrators.invalidate()
    out, info = calibrators.apply(0.7, None)
    assert out == 0.7
    assert info["applied"] is False
    assert "no league" in info["reason"]


def test_apply_bypasses_unsupported_league():
    calibrators.invalidate()
    out, info = calibrators.apply(0.4, "NCAAF")
    assert out == 0.4
    assert info["applied"] is False
    assert info["reason"] == "unsupported league"


def test_apply_bypasses_when_not_loaded():
    calibrators.invalidate()
    out, info = calibrators.apply(0.6, "NBA")
    assert out == 0.6
    assert info["applied"] is False
    assert info["reason"] == "no fitted calibrator"


def test_apply_isotonic_interpolates_between_knots():
    calibrators.invalidate()
    state = calibrators.get_state()
    state.calibrators["NBA"] = calibrators._Calibrator(
        league="NBA", type="isotonic",
        state={"x": [0.0, 0.5, 1.0], "y": [0.1, 0.4, 0.9]},
        fit_at=time.time(), sample_count=100,
    )
    out, info = calibrators.apply(0.5, "NBA")
    assert info["applied"] is True
    assert info["type"] == "isotonic"
    assert out == pytest.approx(0.4)
    out2, _ = calibrators.apply(0.25, "NBA")
    assert out2 == pytest.approx(0.25)  # midpoint of 0.1 and 0.4


def test_apply_platt_returns_sigmoid():
    calibrators.invalidate()
    state = calibrators.get_state()
    state.calibrators["NHL"] = calibrators._Calibrator(
        league="NHL", type="platt",
        state={"a": 2.0, "b": -1.0},  # sigmoid(2p - 1)
        fit_at=time.time(), sample_count=20,
    )
    out, info = calibrators.apply(0.5, "nhl")  # case-insensitive
    assert info["applied"] is True
    assert info["type"] == "platt"
    assert out == pytest.approx(0.5, abs=1e-6)


# ---------------------------------------------------------------------------
# load_all()
# ---------------------------------------------------------------------------


def test_load_all_picks_most_recent_per_league(fake_db):
    fake_db.snapshots.extend([
        {
            "id": 1, "sub_category": "per_league:NBA",
            "calibrator_state": json.dumps({"type": "isotonic", "x": [0.0, 1.0], "y": [0.0, 1.0]}),
            "sample_count": 100, "created_at": time.time() - 100,
        },
        {
            "id": 2, "sub_category": "per_league:NBA",
            "calibrator_state": json.dumps({"type": "platt", "a": 1.0, "b": 0.0}),
            "sample_count": 12, "created_at": time.time(),
        },
        {
            "id": 3, "sub_category": "per_league:MLB",
            "calibrator_state": json.dumps({"type": "isotonic", "x": [0.0, 1.0], "y": [0.2, 0.8]}),
            "sample_count": 75, "created_at": time.time(),
        },
    ])
    state = asyncio.run(calibrators.load_all())
    assert "NBA" in state.calibrators
    assert state.calibrators["NBA"].type == "platt"  # the newer one wins
    assert state.calibrators["MLB"].type == "isotonic"
    fresh = state.freshness()
    assert fresh["loaded_count"] == 2
    # Two leagues fitted (NBA, MLB); the rest of SUPPORTED_LEAGUES are missing.
    assert fresh["missing_count"] == len(calibrators.SUPPORTED_LEAGUES) - 2


# ---------------------------------------------------------------------------
# refit_all()
# ---------------------------------------------------------------------------


def test_refit_all_chooses_isotonic_when_enough_samples(fake_db):
    # 60 NBA samples (>= ISOTONIC_MIN_SAMPLES) — should fit isotonic.
    for i in range(60):
        # Synthetic miscalibration: model says 0.7 but only 50% of the time
        # the side actually wins. Calibrator should pull the prob down.
        fake_db.training_rows.append({
            "league": "NBA",
            "p_side": 0.7,
            "y_side": 1 if i % 2 == 0 else 0,
        })
    summary = asyncio.run(calibrators.refit_all())
    nba = next(r for r in summary["leagues"] if r["league"] == "NBA")
    assert nba["type"] == "isotonic"
    assert nba["sample_count"] == 60
    assert nba["snapshot_id"] is not None
    assert nba["brier_after"] <= nba["brier_before"]
    # State reloaded — apply() should now use the new isotonic curve.
    out, info = calibrators.apply(0.7, "NBA")
    assert info["applied"] is True
    assert out == pytest.approx(0.5, abs=0.05)


def test_refit_all_falls_back_to_platt_for_low_data(fake_db):
    # 15 NHL samples (>= MIN_REFIT but < ISOTONIC_MIN) — should fit platt.
    for i in range(15):
        fake_db.training_rows.append({
            "league": "NHL",
            "p_side": 0.5 + 0.01 * i,
            "y_side": 1 if i >= 7 else 0,  # mix of outcomes
        })
    summary = asyncio.run(calibrators.refit_all())
    nhl = next(r for r in summary["leagues"] if r["league"] == "NHL")
    assert nhl["type"] == "platt"
    assert nhl["snapshot_id"] is not None


def test_refit_all_skips_below_min_samples(fake_db):
    # Only 5 MLB samples — should be skipped, no snapshot persisted.
    for i in range(5):
        fake_db.training_rows.append({
            "league": "MLB", "p_side": 0.6, "y_side": 1,
        })
    summary = asyncio.run(calibrators.refit_all())
    mlb = next(r for r in summary["leagues"] if r["league"] == "MLB")
    assert mlb["snapshot_id"] is None
    assert mlb["skipped_reason"] is not None
    assert summary["fitted"] == 0
    # Every supported league skipped (only one had any rows, all below
    # MIN_REFIT_SAMPLES).
    assert summary["skipped"] == len(calibrators.SUPPORTED_LEAGUES)


def test_fetch_training_data_falls_back_when_v1_archive_absent(monkeypatch):
    """`v1_archive.trade_analyses` may not exist on fresh dev DBs that
    haven't run migration 0037.  In that case the V1 CTE must be
    omitted from the SQL ENTIRELY (not just predicate-gated) — Postgres
    parses FROM-clause relations before WHERE evaluates, so referencing
    a missing table raises ``UndefinedTable`` and would silently drop
    the LIVE rows too if not handled.  Asserts: preflight gates the
    V1 CTE OUT and live rows still flow.
    """
    monkeypatch.setenv("AXIOM_USE_V1_ARCHIVE_FOR_CALIBRATION", "1")

    captured_sql: list[str] = []
    live_row = {"league": "NBA", "p_side": 0.6, "y_side": 1}

    async def fake_fetchval(query: str, *args: Any):
        # Simulate `to_regclass(...) IS NOT NULL` returning False —
        # i.e. the v1_archive table does NOT exist in this DB.
        if "to_regclass" in query and "v1_archive" in query:
            return False
        return None

    async def fake_fetch(query: str, *args: Any):
        captured_sql.append(query)
        # Return live rows regardless — proves they flow even when V1
        # is gated out.
        return [dict(live_row)]

    monkeypatch.setattr(_db, "fetchval", fake_fetchval)
    monkeypatch.setattr(_db, "fetch", fake_fetch)

    grouped = asyncio.run(calibrators._fetch_training_data())

    # Live rows still made it through (the bug we're guarding against
    # would have returned {} here because UndefinedTable hit the broad
    # except).
    assert "NBA" in grouped
    assert grouped["NBA"]["probs"] == [0.6]
    assert grouped["NBA"]["ys"] == [1]

    # The composed SQL must NOT reference the missing table at all —
    # a WHERE-clause guard is insufficient because parsing happens
    # before predicate evaluation.
    assert len(captured_sql) == 1
    assert "v1_archive" not in captured_sql[0]
    assert "v1_settled" not in captured_sql[0]
    # And it must still contain the live CTE.
    assert "live_settled" in captured_sql[0]


def test_fetch_training_data_includes_v1_when_present(monkeypatch):
    """When the preflight reports the V1 archive table exists, the V1
    CTE is composed into the SQL so the UNION'd rows flow into refit.
    """
    monkeypatch.setenv("AXIOM_USE_V1_ARCHIVE_FOR_CALIBRATION", "1")

    captured_sql: list[str] = []

    async def fake_fetchval(query: str, *args: Any):
        if "to_regclass" in query and "v1_archive" in query:
            return True
        return None

    async def fake_fetch(query: str, *args: Any):
        captured_sql.append(query)
        return []  # rows themselves don't matter for this assertion

    monkeypatch.setattr(_db, "fetchval", fake_fetchval)
    monkeypatch.setattr(_db, "fetch", fake_fetch)

    asyncio.run(calibrators._fetch_training_data())

    assert len(captured_sql) == 1
    sql = captured_sql[0]
    assert "v1_archive.trade_analyses" in sql
    assert "v1_settled" in sql
    assert "UNION ALL" in sql
    assert "live_settled" in sql


def test_fetch_training_data_v1_disabled_via_env(monkeypatch):
    """The kill-switch env var skips the preflight AND the V1 CTE."""
    monkeypatch.setenv("AXIOM_USE_V1_ARCHIVE_FOR_CALIBRATION", "0")
    # Also disable shadow buffer so this test is hermetic against
    # the unrelated `AXIOM_USE_SHADOW_BUFFER_FOR_CALIBRATION` env
    # (default on) — its own preflight would otherwise show up in
    # `fetchval_calls` and confuse the v1-scoped assertions below.
    monkeypatch.setenv("AXIOM_USE_SHADOW_BUFFER_FOR_CALIBRATION", "0")

    captured_sql: list[str] = []
    fetchval_calls: list[str] = []

    async def fake_fetchval(query: str, *args: Any):
        fetchval_calls.append(query)
        return None

    async def fake_fetch(query: str, *args: Any):
        captured_sql.append(query)
        return []

    monkeypatch.setattr(_db, "fetchval", fake_fetchval)
    monkeypatch.setattr(_db, "fetch", fake_fetch)

    asyncio.run(calibrators._fetch_training_data())

    # Both kill switches set — neither preflight runs.
    assert not any("to_regclass" in q for q in fetchval_calls)
    # No V1 reference in the composed SQL.
    assert "v1_archive" not in captured_sql[0]
    assert "v1_settled" not in captured_sql[0]


# ---------------------------------------------------------------------------
# predict() integration
# ---------------------------------------------------------------------------


class _FakeProbaModel:
    n_features_in_ = 2

    def predict_proba(self, X):
        return [[0.3, 0.7]]


def _seed_predictor():
    s = models_loader._state
    s.ready = True
    s.in_progress = False
    s.error = None
    s.predictors = {"models/saved/meta_model.pkl": {"version": "test", "sha256": "x"}}
    s.loaded = {"models/saved/meta_model.pkl": {"ok": True}}
    models_loader._loaded_objects.clear()
    models_loader._loaded_objects["models/saved/meta_model.pkl"] = _FakeProbaModel()
    predictor.invalidate()


def test_predict_applies_per_league_calibrator(monkeypatch):
    calibrators.invalidate()
    _seed_predictor()
    state = calibrators.get_state()
    # Compress probabilities towards 0.5 so we can see the shift.
    state.calibrators["NBA"] = calibrators._Calibrator(
        league="NBA", type="isotonic",
        state={"x": [0.0, 0.7, 1.0], "y": [0.1, 0.55, 0.9]},
        fit_at=time.time(), sample_count=100,
    )
    out = asyncio.run(predictor.predict([1.0, 2.0], league="NBA"))
    assert out.probability == pytest.approx(0.55, abs=1e-6)
    assert out.raw["calibration"]["applied"] is True
    assert out.raw["uncalibrated_probability"] == pytest.approx(0.7)


def test_predict_bypasses_calibration_without_league():
    calibrators.invalidate()
    _seed_predictor()
    out = asyncio.run(predictor.predict([1.0, 2.0]))
    assert out.probability == pytest.approx(0.7)
    assert out.raw["calibration"]["applied"] is False
    assert "uncalibrated_probability" not in out.raw


# ---------------------------------------------------------------------------
# Settlement scheduler integration: nightly per-league refit gate
# ---------------------------------------------------------------------------


class _StubSchedulerSettings:
    """Minimal stand-in for `app.config.Settings` — the settlement
    scheduler only reads these two fields out of the frozen dataclass."""

    def __init__(self, interval: float) -> None:
        self.settlement_enabled = True
        self.settlement_interval_seconds = interval


def _stub_cycle_result():
    from app.settlement.pipeline import CycleResult

    return CycleResult(
        started_at="2026-01-01T00:00:00+00:00",
        completed_at="2026-01-01T00:00:00+00:00",
        duration_seconds=0.0,
    )


def _stub_retrain_result():
    from app.settlement.retrain import RetrainCycleResult

    return RetrainCycleResult(
        started_at="2026-01-01T00:00:00+00:00",
        completed_at="2026-01-01T00:00:00+00:00",
    )


def _stub_margin_summary():
    from app import margin_regressor

    return margin_regressor.RefitSummary(
        started_at="2026-01-01T00:00:00+00:00",
        completed_at="2026-01-01T00:00:00+00:00",
    )


def test_settlement_scheduler_drives_per_league_calibrator_refit_then_gates(
    monkeypatch,
):
    """One scheduler iteration runs `calibrators.maybe_refit()` and a
    second iteration immediately after is suppressed by the 24h gate;
    the per-cycle `status()['last_cycle']['calibrators']` payload
    reflects the refit summary and then `None`."""
    import sys

    from app import calibrators as calibrators_mod, margin_regressor
    from app.settlement.scheduler import _SettlementScheduler

    # `app.settlement.__init__` rebinds the submodule name `scheduler`
    # to the singleton instance, so the import-as form picks up the
    # instance, not the module.  Pull the actual module out of
    # `sys.modules` so monkeypatching `run_cycle` etc. lands on it.
    sched_mod = sys.modules["app.settlement.scheduler"]

    calibrators_mod.invalidate()

    # Capture self._last as observed *before* each iteration body runs,
    # by hooking the first dependency the loop calls (`run_cycle`).
    # That gives us a snapshot of the previous iteration's status
    # payload — exactly what /settlement/status would have served.
    sched = _SettlementScheduler()
    last_cycles_observed: list[dict[str, Any]] = []
    refit_call_count = {"n": 0}

    async def fake_run_cycle():
        last_cycles_observed.append(dict(sched._last))
        return _stub_cycle_result()

    async def fake_run_retrain_cycle():
        return _stub_retrain_result()

    async def fake_refit_all_leagues():
        return _stub_margin_summary()

    refit_marker = {"source": "stub_refit_all", "leagues": [], "fitted": 0}

    async def fake_refit_all():
        # Real `refit_all` does this at the end; reproduce it so the
        # real `maybe_refit` gate logic gates the next iteration.
        refit_call_count["n"] += 1
        calibrators_mod._state.last_refit_at = time.time()
        calibrators_mod._state.last_refit_summary = dict(refit_marker)
        return dict(refit_marker)

    monkeypatch.setattr(sched_mod, "run_cycle", fake_run_cycle)
    monkeypatch.setattr(sched_mod, "run_retrain_cycle", fake_run_retrain_cycle)
    monkeypatch.setattr(
        margin_regressor, "refit_all_leagues", fake_refit_all_leagues
    )
    # Stub the underlying `refit_all` so the real `maybe_refit` runs
    # (and therefore the real 24h gate is what we're exercising).
    monkeypatch.setattr(calibrators_mod, "refit_all", fake_refit_all)
    monkeypatch.setattr(
        sched_mod, "get_settings", lambda: _StubSchedulerSettings(0.01)
    )

    async def _drive():
        await sched.start()
        try:
            # Wait until at least three `run_cycle` calls have happened:
            # iteration 1 (snapshot empty), iteration 2 (snapshot iter-1),
            # iteration 3 (snapshot iter-2).  That gives us status
            # payloads for both the refit-runs and refit-gated cycles.
            for _ in range(500):
                if len(last_cycles_observed) >= 3:
                    break
                await asyncio.sleep(0.01)
        finally:
            await sched.stop()

    asyncio.run(_drive())

    # The 24h gate on `maybe_refit()` must have fired exactly once
    # across multiple iterations — the refit runs on the first cycle
    # (state.last_refit_at is None) and is suppressed on every
    # subsequent cycle within REFIT_INTERVAL_SECONDS.
    assert refit_call_count["n"] == 1, (
        f"expected exactly one refit across multiple cycles, "
        f"got {refit_call_count['n']}"
    )
    assert calibrators_mod.REFIT_INTERVAL_SECONDS >= 24 * 3600

    # last_cycles_observed[0] is the empty pre-iteration-1 snapshot.
    assert last_cycles_observed[0] == {}

    # last_cycles_observed[1] is iteration 1's status payload — the
    # refit summary must be present.
    iter1_status = last_cycles_observed[1]
    assert "calibrators" in iter1_status
    assert iter1_status["calibrators"] == refit_marker

    # last_cycles_observed[2] is iteration 2's status payload — the
    # gate suppressed the refit so the field is exactly `None`.
    iter2_status = last_cycles_observed[2]
    assert "calibrators" in iter2_status
    assert iter2_status["calibrators"] is None

    # The live `scheduler.status()` payload reflects the most recent
    # cycle: refit suppressed, calibrators field is None.
    final_status = sched.status()["last_cycle"]
    assert final_status["calibrators"] is None
