"""Tests for the commodities trainer (task #217).

The trainer ships as the operator-facing pipeline that takes the
historical OHLCV in `commodities_ohlcv` and produces the artifacts
the predictor's model loader picks up.  These tests cover:

* dataset construction shape + label balance
* end-to-end trained-stack fit on synthetic bars
* predict_proba contract (compatible with the predictor's `_proba_yes`)
* pickle round-trip (so the loader's deserialize step is sane)
* trained-vs-baseline comparison helper produces both branches
"""
from __future__ import annotations

import math
import os
import pickle
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from app.commodities import training
from app.commodities.features import FEATURE_COUNT
from app.commodities.models import BaselineLGBXGBStack
from app.commodities.training import (
    DEFAULT_STRIKE_OFFSETS,
    TrainedLGBXGBStack,
    build_training_dataset,
    compare_to_baseline,
    load_artifact,
    save_artifact,
    train_all_timeframes,
    train_timeframe_stack,
)


def _bars(n: int = 130, *, base: float = 75.0, drift: float = 0.001,
          vol: float = 0.004) -> list[dict]:
    """Pseudo-random walk with mild drift — deterministic via seed."""
    import random

    rng = random.Random(1234)
    now = datetime.now(timezone.utc)
    out: list[dict] = []
    price = base
    for i in range(n):
        step = drift + rng.gauss(0.0, vol)
        price = max(0.01, price * (1.0 + step))
        out.append({
            "ts": now - timedelta(hours=(n - i)),
            "o": price * (1 - vol / 2),
            "h": price * (1 + vol),
            "l": price * (1 - vol),
            "c": price,
            "v": 1000.0 + i,
        })
    return out


def test_dataset_shape_and_balance():
    bars = {"WTI": _bars(110)}
    X, y, counts = build_training_dataset(
        bars, timeframe="1h", lookback=50,
        strike_offsets=DEFAULT_STRIKE_OFFSETS,
    )
    assert counts["WTI"] == len(X)
    assert len(X) > 0
    assert all(len(row) == FEATURE_COUNT for row in X)
    assert set(y) == {0, 1}, "strike grid should produce both YES and NO labels"


def test_train_timeframe_stack_returns_valid_predictor():
    bars = {"WTI": _bars(130), "GOLD": _bars(130, base=2000.0)}
    stack = train_timeframe_stack(
        bars, timeframe="1h", lookback=50, max_rows=10_000,
    )
    assert isinstance(stack, TrainedLGBXGBStack)
    assert stack.n_features_in_ == FEATURE_COUNT
    assert stack.timeframe == "1h"
    assert stack.metadata["rows_total"] > 0
    assert stack.metadata["rows_train"] > 0
    assert stack.metadata["rows_val"] > 0
    assert "val_brier" in stack.metadata
    # Brier must be in [0, 1] and finite — sanity check on the fit.
    assert 0.0 <= stack.metadata["val_brier"] <= 1.0
    assert math.isfinite(stack.metadata["val_log_loss"])


def test_predict_proba_shape_and_range():
    import numpy as np

    bars = {"WTI": _bars(130)}
    stack = train_timeframe_stack(bars, timeframe="1d", lookback=50)
    X = np.zeros((4, FEATURE_COUNT), dtype=float)
    # Populate `direction_sign` so the orientation feature is sensible.
    X[:, 10] = 1.0
    proba = stack.predict_proba(X)
    assert proba.shape == (4, 2)
    assert ((proba >= 0.0) & (proba <= 1.0)).all()
    np.testing.assert_allclose(proba.sum(axis=1), 1.0, atol=1e-9)


def test_train_too_few_rows_raises():
    bars = {"WTI": _bars(60)}  # lookback=50 -> ~9 bars * 5 strikes * 2 = ~90 rows
    with pytest.raises(RuntimeError, match="not enough training rows"):
        train_timeframe_stack(bars, timeframe="1h", lookback=50)


def test_pickle_round_trip_preserves_predictions():
    import numpy as np

    bars = {"WTI": _bars(130)}
    stack = train_timeframe_stack(bars, timeframe="1h", lookback=50)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "lgb_xgb_1h.pkl")
        save_artifact(stack, path)
        assert os.path.exists(path)
        with open(path, "rb") as f:
            raw = pickle.load(f)
        assert isinstance(raw, TrainedLGBXGBStack)
        loaded = load_artifact(path)
    rng = np.random.default_rng(0)
    X = rng.standard_normal((6, FEATURE_COUNT))
    np.testing.assert_allclose(stack.predict_proba(X), loaded.predict_proba(X))


def test_compare_to_baseline_emits_both_branches():
    bars = {"WTI": _bars(130), "GOLD": _bars(130, base=2000.0)}
    stack = train_timeframe_stack(bars, timeframe="1h", lookback=50)
    rep = compare_to_baseline(stack, bars, timeframe="1h", lookback=50)
    assert rep["n_holdout"] > 0
    for branch in ("trained", "baseline"):
        assert branch in rep and rep[branch] is not None
        assert 0.0 <= rep[branch]["brier"] <= 1.0
        assert math.isfinite(rep[branch]["log_loss"])


def test_train_all_timeframes_skips_short_series_quietly():
    bars = {
        "1h": {"WTI": _bars(130)},
        "4h": {"WTI": _bars(60)},  # too few -> skipped
        "1d": {},                  # empty -> skipped
    }
    out = train_all_timeframes(bars, lookback=50, max_rows=5_000)
    assert "1h" in out
    assert "4h" not in out
    assert "1d" not in out


def test_trained_stack_is_substitutable_in_predictor_helper():
    """The predictor's `_proba_yes` does `predict_proba(X)[0]` and reads
    a 2-column row.  Make sure the trained artifact obeys that shape so
    swapping the baseline never breaks the live serving path."""
    from app.commodities.predictor import _proba_yes

    bars = {"WTI": _bars(130)}
    stack = train_timeframe_stack(bars, timeframe="1h", lookback=50)
    fv_values = [75.0, 0.001, 0.005, 0.01, 0.004, 0.005, 55.0,
                 0.2, 0.3, -0.005, 1.0, 0.0]
    p, conf, raw = _proba_yes(stack, fv_values)
    assert 0.0 <= p <= 1.0
    assert conf is not None and 0.0 <= conf <= 1.0
    assert "proba" in raw and len(raw["proba"]) == 2


def test_baseline_stack_remains_pickleable_for_loader_fallback():
    """The model loader's fallback path returns the in-process baseline
    instance; persisting it via pickle is the contract used by warm
    cache reuse, so guard against accidental dataclass changes that
    break that round-trip."""
    base = BaselineLGBXGBStack(timeframe="1h")
    blob = pickle.dumps(base, protocol=pickle.HIGHEST_PROTOCOL)
    loaded = pickle.loads(blob)
    assert isinstance(loaded, BaselineLGBXGBStack)
    assert loaded.timeframe == "1h"
    assert loaded.n_features_in_ == FEATURE_COUNT
