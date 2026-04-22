"""Public predictor entry point for the commodities-binary vertical.

Mirrors `app.crypto.predictor` — the cycle dispatches to
`predict_commodity_binary(ticker)` whenever the candidate's vertical
opts in via `VerticalSpec.predictor_hook='commodities_binary'`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .. import calibrators, models_loader
from ..predictor import Prediction
from .features import (
    CommodityFeatureError,
    CommodityFeatureVector,
    build_commodity_features,
)
from .models import CANONICAL_PATHS, BaselineLGBXGBStack
from .tickers import CommodityTicker, parse_commodity_ticker


log = logging.getLogger(__name__)


class CommodityPredictionError(RuntimeError):
    """Raised when we can't produce a probability for a commodity ticker."""


_baseline_stacks: dict[str, BaselineLGBXGBStack] = {}


def _get_baseline_stack(timeframe: str) -> BaselineLGBXGBStack:
    if timeframe not in _baseline_stacks:
        _baseline_stacks[timeframe] = BaselineLGBXGBStack(timeframe=timeframe)
    return _baseline_stacks[timeframe]


async def _resolve_model(canonical_name: str, fallback: Any) -> tuple[Any, str, str]:
    """Return (model_obj, model_name, version_tag).

    Tries the model loader first; falls back to the in-process baseline
    when the canonical path isn't in the catalog.  We swallow load
    errors here on purpose — a sick commodity model must NOT poison
    the rest of the verticals.
    """
    state = models_loader.get_state()
    if state.ready and canonical_name in state.predictors:
        try:
            obj = await models_loader.load_predictor(canonical_name)
        except Exception as e:  # pragma: no cover — model-specific failures
            log.warning(
                "[commodities] failed to load %s, falling back to baseline: %s",
                canonical_name, e,
            )
        else:
            meta = state.predictors.get(canonical_name) or {}
            version = meta.get("version", "unknown")
            return obj, canonical_name, str(version)
    name = getattr(fallback, "name", type(fallback).__name__)
    return fallback, name, "baseline"


def _proba_yes(obj: Any, vector: list[float]) -> tuple[float, float | None, dict[str, Any]]:
    """Run the model and pull the YES-class probability + a margin."""
    import numpy as np

    X = np.asarray([vector], dtype=float)
    raw: dict[str, Any] = {}
    if hasattr(obj, "predict_proba"):
        proba = obj.predict_proba(X)
        row = list(map(float, np.asarray(proba)[0].tolist()))
        raw["proba"] = row
        if len(row) == 2:
            return row[1], abs(row[1] - row[0]), raw
        sorted_row = sorted(row, reverse=True)
        margin = sorted_row[0] - sorted_row[1] if len(sorted_row) > 1 else None
        return sorted_row[0], margin, raw
    if hasattr(obj, "predict"):
        yhat = obj.predict(X)
        val = float(np.asarray(yhat).ravel()[0])
        raw["predict"] = val
        return max(0.0, min(1.0, val)), None, raw
    raise CommodityPredictionError(
        f"commodity model {type(obj).__name__} exposes neither predict_proba nor predict"
    )


async def predict_commodity_binary(
    ticker: str | CommodityTicker,
    *,
    league: str | None = "COMMODITIES",
) -> Prediction:
    """Live prediction for a Kalshi commodity-binary market."""
    parsed = ticker if isinstance(ticker, CommodityTicker) else parse_commodity_ticker(ticker)

    try:
        fv: CommodityFeatureVector = await build_commodity_features(parsed)
    except CommodityFeatureError as e:
        raise CommodityPredictionError(str(e)) from e

    stack_path = CANONICAL_PATHS[parsed.timeframe]
    stack_obj, stack_name, stack_version = await _resolve_model(
        stack_path, _get_baseline_stack(parsed.timeframe)
    )

    t0 = time.monotonic()
    stack_p, stack_conf, stack_raw = await asyncio.to_thread(
        _proba_yes, stack_obj, fv.values
    )
    elapsed_ms = (time.monotonic() - t0) * 1000.0

    cal_prob, cal_info = calibrators.apply(stack_p, league)
    if cal_info.get("applied"):
        probability = cal_prob
        cal_extra: dict[str, Any] = {"uncalibrated_probability": float(stack_p)}
    else:
        probability = stack_p
        cal_extra = {}

    raw: dict[str, Any] = {
        "ticker": {
            "raw": parsed.raw,
            "symbol": parsed.symbol,
            "timeframe": parsed.timeframe,
            "threshold": parsed.threshold,
            "direction": parsed.direction,
        },
        "features": fv.as_dict(),
        "feature_count": len(fv.values),
        "feature_present": fv.bar_count,
        "last_price": fv.last_price,
        "stack": {
            "name": stack_name,
            "version": stack_version,
            **stack_raw,
        },
        "calibration": {**cal_info, **cal_extra},
    }

    return Prediction(
        probability=float(probability),
        confidence=float(stack_conf) if stack_conf is not None else None,
        model_name=f"commodities:{parsed.timeframe}:{stack_name}",
        model_version=stack_version,
        model_type=type(stack_obj).__name__,
        elapsed_ms=round(elapsed_ms, 3),
        raw=raw,
    )
