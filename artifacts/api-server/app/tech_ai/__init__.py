"""Tech / AI trading vertical (task #199).

Public surface:
- `tickers.parse_tech_ai_ticker` — parse Kalshi tech-AI binary tickers.
- `evidence.resolve_evidence` — query the `tech_evidence` cache for a market.
- `model.bayesian_probability` — combine evidence rows into a YES probability.
- `predictor.predict_tech_ai_binary` — entry point used by the cycle.
- `promotion.evaluate_promotion` — Kelly auto-promote eligibility check.
"""
from __future__ import annotations

from .tickers import (
    InvalidTechAITicker,
    SUPPORTED_SYMBOLS,
    TechAITicker,
    parse_tech_ai_ticker,
)

__all__ = [
    "InvalidTechAITicker",
    "SUPPORTED_SYMBOLS",
    "TechAITicker",
    "parse_tech_ai_ticker",
]
