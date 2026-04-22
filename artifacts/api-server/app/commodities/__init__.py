"""Commodities-binary trading vertical (task #198).

Public surface mirrors `app.crypto`: parse Kalshi commodity tickers,
build feature vectors from `commodities_ohlcv`, run the per-contract
LGB+XGB ensemble + per-league calibrator, and expose an
`predict_commodity_binary(ticker)` entry point the orchestrator
dispatches to via `VerticalSpec.predictor_hook='commodities_binary'`.
"""
from __future__ import annotations

from .correlations import (
    CORRELATED_PAIRS,
    correlation_for,
    correlated_exposure,
)
from .tickers import (
    CommodityTicker,
    InvalidCommodityTicker,
    SUPPORTED_SYMBOLS,
    parse_commodity_ticker,
)


__all__ = [
    "CommodityTicker",
    "InvalidCommodityTicker",
    "SUPPORTED_SYMBOLS",
    "parse_commodity_ticker",
    "CORRELATED_PAIRS",
    "correlation_for",
    "correlated_exposure",
]
