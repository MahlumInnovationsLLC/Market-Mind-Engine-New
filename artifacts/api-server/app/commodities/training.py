"""Trainer for the production commodity LGB+XGB ensembles.

This module gives the operator a real, end-to-end pipeline that takes
historical OHLCV bars from ``commodities_ohlcv`` and produces the
artifacts the predictor's model loader picks up via ``CANONICAL_PATHS``
in :mod:`app.commodities.models`:

    models/saved/commodities/lgb_xgb_1h.pkl
    models/saved/commodities/lgb_xgb_4h.pkl
    models/saved/commodities/lgb_xgb_1d.pkl

The runtime predictor speaks sklearn's ``predict_proba(X)`` API, so the
trained artifact is just any pickleable object that exposes the same
two-column probability shape.  We ship :class:`TrainedLGBXGBStack` for
that purpose: two component classifiers ("LGB-shaped" gradient-boosted
tree, "XGB-shaped" gradient-boosted tree) plus a tiny meta logistic
fit on the held-out OOF probabilities.

LightGBM and XGBoost are *optional* dependencies — when importable we
use them; when missing we fall back to sklearn's
``HistGradientBoostingClassifier`` / ``GradientBoostingClassifier`` so
the trainer can run in CI and on cold installs without an extra wheel.
The on-disk artifact is the same shape either way.
"""
from __future__ import annotations

import logging
import math
import os
import pickle
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from .features import FEATURE_COUNT, build_features_from_bars
from .models import BaselineLGBXGBStack
from .tickers import (
    SUPPORTED_SYMBOLS,
    SUPPORTED_TIMEFRAMES,
    CommodityTicker,
)


log = logging.getLogger(__name__)


# Strike-grid used to synthesise (X, y) rows from bars: at every bar we
# build N candidates for "next-bar close above K" with K = spot * (1+o).
# A symmetric grid keeps the YES/NO label distribution balanced.
DEFAULT_STRIKE_OFFSETS: tuple[float, ...] = (-0.01, -0.005, 0.0, 0.005, 0.01)
DEFAULT_LOOKBACK: int = 50
DEFAULT_MAX_TRAIN_ROWS: int = 250_000
MIN_TRAIN_ROWS: int = 200


# ---------------------------------------------------------------------------
# Trained-stack wrapper
# ---------------------------------------------------------------------------


@dataclass
class TrainedLGBXGBStack:
    """Trained LGB + XGB + meta-logistic stack.

    Drop-in replacement for :class:`BaselineLGBXGBStack` — exposes the
    same ``predict_proba`` / ``predict`` API and the same
    ``n_features_in_`` attribute so the predictor's auto-pad / proba
    helper stays a no-op.

    Attributes
    ----------
    timeframe : str
        The bar granularity this stack was trained for ("1h"/"4h"/"1d").
    lgb : Any
        LightGBM classifier (or sklearn fallback).
    xgb : Any
        XGBoost classifier (or sklearn fallback).
    meta_a, meta_b, meta_c : float
        Meta logistic coefficients ``sigmoid(a*p_lgb + b*p_xgb + c)``.
    metadata : dict
        Training-time provenance: row count, fit timestamp, component
        algorithms used, OOF brier score.
    """

    timeframe: str
    lgb: Any
    xgb: Any
    meta_a: float
    meta_b: float
    meta_c: float
    metadata: dict[str, Any] = field(default_factory=dict)
    name: str = "trained_lgb_xgb_stack"
    classes_: tuple[int, int] = (0, 1)
    n_features_in_: int = FEATURE_COUNT

    @staticmethod
    def _component_proba(model: Any, X) -> Any:
        import numpy as np

        proba = np.asarray(
            model.predict_proba(np.asarray(X, dtype=float)),
            dtype=float,
        )
        if proba.ndim == 1:
            proba = proba.reshape(-1, 1)
        # Some sklearn classifiers fit on a single-class y degrade to a
        # single-column output.  Pad to (N, 2) using the model's own
        # `classes_` so column 1 always carries P(y=1).
        if proba.shape[1] == 1:
            classes = list(getattr(model, "classes_", [1]))
            sole = int(classes[0]) if classes else 1
            col = proba[:, 0]
            p_yes = col if sole == 1 else (1.0 - col)
            proba = np.column_stack([1.0 - p_yes, p_yes])
        return proba

    def _stack_proba_from_components(self, p_lgb, p_xgb):
        import numpy as np

        z = self.meta_a * p_lgb + self.meta_b * p_xgb + self.meta_c
        # Numerically stable sigmoid.
        out = np.empty_like(z)
        pos = z >= 0
        out[pos] = 1.0 / (1.0 + np.exp(-z[pos]))
        ez = np.exp(z[~pos])
        out[~pos] = ez / (1.0 + ez)
        return out

    def predict_proba(self, X):  # noqa: N802 — sklearn API
        import numpy as np

        Xa = np.asarray(X, dtype=float)
        if Xa.ndim == 1:
            Xa = Xa.reshape(1, -1)
        p_lgb = self._component_proba(self.lgb, Xa)[:, 1]
        p_xgb = self._component_proba(self.xgb, Xa)[:, 1]
        p_yes = self._stack_proba_from_components(p_lgb, p_xgb)
        out = np.empty((Xa.shape[0], 2), dtype=float)
        out[:, 0] = 1.0 - p_yes
        out[:, 1] = p_yes
        return out

    def predict(self, X):  # noqa: N802
        return (self.predict_proba(X)[:, 1] >= 0.5).astype(int)


# ---------------------------------------------------------------------------
# Dataset construction
# ---------------------------------------------------------------------------


def _bars_to_xy(
    bars: Sequence[dict[str, Any]],
    *,
    symbol: str,
    timeframe: str,
    lookback: int,
    strike_offsets: Sequence[float],
) -> tuple[list[list[float]], list[int]]:
    """Build ``(X, y)`` rows for a single (symbol, timeframe) bar series.

    For each bar at index ``i >= lookback`` we sample the strike grid
    relative to ``spot = bars[i].c`` and label the row by whether
    ``bars[i+1].c > strike`` (matching the binary-close settlement the
    model serves).  Both ``above`` and ``below`` orientations are
    sampled so the trained model learns ``direction_sign`` correctly.
    """
    X: list[list[float]] = []
    y: list[int] = []
    for i in range(lookback, len(bars) - 1):
        window = bars[: i + 1]
        spot = float(window[-1]["c"])
        next_close = float(bars[i + 1]["c"])
        if spot <= 0:
            continue
        for offset in strike_offsets:
            strike = spot * (1.0 + offset)
            if strike <= 0:
                continue
            for direction in ("above", "below"):
                ticker = CommodityTicker(
                    raw=f"TRAIN-{symbol}-{timeframe}-{strike:.4f}",
                    symbol=symbol,
                    timeframe=timeframe,
                    threshold=strike,
                    direction=direction,
                )
                try:
                    fv = build_features_from_bars(window, ticker)
                except Exception:  # pragma: no cover — defensive
                    continue
                if direction == "above":
                    label = 1 if next_close > strike else 0
                else:
                    label = 1 if next_close < strike else 0
                X.append(list(fv.values))
                y.append(int(label))
    return X, y


def build_training_dataset(
    bars_by_symbol: dict[str, Sequence[dict[str, Any]]],
    *,
    timeframe: str,
    lookback: int = DEFAULT_LOOKBACK,
    strike_offsets: Sequence[float] = DEFAULT_STRIKE_OFFSETS,
    max_rows: int = DEFAULT_MAX_TRAIN_ROWS,
) -> tuple[list[list[float]], list[int], dict[str, int]]:
    """Concatenate ``(X, y)`` rows across all symbols for one timeframe.

    Returns ``(X, y, per_symbol_counts)``.  When the synthesised set
    exceeds ``max_rows`` we round-robin trim per symbol so no single
    high-history symbol dominates the trained model.
    """
    raw: dict[str, tuple[list[list[float]], list[int]]] = {}
    for sym, bars in bars_by_symbol.items():
        if not bars:
            continue
        Xs, ys = _bars_to_xy(
            bars,
            symbol=sym,
            timeframe=timeframe,
            lookback=lookback,
            strike_offsets=strike_offsets,
        )
        if Xs:
            raw[sym] = (Xs, ys)

    X: list[list[float]] = []
    y: list[int] = []
    counts: dict[str, int] = {}
    for sym, (Xs, ys) in raw.items():
        X.extend(Xs)
        y.extend(ys)
        counts[sym] = len(Xs)

    if max_rows and len(X) > max_rows:
        # Stable downsample (every Nth row) preserves ordering and keeps
        # per-symbol proportions intact.
        step = max(1, len(X) // max_rows)
        X = X[::step][:max_rows]
        y = y[::step][:max_rows]

    return X, y, counts


# ---------------------------------------------------------------------------
# Model factories — optional LGB/XGB with sklearn fallback
# ---------------------------------------------------------------------------


def _make_lgb_classifier() -> tuple[Any, str]:
    try:
        import lightgbm as lgb  # type: ignore[import-not-found]

        clf = lgb.LGBMClassifier(
            n_estimators=200,
            learning_rate=0.05,
            num_leaves=31,
            max_depth=-1,
            subsample=0.9,
            colsample_bytree=0.9,
            min_child_samples=20,
            random_state=42,
            verbose=-1,
        )
        return clf, "lightgbm"
    except Exception:
        from sklearn.ensemble import HistGradientBoostingClassifier

        clf = HistGradientBoostingClassifier(
            max_iter=200,
            learning_rate=0.05,
            max_leaf_nodes=31,
            random_state=42,
        )
        return clf, "hist_gbdt_fallback"


def _make_xgb_classifier() -> tuple[Any, str]:
    try:
        import xgboost as xgb  # type: ignore[import-not-found]

        clf = xgb.XGBClassifier(
            n_estimators=200,
            learning_rate=0.05,
            max_depth=6,
            subsample=0.9,
            colsample_bytree=0.9,
            min_child_weight=1.0,
            random_state=42,
            eval_metric="logloss",
            use_label_encoder=False,
            tree_method="hist",
            verbosity=0,
        )
        return clf, "xgboost"
    except Exception:
        from sklearn.ensemble import GradientBoostingClassifier

        clf = GradientBoostingClassifier(
            n_estimators=150,
            learning_rate=0.05,
            max_depth=3,
            subsample=0.9,
            random_state=43,
        )
        return clf, "gbdt_fallback"


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------


def _fit_meta_logistic(p_lgb, p_xgb, y) -> tuple[float, float, float]:
    """Fit ``sigmoid(a*p_lgb + b*p_xgb + c)``.

    Returns ``(0, 0, logit(mean_y))`` if outcomes are single-class so
    ``predict_proba`` still produces a sensible constant probability.
    """
    import numpy as np
    from sklearn.linear_model import LogisticRegression

    y_arr = np.asarray(y, dtype=int)
    if len(set(y_arr.tolist())) < 2:
        mean = float(y_arr.mean()) if len(y_arr) else 0.5
        m = max(1e-6, min(1.0 - 1e-6, mean))
        return 0.0, 0.0, math.log(m / (1.0 - m))
    X = np.column_stack([np.asarray(p_lgb, dtype=float),
                         np.asarray(p_xgb, dtype=float)])
    lr = LogisticRegression(C=1.0, solver="lbfgs", max_iter=200)
    lr.fit(X, y_arr)
    a = float(lr.coef_[0][0])
    b = float(lr.coef_[0][1])
    c = float(lr.intercept_[0])
    return a, b, c


def _brier(probs, ys) -> float:
    import numpy as np

    p = np.asarray(probs, dtype=float)
    y = np.asarray(ys, dtype=float)
    if not len(p):
        return 0.0
    return float(((p - y) ** 2).mean())


def _log_loss(probs, ys) -> float:
    import numpy as np

    p = np.clip(np.asarray(probs, dtype=float), 1e-9, 1 - 1e-9)
    y = np.asarray(ys, dtype=float)
    if not len(p):
        return 0.0
    return float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean())


def train_timeframe_stack(
    bars_by_symbol: dict[str, Sequence[dict[str, Any]]],
    *,
    timeframe: str,
    lookback: int = DEFAULT_LOOKBACK,
    strike_offsets: Sequence[float] = DEFAULT_STRIKE_OFFSETS,
    max_rows: int = DEFAULT_MAX_TRAIN_ROWS,
    val_fraction: float = 0.2,
) -> TrainedLGBXGBStack:
    """Train a single-timeframe LGB+XGB+meta stack.

    Splits the synthesised dataset chronologically (last ``val_fraction``
    held out) so the meta-logistic fits on out-of-fold component
    probabilities and we can record an honest validation Brier in
    ``metadata`` for the operator's smoke check.
    """
    import numpy as np

    X_rows, y_rows, per_sym = build_training_dataset(
        bars_by_symbol,
        timeframe=timeframe,
        lookback=lookback,
        strike_offsets=strike_offsets,
        max_rows=max_rows,
    )
    n = len(X_rows)
    if n < MIN_TRAIN_ROWS:
        raise RuntimeError(
            f"not enough training rows for {timeframe} (got {n}, "
            f"need >= {MIN_TRAIN_ROWS}); load more bars before retraining"
        )

    X = np.asarray(X_rows, dtype=float)
    y = np.asarray(y_rows, dtype=int)

    split = max(MIN_TRAIN_ROWS // 2, int(n * (1.0 - val_fraction)))
    split = min(split, n - 1)
    X_tr, X_val = X[:split], X[split:]
    y_tr, y_val = y[:split], y[split:]

    lgb_clf, lgb_kind = _make_lgb_classifier()
    xgb_clf, xgb_kind = _make_xgb_classifier()

    # If the training fold is single-class we can't fit a real
    # classifier — return a degenerate stack that always predicts the
    # class prior so the artifact still loads.
    if len(set(y_tr.tolist())) < 2:
        from sklearn.dummy import DummyClassifier

        d1 = DummyClassifier(strategy="prior").fit(X_tr, y_tr)
        d2 = DummyClassifier(strategy="prior").fit(X_tr, y_tr)
        return TrainedLGBXGBStack(
            timeframe=timeframe,
            lgb=d1, xgb=d2,
            meta_a=0.0, meta_b=0.0,
            meta_c=math.log(max(1e-6, y_tr.mean()) / max(1e-6, 1.0 - y_tr.mean())),
            metadata={
                "rows_total": n, "rows_train": int(len(y_tr)),
                "rows_val": int(len(y_val)),
                "lgb_algo": "dummy", "xgb_algo": "dummy",
                "fit_at": datetime.now(timezone.utc).isoformat(),
                "per_symbol_rows": per_sym,
                "warning": "single-class training fold",
            },
        )

    lgb_clf.fit(X_tr, y_tr)
    xgb_clf.fit(X_tr, y_tr)

    p_lgb_val = TrainedLGBXGBStack._component_proba(lgb_clf, X_val)[:, 1]
    p_xgb_val = TrainedLGBXGBStack._component_proba(xgb_clf, X_val)[:, 1]
    a, b, c = _fit_meta_logistic(p_lgb_val, p_xgb_val, y_val)

    stack = TrainedLGBXGBStack(
        timeframe=timeframe,
        lgb=lgb_clf, xgb=xgb_clf,
        meta_a=a, meta_b=b, meta_c=c,
        metadata={
            "rows_total": n,
            "rows_train": int(len(y_tr)),
            "rows_val": int(len(y_val)),
            "lgb_algo": lgb_kind,
            "xgb_algo": xgb_kind,
            "fit_at": datetime.now(timezone.utc).isoformat(),
            "per_symbol_rows": per_sym,
        },
    )
    # Record honest validation scores using the fully-fitted stack.
    p_val = stack.predict_proba(X_val)[:, 1]
    stack.metadata["val_brier"] = _brier(p_val, y_val)
    stack.metadata["val_log_loss"] = _log_loss(p_val, y_val)
    return stack


def train_all_timeframes(
    bars: dict[str, dict[str, Sequence[dict[str, Any]]]],
    *,
    timeframes: Iterable[str] = SUPPORTED_TIMEFRAMES,
    **kwargs: Any,
) -> dict[str, TrainedLGBXGBStack]:
    """Train one stack per timeframe.

    ``bars`` is keyed ``bars[timeframe][symbol] -> list[bar_dict]``.
    Skips any timeframe with no symbols / not enough rows and logs the
    skip — the caller decides whether that's a hard failure.
    """
    out: dict[str, TrainedLGBXGBStack] = {}
    for tf in timeframes:
        sym_map = bars.get(tf) or {}
        if not sym_map:
            log.warning("[commodities.training] no bars for tf=%s, skipping", tf)
            continue
        try:
            out[tf] = train_timeframe_stack(sym_map, timeframe=tf, **kwargs)
        except RuntimeError as e:
            log.warning("[commodities.training] tf=%s skipped: %s", tf, e)
    return out


# ---------------------------------------------------------------------------
# Persistence + comparison
# ---------------------------------------------------------------------------


def save_artifact(stack: TrainedLGBXGBStack, path: str | os.PathLike) -> str:
    """Pickle ``stack`` to ``path`` (creating parent dirs).  Returns path."""
    p = os.fspath(path)
    parent = os.path.dirname(p)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(p, "wb") as f:
        pickle.dump(stack, f, protocol=pickle.HIGHEST_PROTOCOL)
    return p


def load_artifact(path: str | os.PathLike) -> TrainedLGBXGBStack:
    with open(os.fspath(path), "rb") as f:
        obj = pickle.load(f)
    if not hasattr(obj, "predict_proba"):
        raise RuntimeError(
            f"artifact at {path!r} doesn't expose predict_proba "
            f"(got {type(obj).__name__})"
        )
    return obj


def compare_to_baseline(
    stack: TrainedLGBXGBStack,
    bars_by_symbol: dict[str, Sequence[dict[str, Any]]],
    *,
    timeframe: str,
    lookback: int = DEFAULT_LOOKBACK,
    strike_offsets: Sequence[float] = DEFAULT_STRIKE_OFFSETS,
) -> dict[str, Any]:
    """Score ``stack`` and the baseline on a held-out tail and report
    Brier / log-loss for both plus the raw row count.

    Used by the trainer CLI's report and by the unit test that asserts
    the trained stack at least matches the baseline on training data.
    """
    import numpy as np

    X_rows, y_rows, _ = build_training_dataset(
        bars_by_symbol,
        timeframe=timeframe,
        lookback=lookback,
        strike_offsets=strike_offsets,
        max_rows=0,
    )
    n = len(X_rows)
    if n == 0:
        return {"timeframe": timeframe, "n": 0, "trained": None, "baseline": None}
    # Held-out tail: the last 20% of synthesised rows so we measure the
    # generalisation edge rather than train-fit overlap.
    split = max(1, int(n * 0.8))
    X_h = np.asarray(X_rows[split:], dtype=float)
    y_h = np.asarray(y_rows[split:], dtype=int)
    if len(X_h) == 0:
        X_h = np.asarray(X_rows, dtype=float)
        y_h = np.asarray(y_rows, dtype=int)

    p_trained = stack.predict_proba(X_h)[:, 1]
    p_base = BaselineLGBXGBStack(timeframe=timeframe).predict_proba(X_h)[:, 1]
    return {
        "timeframe": timeframe,
        "n_holdout": int(len(X_h)),
        "trained": {
            "brier": _brier(p_trained, y_h),
            "log_loss": _log_loss(p_trained, y_h),
        },
        "baseline": {
            "brier": _brier(p_base, y_h),
            "log_loss": _log_loss(p_base, y_h),
        },
    }


__all__ = [
    "DEFAULT_LOOKBACK",
    "DEFAULT_MAX_TRAIN_ROWS",
    "DEFAULT_STRIKE_OFFSETS",
    "MIN_TRAIN_ROWS",
    "TrainedLGBXGBStack",
    "build_training_dataset",
    "compare_to_baseline",
    "load_artifact",
    "save_artifact",
    "train_all_timeframes",
    "train_timeframe_stack",
]
