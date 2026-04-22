"""Tests for the centralised feature store (registry + drift)."""
from __future__ import annotations

import json

import pytest

from app.features import FEATURE_COUNT, FEATURE_NAMES, build_vector, registry
from app.features import drift as feature_drift
from app.features.registry import FeatureRegistry, FeatureSpec, BuildContext


# ---------------------------------------------------------------------------
# Registry catalogue
# ---------------------------------------------------------------------------


def test_registry_matches_legacy_feature_names():
    """The registry IS the source of truth — these must agree exactly."""
    assert registry.names() == FEATURE_NAMES
    assert len(registry) == FEATURE_COUNT == 32


def test_every_feature_has_metadata():
    meta = registry.metadata()
    assert len(meta) == FEATURE_COUNT
    for row in meta:
        assert row["name"]
        assert row["source"]
        assert row["transform"]
        assert row["dtype"] in {
            "prob", "signed_unit", "bool01", "idx_norm", "unit", "raw",
        }
        assert isinstance(row["default"], float)


def test_registry_rejects_duplicate_registration():
    r = FeatureRegistry()
    r.register(FeatureSpec("a", "src", "t", "raw", 0.0, lambda _: 0.0))
    with pytest.raises(ValueError, match="already registered"):
        r.register(FeatureSpec("a", "src", "t", "raw", 0.0, lambda _: 0.0))


def test_build_vector_populates_present_for_supplied_inputs():
    fv = build_vector({
        "consensus_probability": 0.6,
        "league": "NBA",
        "game_context": {"kalshi_price": 0.55},
    })
    assert "consensus_probability" in fv.present
    assert "kalshi_price" in fv.present
    assert "league" in fv.present


def test_derived_features_read_earlier_computed_values():
    # prob_edge = pred_prob * edge — verifying the in-order derived
    # features pull from BuildContext.computed instead of re-deriving.
    fv = build_vector(
        {"consensus_probability": 0.7},
        current_price=0.55,
    )
    d = fv.as_dict()
    assert d["pred_prob"] == pytest.approx(0.7)
    assert d["edge"] == pytest.approx(0.15)
    assert d["prob_edge"] == pytest.approx(0.7 * 0.15)


# ---------------------------------------------------------------------------
# Drift report
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_drift_collector():
    feature_drift.reset()
    yield
    feature_drift.reset()


def test_drift_report_shape_matches_registry():
    r = feature_drift.report()
    assert r["observations"] == 0
    assert {row["name"] for row in r["items"]} == set(FEATURE_NAMES)
    # No samples yet -> every row is insufficient_samples.
    assert all(row["status"] == "insufficient_samples" for row in r["items"])


def test_drift_records_observations_from_build_features(monkeypatch):
    # Driving build_features (the legacy alias) must tee into drift.
    from app.orchestrator.features import build_features

    for _ in range(feature_drift._MIN_SAMPLES_FOR_DRIFT + 5):
        build_features({"consensus_probability": 0.5})

    r = feature_drift.report()
    assert r["observations"] >= feature_drift._MIN_SAMPLES_FOR_DRIFT
    pred_row = next(row for row in r["items"] if row["name"] == "pred_prob")
    assert pred_row["n_live"] >= feature_drift._MIN_SAMPLES_FOR_DRIFT
    assert pred_row["live_mean"] == pytest.approx(0.5, abs=1e-6)


def test_drift_flags_alert_on_extreme_shift():
    # Push pred_prob far above the baseline mean so |z| > alert threshold.
    for _ in range(60):
        feature_drift.collector.record([1.0] * FEATURE_COUNT)
    r = feature_drift.report()
    pred = next(row for row in r["items"] if row["name"] == "pred_prob")
    assert pred["status"] in {"warn", "alert"}
    assert pred["z_drift"] is not None
    # Constant 1.0 ≫ baseline_mean(0.52) / baseline_std(0.14) ≈ 3.4σ
    assert pred["z_drift"] > 2.0


def test_drift_handles_zero_baseline_std():
    # `kelly` has baseline std 0.0 — we should still get a finite z_drift
    # even if the live value matches (z=0) or differs (z=±50 cap).
    for _ in range(60):
        feature_drift.collector.record([0.0] * FEATURE_COUNT)
    r = feature_drift.report()
    kelly = next(row for row in r["items"] if row["name"] == "kelly")
    # baseline kelly mean=0.25, live mean=0.0 -> negative drift
    assert kelly["z_drift"] is not None
    assert kelly["z_drift"] <= 0.0


def test_drift_baseline_loaded_from_shipped_file():
    r = feature_drift.report()
    assert r["baseline_path"] is not None
    assert r["baseline_features"] == FEATURE_COUNT


def test_custom_baseline_via_env(tmp_path, monkeypatch):
    # Operator-supplied baseline should override the shipped one.
    custom = tmp_path / "custom.json"
    custom.write_text(json.dumps({
        "features": {
            "pred_prob": {"mean": 0.0, "std": 1.0, "n": 1},
        }
    }))
    monkeypatch.setenv("AXIOM_FEATURE_DRIFT_BASELINE", str(custom))
    coll = feature_drift.FeatureDriftCollector()
    for _ in range(60):
        coll.record([0.5] * FEATURE_COUNT)
    rep = coll.report()
    assert rep["baseline_path"] == str(custom)
    pred = next(row for row in rep["items"] if row["name"] == "pred_prob")
    # live_mean 0.5 against baseline mean=0 std=1 -> z=0.5
    assert pred["z_drift"] == pytest.approx(0.5)


def test_drift_drops_nan_and_infinity():
    coll = feature_drift.FeatureDriftCollector()
    coll.record([float("nan")] * FEATURE_COUNT)
    coll.record([float("inf")] * FEATURE_COUNT)
    rep = coll.report()
    assert rep["observations"] == 2  # counted, but values dropped
    pred = next(row for row in rep["items"] if row["name"] == "pred_prob")
    assert pred["n_live"] == 0
