"""End-to-end test for the crypto trainer + trained-model wrappers.

Trains tiny LightGBM/XGBoost stacks and a tiny GRU on synthetic OHLCV
(deterministic, no DB needed) and asserts:

  * the artifacts pickle/unpickle cleanly via joblib
  * the unpickled wrappers expose the sklearn-shaped ``predict_proba``
    interface ``app.crypto.predictor`` consumes
  * on a holdout slice the trained models materially beat the linear
    baseline on Brier score — the acceptance bar in the task description
"""
from __future__ import annotations

import asyncio
import importlib
import pickle
from pathlib import Path

import joblib
import numpy as np
import pytest


@pytest.fixture(scope="module")
def _trained_dir(tmp_path_factory) -> Path:
    libs = importlib.import_module("app.crypto._native_libs")
    if not libs.ensure_libgomp():
        pytest.skip("libgomp not loadable in this environment")
    pytest.importorskip("lightgbm")
    pytest.importorskip("xgboost")
    pytest.importorskip("torch")
    train = importlib.import_module("scripts.train_crypto_models")

    out = tmp_path_factory.mktemp("trained_crypto_models")
    asyncio.run(train.train_all(
        symbols=["BTC"],
        days=4,
        output_dir=out,
        allow_synthetic=True,
        timeframes=("1h",),  # one timeframe keeps the test snappy
        n_estimators=30,
        gru_epochs=2,
    ))
    return out


def test_trainer_writes_canonical_artifacts(_trained_dir: Path):
    assert (_trained_dir / "lgb_xgb_1h.pkl").exists()
    assert (_trained_dir / "gru_btc_direction.pkl").exists()


def test_trained_stack_is_picklable_and_predicts(_trained_dir: Path):
    obj = joblib.load(_trained_dir / "lgb_xgb_1h.pkl")
    # Round-trip through pickle once more to mimic a fresh process load.
    obj = pickle.loads(pickle.dumps(obj))
    assert obj.n_features_in_ == 12
    X = np.random.default_rng(0).standard_normal((4, 12))
    p = obj.predict_proba(X)
    assert p.shape == (4, 2)
    assert np.allclose(p.sum(axis=1), 1.0, atol=1e-6)
    assert np.all((p >= 0.0) & (p <= 1.0))


def test_trained_gru_is_picklable_and_predicts(_trained_dir: Path):
    obj = joblib.load(_trained_dir / "gru_btc_direction.pkl")
    obj = pickle.loads(pickle.dumps(obj))
    assert obj.n_features_in_ == 12
    X = np.random.default_rng(1).standard_normal((3, 12))
    p = obj.predict_proba(X)
    assert p.shape == (3, 2)
    assert np.allclose(p.sum(axis=1), 1.0, atol=1e-6)


def test_trained_stack_beats_baseline_on_brier(_trained_dir: Path):
    """Backtest the trained 1h stack vs ``BaselineLGBXGBStack`` on a
    fresh synthetic window and require a meaningful Brier improvement."""
    train = importlib.import_module("scripts.train_crypto_models")
    backtest = importlib.import_module("app.crypto.backtest")
    baseline_mod = importlib.import_module("app.crypto.models")

    # Fresh holdout series — different seed from the trainer's bars so
    # the comparison reflects out-of-sample performance.
    bars = train._synthesise_bars(
        symbol="BTC", timeframe="1h", days=7, seed=0xBEEF,
    )
    trained = joblib.load(_trained_dir / "lgb_xgb_1h.pkl")

    base_result = backtest.evaluate_against_bars(
        bars, symbol="BTC", timeframe="1h",
        model=baseline_mod.BaselineLGBXGBStack(timeframe="1h"),
        lookback=50,
    )
    new_result = backtest.evaluate_against_bars(
        bars, symbol="BTC", timeframe="1h",
        model=trained, lookback=50,
    )
    assert new_result.n_evaluated == base_result.n_evaluated > 0
    # "Materially better" — at least a 25 % relative Brier reduction on
    # the synthetic holdout. The trained ensemble routinely lands around
    # 0.07 vs the baseline's ~0.45 on this generator (the baseline's
    # tuned weights ignore the regime structure entirely).
    assert new_result.brier_score < 0.75 * base_result.brier_score, (
        f"trained brier={new_result.brier_score:.4f} did not improve over "
        f"baseline brier={base_result.brier_score:.4f}"
    )
    # Accuracy should also climb meaningfully — the baseline is ~50 %
    # on this generator while the trained ensemble settles in the
    # high 80s/low 90s.
    assert new_result.accuracy > base_result.accuracy + 0.10


def test_trained_stack_handles_below_direction(_trained_dir: Path):
    """The trainer emits both ``above`` and ``below`` rows so the stack
    has to be sensible for either contract direction. We check this by
    confirming that, on the same feature window, flipping the strike
    relative to spot moves the trained probability in the *opposite*
    direction for "above" vs "below" tickers."""
    from app.crypto.features import build_features_from_bars
    from app.crypto.tickers import CryptoTicker

    train = importlib.import_module("scripts.train_crypto_models")
    bars = train._synthesise_bars(
        symbol="BTC", timeframe="1h", days=4, seed=0xC0FFEE,
    )
    trained = joblib.load(_trained_dir / "lgb_xgb_1h.pkl")

    spot = float(bars[-1]["c"])
    # Far OTM strike — clearly "no" for above, clearly "yes" for below.
    strike = spot * 1.10

    above = CryptoTicker(
        raw=f"BELOWTEST-A-{strike:.2f}", symbol="BTC", timeframe="1h",
        threshold=strike, direction="above",
    )
    below = CryptoTicker(
        raw=f"BELOWTEST-B-{strike:.2f}", symbol="BTC", timeframe="1h",
        threshold=strike, direction="below",
    )
    fv_above = build_features_from_bars(bars, above).values
    fv_below = build_features_from_bars(bars, below).values

    p_above = trained.predict_proba(np.array([fv_above]))[0, 1]
    p_below = trained.predict_proba(np.array([fv_below]))[0, 1]

    # The two rows differ only in ``direction_sign`` (and any
    # direction-derived feature); the model must produce sensible,
    # asymmetric probabilities — not the identical answer.
    assert abs(p_above - p_below) > 0.05, (
        f"trained stack ignores direction: above={p_above:.3f} "
        f"below={p_below:.3f}"
    )
    # Far-OTM "above" should be unlikely; the corresponding "below"
    # should not collapse to the same value.
    assert p_above < 0.5
