"""Public predictor entry point for the crypto-binary vertical.

Other verticals call ``app.predictor.predict(features, league=...)``;
crypto callers use this module's ``predict_crypto_binary(ticker)`` instead
because:

  * the feature vector is built from on-disk OHLCV (not handed in by
    the cycle), and
  * each timeframe (5m / 15m / 1h) routes to a different ensemble plus,
    for short-horizon BTC tickers, a directional GRU blend.

The returned ``Prediction`` is the same dataclass the rest of the
trading loop consumes (gates, dashboard, cycle telemetry) so the
pipeline only needs to know "ask the crypto predictor for a probability"
rather than the model layout behind the call.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .. import calibrators, models_loader
from ..predictor import Prediction
from .features import (
    CryptoFeatureError,
    CryptoFeatureVector,
    build_crypto_features,
)
from .models import (
    CANONICAL_PATHS,
    BaselineGRUDirection,
    BaselineLGBXGBStack,
)
from .tickers import CryptoTicker, parse_crypto_ticker


log = logging.getLogger(__name__)


class CryptoPredictionError(RuntimeError):
    """Raised when we can't produce a probability for a crypto ticker."""


# Process-cached baseline instances so repeated cycles don't re-allocate.
_baseline_stacks: dict[str, BaselineLGBXGBStack] = {}
_baseline_gru: BaselineGRUDirection | None = None


def _get_baseline_stack(timeframe: str) -> BaselineLGBXGBStack:
    if timeframe not in _baseline_stacks:
        _baseline_stacks[timeframe] = BaselineLGBXGBStack(timeframe=timeframe)
    return _baseline_stacks[timeframe]


def _get_baseline_gru() -> BaselineGRUDirection:
    global _baseline_gru
    if _baseline_gru is None:
        _baseline_gru = BaselineGRUDirection()
    return _baseline_gru


async def _resolve_model(canonical_name: str, fallback: Any) -> tuple[Any, str, str]:
    """Return (model_obj, model_name, version_tag).

    Tries to load a trained artifact via the global model loader first;
    falls back to the in-process baseline class when the canonical path
    isn't in the catalog (cold install) or the load fails.  We swallow
    load errors here on purpose — a sick crypto model must NOT poison
    the rest of the verticals' predict path.
    """
    state = models_loader.get_state()
    if state.ready and canonical_name in state.predictors:
        try:
            obj = await models_loader.load_predictor(canonical_name)
        except Exception as e:  # pragma: no cover — model-specific failures
            log.warning(
                "[crypto] failed to load %s, falling back to baseline: %s",
                canonical_name, e,
            )
        else:
            meta = state.predictors.get(canonical_name) or {}
            version = meta.get("version", "unknown")
            return obj, canonical_name, str(version)
    name = getattr(fallback, "name", type(fallback).__name__)
    return fallback, name, "baseline"


def _proba_yes(obj: Any, vector: list[float]) -> tuple[float, float | None, dict[str, Any]]:
    """Run the model and pull the YES-class probability + a margin.

    Models without ``predict_proba`` fall back to ``predict``, clamped
    to [0, 1] so the gate code can treat the field uniformly.
    """
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
    raise CryptoPredictionError(
        f"crypto model {type(obj).__name__} exposes neither predict_proba nor predict"
    )


def _ensemble_blend(stack_p: float, gru_p: float | None) -> tuple[float, dict[str, Any]]:
    """Blend the timeframe ensemble with the directional GRU.

    The GRU is only meaningful for the short-horizon BTC bets (5m /
    15m), so the caller decides whether to include it.  When supplied,
    we take a 65/35 weighted mean — the ensemble drives, the GRU
    sharpens.  Returns the blended probability plus a small report
    suitable for stuffing into Prediction.raw.
    """
    if gru_p is None:
        return stack_p, {"stack_probability": stack_p, "gru_probability": None}
    blended = 0.65 * stack_p + 0.35 * gru_p
    return blended, {
        "stack_probability": stack_p,
        "gru_probability": gru_p,
        "blend_weights": {"stack": 0.65, "gru": 0.35},
    }


def _should_use_gru(ticker: CryptoTicker) -> bool:
    """The directional GRU is BTC-only and short-horizon by design."""
    return ticker.symbol == "BTC" and ticker.timeframe in ("5m", "15m")


async def predict_crypto_binary(
    ticker: str | CryptoTicker,
    *,
    league: str | None = "CRYPTO",
) -> Prediction:
    """Live prediction for a Kalshi crypto-binary market.

    `ticker` may be a raw Kalshi string (we'll parse it) or a
    pre-parsed `CryptoTicker`.  Raises `CryptoPredictionError` on a
    parse failure or a missing OHLCV window — both are caller-actionable
    (the cycle reports them and falls back to swarm-only signals).
    """
    parsed = ticker if isinstance(ticker, CryptoTicker) else parse_crypto_ticker(ticker)

    try:
        fv: CryptoFeatureVector = await build_crypto_features(parsed)
    except CryptoFeatureError as e:
        raise CryptoPredictionError(str(e)) from e

    stack_path = CANONICAL_PATHS[parsed.timeframe]
    stack_obj, stack_name, stack_version = await _resolve_model(
        stack_path, _get_baseline_stack(parsed.timeframe)
    )

    gru_obj: Any = None
    gru_name: str | None = None
    gru_version: str | None = None
    if _should_use_gru(parsed):
        gru_obj, gru_name, gru_version = await _resolve_model(
            CANONICAL_PATHS["gru_btc_direction"], _get_baseline_gru()
        )

    t0 = time.monotonic()
    stack_p, stack_conf, stack_raw = await asyncio.to_thread(
        _proba_yes, stack_obj, fv.values
    )
    gru_p: float | None = None
    gru_conf: float | None = None
    gru_raw: dict[str, Any] = {}
    if gru_obj is not None:
        gru_p, gru_conf, gru_raw = await asyncio.to_thread(
            _proba_yes, gru_obj, fv.values
        )

    probability, blend_info = _ensemble_blend(stack_p, gru_p)
    elapsed_ms = (time.monotonic() - t0) * 1000.0

    # Per-league calibration on top of the ensemble — same contract the
    # generic predictor uses so the operator-tuned crypto calibrator
    # (when one is loaded) applies automatically.
    cal_prob, cal_info = calibrators.apply(probability, league)
    if cal_info.get("applied"):
        blend_info["uncalibrated_probability"] = float(probability)
        probability = cal_prob

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
        "gru": {
            "name": gru_name,
            "version": gru_version,
            "used": gru_obj is not None,
            **gru_raw,
        },
        "blend": blend_info,
        "calibration": cal_info,
    }

    # Confidence: prefer the ensemble component's margin; for blended
    # results take the smaller of the two (the more cautious signal).
    confidence: float | None
    if stack_conf is not None and gru_conf is not None:
        confidence = min(stack_conf, gru_conf)
    else:
        confidence = stack_conf if stack_conf is not None else gru_conf

    return Prediction(
        probability=float(probability),
        confidence=float(confidence) if confidence is not None else None,
        model_name=f"crypto:{parsed.timeframe}:{stack_name}"
                   + (f"+{gru_name}" if gru_obj is not None else ""),
        model_version=stack_version,
        model_type=type(stack_obj).__name__,
        elapsed_ms=round(elapsed_ms, 3),
        raw=raw,
    )
