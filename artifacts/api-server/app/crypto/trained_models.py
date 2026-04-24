"""Trained crypto-binary model wrappers.

Defines pickleable wrapper classes for the trained LightGBM + XGBoost
ensembles per timeframe and the PyTorch GRU directional model.  These
expose the ``predict_proba(X) -> (n, 2)`` interface that
``app.crypto.predictor._proba_yes`` already understands, so a
joblib-loaded artifact at the canonical
``models/saved/crypto/lgb_xgb_{tf}.pkl`` path is a drop-in replacement
for ``BaselineLGBXGBStack``.

The companion training script
(``scripts/train_crypto_models.py``) constructs and pickles instances
of these classes after fitting on historical OHLCV.  At inference the
API server unpickles the file via ``models_loader.load_predictor``
(which does ``joblib.load``); pickle resolves the class by qualified
name, so this module must live at the same import path on both the
trainer and the server (the pnpm monorepo guarantees that — both run
out of ``artifacts/api-server``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# libgomp preload must happen before lightgbm/xgboost imports.
from . import _native_libs as _libs

_libs.ensure_libgomp()


import numpy as np  # noqa: E402  — after libgomp preload above

# Width of the feature vector produced by ``app.crypto.features``.
# Must stay in lock-step with ``FEATURE_COUNT`` there — the trainer asserts it.
FEATURE_COUNT: int = 12


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(x, -50.0, 50.0)))


# ---------------------------------------------------------------------------
# LightGBM + XGBoost stacked ensemble
# ---------------------------------------------------------------------------


@dataclass
class TrainedLGBXGBStack:
    """LightGBM + XGBoost stack with isotonic calibration.

    Persisted attributes:
      * ``lgb_model``  — fitted ``lightgbm.Booster``
      * ``xgb_model``  — fitted ``xgboost.Booster``
      * ``calibrator`` — fitted ``sklearn.isotonic.IsotonicRegression``
                         applied to the *blended* (mean) component
                         probability
      * ``timeframe``  — "5m" | "15m" | "1h"
      * ``training``   — small dict of training metadata (rows, splits,
                         metrics on holdout) for operator inspection
    """

    timeframe: str
    lgb_model: Any = None
    xgb_model: Any = None
    calibrator: Any = None
    n_features_in_: int = FEATURE_COUNT
    classes_: tuple[int, int] = (0, 1)
    name: str = "trained_lgb_xgb_stack"
    training: dict[str, Any] = field(default_factory=dict)

    def _component_probs(self, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        # LightGBM Booster.predict returns the positive-class probability for
        # binary objectives.  XGBoost Booster.predict over a DMatrix likewise
        # returns the positive-class probability under "binary:logistic".
        import xgboost as xgb

        lgb_p = np.asarray(self.lgb_model.predict(X), dtype=float).ravel()
        dmat = xgb.DMatrix(X)
        xgb_p = np.asarray(self.xgb_model.predict(dmat), dtype=float).ravel()
        return lgb_p, xgb_p

    def predict_proba(self, X):  # noqa: N802
        Xa = np.asarray(X, dtype=float)
        if Xa.ndim == 1:
            Xa = Xa.reshape(1, -1)
        lgb_p, xgb_p = self._component_probs(Xa)
        blended = 0.5 * (lgb_p + xgb_p)
        if self.calibrator is not None:
            try:
                cal = np.asarray(self.calibrator.predict(blended), dtype=float)
            except Exception:
                cal = blended
            blended = np.clip(cal, 1e-6, 1.0 - 1e-6)
        out = np.empty((Xa.shape[0], 2), dtype=float)
        out[:, 0] = 1.0 - blended
        out[:, 1] = blended
        return out

    def predict(self, X):  # noqa: N802
        return (self.predict_proba(X)[:, 1] >= 0.5).astype(int)


# ---------------------------------------------------------------------------
# PyTorch GRU directional model
# ---------------------------------------------------------------------------


@dataclass
class TrainedGRUDirection:
    """Tiny GRU classifier over the 12-feature vector.

    The trained model treats each feature row as a length-1 sequence —
    short enough to keep inference cheap, wide enough that the GRU can
    learn cross-feature interactions the linear baseline misses.

    Persisted attributes:
      * ``state_dict`` — ``torch.nn.Module.state_dict()`` of the
                         underlying GRU + linear head, captured on a
                         CPU device so loading on the API server (also
                         CPU) is straightforward
      * ``arch`` — dict of architecture hyperparameters needed to
                   rebuild the module on first inference
      * ``training`` — small training metadata dict
    """

    state_dict: dict[str, Any] | None = None
    arch: dict[str, Any] = field(default_factory=lambda: {
        "input_size": FEATURE_COUNT,
        "hidden_size": 16,
        "num_layers": 1,
    })
    n_features_in_: int = FEATURE_COUNT
    classes_: tuple[int, int] = (0, 1)
    name: str = "trained_gru_btc_direction"
    training: dict[str, Any] = field(default_factory=dict)

    # Lazily-rebuilt torch module — never pickled (excluded from joblib
    # via __getstate__/__setstate__).
    _module: Any = field(default=None, repr=False, compare=False)

    # ---- lazy module construction ----------------------------------
    def _ensure_module(self):
        if self._module is not None:
            return self._module
        import torch  # heavy import — lazy on purpose
        from torch import nn

        class _GRUHead(nn.Module):
            def __init__(self, input_size: int, hidden_size: int, num_layers: int):
                super().__init__()
                self.gru = nn.GRU(
                    input_size=input_size,
                    hidden_size=hidden_size,
                    num_layers=num_layers,
                    batch_first=True,
                )
                self.head = nn.Linear(hidden_size, 1)

            def forward(self, x):  # x: (B, T, F)
                _, h = self.gru(x)
                last = h[-1]
                return self.head(last).squeeze(-1)

        mod = _GRUHead(
            input_size=int(self.arch["input_size"]),
            hidden_size=int(self.arch["hidden_size"]),
            num_layers=int(self.arch.get("num_layers", 1)),
        )
        if self.state_dict is not None:
            mod.load_state_dict(self.state_dict)
        mod.eval()
        self._module = mod
        return mod

    def predict_proba(self, X):  # noqa: N802
        import torch

        Xa = np.asarray(X, dtype=np.float32)
        if Xa.ndim == 1:
            Xa = Xa.reshape(1, -1)
        # (B, F) -> (B, T=1, F) — treat each sample as a length-1 sequence.
        seq = torch.from_numpy(Xa).unsqueeze(1)
        mod = self._ensure_module()
        with torch.no_grad():
            logits = mod(seq).cpu().numpy().ravel()
        p = _sigmoid(logits)
        out = np.empty((Xa.shape[0], 2), dtype=float)
        out[:, 0] = 1.0 - p
        out[:, 1] = p
        return out

    def predict(self, X):  # noqa: N802
        return (self.predict_proba(X)[:, 1] >= 0.5).astype(int)

    # ---- pickle hooks ---------------------------------------------
    def __getstate__(self) -> dict[str, Any]:
        # Drop the live torch module; only the state_dict travels.
        return {
            "state_dict": self.state_dict,
            "arch": self.arch,
            "n_features_in_": self.n_features_in_,
            "classes_": self.classes_,
            "name": self.name,
            "training": self.training,
        }

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.state_dict = state.get("state_dict")
        self.arch = state.get("arch") or {
            "input_size": FEATURE_COUNT, "hidden_size": 16, "num_layers": 1,
        }
        self.n_features_in_ = int(state.get("n_features_in_", FEATURE_COUNT))
        self.classes_ = tuple(state.get("classes_", (0, 1)))
        self.name = state.get("name", "trained_gru_btc_direction")
        self.training = state.get("training") or {}
        self._module = None
