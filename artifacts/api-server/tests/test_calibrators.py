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
    out, info = calibrators.apply(0.4, "PGA")
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
    assert fresh["missing_count"] == 3  # NHL, NCAAB, NFL


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
    assert summary["skipped"] == 5  # all 5 leagues skipped


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
