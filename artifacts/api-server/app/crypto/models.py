"""Baseline crypto-binary models.

Spec calls for LightGBM + XGBoost ensembles per timeframe (5m/15m/1h)
and a small PyTorch GRU for short-horizon BTC direction.  Per the task
description, *initial* weights ship as a tiny, dependency-light
baseline — the AutoResearch + drift-retrain loop replaces them with
trained artifacts over time.

This module defines two predictor classes that:

* expose the standard sklearn ``predict_proba`` interface (so the
  generic predictor service in ``app.predictor`` can call them via the
  same code path it uses for the meta-model);
* carry an ``n_features_in_`` attribute matching ``FEATURE_COUNT`` in
  ``app.crypto.features`` so the cycle's auto-pad logic stays a no-op;
* are pickleable with stdlib ``pickle`` / ``joblib`` so a future
  trainer can persist refined weights to the same canonical path
  without changing the loader.

The "baseline" weights are hand-tuned to produce sensible
probabilities: a market that's already past the threshold (positive
``log_distance_to_strike`` for "above") should get a probability < 0.5
of crossing back; a sustained positive momentum signal nudges the
probability up.  They are NOT meant to be profitable on their own —
they exist so the vertical produces a well-defined prediction on cold
install.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence


# Feature index map (must match `app.crypto.features.FEATURE_NAMES`).
_F = {
    "last_price": 0,
    "return_1": 1,
    "return_5": 2,
    "return_10": 3,
    "vol_5": 4,
    "vol_20": 5,
    "rsi_14": 6,
    "obv_z": 7,
    "hour_norm": 8,
    "log_distance_to_strike": 9,
    "direction_sign": 10,
    "timeframe_idx_norm": 11,
}
_FEATURE_COUNT = len(_F)


def _sigmoid(z: float) -> float:
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    ez = math.exp(z)
    return ez / (1.0 + ez)


def _row_logit(weights: Sequence[float], bias: float, x: Sequence[float]) -> float:
    """Per-row logit; degrades gracefully when X is shorter than weights."""
    z = bias
    n = min(len(weights), len(x))
    for i in range(n):
        z += float(weights[i]) * float(x[i])
    return z


@dataclass
class BaselineLGBXGBStack:
    """Stand-in for the LightGBM + XGBoost ensemble per timeframe.

    Internally averages two logistic models (the "LGB-shaped" and
    "XGB-shaped" baselines, distinguished by their weight signatures)
    plus a meta-logistic on top of the two component probabilities.
    Outputs the 2-class proba shape sklearn classifiers use.
    """

    timeframe: str
    name: str = "baseline_lgb_xgb_stack"
    n_features_in_: int = _FEATURE_COUNT
    classes_: tuple[int, int] = (0, 1)
    # Component "LGB" weights: weighted toward momentum + RSI mean reversion.
    lgb_weights: tuple[float, ...] = field(
        default=(0.0, 8.0, 4.0, 2.0, -3.0, -2.0, 0.02, 0.5, 0.0, -3.0, 0.0, 0.0)
    )
    lgb_bias: float = -1.0
    # Component "XGB" weights: weighted toward distance-to-strike + OBV.
    xgb_weights: tuple[float, ...] = field(
        default=(0.0, 6.0, 6.0, 3.0, -1.0, -4.0, 0.01, 1.0, 0.0, -4.0, 0.0, 0.0)
    )
    xgb_bias: float = -0.5
    # Meta-logistic over the two component probabilities (intercept,
    # w_lgb, w_xgb).  Tuned so a well-aligned pair of components
    # reinforces the ensemble probability without runaway saturation.
    meta_weights: tuple[float, float] = (4.0, 4.0)
    meta_bias: float = -3.5

    def _component_probs(self, row: Sequence[float]) -> tuple[float, float]:
        # Re-orient the move so a "below" market reads as "needs price to
        # fall to win".  We store a `direction_sign` feature exactly so
        # the same weight vector services both directions: flipping the
        # sign on the directional inputs (returns + log-distance) inverts
        # the bias correctly.
        direction = float(row[_F["direction_sign"]]) if len(row) > _F["direction_sign"] else 1.0
        oriented = list(row)
        for k in ("return_1", "return_5", "return_10",
                  "log_distance_to_strike", "obv_z"):
            i = _F[k]
            if i < len(oriented):
                oriented[i] = float(oriented[i]) * direction
        p_lgb = _sigmoid(_row_logit(self.lgb_weights, self.lgb_bias, oriented))
        p_xgb = _sigmoid(_row_logit(self.xgb_weights, self.xgb_bias, oriented))
        return p_lgb, p_xgb

    def predict_proba(self, X):  # noqa: N802 — match sklearn API
        import numpy as np

        Xa = np.asarray(X, dtype=float)
        if Xa.ndim == 1:
            Xa = Xa.reshape(1, -1)
        out = np.empty((Xa.shape[0], 2), dtype=float)
        for i, row in enumerate(Xa):
            p_lgb, p_xgb = self._component_probs(row.tolist())
            z = (
                self.meta_bias
                + self.meta_weights[0] * p_lgb
                + self.meta_weights[1] * p_xgb
            )
            p_yes = _sigmoid(z)
            out[i, 0] = 1.0 - p_yes
            out[i, 1] = p_yes
        return out

    def predict(self, X):  # noqa: N802 — match sklearn API
        return (self.predict_proba(X)[:, 1] >= 0.5).astype(int)


@dataclass
class BaselineGRUDirection:
    """Stand-in for the PyTorch GRU directional model (BTC short-horizon).

    The trained model will consume a sequence of recent returns; for the
    baseline we collapse the same 12-feature vector into a tiny linear
    "GRU-shaped" combination that emphasises the most recent return,
    short-window vol (lower vol = higher conviction in the directional
    bet), and OBV momentum.  Like the stack above this is a placeholder
    that the AutoResearch loop replaces.
    """

    name: str = "baseline_gru_btc_direction"
    n_features_in_: int = _FEATURE_COUNT
    classes_: tuple[int, int] = (0, 1)
    weights: tuple[float, ...] = field(
        default=(0.0, 12.0, 5.0, 2.0, -8.0, -3.0, 0.0, 1.5, 0.0, -2.0, 0.0, 0.0)
    )
    bias: float = -0.2

    def predict_proba(self, X):  # noqa: N802
        import numpy as np

        Xa = np.asarray(X, dtype=float)
        if Xa.ndim == 1:
            Xa = Xa.reshape(1, -1)
        out = np.empty((Xa.shape[0], 2), dtype=float)
        for i, row in enumerate(Xa):
            row_l = row.tolist()
            direction = float(row_l[_F["direction_sign"]]) if len(row_l) > _F["direction_sign"] else 1.0
            oriented = list(row_l)
            for k in ("return_1", "return_5", "return_10",
                      "log_distance_to_strike", "obv_z"):
                idx = _F[k]
                if idx < len(oriented):
                    oriented[idx] = float(oriented[idx]) * direction
            p_yes = _sigmoid(_row_logit(self.weights, self.bias, oriented))
            out[i, 0] = 1.0 - p_yes
            out[i, 1] = p_yes
        return out

    def predict(self, X):  # noqa: N802
        return (self.predict_proba(X)[:, 1] >= 0.5).astype(int)


# Canonical paths the loader will look for first.  When a trained model
# lands at one of these paths the registry below picks it up; otherwise
# the matching baseline class is returned.
CANONICAL_PATHS: dict[str, str] = {
    "5m":  "models/saved/crypto/lgb_xgb_5m.pkl",
    "15m": "models/saved/crypto/lgb_xgb_15m.pkl",
    "1h":  "models/saved/crypto/lgb_xgb_1h.pkl",
    "gru_btc_direction": "models/saved/crypto/gru_btc_direction.pkl",
}
