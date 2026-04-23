"""Public predictor entry point for the precipitation vertical (task #255).

Mirrors `app.weather.predictor` but for KXRAIN contracts: pulls the
unified weather payload, builds a multi-source ensemble of
precipitation point forecasts (Open-Meteo, Tomorrow.io, AccuWeather
minutecast intensity, optional HRRR daily QPF when surfaced), runs
the `precip_emos.apply_precip_emos` post-processor with a gamma /
logistic link, and folds the YES probability through the per-vertical
isotonic calibrator so retrain.py's drift gate keeps working
unchanged.

Failures degrade to ``None`` (swarm-only signals), exactly like the
temperature path — the cycle dispatcher then falls through to the
generic meta-model.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from .. import calibrators
from ..feeds.weather_payload import build_weather_payload
from ..predictor import Prediction
from . import precip_emos
from .precip_tickers import (
    PrecipitationTicker,
    parse_precipitation_ticker,
)


log = logging.getLogger(__name__)


class PrecipitationPredictionError(RuntimeError):
    """Raised when we can't produce a probability for a KXRAIN ticker."""


# ---------------------------------------------------------------------------
# Ensemble extraction
# ---------------------------------------------------------------------------


# Keys we treat as "expected daily precipitation in inches" point
# forecasts from the unified weather payload.  Each source is folded
# in only when present so a single-source degradation doesn't kill the
# call — the EMOS layer handles a thin (or empty) ensemble gracefully.
_PRECIP_KEYS: tuple[str, ...] = (
    "om_precip_1hr_in",      # Open-Meteo precipitation last hour (inches)
    "tio_precip_1hr_in",     # Tomorrow.io precipitation last hour (inches)
    "hrrr_precip_in",        # Optional HRRR daily QPF (inches), if surfaced
    "accu_precip_intensity",  # AccuWeather minutecast intensity proxy
)


def _ensemble_for_precip(payload: dict[str, Any]) -> list[float]:
    out: list[float] = []
    for k in _PRECIP_KEYS:
        v = payload.get(k)
        if v is None:
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if fv < 0:
            continue
        out.append(fv)
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def predict_precipitation_binary(
    ticker: str | PrecipitationTicker,
    *,
    league: str | None = "WEATHER",
    payload_override: dict[str, Any] | None = None,
) -> Prediction:
    """Live prediction for a Kalshi KXRAIN precipitation contract.

    ``payload_override`` is a test seam — production callers leave it
    None and let the predictor pull the unified weather payload itself.
    """
    parsed = (
        ticker if isinstance(ticker, PrecipitationTicker)
        else parse_precipitation_ticker(ticker)
    )

    if payload_override is not None:
        payload = dict(payload_override)
    else:
        try:
            payload = await build_weather_payload(parsed.city)
        except Exception as e:
            raise PrecipitationPredictionError(
                f"weather payload fetch failed for {parsed.city}: "
                f"{type(e).__name__}: {e}"
            ) from e

    ensemble = _ensemble_for_precip(payload)
    if not ensemble:
        raise PrecipitationPredictionError(
            f"no usable precipitation forecasts for {parsed.city}"
        )

    t0 = time.monotonic()
    emos_result = precip_emos.apply_precip_emos(
        ensemble=ensemble,
        threshold_in=parsed.threshold_in,
    )
    elapsed_ms = (time.monotonic() - t0) * 1000.0

    raw_p = emos_result.probability_for(parsed.direction)
    cal_prob, cal_info = calibrators.apply(raw_p, league)
    if cal_info.get("applied"):
        probability = cal_prob
        cal_extra: dict[str, Any] = {"uncalibrated_probability": float(raw_p)}
    else:
        probability = raw_p
        cal_extra = {}

    raw: dict[str, Any] = {
        "ticker": {
            "raw": parsed.raw,
            "metric": "RAIN",
            "city": parsed.city,
            "threshold": parsed.threshold_in,
            "direction": parsed.direction,
        },
        "ensemble": {
            "values": ensemble,
            "mean": emos_result.ensemble_mean,
            "var": emos_result.ensemble_var,
            "n": len(ensemble),
        },
        "emos": {
            "mu": emos_result.mu,
            "sigma": emos_result.sigma,
            "p_above": emos_result.p_above,
            "p_below": emos_result.p_below,
            "link": emos_result.link,
        },
        "calibration": {**cal_info, **cal_extra},
    }

    confidence = abs(2.0 * float(probability) - 1.0)
    return Prediction(
        probability=float(probability),
        confidence=float(confidence),
        model_name="weather_precip_emos:rain",
        model_version="emos-v1",
        model_type="EMOS-Gamma",
        elapsed_ms=round(elapsed_ms, 3),
        raw=raw,
    )


__all__ = [
    "PrecipitationPredictionError",
    "predict_precipitation_binary",
]
