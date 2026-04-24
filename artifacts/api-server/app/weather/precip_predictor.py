"""Public predictor entry point for the precipitation vertical (task #255 + #281).

Mirrors `app.weather.predictor` (the temperature path) for KXRAIN
contracts: pulls the unified weather payload, builds a multi-source
ensemble of precipitation point forecasts (Open-Meteo, Tomorrow.io,
AccuWeather minutecast intensity, optional HRRR daily QPF when
surfaced), runs the precipitation GRU bias head over the same feature
trace, hands the GRU's non-negative bias + spread to
`precip_emos.apply_precip_emos` (gamma / logistic link), and folds the
YES probability through the per-vertical isotonic calibrator so
retrain.py's drift gate keeps working unchanged.

Failures degrade to ``None`` (swarm-only signals), exactly like the
temperature path — the cycle dispatcher then falls through to the
generic meta-model.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from .. import calibrators, models_loader
from ..feeds.weather_payload import build_weather_payload
from ..predictor import Prediction
from . import precip_emos, precip_gru
from .precip_tickers import (
    PrecipitationTicker,
    parse_precipitation_ticker,
)


log = logging.getLogger(__name__)


CANONICAL_PRECIP_GRU_PATH = "models/saved/precip_gru.pkl"


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
# GRU feature extraction
# ---------------------------------------------------------------------------


def _hour_norm(now: datetime) -> float:
    return (now.hour + now.minute / 60.0) / 24.0


def _doy_norm(now: datetime) -> float:
    return (int(now.strftime("%j")) - 1) / 365.0


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        fv = float(value)
    except (TypeError, ValueError):
        return None
    if fv < 0:
        return None
    return fv


def _ensemble_stats(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    n = len(values)
    mean = sum(values) / n
    if n == 1:
        return mean, 0.0
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return mean, var


def _build_precip_sequence(payload: dict[str, Any]) -> list[list[float]]:
    """Produce a (T, 8) precipitation feature trace for the GRU.

    Trace = AccuWeather minutecast samples interleaved with the
    multi-source point forecasts when minutecast is surfaced; otherwise
    a single-step fallback composed of the current-cycle forecasts so
    the GRU still runs and contributes a calibrated spread + bias
    estimate.

    Feature order (matches ``precip_gru.baseline_weights``):
      [om_in, tio_in, hrrr_in, accu_in, em_mean_in, em_var_in,
       hour_norm, doy_norm]
    """
    now = datetime.now(timezone.utc)
    hour_n = _hour_norm(now)
    doy_n = _doy_norm(now)

    om = _to_float(payload.get("om_precip_1hr_in"))
    tio = _to_float(payload.get("tio_precip_1hr_in"))
    hrrr = _to_float(payload.get("hrrr_precip_in"))
    accu = _to_float(payload.get("accu_precip_intensity"))
    present = [v for v in (om, tio, hrrr, accu) if v is not None]
    em_mean, em_var = _ensemble_stats(present)

    samples = _accu_minutecast_samples(payload)
    if samples:
        seq: list[list[float]] = []
        for s in samples[:12]:
            # NB: explicit None checks — `0.0` (a dry minute) is a
            # valid observed intensity and must NOT fall through to
            # `s.get("in")` or to the ensemble mean.  An `or` chain
            # would silently inject artificial precipitation on dry
            # samples.
            intensity = _to_float(s.get("intensity"))
            step_v = intensity if intensity is not None else _to_float(s.get("in"))
            if step_v is None:
                step_v = em_mean
            seq.append([
                _coalesce(om, step_v, 0.0),
                _coalesce(tio, step_v, 0.0),
                _coalesce(hrrr, step_v, 0.0),
                float(step_v),
                em_mean,
                em_var,
                hour_n,
                doy_n,
            ])
        return seq

    # Single-step fallback — the GRU still runs and contributes its
    # default calibrated bias + spread head.
    return [[
        _coalesce(om, 0.0),
        _coalesce(tio, 0.0),
        _coalesce(hrrr, 0.0),
        _coalesce(accu, 0.0),
        em_mean,
        em_var,
        hour_n,
        doy_n,
    ]]


def _accu_minutecast_samples(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull the AccuWeather minutecast samples list if surfaced."""
    raw = (
        payload.get("accu_minutecast_samples")
        or payload.get("_accu_samples")
    )
    if isinstance(raw, list):
        return [s for s in raw if isinstance(s, dict)]
    return []


def _coalesce(*values: Any) -> float:
    for v in values:
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return 0.0


# ---------------------------------------------------------------------------
# GRU weight resolution: trained checkpoint > in-process baseline.
# ---------------------------------------------------------------------------


async def _resolve_precip_gru_weights() -> tuple[
    precip_gru.PrecipGRUWeights, str, str
]:
    """Try the model loader first; fall back to the deterministic
    baseline so cold installs still get a real (calibrated) prediction.

    Resolution order mirrors ``predictor._resolve_gru_weights``:
      1. Cached in-memory deserialized object (set by a previous call
         or by the boot-time smoke load).
      2. Lazy ``models_loader.load_predictor(CANONICAL_PRECIP_GRU_PATH)``
         — deserializes the on-disk artifact and registers it in the
         loader's process-wide cache so subsequent calls are O(1).
      3. Deterministic baseline weights (no trained checkpoint
         published yet).
    """
    try:
        state = models_loader.get_state()
    except Exception:  # pragma: no cover — defensive
        state = None
    if (
        state is None
        or not state.ready
        or CANONICAL_PRECIP_GRU_PATH not in state.predictors
    ):
        return (
            precip_gru.baseline_weights(),
            "weather_precip_gru_baseline",
            "baseline",
        )

    meta = state.predictors.get(CANONICAL_PRECIP_GRU_PATH) or {}
    version = str(meta.get("version", "unknown"))

    cached = models_loader.get_loaded_predictor(CANONICAL_PRECIP_GRU_PATH)
    if cached is not None and isinstance(cached, precip_gru.PrecipGRUWeights):
        return cached, CANONICAL_PRECIP_GRU_PATH, version

    try:
        obj = await models_loader.load_predictor(CANONICAL_PRECIP_GRU_PATH)
    except Exception as e:
        log.warning("[precipitation] gru weight load failed: %s", e)
        return (
            precip_gru.baseline_weights(),
            "weather_precip_gru_baseline",
            "baseline",
        )

    if not isinstance(obj, precip_gru.PrecipGRUWeights):
        log.warning(
            "[precipitation] gru artifact at %s deserialized to %s — "
            "expected PrecipGRUWeights; using baseline",
            CANONICAL_PRECIP_GRU_PATH, type(obj).__name__,
        )
        return (
            precip_gru.baseline_weights(),
            "weather_precip_gru_baseline",
            "baseline",
        )
    return obj, CANONICAL_PRECIP_GRU_PATH, version


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

    sequence = _build_precip_sequence(payload)
    weights, weight_name, weight_version = await _resolve_precip_gru_weights()

    t0 = time.monotonic()
    gru_out = precip_gru.forward(sequence, weights)
    emos_result = precip_emos.apply_precip_emos(
        ensemble=ensemble,
        threshold_in=parsed.threshold_in,
        gru_point_in=gru_out["point_bias_in"],
        gru_spread_in=gru_out["spread_in"],
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
        "gru": {
            "name": weight_name,
            "version": weight_version,
            "point_bias_in": gru_out["point_bias_in"],
            "spread_in": gru_out["spread_in"],
            "sequence_steps": len(sequence),
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
        model_name="weather_precip_gru_emos:rain",
        model_version=weight_version,
        model_type="GRU+EMOS-Gamma",
        elapsed_ms=round(elapsed_ms, 3),
        raw=raw,
    )


__all__ = [
    "CANONICAL_PRECIP_GRU_PATH",
    "PrecipitationPredictionError",
    "predict_precipitation_binary",
]
