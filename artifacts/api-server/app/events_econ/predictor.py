"""Public predictor entry point for the events-econ vertical (task #202).

Mirrors `app.elections.predictor` so the cycle dispatcher can swap
between predictor hooks via the registry.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from .. import calibrators
from ..predictor import Prediction
from .calendar import resolve_releases
from .model import consensus_prior_probability
from .tickers import EventsEconTicker, parse_events_econ_ticker


log = logging.getLogger(__name__)


class EventsEconPredictionError(RuntimeError):
    """Raised when we can't produce a probability for an events-econ ticker."""


async def predict_events_econ_binary(
    ticker: EventsEconTicker | str,
    *,
    league: str = "EVENTS_ECON",
    lookback_days: int = 730,
) -> Prediction:
    """Resolve releases + run the consensus-prior model for a macro binary."""
    if isinstance(ticker, str):
        ticker = parse_events_econ_ticker(ticker)
    t0 = time.monotonic()
    releases = await resolve_releases(ticker, lookback_days=lookback_days)
    result = consensus_prior_probability(ticker, releases)
    elapsed_ms = (time.monotonic() - t0) * 1000.0

    cal_prob, cal_info = calibrators.apply(result.probability, league)
    if cal_info.get("applied"):
        probability = cal_prob
        cal_extra: dict[str, Any] = {"uncalibrated_probability": float(result.probability)}
    else:
        probability = result.probability
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
