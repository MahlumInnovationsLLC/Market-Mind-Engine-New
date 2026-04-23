"""Temperature trading vertical (task #246).

GRU sequence model over the multi-source weather feed
(``weather_observations`` + ``weather_extended`` + HRRR + climatology),
post-processed by a Nonhomogeneous Gaussian Regression (EMOS) layer
that turns the model's point forecast + ensemble spread into a
calibrated probabilistic forecast.  Wired into the orchestrator via
``VerticalSpec.predictor_hook='weather_temp_gru_emos'``.
"""
from __future__ import annotations

from .tickers import (
    InvalidTemperatureTicker,
    SUPPORTED_CITIES,
    TemperatureTicker,
    parse_temperature_ticker,
)


__all__ = [
    "TemperatureTicker",
    "InvalidTemperatureTicker",
    "SUPPORTED_CITIES",
    "parse_temperature_ticker",
]
