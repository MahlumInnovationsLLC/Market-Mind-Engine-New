"""Public predictor entry point for the tech-AI vertical (task #199).

Mirrors `app.commodities.predictor` so the cycle dispatcher can swap
between predictor hooks via the registry.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from .. import calibrators
from ..predictor import Prediction
from .evidence import resolve_evidence
from .model import bayesian_probability
from .tickers import TechAITicker, parse_tech_ai_ticker


log = logging.getLogger(__name__)


class TechAIPredictionError(RuntimeError):
    """Raised when we can't produce a probability for a tech-AI ticker."""


async def predict_tech_ai_binary(
    ticker: TechAITicker | str,
    *,
    league: str = "TECH_AI",
    lookback_days: int = 30,
) -> Prediction:
    """Resolve evidence + run the Bayesian model for a tech-AI binary."""
    if isinstance(ticker, str):
        ticker = parse_tech_ai_ticker(ticker)
    t0 = time.monotonic()
    evidence = await resolve_evidence(ticker, lookback_days=lookback_days)
    result = bayesian_probability(ticker, evidence)
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
            "symbol": ticker.symbol,
            "direction": ticker.direction,
            "threshold": ticker.threshold,
            "when": ticker.when,
        },
        "evidence_count": len(evidence),
        "contributing_rows": result.contributing_rows,
        "log_odds_shift": result.total_log_odds_shift,
        "features": result.feature_summary,
        "feature_count": len(result.feature_summary),
        "calibration": {**cal_info, **cal_extra},
    }
    # Confidence: 0.5 with no evidence, scales with contributing rows.
    if result.contributing_rows == 0:
        confidence: float | None = 0.0
    else:
        confidence = min(1.0, 0.2 + 0.1 * result.contributing_rows)

    return Prediction(
        probability=float(probability),
        confidence=confidence,
        model_name="tech_ai_bayesian",
        model_version="1",
        model_type="BayesianLogOdds",
        elapsed_ms=round(elapsed_ms, 3),
        raw=raw,
    )
