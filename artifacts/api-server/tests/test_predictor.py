"""Unit tests for the predictor service."""
from __future__ import annotations

import asyncio

import pytest

from app import models_loader, predictor


class _StubProbaModel:
    """Two-class classifier stub: returns a fixed proba row."""
    n_features_in_ = 4

    def __init__(self, row=(0.3, 0.7)):
        self._row = list(row)

    def predict_proba(self, X):
        import numpy as np
        return np.array([self._row])


class _StubMultiProbaModel:
    n_features_in_ = 2

    def predict_proba(self, X):
        import numpy as np
        return np.array([[0.1, 0.6, 0.3]])


class _StubRegressor:
    """No predict_proba — exercises the predict() fallback + clamp."""
    n_features_in_ = 3

    def __init__(self, value):
        self._value = value

    def predict(self, X):
        import numpy as np
        return np.array([self._value])


def _seed_state(predictors: dict, loaded: dict, objects: dict) -> None:
    """Reset and seed the loader's in-memory state for a single test."""
    s = models_loader._state
    s.ready = True
    s.in_progress = False
    s.error = None
    s.predictors = dict(predictors)
    s.loaded = dict(loaded)
    models_loader._loaded_objects.clear()
    models_loader._loaded_objects.update(objects)
    predictor.invalidate()


def _make_meta(version: str = "abc123def456") -> dict:
    return {"size": 1, "mtime": 0, "sha256": version + "0" * 32, "version": version}


def test_pick_active_prefers_saved_meta_model():
    saved = "models/saved/meta_model.pkl"
    archived = "models/archive/meta_model.pkl"
    obj_saved = _StubProbaModel()
    obj_archived = _StubProbaModel(row=(0.9, 0.1))
    _seed_state(
        {saved: _make_meta("aaa"), archived: _make_meta("bbb")},
        {saved: {"ok": True}, archived: {"ok": True}},
        {saved: obj_saved, archived: obj_archived},
    )
    info = predictor.active_model_info()
    assert info is not None and info["name"] == saved
    assert info["version"] == "aaa"


def test_pick_active_falls_back_to_archive_when_saved_missing():
    archived = "models/archive/meta_model.pkl"
    obj = _StubProbaModel()
    _seed_state(
        {archived: _make_meta("zzz")},
        {archived: {"ok": True}},
        {archived: obj},
    )
    info = predictor.active_model_info()
    assert info is not None and info["name"] == archived


def test_pick_active_prefers_latest_versioned_over_archive():
    versioned_old = "models/versions/v1.5/meta_model.pkl"
    versioned_new = "models/versions/v1.42/meta_model.pkl"
    archived = "models/archive/meta_model.pkl"
    obj_new = _StubProbaModel()
    obj_archived = _StubProbaModel()
    _seed_state(
        {
            versioned_old: _make_meta("oldv"),
            versioned_new: _make_meta("newv"),
            archived: _make_meta("arc"),
        },
        {archived: {"ok": True}},  # only archive smoke-loaded
        {versioned_new: obj_new, archived: obj_archived},
    )
    info = predictor.active_model_info()
    assert info is not None and info["name"] == versioned_new


def test_predict_two_class_proba():
    saved = "models/saved/meta_model.pkl"
    obj = _StubProbaModel(row=(0.25, 0.75))
    _seed_state(
        {saved: _make_meta("v1")},
        {saved: {"ok": True}},
        {saved: obj},
    )
    out = asyncio.run(predictor.predict([1.0, 2.0]))  # short — auto-padded
    assert out.probability == pytest.approx(0.75)
    assert out.confidence == pytest.approx(0.5)
    assert out.model_name == saved
    assert out.model_version == "v1"
    assert out.model_type == "_StubProbaModel"


def test_predict_dict_features():
    saved = "models/saved/meta_model.pkl"
    obj = _StubProbaModel()
    _seed_state(
        {saved: _make_meta("v1")},
        {saved: {"ok": True}},
        {saved: obj},
    )
    out = asyncio.run(predictor.predict({"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0}))
    assert out.probability == pytest.approx(0.7)


def test_predict_multi_class_uses_max_and_top_two_margin():
    saved = "models/saved/meta_model.pkl"
    obj = _StubMultiProbaModel()
    _seed_state(
        {saved: _make_meta("vm")},
        {saved: {"ok": True}},
        {saved: obj},
    )
    out = asyncio.run(predictor.predict([0.0, 0.0]))
    assert out.probability == pytest.approx(0.6)
    assert out.confidence == pytest.approx(0.3)


def test_predict_regressor_fallback_clamps():
    saved = "models/saved/meta_model.pkl"
    obj = _StubRegressor(value=1.5)  # clamped to 1.0
    _seed_state(
        {saved: _make_meta("vr")},
        {saved: {"ok": True}},
        {saved: obj},
    )
    out = asyncio.run(predictor.predict([0.0, 0.0, 0.0]))
    assert out.probability == 1.0
    assert out.confidence is None


def test_predict_unavailable_when_loader_not_ready():
    s = models_loader._state
    s.ready = False
    s.predictors = {}
    s.loaded = {}
    models_loader._loaded_objects.clear()
    predictor.invalidate()
    with pytest.raises(predictor.PredictorUnavailable):
        asyncio.run(predictor.predict([1.0, 2.0, 3.0]))


def test_predict_unavailable_when_no_canonical_loaded(monkeypatch):
    saved = "models/saved/meta_model.pkl"
    _seed_state(
        {saved: _make_meta("v1")},
        {saved: {"ok": False, "error": "broken"}},
        {},  # nothing actually loaded
    )

    # The lazy-load path would otherwise read the real on-disk artifact
    # cache (if a prior dev session populated it). Force the lazy load
    # to fail so we exercise the "nothing canonical loaded" branch.
    async def _boom(name):
        raise RuntimeError(f"forced load failure for {name}")

    monkeypatch.setattr(models_loader, "load_predictor", _boom)

    with pytest.raises(predictor.PredictorUnavailable):
        asyncio.run(predictor.predict([1.0, 2.0, 3.0, 4.0]))


def test_invalidate_rebinds_after_reload():
    saved = "models/saved/meta_model.pkl"
    obj_old = _StubProbaModel(row=(0.4, 0.6))
    _seed_state(
        {saved: _make_meta("old")},
        {saved: {"ok": True}},
        {saved: obj_old},
    )
    out1 = asyncio.run(predictor.predict([0.0] * 4))
    assert out1.model_version == "old"

    # Simulate a /api/models/reload swap.
    obj_new = _StubProbaModel(row=(0.1, 0.9))
    _seed_state(
        {saved: _make_meta("new")},
        {saved: {"ok": True}},
        {saved: obj_new},
    )
    out2 = asyncio.run(predictor.predict([0.0] * 4))
    assert out2.model_version == "new"
    assert out2.probability == pytest.approx(0.9)


def test_active_model_info_lazy_peek_before_first_predict():
    saved = "models/saved/meta_model.pkl"
    obj = _StubProbaModel()
    _seed_state(
        {saved: _make_meta("vp")},
        {saved: {"ok": True}},
        {saved: obj},
    )
    info = predictor.active_model_info()
    assert info is not None
    assert info["name"] == saved
    assert info["version"] == "vp"
    assert info["bound"] is False  # not yet bound by a predict() call
    asyncio.run(predictor.predict([0.0] * 4))
    info_after = predictor.active_model_info()
    assert info_after is not None and info_after["bound"] is True


def test_coerce_features_truncates_when_too_long():
    saved = "models/saved/meta_model.pkl"
    obj = _StubProbaModel()  # n_features_in_=4
    _seed_state(
        {saved: _make_meta("v1")},
        {saved: {"ok": True}},
        {saved: obj},
    )
    # 6 values supplied — should be truncated to 4 without error.
    out = asyncio.run(predictor.predict([1.0, 2.0, 3.0, 4.0, 5.0, 6.0]))
    assert out.probability == pytest.approx(0.7)
