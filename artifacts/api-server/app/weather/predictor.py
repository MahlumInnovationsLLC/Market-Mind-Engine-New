"""Public predictor entry point for the temperature vertical (task #246).

Mirrors `app.commodities.predictor` — the cycle dispatches to
`predict_temperature_binary(ticker)` whenever the candidate's vertical
opts in via `VerticalSpec.predictor_hook='weather_temp_gru_emos'`.

Pipeline per call:
  1. Pull the unified weather payload (Open-Meteo + NWS + HRRR +
     climatology) for the ticker's city via
     `app.feeds.weather_payload.build_weather_payload`.
  2. Build a 12-step feature trace from the HRRR hourly samples (with
     current observations + climatology baked into every step) and run
     it through the `gru.forward` pure-numpy GRU.
  3. Hand the GRU's bias + spread to the `emos.apply_emos` NGR layer
     alongside the multi-source ensemble of point forecasts; that
     turns the regression output into a calibrated normal predictive
     distribution and reads off the Kalshi threshold tail.
  4. Pass the YES probability through the per-vertical isotonic
     calibrator (`calibrators.apply`) so retrain.py's drift gate keeps
     working unchanged.

Both the GRU forward and the EMOS apply are pure-Python and add a
single-digit-millisecond per-call cost — the trading loop never
blocks.  Failures degrade to ``None`` (swarm-only signals), exactly
like every other vertical's predictor hook.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from .. import calibrators, models_loader
from ..feeds.weather_payload import build_weather_payload
from ..predictor import Prediction
from . import emos, gru
from .tickers import TemperatureTicker, parse_temperature_ticker


log = logging.getLogger(__name__)


CANONICAL_GRU_PATH = "models/saved/temperature_gru.pkl"


class TemperaturePredictionError(RuntimeError):
    """Raised when we can't produce a probability for a temperature ticker."""


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------


def _ensemble_for_metric(payload: dict[str, Any], metric: str) -> list[float]:
    """Pick the right point-forecast columns from the unified payload
    given the Kalshi metric (HIGH/LOW/TEMP)."""
    metric = metric.upper()
    keys: tuple[str, ...]
    if metric == "HIGH":
        # Tomorrow.io's realtime obs is folded in as a current-temperature
        # anchor for daily-high contracts (helps catch warm-skewed days
        # where HRRR's tmax under-reads the marine-layer-clearing peak).
        keys = (
            "hrrr_tmax_f", "clim_tmax_f", "om_temperature_f",
            "nws_temperature_f", "tio_temperature_f",
        )
    elif metric == "LOW":
        keys = (
            "hrrr_tmin_f", "clim_tmin_f", "om_temperature_f",
            "nws_temperature_f", "tio_temperature_f",
        )
    else:  # "TEMP" — current observation
        keys = (
            "om_temperature_f", "nws_temperature_f", "hrrr_t2m_f",
            "tio_temperature_f", "clim_tavg_f",
        )
    out: list[float] = []
    for k in keys:
        v = payload.get(k)
        if v is None:
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        out.append(fv)
    return out


def _hour_norm(now: datetime) -> float:
    return (now.hour + now.minute / 60.0) / 24.0


def _doy_norm(now: datetime) -> float:
    return (int(now.strftime("%j")) - 1) / 365.0


def _build_sequence(payload: dict[str, Any]) -> list[list[float]]:
    """Produce a (T, 8) sequence for the GRU.

    Trace = HRRR hourly samples (when present) interleaved with the
    current observations; if HRRR isn't available we fall back to a
    single step composed of the current observations + climatology so
    the GRU still runs and contributes a tiny bias estimate.
    """
    now = datetime.now(timezone.utc)
    hour_n = _hour_norm(now)
    doy_n = _doy_norm(now)
    om_now = payload.get("om_temperature_f")
    nws_now = payload.get("nws_temperature_f")
    hrrr_t2m = payload.get("hrrr_t2m_f")
    hrrr_tmax = payload.get("hrrr_tmax_f")
    hrrr_tmin = payload.get("hrrr_tmin_f")
    clim_tmax = payload.get("clim_tmax_f")
    clim_tavg = payload.get("clim_tavg_f")

    base_now = _coalesce(om_now, nws_now, hrrr_t2m, clim_tavg, 50.0)

    # The dashboard payload doesn't surface the HRRR hourly samples,
    # but the predictor's process-local payload (built from
    # weather_hrrr_forecasts.hrrr_hourly) does — try to pull a trace
    # from it when available.  Otherwise build a synthetic 4-step
    # trace anchored on the current observation so the GRU has *some*
    # temporal signal.
    samples = _hrrr_samples(payload)
    if samples:
        seq: list[list[float]] = []
        for s in samples[:12]:
            t_step = _coalesce(s.get("f"), base_now, 50.0)
            seq.append([
                float(t_step),
                _coalesce(hrrr_t2m, t_step, 50.0),
                _coalesce(hrrr_tmax, t_step, 60.0),
                _coalesce(hrrr_tmin, t_step, 40.0),
                _coalesce(clim_tmax, 60.0),
                _coalesce(clim_tavg, 50.0),
                hour_n,
                doy_n,
            ])
        return seq

    # Single-step fallback.
    return [[
        float(base_now),
        _coalesce(hrrr_t2m, base_now, 50.0),
        _coalesce(hrrr_tmax, base_now, 60.0),
        _coalesce(hrrr_tmin, base_now, 40.0),
        _coalesce(clim_tmax, 60.0),
        _coalesce(clim_tavg, 50.0),
        hour_n,
        doy_n,
    ]]


def _hrrr_samples(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull the HRRR hourly samples list if the caller surfaced it."""
    raw = payload.get("hrrr_hourly_samples") or payload.get("_hrrr_samples")
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


async def _resolve_gru_weights() -> tuple[gru.GRUWeights, str, str]:
    """Try the model loader first; fall back to the deterministic
    baseline so cold installs still get a real (calibrated) prediction.

    Resolution order:
      1. Cached in-memory deserialized object (set by a previous call
         or by the boot-time smoke load).
      2. Lazy ``models_loader.load_predictor(CANONICAL_GRU_PATH)`` —
         deserializes the on-disk artifact and registers it in the
         loader's process-wide cache so subsequent calls are O(1).
      3. Deterministic baseline weights (no trained checkpoint
         published yet).
    """
    try:
        state = models_loader.get_state()
    except Exception:  # pragma: no cover — defensive
        state = None
    if state is None or not state.ready or CANONICAL_GRU_PATH not in state.predictors:
        return gru.baseline_weights(), "weather_temp_gru_baseline", "baseline"

    meta = state.predictors.get(CANONICAL_GRU_PATH) or {}
    version = str(meta.get("version", "unknown"))

    cached = models_loader.get_loaded_predictor(CANONICAL_GRU_PATH)
    if cached is not None and isinstance(cached, gru.GRUWeights):
        return cached, CANONICAL_GRU_PATH, version

    try:
        obj = await models_loader.load_predictor(CANONICAL_GRU_PATH)
    except Exception as e:
        log.warning("[temperature] gru weight load failed: %s", e)
        return gru.baseline_weights(), "weather_temp_gru_baseline", "baseline"

    if not isinstance(obj, gru.GRUWeights):
        log.warning(
            "[temperature] gru artifact at %s deserialized to %s — "
            "expected GRUWeights; using baseline",
            CANONICAL_GRU_PATH, type(obj).__name__,
        )
        return gru.baseline_weights(), "weather_temp_gru_baseline", "baseline"
    return obj, CANONICAL_GRU_PATH, version


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def predict_temperature_binary(
    ticker: str | TemperatureTicker,
    *,
    league: str | None = "WEATHER",
    payload_override: dict[str, Any] | None = None,
) -> Prediction:
    """Live prediction for a Kalshi temperature contract.

    ``payload_override`` is a test seam — production callers leave it
    None and let the predictor pull the unified payload itself.
    """
    parsed = (
        ticker if isinstance(ticker, TemperatureTicker)
        else parse_temperature_ticker(ticker)
    )

    if payload_override is not None:
        payload = dict(payload_override)
    else:
        try:
            payload = await build_weather_payload(parsed.city)
        except Exception as e:
            raise TemperaturePredictionError(
                f"weather payload fetch failed for {parsed.city}: "
                f"{type(e).__name__}: {e}"
            ) from e

    ensemble = _ensemble_for_metric(payload, parsed.metric)
    if not ensemble:
        raise TemperaturePredictionError(
            f"no usable forecasts available for {parsed.city}/{parsed.metric}"
        )

    sequence = _build_sequence(payload)
    weights, weight_name, weight_version = await _resolve_gru_weights()

    t0 = time.monotonic()
    gru_out = gru.forward(sequence, weights)
    emos_result = emos.apply_emos(
        ensemble=ensemble,
        threshold_f=parsed.threshold,
        gru_point_f=gru_out["point_bias_f"],
        gru_spread_f=gru_out["spread_f"],
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
            "metric": parsed.metric,
            "city": parsed.city,
            "threshold": parsed.threshold,
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
            "point_bias_f": gru_out["point_bias_f"],
            "spread_f": gru_out["spread_f"],
            "sequence_steps": len(sequence),
        },
        "emos": {
            "mu": emos_result.mu,
            "sigma": emos_result.sigma,
            "p_above": emos_result.p_above,
            "p_below": emos_result.p_below,
        },
        "calibration": {**cal_info, **cal_extra},
    }

    confidence = abs(2.0 * float(probability) - 1.0)
    return Prediction(
        probability=float(probability),
        confidence=float(confidence),
        model_name=f"weather_temp_gru_emos:{parsed.metric.lower()}",
        model_version=weight_version,
        model_type="GRU+EMOS",
        elapsed_ms=round(elapsed_ms, 3),
        raw=raw,
    )


__all__ = [
    "CANONICAL_GRU_PATH",
    "TemperaturePredictionError",
    "predict_temperature_binary",
]
