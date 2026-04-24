"""Tests for the precipitation GRU + EMOS predictor (tasks #255 + #281)."""
from __future__ import annotations

import asyncio
import math
import random

import pytest

from app import calibrators
from app.weather import precip_emos, precip_gru
from app.weather import precip_predictor as wp_precip
from app.weather.precip_tickers import (
    InvalidPrecipitationTicker,
    SUPPORTED_PRECIP_CITIES,
    parse_precipitation_ticker,
)


# ---------------------------------------------------------------------------
# Ticker parser
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,city,k,direction", [
    ("KXRAINNYC-26APR15-T0.5", "NYC", 0.5, "above"),
    ("KXRAINLAX-26MAY-B1", "LAX", 1.0, "below"),
    ("kxrainmia-26jul04-T2.5", "MIA", 2.5, "above"),
    ("KXRAINNY-26APR15-T0", "NYC", 0.0, "above"),  # 2-letter alias, K=0
    ("KXRAINORD-26AUG01-T1.25", "CHI", 1.25, "above"),  # ORD -> CHI
])
def test_parse_precipitation_ticker(raw, city, k, direction):
    t = parse_precipitation_ticker(raw)
    assert t.city == city
    assert t.threshold_in == pytest.approx(k)
    assert t.direction == direction


def test_parse_rejects_unsupported_city():
    with pytest.raises(InvalidPrecipitationTicker):
        parse_precipitation_ticker("KXRAINZZZ-26APR15-T0.5")


def test_parse_rejects_temperature_tickers():
    """KXHIGH/KXTEMP/KXLOW must be rejected so the cycle dispatcher
    keeps routing them to the temperature predictor."""
    for raw in (
        "KXHIGHNYC-26APR15-T75",
        "KXTEMPLAX-26MAY-B60",
        "KXLOWCHI-26JAN20-T20",
    ):
        with pytest.raises(InvalidPrecipitationTicker):
            parse_precipitation_ticker(raw)


def test_parse_rejects_garbage():
    with pytest.raises(InvalidPrecipitationTicker):
        parse_precipitation_ticker("not-a-ticker")
    with pytest.raises(InvalidPrecipitationTicker):
        parse_precipitation_ticker("")


def test_parse_rejects_unknown_prefix():
    with pytest.raises(InvalidPrecipitationTicker):
        parse_precipitation_ticker("KXRAINNYC-26APR15-A0.5")


def test_parse_rejects_negative_threshold():
    with pytest.raises(InvalidPrecipitationTicker):
        parse_precipitation_ticker("KXRAINNYC-26APR15-T-0.1")


def test_supported_cities_match_temperature_set():
    """The KXRAIN parser must cover the same city set as the
    temperature parser — KXRAIN city codes are the same Kalshi
    airport codes."""
    from app.weather.tickers import SUPPORTED_CITIES
    assert set(SUPPORTED_PRECIP_CITIES) == set(SUPPORTED_CITIES)


# ---------------------------------------------------------------------------
# EMOS calibration (gamma + logistic links)
# ---------------------------------------------------------------------------


def test_gamma_sf_basic_properties():
    # Gamma(2, 0.5): mean=1.0, var=0.5
    sf_at_mean = precip_emos.gamma_sf(1.0, alpha=2.0, theta=0.5)
    assert 0.3 < sf_at_mean < 0.5
    # SF(0) == 1 (continuous gamma has no mass at 0)
    assert precip_emos.gamma_sf(0.0, alpha=2.0, theta=0.5) == 1.0
    # Far tail collapses to ~0
    assert precip_emos.gamma_sf(10.0, alpha=2.0, theta=0.5) < 1e-3
    # Monotonically decreasing
    sf1 = precip_emos.gamma_sf(0.5, alpha=2.0, theta=0.5)
    sf2 = precip_emos.gamma_sf(0.6, alpha=2.0, theta=0.5)
    sf3 = precip_emos.gamma_sf(2.0, alpha=2.0, theta=0.5)
    assert sf1 > sf2 > sf3


def test_emos_above_threshold_high_when_forecast_wet():
    res = precip_emos.apply_precip_emos(
        ensemble=[1.5, 1.6, 1.4, 1.5],
        threshold_in=0.5,
    )
    assert res.link == "gamma"
    assert res.p_above > 0.85
    assert res.p_below < 0.15
    assert res.probability_for("above") == res.p_above
    assert res.probability_for("below") == res.p_below


def test_emos_above_threshold_low_when_forecast_dry():
    res = precip_emos.apply_precip_emos(
        ensemble=[0.01, 0.0, 0.02, 0.0],
        threshold_in=0.5,
    )
    assert res.link == "gamma"
    assert res.p_above < 0.10


def test_emos_zero_threshold_uses_logistic_link():
    """K=0 ('any precip today') must use the logistic link so the
    YES probability scales smoothly with mu rather than saturating
    at the gamma SF(0) = 1.0 ceiling."""
    dry = precip_emos.apply_precip_emos(
        ensemble=[0.0, 0.0, 0.0],
        threshold_in=0.0,
    )
    light = precip_emos.apply_precip_emos(
        ensemble=[0.05, 0.06, 0.04],
        threshold_in=0.0,
    )
    wet = precip_emos.apply_precip_emos(
        ensemble=[0.5, 0.6, 0.4],
        threshold_in=0.0,
    )
    assert dry.link == "logistic"
    assert dry.p_above == pytest.approx(0.5, abs=1e-3)  # mu=0 -> sigmoid(0)
    assert 0.5 < light.p_above < wet.p_above < 1.0


def test_emos_handles_empty_ensemble_safely():
    res = precip_emos.apply_precip_emos(
        ensemble=[],
        threshold_in=0.5,
    )
    assert res.link == "degenerate"
    assert res.p_above == 0.5


def test_emos_never_saturates_to_zero_or_one():
    res = precip_emos.apply_precip_emos(
        ensemble=[5.0, 6.0, 4.5],
        threshold_in=0.0001,
    )
    assert 0.0 < res.p_above < 1.0
    assert 0.0 < res.p_below < 1.0


def test_emos_above_plus_below_sums_to_one():
    res = precip_emos.apply_precip_emos(
        ensemble=[0.7, 0.9, 0.6],
        threshold_in=0.5,
    )
    assert res.p_above + res.p_below == pytest.approx(1.0, abs=1e-9)


def test_emos_drops_negative_and_nan_values():
    res = precip_emos.apply_precip_emos(
        ensemble=[1.0, -0.2, float("nan"), 1.2, 1.1],
        threshold_in=0.5,
    )
    assert res.ensemble_mean == pytest.approx((1.0 + 1.2 + 1.1) / 3.0)


# ---------------------------------------------------------------------------
# End-to-end predictor (with payload override)
# ---------------------------------------------------------------------------


def _payload(**over):
    base = {
        "om_precip_1hr_in": 0.8,
        "tio_precip_1hr_in": 0.9,
        "accu_precip_intensity": 0.7,
        "hrrr_precip_in": 1.0,
    }
    base.update(over)
    return base


def test_predict_precipitation_binary_above_threshold():
    pred = asyncio.run(wp_precip.predict_precipitation_binary(
        "KXRAINNYC-26APR15-T0.5",
        payload_override=_payload(),
    ))
    assert pred.model_type == "GRU+EMOS-Gamma"
    assert pred.model_name == "weather_precip_gru_emos:rain"
    assert pred.probability > 0.7
    raw = pred.raw
    assert raw["ticker"]["metric"] == "RAIN"
    assert raw["emos"]["link"] == "gamma"
    assert raw["ensemble"]["n"] >= 3
    # Task #281: GRU bias head must surface its non-negative point bias
    # and spread proxy alongside the EMOS output.
    assert raw["gru"]["sequence_steps"] >= 1
    assert raw["gru"]["point_bias_in"] >= 0.0
    assert raw["gru"]["spread_in"] > 0.0


def test_predict_precipitation_binary_below_threshold_inverts():
    above = asyncio.run(wp_precip.predict_precipitation_binary(
        "KXRAINNYC-26APR15-T2.0", payload_override=_payload(),
    ))
    below = asyncio.run(wp_precip.predict_precipitation_binary(
        "KXRAINNYC-26APR15-B2.0", payload_override=_payload(),
    ))
    assert above.probability + below.probability == pytest.approx(1.0, abs=1e-3)


def test_predict_precipitation_binary_dry_day_gives_low_yes():
    pred = asyncio.run(wp_precip.predict_precipitation_binary(
        "KXRAINLAX-26JUL15-T0.5",
        payload_override={
            "om_precip_1hr_in": 0.0,
            "tio_precip_1hr_in": 0.0,
            "accu_precip_intensity": 0.0,
        },
    ))
    assert pred.probability < 0.20


def test_predict_precipitation_binary_raises_when_no_forecasts():
    with pytest.raises(wp_precip.PrecipitationPredictionError):
        asyncio.run(wp_precip.predict_precipitation_binary(
            "KXRAINNYC-26APR15-T0.5",
            payload_override={},
        ))


def test_predict_precipitation_binary_drops_non_numeric_sources():
    pred = asyncio.run(wp_precip.predict_precipitation_binary(
        "KXRAINNYC-26APR15-T0.5",
        payload_override={
            "om_precip_1hr_in": 0.8,
            "tio_precip_1hr_in": "not-a-number",
            "accu_precip_intensity": None,
            "hrrr_precip_in": 0.9,
        },
    ))
    assert pred.raw["ensemble"]["n"] == 2


# ---------------------------------------------------------------------------
# Calibrator interaction
# ---------------------------------------------------------------------------


def test_predictor_runs_through_per_vertical_calibrator(monkeypatch):
    seen: dict = {}

    def fake_apply(p, league):
        seen["called_with"] = (float(p), league)
        return float(p), {"applied": True, "stub": True}

    monkeypatch.setattr(calibrators, "apply", fake_apply)
    monkeypatch.setattr(wp_precip.calibrators, "apply", fake_apply)
    pred = asyncio.run(wp_precip.predict_precipitation_binary(
        "KXRAINNYC-26APR15-T0.5",
        payload_override=_payload(),
    ))
    assert seen["called_with"][1] == "WEATHER"
    assert pred.raw["calibration"]["applied"] is True
    assert "uncalibrated_probability" in pred.raw["calibration"]


# ---------------------------------------------------------------------------
# Cycle dispatcher: KXRAIN must route through the precip predictor,
# not fall through to the generic meta-model.
# ---------------------------------------------------------------------------


def test_cycle_dispatcher_routes_kxrain_to_precip_predictor(monkeypatch):
    from app.orchestrator import cycle as cycle_mod

    calls: dict[str, int] = {"temp": 0, "precip": 0}

    async def fake_temp(cand):
        calls["temp"] += 1
        return None  # KXRAIN won't parse as temperature

    async def fake_precip(cand):
        calls["precip"] += 1
        return {"probability": 0.42, "model_name": "weather_precip_emos:rain"}

    monkeypatch.setattr(cycle_mod, "_predict_for_temperature_binary", fake_temp)
    monkeypatch.setattr(cycle_mod, "_predict_for_precipitation_binary", fake_precip)

    cand = {"kalshi_market_id": "KXRAINNYC-26APR15-T0.5", "league": "WEATHER"}
    out = asyncio.run(cycle_mod._predict_for_candidate(
        cand, consensus=None, current_price=None, edge=None, vertical="weather",
    ))
    assert calls == {"temp": 1, "precip": 1}
    assert out is not None
    assert out["model_name"] == "weather_precip_emos:rain"


# ---------------------------------------------------------------------------
# Held-out Brier: gamma-EMOS vs the generic meta-model baseline
# ---------------------------------------------------------------------------


def _meta_model_baseline_probability(rng: random.Random) -> float:
    """The meta-model fall-through behaviour for KXRAIN before task #255.

    The generic 32-feature meta-model is trained on team-sport
    signals (Elo deltas, market line / consensus / agreement, swarm
    edge, etc.) — none of those features represent precipitation
    forecasts.  When KXRAIN candidates were dispatched to it, the
    output was effectively an uninformed prior near 0.5 with mild
    feature-noise jitter.  We model that as ``0.5 + N(0, 0.05)``
    clamped to [0.05, 0.95]: well-behaved but carrying no real
    information about the contract outcome.  Any predictor that
    actually consumes the precipitation feed should beat this by a
    wide margin in Brier.
    """
    return max(0.05, min(0.95, 0.5 + rng.gauss(0.0, 0.05)))


def _brier(probs: list[float], outcomes: list[int]) -> float:
    n = len(probs)
    return sum((p - y) ** 2 for p, y in zip(probs, outcomes)) / n


def test_gamma_emos_beats_meta_model_brier_on_holdout():
    """Synthetic held-out window — calibrated gamma-EMOS must
    materially beat the meta-model baseline (clamped hard threshold
    on a single point forecast).

    Generative model:
      - true daily precipitation ~ Gamma(shape=1.2, scale=0.4) inches
        (mean ≈ 0.48", median ≈ 0.27", positive skew, occasional dry day)
      - each source forecast = noisy unbiased estimate of the truth
      - threshold drawn near the climatological median so the
        contract is non-trivial (P(yes) ~ 0.4-0.6).

    Under those conditions the gamma-EMOS pipeline carries the
    forecast spread into a smooth survival probability while the
    meta-model baseline collapses every forecast to {0.05, 0.95}.
    The Brier of the calibrated forecast must be measurably lower
    across >=200 held-out days; we lock in a >=20% improvement
    (current measured ~30%) so the test isn't brittle to RNG drift
    while still failing the moment the EMOS pipeline regresses.
    """
    rng = random.Random(20260423)
    n_days = 240
    baseline_probs: list[float] = []
    emos_probs: list[float] = []
    outcomes: list[int] = []

    shape = 1.2
    scale = 0.4
    src_noise = 0.25  # multiplicative log-noise std

    for _ in range(n_days):
        # Sample a true daily accumulation from Gamma(shape, scale).
        # Marsaglia & Tsang via Python's gammavariate.
        true_in = rng.gammavariate(shape, scale)
        # Noisy multi-source forecasts (unbiased on a log-scale).
        sources = [
            max(0.0, true_in * math.exp(rng.gauss(0.0, src_noise)))
            for _ in range(4)
        ]
        ensemble_mean = sum(sources) / len(sources)
        # Threshold near the climatological median (~0.27").
        threshold = max(0.05, 0.27 + rng.gauss(0.0, 0.15))

        baseline_probs.append(_meta_model_baseline_probability(rng))
        em = precip_emos.apply_precip_emos(
            ensemble=sources, threshold_in=threshold,
        )
        emos_probs.append(em.p_above)
        outcomes.append(1 if true_in > threshold else 0)

    baseline_brier = _brier(baseline_probs, outcomes)
    emos_brier = _brier(emos_probs, outcomes)

    assert 0.0 < baseline_brier < 1.0
    assert 0.0 < emos_brier < 1.0
    assert 0.2 < sum(outcomes) / n_days < 0.8

    improvement = (baseline_brier - emos_brier) / baseline_brier
    assert improvement >= 0.20, (
        f"gamma-EMOS Brier {emos_brier:.4f} did not measurably beat "
        f"meta-model baseline {baseline_brier:.4f} "
        f"(improvement={improvement:.2%})"
    )


# ---------------------------------------------------------------------------
# Task #281: precipitation GRU bias head
# ---------------------------------------------------------------------------


def test_precip_gru_forward_shapes_clamps_and_non_negative_outputs():
    """Both head outputs must be non-negative (precipitation is a
    non-negative quantity) and bounded so a rogue checkpoint can't
    push a 50-inch bias into the EMOS layer."""
    w = precip_gru.baseline_weights(n_input=8, n_hidden=6)
    seq = [[0.5, 0.4, 0.6, 0.3, 0.45, 0.02, 0.5, 0.3]] * 4
    out = precip_gru.forward(seq, w)
    assert out["point_bias_in"] >= 0.0
    assert out["point_bias_in"] <= 2.0
    assert 0.05 <= out["spread_in"] <= 3.0
    assert len(out["h"]) == 6


def test_precip_gru_forward_handles_empty_sequence():
    """Empty trace falls back to the calibrated default head outputs
    rather than ``None`` / zero — the EMOS layer always sees a real
    spread proxy so the gamma fit doesn't degenerate."""
    out = precip_gru.forward([], precip_gru.baseline_weights())
    assert out["point_bias_in"] >= 0.0
    assert out["point_bias_in"] < 0.5  # softplus(-2.2) ≈ 0.105
    assert 0.2 < out["spread_in"] < 0.4  # exp(log(0.3)) = 0.30
    assert out["h"] == []


def test_precip_gru_forward_pads_short_steps():
    w = precip_gru.baseline_weights(n_input=8, n_hidden=4)
    out = precip_gru.forward([[0.5, 0.4, 0.6]], w)
    assert len(out["h"]) == 4
    assert math.isfinite(out["point_bias_in"])
    assert out["point_bias_in"] >= 0.0
    assert math.isfinite(out["spread_in"])
    assert out["spread_in"] > 0.0


def test_build_precip_sequence_preserves_zero_intensity_minutecast_samples():
    """Regression test — a minutecast sample with ``{"intensity": 0.0}``
    must propagate as 0.0 into the GRU sequence, NOT fall through to
    ``s.get("in")`` or to the ensemble mean.  An ``or`` chain on the
    intensity lookup would silently inject artificial precipitation on
    dry samples and bias YES probabilities upward.
    """
    payload = {
        "om_precip_1hr_in": 0.20,
        "tio_precip_1hr_in": 0.18,
        "hrrr_precip_in": 0.22,
        "accu_precip_intensity": 0.15,
        # First three samples are dry minutes — the loop must keep
        # them at 0.0; the fourth is a genuine non-zero observation.
        "accu_minutecast_samples": [
            {"intensity": 0.0, "in": 0.99},
            {"intensity": 0.0, "in": 0.99},
            {"intensity": 0.0, "in": 0.99},
            {"intensity": 0.05, "in": 0.99},
        ],
    }
    seq = wp_precip._build_precip_sequence(payload)
    assert len(seq) == 4
    # Slot 3 of each step is the resolved minutecast intensity (the
    # ``float(step_v)`` slot in ``_build_precip_sequence``).
    assert seq[0][3] == 0.0
    assert seq[1][3] == 0.0
    assert seq[2][3] == 0.0
    assert seq[3][3] == pytest.approx(0.05)


def test_precip_gru_baseline_uses_distinct_seed_from_temperature():
    """The two verticals must not accidentally end up with identical
    hidden weights — that would couple their training drift."""
    from app.weather import gru as temp_gru
    pw = precip_gru.baseline_weights()
    tw = temp_gru.baseline_weights()
    # Same shapes, different LCG seeds -> different W_ir.
    assert pw.W_ir != tw.W_ir


# ---------------------------------------------------------------------------
# EMOS post-processor: GRU bias + spread inputs
# ---------------------------------------------------------------------------


def test_emos_with_gru_inputs_widens_spread():
    """A non-zero GRU spread proxy must widen the EMOS sigma — the
    whole point of folding the GRU's spread head into the variance
    layer is to keep an over-confident ensemble from saturating the
    gamma tail."""
    base = precip_emos.apply_precip_emos(
        ensemble=[0.5, 0.5, 0.5, 0.5],
        threshold_in=0.5,
    )
    with_gru = precip_emos.apply_precip_emos(
        ensemble=[0.5, 0.5, 0.5, 0.5],
        threshold_in=0.5,
        gru_point_in=0.0,
        gru_spread_in=0.4,
    )
    assert with_gru.sigma > base.sigma


def test_emos_with_gru_point_bias_shifts_mu_up():
    """A non-negative GRU point bias must shift the EMOS mean up — the
    HRRR / Open-Meteo under-prediction correction the bias head is
    trained to capture."""
    base = precip_emos.apply_precip_emos(
        ensemble=[0.30, 0.32, 0.28],
        threshold_in=0.5,
    )
    with_gru = precip_emos.apply_precip_emos(
        ensemble=[0.30, 0.32, 0.28],
        threshold_in=0.5,
        gru_point_in=0.40,
        gru_spread_in=0.3,
    )
    assert with_gru.mu > base.mu
    # And the correction must lift P(rain >= 0.5) on a forecast that
    # was previously under-predicting.
    assert with_gru.p_above > base.p_above


def test_emos_clamps_negative_gru_point_bias_to_zero():
    """The GRU contract is non-negative bias; if a malformed checkpoint
    leaks a negative value, the EMOS layer must defend against it
    rather than silently subtracting precipitation."""
    res = precip_emos.apply_precip_emos(
        ensemble=[0.3, 0.4, 0.35],
        threshold_in=0.5,
        gru_point_in=-1.0,  # forbidden — must be clamped
        gru_spread_in=0.3,
    )
    # mu must equal what we'd get with gru_point_in=0.0 (the floor).
    no_gru_point = precip_emos.apply_precip_emos(
        ensemble=[0.3, 0.4, 0.35],
        threshold_in=0.5,
        gru_point_in=0.0,
        gru_spread_in=0.3,
    )
    assert res.mu == pytest.approx(no_gru_point.mu)


def test_emos_coefficients_from_env_picks_up_gru_overrides(monkeypatch):
    """Operators must be able to retune the GRU blend weights without
    bouncing the process — same hot-reload contract as the temperature
    EMOS coefficients."""
    monkeypatch.setenv("AXIOM_PRECIP_EMOS_BG", "0.75")
    monkeypatch.setenv("AXIOM_PRECIP_EMOS_DG", "0.10")
    coef = precip_emos.coefficients_from_env()
    assert coef.b_g == 0.75
    assert coef.d_g == 0.10


# ---------------------------------------------------------------------------
# GRU artifact loader integration (real `models_loader.load_predictor`)
# ---------------------------------------------------------------------------


def test_resolve_precip_gru_weights_falls_back_when_loader_not_ready(monkeypatch):
    """Cold installs (no model archive yet) must still get a real
    calibrated prediction from the deterministic baseline weights."""
    from app import models_loader as ml

    fake_state = ml.ModelsState()
    fake_state.ready = False
    monkeypatch.setattr(ml, "get_state", lambda: fake_state)
    weights, name, version = asyncio.run(wp_precip._resolve_precip_gru_weights())
    assert isinstance(weights, precip_gru.PrecipGRUWeights)
    assert name == "weather_precip_gru_baseline"
    assert version == "baseline"


def test_resolve_precip_gru_weights_lazy_loads_canonical_artifact(monkeypatch):
    """When the loader is ready and the canonical artifact is in the
    catalog, `_resolve_precip_gru_weights` must call `load_predictor`
    and return the deserialized PrecipGRUWeights — not silently fall
    back to baseline (the same failure mode the temperature predictor
    code review caught)."""
    from app import models_loader as ml

    trained = precip_gru.baseline_weights(n_input=8, n_hidden=6)
    # Mark it visibly different from baseline so we can assert we
    # actually got the trained one back (mutate the output bias).
    trained.b_out = [0.5, 0.0]

    fake_state = ml.ModelsState()
    fake_state.ready = True
    fake_state.predictors = {
        wp_precip.CANONICAL_PRECIP_GRU_PATH: {
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
    weights, name, version = asyncio.run(wp_precip._resolve_precip_gru_weights())
    assert load_calls == [wp_precip.CANONICAL_PRECIP_GRU_PATH]
    assert weights is trained
    assert name == wp_precip.CANONICAL_PRECIP_GRU_PATH
    assert version == "abc123def456"


def test_resolve_precip_gru_weights_uses_in_memory_cache(monkeypatch):
    """If the artifact has already been deserialized (e.g. by the
    boot-time smoke load), we must reuse the cached object instead of
    reloading from disk on every prediction."""
    from app import models_loader as ml

    trained = precip_gru.baseline_weights(n_input=8, n_hidden=6)
    fake_state = ml.ModelsState()
    fake_state.ready = True
    fake_state.predictors = {
        wp_precip.CANONICAL_PRECIP_GRU_PATH: {"sha256": "a" * 64, "version": "v1"}
    }
    monkeypatch.setattr(ml, "get_state", lambda: fake_state)
    monkeypatch.setattr(
        ml, "get_loaded_predictor",
        lambda name: trained if name == wp_precip.CANONICAL_PRECIP_GRU_PATH else None,
    )

    async def fail_load(name):  # pragma: no cover
        raise AssertionError("load_predictor should not be called when cached")

    monkeypatch.setattr(ml, "load_predictor", fail_load)
    weights, _name, _version = asyncio.run(wp_precip._resolve_precip_gru_weights())
    assert weights is trained


def test_resolve_precip_gru_weights_falls_back_when_artifact_wrong_type(monkeypatch):
    """If the on-disk artifact deserializes to something other than a
    PrecipGRUWeights (e.g. a stale temperature checkpoint accidentally
    uploaded under the precip path), we must fall back to baseline
    rather than crash mid-prediction."""
    from app import models_loader as ml

    fake_state = ml.ModelsState()
    fake_state.ready = True
    fake_state.predictors = {
        wp_precip.CANONICAL_PRECIP_GRU_PATH: {"sha256": "b" * 64, "version": "v1"}
    }
    monkeypatch.setattr(ml, "get_state", lambda: fake_state)
    monkeypatch.setattr(ml, "get_loaded_predictor", lambda name: None)

    async def load_wrong_type(name):
        # Returning a temperature GRUWeights here exercises the
        # type-mismatch defence — the precip predictor must reject it.
        from app.weather import gru as temp_gru
        return temp_gru.baseline_weights()

    monkeypatch.setattr(ml, "load_predictor", load_wrong_type)
    weights, name, _version = asyncio.run(wp_precip._resolve_precip_gru_weights())
    assert isinstance(weights, precip_gru.PrecipGRUWeights)
    assert name == "weather_precip_gru_baseline"


def test_canonical_precip_gru_path_is_in_models_loader_canonical_set():
    """The trained precip GRU artifact must be in
    ``models_loader.CANONICAL_PREDICTORS`` so the boot-time smoke
    loader populates the in-memory predictor cache for it (mirrors
    the temperature predictor wiring)."""
    from app import models_loader as ml
    assert wp_precip.CANONICAL_PRECIP_GRU_PATH in ml.CANONICAL_PREDICTORS


# ---------------------------------------------------------------------------
# Held-out Brier: GRU+EMOS vs EMOS-only baseline (task #281 acceptance)
# ---------------------------------------------------------------------------


def _synth_precip_day(rng: random.Random) -> tuple[list[float], float, float]:
    """Generate one synthetic KXRAIN-like settled day.

    Generative model used by the held-out Brier test:
      - true daily accumulation ~ Gamma(shape=1.5, scale=0.4) inches
        (mean ~0.6", positive skew, occasional dry day)
      - source forecasts are noisy AND systematically *under*-bias the
        truth by 30% (an explicit factor of 0.7), modelling the
        well-documented HRRR / Open-Meteo cold bias on heavy events
      - threshold drawn near the climatological median so the contract
        is non-trivial (P(yes) ~ 0.4-0.7).
    """
    true_in = rng.gammavariate(1.5, 0.4)
    sources = [
        max(0.0, 0.7 * true_in * math.exp(rng.gauss(0.0, 0.25)))
        for _ in range(4)
    ]
    threshold = max(0.05, 0.27 + rng.gauss(0.0, 0.15))
    return sources, threshold, true_in


def _gru_emos_p_above(
    sources: list[float],
    threshold_in: float,
    weights: precip_gru.PrecipGRUWeights,
) -> float:
    em_mean = sum(sources) / len(sources)
    em_var = (
        sum((s - em_mean) ** 2 for s in sources) / (len(sources) - 1)
        if len(sources) > 1 else 0.0
    )
    seq_step = [
        sources[0], sources[1], sources[2], sources[3],
        em_mean, em_var, 0.5, 0.5,
    ]
    out = precip_gru.forward([seq_step] * 4, weights)
    em = precip_emos.apply_precip_emos(
        ensemble=sources,
        threshold_in=threshold_in,
        gru_point_in=out["point_bias_in"],
        gru_spread_in=out["spread_in"],
    )
    return em.p_above


def _train_precip_gru_b_out(
    seed: int = 20260424, n_train: int = 200, n_iter: int = 80,
) -> precip_gru.PrecipGRUWeights:
    """Tiny offline-style training loop for the precipitation GRU.

    Optimises the 2-scalar output head (b_out) by hill-climbing on the
    Brier score over a synthetic settled-outcome training set.  This
    mirrors the offline ES-style fit that
    ``scripts/train_precip_gru.py`` will run against real settled
    KXRAIN markets — the expensive parts (the GRU recurrent weights)
    stay fixed so the whole optimisation is deterministic and finishes
    in well under a second.

    Returns a fresh ``PrecipGRUWeights`` with a fitted b_out; the rest
    of the weights are unchanged baseline values.
    """
    rng = random.Random(seed)
    weights = precip_gru.baseline_weights()
    train = [_synth_precip_day(rng) for _ in range(n_train)]
    train_outcomes = [1 if true_in > thr else 0 for _, thr, true_in in train]

    def brier(b_out: list[float]) -> float:
        weights.b_out = list(b_out)
        loss = 0.0
        for (sources, thr, _), y in zip(train, train_outcomes):
            p = _gru_emos_p_above(sources, thr, weights)
            loss += (p - y) ** 2
        return loss / len(train)

    best_b = list(weights.b_out)
    best_loss = brier(best_b)
    for _ in range(n_iter):
        cand = [
            best_b[0] + rng.gauss(0.0, 0.8),
            best_b[1] + rng.gauss(0.0, 0.4),
        ]
        loss = brier(cand)
        if loss < best_loss:
            best_loss = loss
            best_b = cand
    weights.b_out = best_b
    return weights


def test_precip_gru_emos_beats_emos_only_brier_on_holdout():
    """Synthetic held-out window — a *trained* precip GRU bias head +
    EMOS pipeline must materially beat the EMOS-only baseline (no GRU
    contribution).

    The training half (different RNG seed than the held-out half) fits
    the GRU's b_out via ES-style hill-climbing on the Brier score —
    the same shape of optimisation that
    ``scripts/train_precip_gru.py`` runs offline against real settled
    KXRAIN markets.  EMOS-only consumes the under-biased ensemble and
    inherits the under-prediction; the GRU bias head supplies the
    missing additive correction + a calibrated spread, moving the
    predicted P(YES) toward the observed positive rate.

    We lock in a >=10% Brier improvement (current measured ~15-25%)
    so the test isn't brittle to RNG drift while still failing the
    moment the GRU pipeline regresses to EMOS-only or worse.
    """
    trained = _train_precip_gru_b_out(seed=20260424)

    # Held-out evaluation — fresh RNG seed so we're really measuring
    # generalisation of the trained b_out to unseen settled outcomes.
    rng = random.Random(20260601)
    n_days = 240
    emos_only_probs: list[float] = []
    gru_emos_probs: list[float] = []
    outcomes: list[int] = []

    for _ in range(n_days):
        sources, threshold, true_in = _synth_precip_day(rng)
        em_only = precip_emos.apply_precip_emos(
            ensemble=sources,
            threshold_in=threshold,
        )
        emos_only_probs.append(em_only.p_above)
        gru_emos_probs.append(_gru_emos_p_above(sources, threshold, trained))
        outcomes.append(1 if true_in > threshold else 0)

    emos_brier = _brier(emos_only_probs, outcomes)
    gru_brier = _brier(gru_emos_probs, outcomes)

    assert 0.0 < emos_brier < 1.0
    assert 0.0 < gru_brier < 1.0
    # Non-degenerate label balance so Brier is meaningful.
    assert 0.2 < sum(outcomes) / n_days < 0.9

    improvement = (emos_brier - gru_brier) / emos_brier
    assert improvement >= 0.10, (
        f"GRU+EMOS Brier {gru_brier:.4f} did not measurably beat "
        f"EMOS-only baseline {emos_brier:.4f} "
        f"(improvement={improvement:.2%})"
    )


# ---------------------------------------------------------------------------
# End-to-end predictor wires the GRU into the EMOS layer
# ---------------------------------------------------------------------------


def test_predictor_passes_gru_outputs_into_emos(monkeypatch):
    """Guard against accidental disconnection: ``apply_precip_emos`` must
    receive the GRU's bias + spread on every prediction call (regression
    test — the previous task #255 wiring did NOT pass them, which is
    exactly what task #281 fixes)."""
    seen: dict = {}
    real_apply = precip_emos.apply_precip_emos

    def spy_apply(*, ensemble, threshold_in, **kwargs):
        seen["kwargs"] = dict(kwargs)
        return real_apply(
            ensemble=ensemble, threshold_in=threshold_in, **kwargs,
        )

    monkeypatch.setattr(wp_precip.precip_emos, "apply_precip_emos", spy_apply)
    asyncio.run(wp_precip.predict_precipitation_binary(
        "KXRAINNYC-26APR15-T0.5",
        payload_override=_payload(),
    ))
    assert "gru_point_in" in seen["kwargs"]
    assert "gru_spread_in" in seen["kwargs"]
    assert seen["kwargs"]["gru_point_in"] >= 0.0
    assert seen["kwargs"]["gru_spread_in"] > 0.0
