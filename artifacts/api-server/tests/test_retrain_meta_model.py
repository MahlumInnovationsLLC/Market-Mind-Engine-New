"""Tests for the meta-model retrain pipeline + promoted-feature wiring."""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Promoted-feature wiring (registry + vector)
# ---------------------------------------------------------------------------


def test_registry_production_specs_includes_promoted_shadow():
    from app.features.registry import FeatureRegistry, FeatureSpec, registry as live_registry

    r = FeatureRegistry()
    r.register(FeatureSpec("a", "src", "t", "raw", 0.0, lambda _: 1.0))
    r.register(FeatureSpec(
        "uw__shadow_x", "src", "t", "raw", 0.0, lambda _: 2.0, shadow=True,
    ))
    r.register(FeatureSpec(
        "uw__shadow_y", "src", "t", "raw", 0.0, lambda _: 3.0, shadow=True,
    ))

    # No promotion — production view matches the built-in (non-shadow) set.
    assert r.production_names() == ("a",)
    # Promoting one shadow opts it back into the production layout in
    # the registry's declared order.
    assert r.production_names({"uw__shadow_y"}) == ("a", "uw__shadow_y")
    assert r.production_names({"uw__shadow_x", "uw__shadow_y"}) == (
        "a", "uw__shadow_x", "uw__shadow_y",
    )
    # Live registry singleton stays unchanged when no promotion set is
    # given (regression guard for the shipped 32-column model).
    assert len(live_registry.production_specs()) == len(live_registry.specs())


def test_build_vector_extends_when_shadow_promoted(monkeypatch):
    """Promoting a shadow spec should grow the live vector by one slot."""
    from app.features import vector as feature_vector
    from app.features.registry import FeatureSpec, registry

    # Register a brand-new shadow spec for the duration of the test;
    # we can't unregister, but the spec name is unique so re-running
    # the test (or other tests) won't collide.
    spec_name = "uw__test__count_24h__retrain_pipeline"
    if registry.get(spec_name) is None:
        registry.register(FeatureSpec(
            spec_name,
            source="autoresearch",
            transform="constant 0.42",
            dtype="raw",
            default=0.0,
            compute=lambda _bc: 0.42,
            shadow=True,
        ))

    feature_vector.set_promoted_names([])
    fv_before = feature_vector.build_vector({"consensus_probability": 0.5})
    n_before = len(fv_before.values)
    assert spec_name not in fv_before.names

    feature_vector.set_promoted_names([spec_name])
    try:
        fv_after = feature_vector.build_vector({"consensus_probability": 0.5})
        assert len(fv_after.values) == n_before + 1
        assert fv_after.names[-1] == spec_name
        assert fv_after.values[-1] == pytest.approx(0.42)
        # Built-in slots stay positionally stable so the older 32-column
        # model artifacts keep working under the predictor's
        # pad/truncate path.
        assert fv_after.names[:n_before] == fv_before.names
    finally:
        feature_vector.set_promoted_names([])


def test_set_promoted_names_ignores_unregistered(monkeypatch):
    from app.features import vector as feature_vector

    feature_vector.set_promoted_names(["does_not_exist"])
    try:
        assert feature_vector.get_promoted_names() == frozenset()
    finally:
        feature_vector.set_promoted_names([])


# ---------------------------------------------------------------------------
# Retrain script — column-list assembly + artifact write
# ---------------------------------------------------------------------------


def _import_retrain_script():
    """Import scripts/retrain_meta_model.py as a module for unit tests."""
    script_dir = Path(__file__).resolve().parent.parent / "scripts"
    if str(script_dir) not in sys.path:
        sys.path.insert(0, str(script_dir))
    if "retrain_meta_model" in sys.modules:
        return importlib.reload(sys.modules["retrain_meta_model"])
    return importlib.import_module("retrain_meta_model")


def test_resolve_columns_orders_promoted_after_builtins():
    retrain = _import_retrain_script()
    from app.features import FEATURE_NAMES
    from app.features.registry import FeatureSpec, registry

    spec_name = "uw__resolve__test__retrain_columns"
    if registry.get(spec_name) is None:
        registry.register(FeatureSpec(
            spec_name, "autoresearch", "constant 0.0",
            "raw", 0.0, lambda _bc: 0.0, shadow=True,
        ))

    cols = retrain._resolve_columns([spec_name])
    assert cols[: len(FEATURE_NAMES)] == FEATURE_NAMES
    assert cols[-1] == spec_name
    assert len(cols) == len(FEATURE_NAMES) + 1


def test_train_produces_classifier_with_expected_n_features_in():
    retrain = _import_retrain_script()
    from app.features import FEATURE_NAMES

    columns = list(FEATURE_NAMES) + ["uw__synthetic__count_24h__zzz"]
    clf, metrics = retrain.train(columns, n_rows=400, seed=42)
    assert getattr(clf, "n_features_in_", None) == len(columns)
    assert metrics["n_features"] == len(columns)
    assert 0.0 <= metrics["accuracy"] <= 1.0
    assert metrics["log_loss"] > 0.0


def test_write_artifact_writes_sidecar(tmp_path):
    retrain = _import_retrain_script()
    from app.features import FEATURE_NAMES

    columns = list(FEATURE_NAMES) + ["uw__synthetic__count_24h__sidecar"]
    clf, metrics = retrain.train(columns, n_rows=200, seed=1)
    out = tmp_path / "meta_model.pkl"
    written = retrain._write_artifact(clf, columns, metrics, out, ["uw__a"])
    assert Path(written["model"]).exists()
    sidecar_path = Path(written["sidecar"])
    assert sidecar_path.exists()
    side = json.loads(sidecar_path.read_text())
    assert side["columns"] == columns
    assert side["promoted"] == ["uw__a"]
    assert side["metrics"]["n_features"] == len(columns)


# ---------------------------------------------------------------------------
# End-to-end: feature_bridge + real registry + retrain script
# ---------------------------------------------------------------------------


def test_feature_bridge_registers_shadow_spec_in_real_registry():
    """A name from autoresearch_features must turn into a real shadow spec."""
    from app.autoresearch import feature_bridge
    from app.features.registry import registry

    name = "uw__bridge__test__count_24h__nfl"
    # Defensive: this test's name is unique, so no prior registration.
    assert registry.get(name) is None

    spec = feature_bridge.ensure_shadow_spec(name, default=0.0)
    assert spec.shadow is True
    assert spec.name == name
    # Idempotent: calling again returns the same spec, no double-register.
    assert feature_bridge.ensure_shadow_spec(name) is spec
    # Registry now reports it as a shadow spec available for promotion.
    assert spec in registry.shadow_specs()


def test_promotion_extends_live_vector_via_bridge_and_real_registry():
    """End-to-end: bridge-register → set_promoted_names → build_vector grows."""
    from app.autoresearch import feature_bridge
    from app.features import vector as feature_vector
    from app.features.registry import registry

    name = "uw__bridge__test__sum_premium_24h__crypto__btc"
    feature_bridge.ensure_shadow_spec(name, default=0.0)
    assert registry.get(name) is not None

    feature_vector.set_promoted_names([])
    fv_before = feature_vector.build_vector({"consensus_probability": 0.5})

    feature_vector.set_promoted_names([name])
    try:
        # The promoted set was accepted (the bridge made the spec real).
        assert name in feature_vector.get_promoted_names()
        # Live vector grows by exactly one slot, in the registry's
        # declared order (i.e. at the tail since the spec was added
        # after the built-ins).
        fv_after = feature_vector.build_vector({
            "consensus_probability": 0.5,
            "uw_signals": {name: 7.0},
        })
        assert len(fv_after.values) == len(fv_before.values) + 1
        assert fv_after.names[-1] == name
        assert fv_after.values[-1] == pytest.approx(7.0)
        # Missing snapshot → spec falls back to its default rather than NaN.
        fv_default = feature_vector.build_vector(
            {"consensus_probability": 0.5}
        )
        assert fv_default.values[-1] == pytest.approx(0.0)
    finally:
        feature_vector.set_promoted_names([])


def test_retrain_main_fails_fast_when_promoted_unresolved(monkeypatch, capsys):
    """If a promoted name can't be registered as a shadow spec, abort."""
    retrain = _import_retrain_script()

    # Simulate a DB row whose name we will fail to register by stubbing
    # the bridge to refuse it.
    monkeypatch.setattr(
        retrain.ar_store, "list_promoted_names_sync",
        lambda dsn=None: ["uw__broken__no_register__nfl"],
    )
    monkeypatch.setattr(
        retrain.ar_feature_bridge, "register_names",
        lambda names: [],  # nothing successfully registered
    )
    rc = retrain.main(["--dry-run"])
    assert rc == 2  # explicit non-zero exit so the caller sees the failure


def test_retrain_main_dry_run_includes_promoted_after_bridge(
    monkeypatch, capsys
):
    """Real path: promoted name is bridge-registered, columns include it."""
    retrain = _import_retrain_script()
    from app.features import FEATURE_NAMES

    name = "uw__retrain_main_dry_run__count_1h__elections"
    monkeypatch.setattr(
        retrain.ar_store, "list_promoted_names_sync",
        lambda dsn=None: [name],
    )
    rc = retrain.main(["--dry-run"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["promoted"] == [name]
    assert payload["columns"][: len(FEATURE_NAMES)] == list(FEATURE_NAMES)
    assert payload["columns"][-1] == name
    assert payload["n_columns"] == len(FEATURE_NAMES) + 1


def test_main_dry_run_prints_columns(tmp_path, capsys, monkeypatch):
    retrain = _import_retrain_script()
    monkeypatch.setattr(
        retrain.ar_store, "list_promoted_names_sync",
        lambda dsn=None: [],
    )
    rc = retrain.main(["--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    from app.features import FEATURE_NAMES
    assert payload["promoted"] == []
    assert payload["columns"] == list(FEATURE_NAMES)
    assert payload["n_columns"] == len(FEATURE_NAMES)
