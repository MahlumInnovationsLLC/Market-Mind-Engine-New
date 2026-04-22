"""Public predictor entry point for the entertainment vertical (task #203).

Mirrors `app.events_econ.predictor`.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Any

from .. import calibrators
from ..predictor import Prediction
from .model import hybrid_probability
from .signals import resolve_signals
from .tickers import EntertainmentTicker, parse_entertainment_ticker


log = logging.getLogger(__name__)


_DEFAULT_KALSHI_BLEND_WEIGHT = 0.4


async def snapshot_kalshi_mids() -> dict[str, float]:
    """Return a {SIGNAL_TYPE:TITLE: yes_mid} snapshot of live Kalshi
    entertainment markets, used by the public calendar route to surface
    model-vs-market expected edge.

    Best-effort — returns an empty dict when Kalshi is unreachable, the
    auth secrets aren't configured, or no entertainment markets are
    open.  The calendar route swallows any exception this raises.
    """
    out: dict[str, float] = {}
    try:
        from ..kalshi import client as _kalshi_client
    except Exception:
        return out
    list_markets = getattr(_kalshi_client, "list_open_markets", None)
    if list_markets is None:
        return out
    try:
        markets = await list_markets(series_prefix="KX")
    except Exception:
        return out
    for m in markets or []:
        ticker_str = (m.get("ticker") or m.get("market_id") or "").strip()
        if not ticker_str:
            continue
        try:
            t = parse_entertainment_ticker(ticker_str)
        except Exception:
            continue
        yes_mid = m.get("yes_mid") or m.get("last_price")
        if yes_mid is None:
            yes_bid, yes_ask = m.get("yes_bid"), m.get("yes_ask")
            if yes_bid is not None and yes_ask is not None:
                try:
                    yes_mid = (float(yes_bid) + float(yes_ask)) / 2.0
                except (TypeError, ValueError):
                    yes_mid = None
        if yes_mid is None:
            continue
        try:
            out[f"{t.signal_type}:{t.title}".upper()] = float(yes_mid)
        except (TypeError, ValueError):
            continue
    return out


def _logit(p: float) -> float:
    p = min(0.999, max(0.001, p))
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


class EntertainmentPredictionError(RuntimeError):
    """Raised when we can't produce a probability for an entertainment ticker."""


async def predict_entertainment_binary(
    ticker: EntertainmentTicker | str,
    *,
    league: str = "ENTERTAINMENT",
    lookback_days: int = 1825,
    kalshi_mid: float | None = None,
    kalshi_blend_weight: float = _DEFAULT_KALSHI_BLEND_WEIGHT,
) -> Prediction:
    """Resolve signals + run the hybrid model for an entertainment binary."""
    if isinstance(ticker, str):
        ticker = parse_entertainment_ticker(ticker)
    t0 = time.monotonic()
    signals = await resolve_signals(ticker, lookback_days=lookback_days)
    result = hybrid_probability(ticker, signals)
    elapsed_ms = (time.monotonic() - t0) * 1000.0

    model_prob_pre_blend = float(result.probability)

    blend_info: dict[str, Any] = {"applied": False, "weight": 0.0}
    blended_prob = model_prob_pre_blend
    if kalshi_mid is not None and 0.0 < kalshi_mid < 1.0 and kalshi_blend_weight > 0.0:
        w = max(0.0, min(1.0, kalshi_blend_weight))
        blended_prob = _sigmoid(
            (1.0 - w) * _logit(model_prob_pre_blend) + w * _logit(float(kalshi_mid))
        )
        blended_prob = max(0.05, min(0.95, blended_prob))
        blend_info = {
            "applied": True,
            "weight": w,
            "kalshi_mid": float(kalshi_mid),
            "model_prob_pre_blend": model_prob_pre_blend,
        }

    cal_prob, cal_info = calibrators.apply(blended_prob, league)
    if cal_info.get("applied"):
        probability = cal_prob
        cal_extra: dict[str, Any] = {"uncalibrated_probability": float(blended_prob)}
    else:
        probability = blended_prob
        cal_extra = {}

    raw: dict[str, Any] = {
        "ticker": {
            "raw": ticker.raw,
            "signal_type": ticker.signal_type,
            "category": ticker.category,
            "when": ticker.when,
            "title": ticker.title,
            "threshold": ticker.threshold,
        },
        "signal_count": len(signals),
        "contributing_history": result.contributing_history,
        "head": result.head,
        "threshold": result.threshold,
        "direction": result.direction,
        "sigma": result.sigma,
        "features": result.feature_summary,
        "feature_count": len(result.feature_summary),
        "kalshi_blend": blend_info,
        "calibration": {**cal_info, **cal_extra},
    }
    if result.contributing_history == 0 and result.head == "unknown":
        confidence: float | None = 0.0
    else:
        confidence = min(1.0, 0.2 + 0.05 * max(0, result.contributing_history))

    return Prediction(
        probability=float(probability),
        confidence=confidence,
        model_name="entertainment_hybrid",
        model_version="1",
        model_type="EntertainmentHybrid",
        elapsed_ms=round(elapsed_ms, 3),
        raw=raw,
    )
