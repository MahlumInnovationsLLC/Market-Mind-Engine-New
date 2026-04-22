"""Public predictor entry point for the events-econ vertical (task #202).

Mirrors `app.elections.predictor` so the cycle dispatcher can swap
between predictor hooks via the registry.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Any

from .. import calibrators
from ..predictor import Prediction
from .calendar import resolve_releases
from .model import consensus_prior_probability
from .tickers import EventsEconTicker, parse_events_econ_ticker


log = logging.getLogger(__name__)


# Weight given to the Kalshi pre-release market mid in the log-odds
# blend with the consensus-prior probability.  0.4 means the model
# probability shifts ~40% of the way toward the market mid in
# log-odds space, leaving the consensus prior in charge but treating
# the market as a soft Bayesian update on its own information set.
_DEFAULT_KALSHI_BLEND_WEIGHT = 0.4


def _logit(p: float) -> float:
    p = min(0.999, max(0.001, p))
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


class EventsEconPredictionError(RuntimeError):
    """Raised when we can't produce a probability for an events-econ ticker."""


async def predict_events_econ_binary(
    ticker: EventsEconTicker | str,
    *,
    league: str = "EVENTS_ECON",
    lookback_days: int = 730,
    kalshi_mid: float | None = None,
    kalshi_blend_weight: float = _DEFAULT_KALSHI_BLEND_WEIGHT,
) -> Prediction:
    """Resolve releases + run the consensus-prior model for a macro binary.

    `kalshi_mid` is the pre-release Kalshi YES mid (0..1).  When
    provided, the model probability is blended with the market mid
    in log-odds space — required for the events-econ vertical because
    the market itself encodes information not in the consensus +
    history alone (e.g. options-implied risk, FOMC speech tone,
    intraday surprise leaks).
    """
    if isinstance(ticker, str):
        ticker = parse_events_econ_ticker(ticker)
    t0 = time.monotonic()
    releases = await resolve_releases(ticker, lookback_days=lookback_days)
    result = consensus_prior_probability(ticker, releases)
    elapsed_ms = (time.monotonic() - t0) * 1000.0

    model_prob_pre_blend = float(result.probability)

    # Kalshi mid blend (Bayesian-style log-odds combination).
    blend_info: dict[str, Any] = {"applied": False, "weight": 0.0}
    blended_prob = model_prob_pre_blend
    if kalshi_mid is not None and 0.0 < kalshi_mid < 1.0 and kalshi_blend_weight > 0.0:
        w = max(0.0, min(1.0, kalshi_blend_weight))
        blended_prob = _sigmoid(
            (1.0 - w) * _logit(model_prob_pre_blend) + w * _logit(float(kalshi_mid))
        )
        # Re-clamp into the same [0.05, 0.95] band the model uses.
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
            "release_type": ticker.release_type,
            "when": ticker.when,
            "threshold": ticker.threshold,
        },
        "release_count": len(releases),
        "contributing_history": result.contributing_history,
        "consensus": result.consensus,
        "threshold": result.threshold,
        "direction": result.direction,
        "sigma": result.sigma,
        "features": result.feature_summary,
        "feature_count": len(result.feature_summary),
        "kalshi_blend": blend_info,
        "calibration": {**cal_info, **cal_extra},
    }
    if result.contributing_history == 0 and result.consensus is None:
        confidence: float | None = 0.0
    else:
        confidence = min(1.0, 0.2 + 0.05 * max(0, result.contributing_history))

    return Prediction(
        probability=float(probability),
        confidence=confidence,
        model_name="events_econ_consensus_prior",
        model_version="1",
        model_type="ConsensusPrior",
        elapsed_ms=round(elapsed_ms, 3),
        raw=raw,
    )
