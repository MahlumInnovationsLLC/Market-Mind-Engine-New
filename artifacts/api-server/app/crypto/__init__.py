"""Crypto-binary vertical: predictors, features, ticker parsing, backtest.

Public re-exports keep callers from having to know the file layout:

    from app.crypto import predict_crypto_binary, parse_crypto_ticker

The vertical ships baseline LGB+XGB ensembles per timeframe (5m / 15m /
1h) and a small GRU for short-horizon BTC direction.  Initial weights
are deliberately tiny placeholders — the AutoResearch + drift-retrain
loop replaces them with trained artifacts over time.  See
``app.crypto.models`` for the baseline definitions.
"""
from __future__ import annotations

from .predictor import (
    CryptoPredictionError,
    predict_crypto_binary,
)
from .tickers import (
    CryptoTicker,
    InvalidCryptoTicker,
    parse_crypto_ticker,
)

__all__ = [
    "CryptoPredictionError",
    "CryptoTicker",
    "InvalidCryptoTicker",
    "parse_crypto_ticker",
    "predict_crypto_binary",
]
