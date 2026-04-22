"""Predictor service.

Thin wrapper over `models_loader` that exposes a typed `predict()` API
for the trading orchestrator. The loader is responsible for downloading
+ extracting the model artifacts and deserializing the canonical
predictors at boot; this module picks the best available canonical
predictor, introspects it once to learn its feature width, and exposes
`predict(features) -> Prediction`.

Active predictor selection priority (first that loaded successfully):
    1. models/saved/meta_model.pkl   (latest trained meta-model)
    2. models/archive/meta_model.pkl (frozen reference)

The selection is recomputed lazily on first use and after a reload, so
calling `/api/models/reload` then `/api/models/predict` always returns
the freshly-loaded model's version.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import asdict, dataclass
from typing import Any, Sequence

from . import calibrators, models_loader


log = logging.getLogger(__name__)


# Static order: first entry that is in catalog wins. The latest
# `models/versions/v1.NNN/meta_model.pkl` is injected dynamically
# between saved and archive — see `_priority_chain`.
_STATIC_PRIORITY: tuple[str, ...] = (
    "models/saved/meta_model.pkl",
    "models/archive/meta_model.pkl",
)

_VERSIONED_RE = re.compile(r"^models/versions/v(\d+(?:\.\d+)*)/meta_model\.pkl$")


@dataclass
class Prediction:
    """Typed result returned by the predictor service."""
    probability: float
    confidence: float | None
    model_name: str
    model_version: str
    model_type: str
    elapsed_ms: float
    raw: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class PredictorUnavailable(RuntimeError):
    """Raised when no canonical predictor is loaded yet."""


class PredictorInputError(ValueError):
    """Raised when supplied features can't be coerced to the expected shape."""


# In-memory selection state. Populated on first use; cleared via
# `invalidate()` so a /api/models/reload picks up the new artifacts.
_active_name: str | None = None
_active_obj: Any = None
_active_n_features: int | None = None
_active_loaded_at: float | None = None
_select_lock = asyncio.Lock()


def invalidate() -> None:
    """Forget the cached active predictor selection."""
    global _active_name, _active_obj, _active_n_features, _active_loaded_at
    _active_name = None
    _active_obj = None
    _active_n_features = None
    _active_loaded_at = None


def _version_tuple(s: str) -> tuple[int, ...]:
    return tuple(int(p) for p in s.split("."))


def _latest_versioned(catalog: dict[str, Any]) -> str | None:
    """Return the catalog key for the highest-numbered versioned meta-model."""
    best: tuple[tuple[int, ...], str] | None = None
    for name in catalog:
        m = _VERSIONED_RE.match(name)
        if not m:
            continue
        try:
            v = _version_tuple(m.group(1))
        except ValueError:
            continue
        if best is None or v > best[0]:
            best = (v, name)
    return best[1] if best else None


def _priority_chain() -> list[str]:
    """Resolve the per-call selection priority against the live catalog."""
    state = models_loader.get_state()
    chain: list[str] = []
    chain.append(_STATIC_PRIORITY[0])              # saved/meta_model.pkl
    latest = _latest_versioned(state.predictors)   # versions/vN/meta_model.pkl
    if latest:
        chain.append(latest)
    chain.append(_STATIC_PRIORITY[1])              # archive/meta_model.pkl
    # De-dup while preserving order.
    seen: set[str] = set()
    return [n for n in chain if not (n in seen or seen.add(n))]


def _pick_active_sync() -> tuple[str, Any] | None:
    """Pick the highest-priority predictor that has already been loaded.

    Used by `active_model_info()` to peek without paying lazy-load cost.
    """
    state = models_loader.get_state()
    if not state.ready:
        return None
    for name in _priority_chain():
        if name not in state.predictors:
            continue
        obj = models_loader.get_loaded_predictor(name)
        if obj is not None:
            return name, obj
    # Fallback: any canonical predictor that smoke-loaded successfully.
    for name, meta in state.loaded.items():
        if not meta.get("ok"):
            continue
        obj = models_loader.get_loaded_predictor(name)
        if obj is not None:
            return name, obj
    return None


async def _ensure_active() -> tuple[str, Any]:
    """Bind an active predictor, lazy-loading from disk if needed."""
    global _active_name, _active_obj, _active_n_features, _active_loaded_at
    if _active_obj is not None and _active_name is not None:
        return _active_name, _active_obj
    async with _select_lock:
        if _active_obj is not None and _active_name is not None:
            return _active_name, _active_obj
        state = models_loader.get_state()
        if not state.ready:
            raise PredictorUnavailable(
                "model loader not ready; check /api/models/status"
            )
        last_err: str | None = None
        for name in _priority_chain():
            if name not in state.predictors:
                continue
            obj = models_loader.get_loaded_predictor(name)
            if obj is None:
                # Catalog has it but it wasn't smoke-loaded — load now.
                try:
                    obj = await models_loader.load_predictor(name)
                except Exception as e:
                    last_err = f"{name}: {type(e).__name__}: {e}"
                    log.warning("[predictor] lazy load failed for %s: %s", name, e)
                    continue
            _active_name = name
            _active_obj = obj
            n = getattr(obj, "n_features_in_", None)
            _active_n_features = int(n) if isinstance(n, int) else None
            _active_loaded_at = time.time()
            log.info(
                "[predictor] active=%s type=%s n_features_in=%s",
                name, type(obj).__name__, _active_n_features,
            )
            return name, obj
        # Last-ditch fallback: any loaded canonical predictor.
        picked = _pick_active_sync()
        if picked is not None:
            name, obj = picked
            _active_name = name
            _active_obj = obj
            n = getattr(obj, "n_features_in_", None)
            _active_n_features = int(n) if isinstance(n, int) else None
            _active_loaded_at = time.time()
            return name, obj
        raise PredictorUnavailable(
            "no canonical predictor available"
            + (f" (last error: {last_err})" if last_err else "")
        )


def _coerce_features(
    features: Sequence[float] | dict[str, float] | None,
    n_expected: int | None,
) -> list[float]:
    """Coerce caller-supplied features to a flat list of floats.

    Accepts a numeric sequence or a {name: value} mapping (values only,
    in insertion order — callers that need named features should manage
    the order themselves). Pads with zeros / truncates to match
    `n_expected` when the model advertises a fixed feature width, so a
    smoke-test caller can pass `[]` and still get a real prediction.
    """
    if features is None:
        values: list[float] = []
    elif isinstance(features, dict):
        values = [float(v) for v in features.values()]
    elif isinstance(features, (list, tuple)) or _is_numeric_iter(features):
        values = [float(v) for v in features]
    else:
        raise PredictorInputError(
            f"unsupported features type {type(features).__name__}; "
            "pass a list[float] or dict[str, float]"
        )
    if n_expected is None:
        if not values:
            raise PredictorInputError(
                "no features provided and active model has no n_features_in_; "
                "supply an explicit feature vector"
            )
        return values
    if len(values) > n_expected:
        return values[:n_expected]
    if len(values) < n_expected:
        values = values + [0.0] * (n_expected - len(values))
    return values


def _is_numeric_iter(x: Any) -> bool:
    try:
        iter(x)
    except TypeError:
        return False
    return not isinstance(x, (str, bytes))


def _run_predict(obj: Any, vector: list[float]) -> tuple[float, float | None, dict[str, Any]]:
    """Execute the model on a single feature row.

    Returns `(probability, confidence, raw_outputs)` where `probability`
    is the YES-class probability when the model is a classifier, and
    falls back to the raw point estimate otherwise. `confidence` is
    derived from `predict_proba` margins when available.
    """
    raw: dict[str, Any] = {}
    # Use numpy lazily so the import cost is only paid when an actual
    # prediction request lands (boot stays fast on a cold cache).
    import numpy as np
    X = np.asarray([vector], dtype=float)

    proba = None
    if hasattr(obj, "predict_proba"):
        try:
            proba = obj.predict_proba(X)
        except Exception as e:  # pragma: no cover - model-specific
            raw["predict_proba_error"] = f"{type(e).__name__}: {e}"
            proba = None

    if proba is not None:
        row = list(map(float, np.asarray(proba)[0].tolist()))
        raw["proba"] = row
        if len(row) == 2:
            probability = row[1]
            confidence = abs(row[1] - row[0])
        else:
            # Multi-class: use max probability and margin between top two.
            sorted_row = sorted(row, reverse=True)
            probability = sorted_row[0]
            confidence = (sorted_row[0] - sorted_row[1]) if len(sorted_row) > 1 else None
        return probability, confidence, raw

    if hasattr(obj, "predict"):
        try:
            yhat = obj.predict(X)
        except Exception as e:
            raise PredictorInputError(
                f"model.predict failed: {type(e).__name__}: {e}"
            ) from e
        val = float(np.asarray(yhat).ravel()[0])
        raw["predict"] = val
        # Squash to [0, 1] for the contract — many regressors emit raw
        # scores; clamp so callers can treat the field uniformly.
        probability = max(0.0, min(1.0, val))
        return probability, None, raw

    raise PredictorUnavailable(
        f"active predictor of type {type(obj).__name__} has neither "
        "predict_proba nor predict"
    )


async def predict(
    features: Sequence[float] | dict[str, float] | None = None,
    *,
    league: str | None = None,
) -> Prediction:
    """Run the active canonical predictor on `features`.

    When ``league`` is supplied (and a per-league calibrator is loaded
    in the ``calibrators`` module) the raw model probability is passed
    through the calibrator before being returned, so downstream edge
    gates work against the corrected probability.  Both the raw and
    calibrated values are surfaced via the ``raw`` payload for
    observability.

    Raises `PredictorUnavailable` if no model is loaded, or
    `PredictorInputError` if the features can't be coerced.
    """
    name, obj = await _ensure_active()
    vector = _coerce_features(features, _active_n_features)
    t0 = time.monotonic()
    probability, confidence, raw = await asyncio.to_thread(_run_predict, obj, vector)
    elapsed_ms = (time.monotonic() - t0) * 1000.0
    # Per-league calibration on top of the raw stack output. Bypasses
    # cleanly when no calibrator is loaded for the league (or no
    # league supplied) so cold installs still get a real prediction.
    cal_prob, cal_info = calibrators.apply(probability, league)
    if cal_info.get("applied"):
        raw["uncalibrated_probability"] = float(probability)
        probability = cal_prob
    raw["calibration"] = cal_info
    state = models_loader.get_state()
    version = (state.predictors.get(name) or {}).get("version", "unknown")
    return Prediction(
        probability=float(probability),
        confidence=float(confidence) if confidence is not None else None,
        model_name=name,
        model_version=str(version),
        model_type=type(obj).__name__,
        elapsed_ms=round(elapsed_ms, 3),
        raw=raw,
    )


def active_model_info() -> dict[str, Any] | None:
    """Compact descriptor of the currently-selected predictor.

    Returns None if no predictor has been bound yet. Used by /api/health
    so the operator console can render the *active* model version, not
    just the inventory.
    """
    if _active_name is None or _active_obj is None:
        # Lazy peek: if the loader is ready we can already report what
        # predict() would pick, even before the first call.
        picked = _pick_active_sync()
        if picked is None:
            return None
        name, obj = picked
        n_features = getattr(obj, "n_features_in_", None)
    else:
        name = _active_name
        obj = _active_obj
        n_features = _active_n_features
    state = models_loader.get_state()
    meta = state.predictors.get(name) or {}
    version = meta.get("version", "unknown")
    sha256 = meta.get("sha256")
    return {
        "name": name,
        "version": str(version),
        "sha256": str(sha256) if sha256 else None,
        "type": type(obj).__name__,
        "module": type(obj).__module__,
        "n_features_in": int(n_features) if isinstance(n_features, int) else None,
        "bound": _active_obj is not None,
        "loaded_at": _active_loaded_at,
    }
