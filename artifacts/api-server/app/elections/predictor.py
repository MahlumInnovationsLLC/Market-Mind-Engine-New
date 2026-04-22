"""Public predictor entry point for the elections vertical (task #201).

Mirrors `app.tech_ai.predictor` so the cycle dispatcher can swap
between predictor hooks via the registry.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from .. import calibrators
from ..predictor import Prediction
from .model import poll_of_polls_probability
from .polls import resolve_polls
from .tickers import ElectionTicker, parse_election_ticker


log = logging.getLogger(__name__)


class ElectionPredictionError(RuntimeError):
    """Raised when we can't produce a probability for an election ticker."""


async def predict_election_binary(
    ticker: ElectionTicker | str,
    *,
    league: str = "ELECTIONS",
    lookback_days: int = 60,
) -> Prediction:
    """Resolve polls + run the poll-of-polls model for an election binary."""
    if isinstance(ticker, str):
        ticker = parse_election_ticker(ticker)
    t0 = time.monotonic()
    polls = await resolve_polls(ticker, lookback_days=lookback_days)
    result = poll_of_polls_probability(ticker, polls)
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
            "office": ticker.office,
            "candidate": ticker.candidate,
            "region": ticker.region,
            "scope": ticker.scope,
            "when": ticker.when,
        },
        "poll_count": len(polls),
        "contributing_polls": result.contributing_polls,
        "weighted_support_pct": result.weighted_support_pct,
        "raw_support_pct": result.raw_support_pct,
        "state_bias_pp": result.state_bias_pp,
        "polymarket_implied_prob": result.polymarket_implied_prob,
        "features": result.feature_summary,
        "feature_count": len(result.feature_summary),
        "calibration": {**cal_info, **cal_extra},
    }
    if result.contributing_polls == 0 and result.polymarket_implied_prob is None:
        confidence: float | None = 0.0
    else:
        confidence = min(1.0, 0.2 + 0.05 * max(0, result.contributing_polls))

    return Prediction(
        probability=float(probability),
        confidence=confidence,
        model_name="elections_poll_of_polls",
        model_version="1",
        model_type="PollOfPolls",
        elapsed_ms=round(elapsed_ms, 3),
        raw=raw,
    )
