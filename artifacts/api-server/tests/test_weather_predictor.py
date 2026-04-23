"""Tests for the temperature GRU + EMOS predictor (task #246)."""
from __future__ import annotations

import asyncio
import math
import random

import pytest

from app import calibrators, verticals
from app.weather import emos, gru, predictor as weather_predictor
from app.weather.tickers import (
    InvalidTemperatureTicker,
    SUPPORTED_CITIES,
    parse_temperature_ticker,
)


# ---------------------------------------------------------------------------
# Vertical wiring
# ---------------------------------------------------------------------------


def test_weather_vertical_uses_gru_emos_hook():
    spec = verticals.require_spec("weather")
    assert spec.predictor_hook == "weather_temp_gru_emos"


# ---------------------------------------------------------------------------
# Ticker parser
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,city,metric,k,direction", [
    ("KXHIGHNYC-26APR15-T75", "NYC", "HIGH", 75.0, "above"),
    ("KXTEMPLAX-26MAY-B60", "LAX", "TEMP", 60.0, "below"),
    ("KXLOWCHI-26JAN20-T20", "CHI", "LOW", 20.0, "above"),
    ("kxhighmia-26jul04-T95", "MIA", "HIGH", 95.0, "above"),
    ("KXTEMPNY-26APR15-T55", "NYC", "TEMP", 55.0, "above"),  # 2-letter alias
])
def test_parse_temperature_ticker(raw, city, metric, k, direction):
    t = parse_temperature_ticker(raw)
    assert t.city == city
    assert t.metric == metric
    assert t.threshold == pytest.approx(k)
    assert t.direction == direction


def test_parse_rejects_unsupported_city():
    with pytest.raises(InvalidTemperatureTicker):
        parse_temperature_ticker("KXHIGHZZZ-26APR15-T75")


def test_parse_rejects_garbage():
    with pytest.raises(InvalidTemperatureTicker):
        parse_temperature_ticker("not-a-ticker")
    with pytest.raises(InvalidTemperatureTicker):
        parse_temperature_ticker("KXRAINNYC-26APR15-T0.5")  # KXRAIN isn't temp


def test_parse_rejects_unknown_threshold_prefix():
    """Only T (above) and B (below) are valid threshold prefixes —
    silently treating an unknown letter (e.g. 'A75') as 'above' would
    mask format drift the moment Kalshi adds a new contract type."""
    with pytest.raises(InvalidTemperatureTicker):
        parse_temperature_ticker("KXHIGHNYC-26APR15-A75")
    with pytest.raises(InvalidTemperatureTicker):
        parse_temperature_ticker("KXHIGHNYC-26APR15-X75")


def test_supported_cities_match_climatology():
    from app.feeds import weather_climatology as clim
    assert set(SUPPORTED_CITIES) == set(clim.supported_cities())


# ---------------------------------------------------------------------------
# GRU forward
# ---------------------------------------------------------------------------


def test_gru_forward_shapes_and_clamps():
    w = gru.baseline_weights(n_input=8, n_hidden=6)
    seq = [[60.0, 62.0, 70.0, 50.0, 72.0, 60.0, 0.5, 0.3]] * 4
    out = gru.forward(seq, w)
    assert -5.0 <= out["point_bias_f"] <= 5.0
    assert 0.5 <= out["spread_f"] <= 12.0
    assert len(out["h"]) == 6


def test_gru_forward_handles_empty_sequence():
    out = gru.forward([], gru.baseline_weights())
    assert out["point_bias_f"] == 0.0
    assert out["spread_f"] == pytest.approx(2.5)
    assert out["h"] == []


def test_gru_forward_pads_short_steps():
    w = gru.baseline_weights(n_input=8, n_hidden=4)
    out = gru.forward([[60.0, 62.0, 70.0]], w)
    assert len(out["h"]) == 4
    assert math.isfinite(out["point_bias_f"])
    assert math.isfinite(out["spread_f"])


# ---------------------------------------------------------------------------
# EMOS post-processor
# ---------------------------------------------------------------------------


def test_emos_above_threshold_is_high_when_mean_above():
    res = emos.apply_emos(
        ensemble=[78.0, 80.0, 79.0],
        threshold_f=70.0,
        gru_point_f=0.0,
        gru_spread_f=2.0,
    )
    assert res.p_above > 0.95
    assert res.p_below < 0.05
    assert res.probability_for("above") == res.p_above
    assert res.probability_for("below") == res.p_below


def test_emos_around_threshold_is_near_half():
    res = emos.apply_emos(
        ensemble=[70.0, 70.0, 70.0],
        threshold_f=70.0,
        gru_point_f=0.0,
        gru_spread_f=2.0,
    )
    assert 0.45 <= res.p_above <= 0.55


def test_emos_handles_empty_ensemble_safely():
    res = emos.apply_emos(
        ensemble=[],
        threshold_f=70.0,
    )
    # Falls back to a flat 0.5 with a wide cushion.
    assert res.p_above == 0.5


def test_emos_never_saturates_to_zero_or_one():
    res = emos.apply_emos(
        ensemble=[200.0, 210.0, 220.0],
        threshold_f=0.0,
    )
    assert 0.0 < res.p_below < 1.0
    assert 0.0 < res.p_above < 1.0


# ---------------------------------------------------------------------------
# End-to-end predictor (with a payload override)
# ---------------------------------------------------------------------------


def _payload(**over):
    base = {
        "om_temperature_f": 78.0,
        "nws_temperature_f": 77.5,
        "tio_temperature_f": 78.4,  # tomorrow.io realtime obs
        "hrrr_t2m_f": 78.2,
        "hrrr_tmax_f": 84.0,
        "hrrr_tmin_f": 65.0,
        "clim_tmax_f": 80.0,
        "clim_tavg_f": 73.0,
    }
    base.update(over)
    return base


def test_ensemble_uses_all_three_independent_sources():
    """Task #246 acceptance: GRU+EMOS must consume the multi-source
    feed (Open-Meteo + NWS + HRRR + tomorrow.io + climatology) — not
    the single-source baseline."""
    from app.weather.predictor import _ensemble_for_metric
    p = _payload()
    high = _ensemble_for_metric(p, "HIGH")
    low = _ensemble_for_metric(p, "LOW")
    temp = _ensemble_for_metric(p, "TEMP")
    # tomorrow.io must be folded into all three metrics.
    assert 78.4 in high
    assert 78.4 in low
    assert 78.4 in temp
    # All three independent realtime sources are present in TEMP.
    assert 78.0 in temp and 77.5 in temp and 78.4 in temp
    # Drop tomorrow.io — predictor should still produce an ensemble
    # (graceful degradation), just smaller.
    p2 = _payload()
    p2.pop("tio_temperature_f")
    assert 78.4 not in _ensemble_for_metric(p2, "HIGH")


def test_predict_temperature_binary_above_threshold():
    pred = asyncio.run(weather_predictor.predict_temperature_binary(
        "KXHIGHNYC-26JUL15-T75",
        payload_override=_payload(),
    ))
    assert pred.model_type == "GRU+EMOS"
    assert pred.model_name.startswith("weather_temp_gru_emos")
    assert pred.probability > 0.7
    raw = pred.raw
    assert raw["emos"]["mu"] > 75.0
    assert raw["gru"]["sequence_steps"] >= 1
    assert raw["ensemble"]["n"] >= 2


def test_predict_temperature_binary_below_threshold_inverts():
    pred_above = asyncio.run(weather_predictor.predict_temperature_binary(
        "KXHIGHNYC-26JUL15-T90", payload_override=_payload(),
    ))
    pred_below = asyncio.run(weather_predictor.predict_temperature_binary(
        "KXHIGHNYC-26JUL15-B90", payload_override=_payload(),
    ))
    # P(above 90) + P(below 90) ≈ 1.
    assert pred_above.probability + pred_below.probability == pytest.approx(1.0, abs=1e-3)


def test_predict_temperature_binary_low_metric_uses_tmin():
    pred = asyncio.run(weather_predictor.predict_temperature_binary(
        "KXLOWCHI-26JAN15-T10",
        payload_override={
            "hrrr_tmin_f": 5.0,
            "clim_tmin_f": 8.0,
            "om_temperature_f": 6.0,
        },
    ))
    # Forecast low ~6°F → P(low > 10°F) should be small.
    assert pred.probability < 0.25


def test_predict_temperature_binary_drives_real_payload_with_hrrr_sequence(
    monkeypatch,
):
    """End-to-end through `build_weather_payload` (the production
    path — no `payload_override` seam) must surface a *multi-step*
    HRRR hourly sequence into the GRU, not the single-step fallback.

    We monkeypatch `db.fetchrow` / `clim.get_climatology` so we don't
    need a real Postgres, but every other layer (payload assembly,
    JSONB decode, `_build_sequence`, GRU forward, EMOS apply,
    calibrator) runs unmodified — proving the GRU sees a true
    sequence in production.
    """
    from datetime import datetime, timezone
    from app import db
    from app.feeds import weather_climatology as clim
    from app.feeds import weather_payload as wp

    now = datetime.now(timezone.utc)
    samples = [
        {"t": f"2026-07-15T{h:02d}:00:00Z", "f": 70.0 + h * 0.6}
        for h in range(0, 24)
    ]
    hrrr_row = {
        "hrrr_high_f": 86.5,
        "hrrr_hourly": {
            "t2m_f": 81.0,
            "tmin_f": 66.0,
            "samples": samples,
            "nomads": {"cycle": "12z", "inventory_count": 38},
        },
        "hrrr_run_time": now,
        "fetched_at": now,
    }
    om_row = {"temperature_f": 80.0, "observed_at": now}
    nws_row = {"temperature_f": 79.5, "observed_at": now}
    tio_row = {"temperature_f": 80.7, "observed_at": now}

    async def fake_fetchrow(query, *args, **kwargs):
        q = " ".join(query.split())
        if "weather_hrrr_forecasts" in q:
            return hrrr_row
        if "weather_observations" in q:
            src = args[0] if args else None
            # the SQL embeds `source = '...'` literally, so route by
            # which substring is present in the query
            if "open-meteo" in q:
                return om_row
            if "'nws'" in q:
                return nws_row
            if "tomorrow.io" in q:
                return tio_row
            return None
        if "weather_extended" in q or "weather_minutecast" in q:
            return None
        return None

    async def fake_clim(city, doy, metric):
        return {"tmax": 85.0, "tmin": 65.0, "tavg": 75.0}.get(metric)

    monkeypatch.setattr(db, "fetchrow", fake_fetchrow)
    monkeypatch.setattr(clim, "get_climatology", fake_clim)
    # Predictor imports `build_weather_payload` directly; patch the
    # symbol it bound (not the source module) so the override sticks.
    monkeypatch.setattr(
        weather_predictor, "build_weather_payload", wp.build_weather_payload
    )

    pred = asyncio.run(weather_predictor.predict_temperature_binary(
        "KXHIGHNYC-26JUL15-T80",
    ))
    raw = pred.raw
    # The whole point of this test: the GRU must see a real sequence.
    assert raw["gru"]["sequence_steps"] >= 12, (
        f"GRU only got {raw['gru']['sequence_steps']} steps — expected "
        "the full HRRR hourly trace via build_weather_payload"
    )
    # The ensemble should have folded in all four real point sources
    # (HRRR tmax + climatology tmax + Open-Meteo + NWS + tomorrow.io).
    assert raw["ensemble"]["n"] >= 4
    # And EMOS should have produced a non-degenerate probability.
    assert 0.0 < pred.probability < 1.0
    assert pred.model_type == "GRU+EMOS"


def test_predict_temperature_binary_raises_when_no_forecasts():
    with pytest.raises(weather_predictor.TemperaturePredictionError):
        asyncio.run(weather_predictor.predict_temperature_binary(
            "KXHIGHNYC-26JUL15-T75",
            payload_override={},
        ))


# ---------------------------------------------------------------------------
# Held-out Brier: GRU+EMOS vs simple-regression baseline
# ---------------------------------------------------------------------------


def _baseline_probability(forecast_f: float, threshold_f: float) -> float:
    """Naive baseline: hard 0/1 from a single point forecast.

    This mirrors the legacy "simple regression" behaviour for the
    weather vertical — point estimate, no spread, threshold compared
    directly.  Clamped lightly so Brier doesn't go to 1 on every
    miss (matches what the old generic meta-model produced after the
    [0, 1] clamp in `predictor._run_predict`).
    """
    if forecast_f >= threshold_f:
        return 0.95
    return 0.05


def _brier(probs: list[float], outcomes: list[int]) -> float:
    n = len(probs)
    return sum((p - y) ** 2 for p, y in zip(probs, outcomes)) / n


def test_gru_emos_beats_baseline_brier_on_holdout():
    """Materially-better Brier on a held-out window of synthetic
    temperature forecasts.

    The synthetic ground-truth model:
      - true daily-high T ~ N(climatology, 8°F)
      - the ensemble of source forecasts is unbiased but noisy
      - the threshold is drawn near the climatological mean so the
        contract is non-trivial (mean P(yes) ~ 0.5)

    Under those conditions the EMOS-calibrated normal CDF carries the
    forecast spread into the probability, while the baseline hard
    threshold collapses every forecast to {0.05, 0.95}.  The Brier of
    the calibrated forecast must be measurably lower across ≥200 held-out
    days; we lock in a ≥10% improvement (current measured ≈ 15%) so the
    test isn't brittle to RNG drift while still failing the moment the
    GRU+EMOS pipeline regresses to baseline-or-worse.
    """
    rng = random.Random(20260423)
    n_days = 240
    baseline_probs: list[float] = []
    gru_probs: list[float] = []
    outcomes: list[int] = []

    clim = 70.0
    true_sigma = 8.0
    src_sigma = 4.0
    for _ in range(n_days):
        true_t = rng.gauss(clim, true_sigma)
        # Threshold near the climatological mean so the contract is
        # actually a coin-flip — this is where calibration matters.
        threshold = clim + rng.gauss(0.0, 4.0)
        sources = [rng.gauss(true_t, src_sigma) for _ in range(4)]
        ensemble_mean = sum(sources) / len(sources)

        # Baseline = single point-forecast hard threshold.
        baseline_probs.append(_baseline_probability(ensemble_mean, threshold))

        # GRU + EMOS path.
        seq_step = [
            ensemble_mean, ensemble_mean, ensemble_mean + 4.0,
            ensemble_mean - 4.0, clim + 8.0, clim,
            0.5, 0.5,
        ]
        out = gru.forward([seq_step] * 4, gru.baseline_weights())
        em = emos.apply_emos(
            ensemble=sources,
            threshold_f=threshold,
            gru_point_f=out["point_bias_f"],
            gru_spread_f=out["spread_f"],
        )
        gru_probs.append(em.p_above)
        outcomes.append(1 if true_t > threshold else 0)

    baseline_brier = _brier(baseline_probs, outcomes)
    gru_brier = _brier(gru_probs, outcomes)
    # Sanity: both Briers are in [0, 1] and our outcomes aren't degenerate.
    assert 0.0 < baseline_brier < 1.0
    assert 0.0 < gru_brier < 1.0
    assert 0.2 < sum(outcomes) / n_days < 0.8

    improvement = (baseline_brier - gru_brier) / baseline_brier
    assert improvement >= 0.10, (
        f"GRU+EMOS Brier {gru_brier:.4f} did not measurably beat "
        f"baseline {baseline_brier:.4f} (improvement={improvement:.2%})"
    )


# ---------------------------------------------------------------------------
# Calibrator interaction
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# GRU artifact loader integration (real `models_loader.load_predictor`)
# ---------------------------------------------------------------------------


def test_resolve_gru_weights_falls_back_when_loader_not_ready(monkeypatch):
    """Cold installs (no model archive yet) must still get a real
    calibrated prediction from the deterministic baseline weights."""
    from app import models_loader as ml
    from app.weather.predictor import _resolve_gru_weights

    fake_state = ml.ModelsState()
    fake_state.ready = False
    monkeypatch.setattr(ml, "get_state", lambda: fake_state)
    weights, name, version = asyncio.run(_resolve_gru_weights())
    assert isinstance(weights, gru.GRUWeights)
    assert name == "weather_temp_gru_baseline"
    assert version == "baseline"


def test_resolve_gru_weights_lazy_loads_canonical_artifact(monkeypatch):
    """When the loader is ready and the canonical artifact is in the
    catalog, `_resolve_gru_weights` must call `load_predictor` and
    return the deserialized GRUWeights — not silently fall back to
    baseline (the failure mode the code review caught)."""
    from app import models_loader as ml
    from app.weather import predictor as wp

    trained = gru.baseline_weights(n_input=8, n_hidden=6)
    # Mark it visibly different from baseline so we can assert we
    # actually got the trained one back (mutate the output bias).
    trained.b_out = [42.0, 1.0]

    fake_state = ml.ModelsState()
    fake_state.ready = True
    fake_state.predictors = {
        wp.CANONICAL_GRU_PATH: {
            "size": 4096, "mtime": 0.0,
            "sha256": "f" * 64, "version": "abc123def456",
        }
    }
    monkeypatch.setattr(ml, "get_state", lambda: fake_state)
    monkeypatch.setattr(ml, "get_loaded_predictor", lambda name: None)

    load_calls: list[str] = []

    async def fake_load(name):
        load_calls.append(name)
        return trained

    monkeypatch.setattr(ml, "load_predictor", fake_load)
    weights, name, version = asyncio.run(wp._resolve_gru_weights())
    assert load_calls == [wp.CANONICAL_GRU_PATH], (
        "_resolve_gru_weights must lazily call load_predictor when the "
        "trained checkpoint is in the catalog but not yet cached"
    )
    assert weights is trained
    assert name == wp.CANONICAL_GRU_PATH
    assert version == "abc123def456"


def test_resolve_gru_weights_uses_in_memory_cache(monkeypatch):
    """If the artifact has already been deserialized (e.g. by the
    boot-time smoke load), we must reuse the cached object instead of
    reloading from disk on every prediction."""
    from app import models_loader as ml
    from app.weather import predictor as wp

    trained = gru.baseline_weights(n_input=8, n_hidden=6)
    fake_state = ml.ModelsState()
    fake_state.ready = True
    fake_state.predictors = {
        wp.CANONICAL_GRU_PATH: {"sha256": "a" * 64, "version": "v1"}
    }
    monkeypatch.setattr(ml, "get_state", lambda: fake_state)
    monkeypatch.setattr(
        ml, "get_loaded_predictor",
        lambda name: trained if name == wp.CANONICAL_GRU_PATH else None,
    )

    async def fail_load(name):  # pragma: no cover
        raise AssertionError("load_predictor should not be called when cached")

    monkeypatch.setattr(ml, "load_predictor", fail_load)
    weights, name, version = asyncio.run(wp._resolve_gru_weights())
    assert weights is trained


def test_resolve_gru_weights_falls_back_when_artifact_wrong_type(monkeypatch):
    """If the on-disk artifact deserializes to something other than a
    GRUWeights (e.g. an old sklearn pipeline left over from the
    baseline regression), we must fall back to baseline rather than
    crash mid-prediction."""
    from app import models_loader as ml
    from app.weather import predictor as wp

    fake_state = ml.ModelsState()
    fake_state.ready = True
    fake_state.predictors = {
        wp.CANONICAL_GRU_PATH: {"sha256": "b" * 64, "version": "v1"}
    }
    monkeypatch.setattr(ml, "get_state", lambda: fake_state)
    monkeypatch.setattr(ml, "get_loaded_predictor", lambda name: None)

    async def load_wrong_type(name):
        return {"not_a_gru": True}  # legacy sklearn-ish artifact

    monkeypatch.setattr(ml, "load_predictor", load_wrong_type)
    weights, name, version = asyncio.run(wp._resolve_gru_weights())
    assert isinstance(weights, gru.GRUWeights)
    assert name == "weather_temp_gru_baseline"


def test_canonical_gru_path_is_in_models_loader_canonical_set():
    """The trained GRU artifact must be in
    `models_loader.CANONICAL_PREDICTORS` so the boot-time smoke
    loader populates `_loaded_objects` for it (per code review)."""
    from app import models_loader as ml
    from app.weather.predictor import CANONICAL_GRU_PATH
    assert CANONICAL_GRU_PATH in ml.CANONICAL_PREDICTORS


# ---------------------------------------------------------------------------
# Calibration-snapshot wiring (predictions must flow into the per-vertical
# isotonic retrain → calibration_snapshots loop)
# ---------------------------------------------------------------------------


def test_prediction_payload_is_buffer_compatible():
    """The trade buffer / online-learning loop reads `predicted_prob`
    from the prediction's `as_dict` payload (`probability`).
    `app/settlement/retrain.py` then aggregates buffered rows by
    vertical='weather' and writes a fresh `calibration_snapshots`
    row.  This test guards the field shape so weather forecasts
    actually surface in the calibration snapshot pipeline."""
    pred = asyncio.run(weather_predictor.predict_temperature_binary(
        "KXHIGHNYC-26JUL15-T75",
        payload_override=_payload(),
    ))
    payload = pred.as_dict()
    # Fields read by the buffer / retrain pipeline:
    assert isinstance(payload["probability"], float)
    assert 0.0 < payload["probability"] < 1.0
    assert payload["model_name"].startswith("weather_temp_gru_emos")
    assert payload["model_type"] == "GRU+EMOS"
    # Raw payload must include the ensemble + emos detail the
    # retrain Brier comparison reads when computing post-vs-pre lift.
    assert "ensemble" in payload["raw"]
    assert "emos" in payload["raw"]


def test_predictor_runs_through_per_vertical_calibrator(monkeypatch):
    seen: dict = {}

    def fake_apply(p, league):
        seen["called_with"] = (float(p), league)
        return float(p), {"applied": True, "stub": True}

    monkeypatch.setattr(calibrators, "apply", fake_apply)
    monkeypatch.setattr(weather_predictor.calibrators, "apply", fake_apply)
    pred = asyncio.run(weather_predictor.predict_temperature_binary(
        "KXHIGHNYC-26JUL15-T75",
        payload_override=_payload(),
    ))
    assert seen["called_with"][1] == "WEATHER"
    assert pred.raw["calibration"]["applied"] is True
    assert "uncalibrated_probability" in pred.raw["calibration"]
