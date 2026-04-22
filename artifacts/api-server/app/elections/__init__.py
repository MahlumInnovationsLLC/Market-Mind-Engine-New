"""Elections trading vertical (task #201).

Public surface:
- `tickers.parse_election_ticker` — parse Kalshi election binary tickers.
- `polls.resolve_polls` — query the `election_polls` cache for a market.
- `model.poll_of_polls_probability` — combine poll rows into a YES probability.
- `arb.detect_arb` — Kalshi vs Polymarket spread / arb detector.
- `predictor.predict_election_binary` — entry point used by the cycle.
- `promotion.evaluate_promotion` — Kelly auto-promote eligibility check.
"""
from __future__ import annotations

from .tickers import (
    InvalidElectionTicker,
    SUPPORTED_OFFICES,
    ElectionTicker,
    parse_election_ticker,
)

__all__ = [
    "InvalidElectionTicker",
    "SUPPORTED_OFFICES",
    "ElectionTicker",
    "parse_election_ticker",
]
