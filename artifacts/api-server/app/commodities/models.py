"""Baseline commodities-binary models.

Spec calls for LightGBM + XGBoost ensembles per timeframe with a
calibrator on top.  Initial weights ship as a tiny dependency-light
baseline tuned to produce sensible probabilities — the AutoResearch +
drift-retrain loop replaces them with trained artifacts as the
`commodities_ohlcv` history fills in.

Mirrors `app.crypto.models` for shape so the model loader and
`_proba_yes` helper in `app.commodities.predictor` are reusable.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence


_F = {
    "last_price": 0,
    "return_1": 1,
    "return_5": 2,
    "return_10": 3,
    "vol_5": 4,
    "vol_20": 5,
    "rsi_14": 6,
    "obv_z": 7,
    "seasonality_norm": 8,
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
    z = bias
    n = min(len(weights), len(x))
    for i in range(n):
        z += float(weights[i]) * float(x[i])
    return z


@dataclass
class BaselineLGBXGBStack:
    """Stand-in for the LightGBM + XGBoost ensemble per timeframe."""

    timeframe: str
    name: str = "baseline_lgb_xgb_stack"
    n_features_in_: int = _FEATURE_COUNT
    classes_: tuple[int, int] = (0, 1)
    # LGB component: momentum-and-trend leaning.
    lgb_weights: tuple[float, ...] = field(
        default=(0.0, 8.0, 4.0, 2.0, -3.0, -2.0, 0.02, 0.5, 0.0, -3.0, 0.0, 0.0)
    )
    lgb_bias: float = -1.0
    # XGB component: distance-to-strike + OBV leaning.
    xgb_weights: tuple[float, ...] = field(
        default=(0.0, 6.0, 6.0, 3.0, -1.0, -4.0, 0.01, 1.0, 0.2, -4.0, 0.0, 0.0)
    )
    xgb_bias: float = -0.5
    # Meta-logistic over the two component probabilities.
    meta_weights: tuple[float, float] = (4.0, 4.0)
    meta_bias: float = -3.5

    def _component_probs(self, row: Sequence[float]) -> tuple[float, float]:
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

    def predict(self, X):  # noqa: N802
        return (self.predict_proba(X)[:, 1] >= 0.5).astype(int)


# Canonical paths the model loader will look for first.  When trained
# weights land at one of these paths the registry picks them up;
# otherwise the matching baseline class is returned.
CANONICAL_PATHS: dict[str, str] = {
    "1h":  "models/saved/commodities/lgb_xgb_1h.pkl",
    "4h":  "models/saved/commodities/lgb_xgb_4h.pkl",
    "1d":  "models/saved/commodities/lgb_xgb_1d.pkl",
}
