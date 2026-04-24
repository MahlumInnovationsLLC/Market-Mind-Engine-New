"""Tests for the per-(city, metric) GRU+EMOS retrain (task #254)."""
from __future__ import annotations

import asyncio
import json
import math
import random
from datetime import date, datetime, timezone
from typing import Any

import pytest

from app import db
from app.feeds import weather_climatology as clim
from app.weather import (
    emos as emos_mod,
    gru as gru_mod,
    predictor as weather_predictor,
    training,
)


# ---------------------------------------------------------------------------
# Synthetic-history training: held-out Brier must beat baseline
# ---------------------------------------------------------------------------


def _synth_samples(
    *, city: str, metric: str, n: int, seed: int = 20260424,
) -> list[training.TrainingSample]:
    """Generate a synthetic but realistic history for one (city, metric).

    The ground-truth process is:
      * climatology mean = 70°F + city-specific offset
      * day-to-day true temperature ~ N(climatology, 7°F)
      * each "source" forecast = true + N(0, 4°F) + per-source bias
      * threshold ~ N(observed, 4°F) so the contract is non-trivial

    With the ensemble of 4 sources, the EMOS NGR fit should pull the
    bias term down and the variance fit should give a sigma close to
    the empirical residual std — both of which beat the deterministic
    baseline by a healthy margin.
    """
    rng = random.Random(seed)
    out: list[training.TrainingSample] = []
    clim_mean = 70.0
    for i in range(n):
        true_t = rng.gauss(clim_mean, 7.0)
        sources = [
            rng.gauss(true_t + 0.5, 4.0),  # HRRR — tiny warm bias
            rng.gauss(true_t - 0.3, 4.0),  # climatology
            rng.gauss(true_t, 3.5),        # open-meteo
            rng.gauss(true_t + 0.2, 3.5),  # NWS
        ]
        # Sequence mirrors what predictor._build_sequence emits.
        seq_step = [
            sources[2], sources[2], sources[0] + 4.0, sources[0] - 4.0,
            clim_mean + 8.0, clim_mean, 0.5, (i % 365) / 365.0,
        ]
        threshold = true_t + rng.gauss(0.0, 4.0)
        out.append(training.TrainingSample(
            city=city,
            metric=metric,
            forecast_date=date(2026, 1, 1),
            ensemble=sources,
            sequence=[seq_step] * 6,
            observed_f=true_t,
            threshold_f=threshold,
        ))
    return out


def test_train_one_beats_baseline_on_synthetic_history():
    """The headline acceptance criterion for task #254: held-out Brier
    of the trained pipeline must be lower than the deterministic
    baseline floor.  We synthesize 240 days of NYC HIGH samples so
    the test is RNG-stable and assert a ≥5% improvement."""
    samples = _synth_samples(city="NYC", metric="HIGH", n=240)
    res = training.train_one(samples, holdout_frac=0.25)

    assert res.error is None
    assert res.weights is not None and res.coefficients is not None
    assert res.holdout_brier is not None and res.baseline_brier is not None
    assert res.promoted, (
        f"trained holdout Brier {res.holdout_brier:.4f} did not beat "
        f"baseline {res.baseline_brier:.4f}"
    )
    improvement = (
        (res.baseline_brier - res.holdout_brier) / max(1e-9, res.baseline_brier)
    )
    assert improvement >= 0.05, (
        f"expected ≥5% Brier improvement; got {improvement:.2%} "
        f"(holdout={res.holdout_brier:.4f}, baseline={res.baseline_brier:.4f})"
    )
    # Sanity: sigmoid-ish coefficients look plausible.
    assert math.isfinite(res.coefficients.a)
    assert res.coefficients.c > 0.0
    assert res.coefficients.d >= 0.0
    assert res.coefficients.d_g >= 0.0


def test_train_one_skips_below_min_samples():
    """Below ``MIN_TRAINING_SAMPLES`` we must short-circuit — running
    a regression on 5 noisy points is pure noise and would happily
    overfit to a worse Brier than baseline."""
    samples = _synth_samples(city="MIA", metric="LOW", n=4)
    res = training.train_one(samples)
    assert not res.promoted
    assert res.error and "too few samples" in res.error


def test_baseline_brier_matches_pre_training_pipeline():
    """``baseline_brier`` should exactly mirror what the live
    predictor produces with the deterministic GRU + env-default EMOS
    coefficients — that's the floor the trainer has to beat."""
    samples = _synth_samples(city="LAX", metric="HIGH", n=20)
    base = training.baseline_brier(samples)
    # Recompute with the predictor's actual call shape.
    base_w = gru_mod.baseline_weights()
    base_c = emos_mod.EMOSCoefficients()
    expected = training.evaluate_brier(samples, base_w, base_c)
    assert base == pytest.approx(expected, abs=1e-9)


# ---------------------------------------------------------------------------
# DB persistence round-trip
# ---------------------------------------------------------------------------


def test_save_to_db_skips_unpromoted_results(monkeypatch):
    """Refits that fail the held-out Brier gate must NOT be written —
    the predictor would otherwise pick up a regression on the next
    cache miss."""
    calls: list[Any] = []

    async def fake_execute(query, *args):
        calls.append(args)
        return "INSERT 0 1"

    monkeypatch.setattr(db, "execute", fake_execute)
    res = training.TrainResult(
        city="NYC", metric="HIGH",
        n_samples=20, n_train=15, n_holdout=5,
        weights=gru_mod.baseline_weights(),
        coefficients=emos_mod.EMOSCoefficients(),
        baseline_brier=0.20, holdout_brier=0.30,
        promoted=False,
    )
    asyncio.run(training.save_to_db(res))
    assert calls == [], "unpromoted result must be skipped"


def test_save_to_db_writes_promoted_result(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_execute(query, *args):
        captured["query"] = " ".join(query.split())
        captured["args"] = args
        return "INSERT 0 1"

    monkeypatch.setattr(db, "execute", fake_execute)
    samples = _synth_samples(city="NYC", metric="HIGH", n=80)
    res = training.train_one(samples, holdout_frac=0.25)
    assert res.promoted, "synthetic NYC/HIGH must promote (set up by fixture)"
    asyncio.run(training.save_to_db(res))

    assert "INSERT INTO weather_temp_trained_models" in captured["query"]
    args = captured["args"]
    assert args[0] == "NYC"
    assert args[1] == "HIGH"
    # Round-tripping the JSON payloads exposes the trained dataclass shape.
    gru_dict = json.loads(args[2])
    coef_dict = json.loads(args[3])
    assert {"W_out", "b_out", "input_mean", "input_std"} <= set(gru_dict)
    assert {"a", "b", "b_g", "c", "d", "d_g"} == set(coef_dict)
    metrics = json.loads(args[4])
    assert "holdout_brier" in metrics and "baseline_brier" in metrics


def test_load_from_db_returns_none_when_table_missing(monkeypatch):
    async def fake_fetchrow(query, *args):
        raise RuntimeError('relation "weather_temp_trained_models" does not exist')

    monkeypatch.setattr(db, "fetchrow", fake_fetchrow)
    rec = asyncio.run(training.load_from_db("NYC", "HIGH"))
    assert rec is None


def test_load_from_db_returns_none_when_no_row(monkeypatch):
    async def fake_fetchrow(query, *args):
        return None

    monkeypatch.setattr(db, "fetchrow", fake_fetchrow)
    rec = asyncio.run(training.load_from_db("NYC", "HIGH"))
    assert rec is None


def test_save_load_roundtrip(monkeypatch):
    """A promoted fit must round-trip through the DB layer back into
    a usable ``TrainedRecord`` whose weights produce identical
    predictions to the in-memory result."""
    storage: dict[tuple[str, str], dict[str, Any]] = {}

    async def fake_execute(query, *args):
        # mimic the upsert
        key = (args[0], args[1])
        storage[key] = {
            "gru_weights": json.loads(args[2]),
            "emos_coef": json.loads(args[3]),
            "n_samples": int(args[5]),
            "n_holdout": int(args[6]),
            "baseline_brier": args[7],
            "holdout_brier": args[8],
            "fit_at": datetime.now(timezone.utc),
        }
        return "INSERT 0 1"

    async def fake_fetchrow(query, *args):
        key = (args[0], args[1])
        return storage.get(key)

    monkeypatch.setattr(db, "execute", fake_execute)
    monkeypatch.setattr(db, "fetchrow", fake_fetchrow)

    samples = _synth_samples(city="CHI", metric="LOW", n=80)
    res = training.train_one(samples, holdout_frac=0.25)
    assert res.promoted
    asyncio.run(training.save_to_db(res))
    rec = asyncio.run(training.load_from_db("CHI", "LOW"))
    assert rec is not None
    assert rec.city == "CHI" and rec.metric == "LOW"
    # Compare a forward pass to confirm the round-trip preserves
    # the trained weights bit-for-bit.
    seq = samples[0].sequence
    a = gru_mod.forward(seq, res.weights)
    b = gru_mod.forward(seq, rec.weights)
    assert a["point_bias_f"] == pytest.approx(b["point_bias_f"], abs=1e-9)
    assert a["spread_f"] == pytest.approx(b["spread_f"], abs=1e-9)
    # And the EMOS coefs survive serialization too.
    for fld in ("a", "b", "b_g", "c", "d", "d_g"):
        assert getattr(rec.coefficients, fld) == pytest.approx(
            getattr(res.coefficients, fld), abs=1e-9
        )


# ---------------------------------------------------------------------------
# Predictor consults the DB before falling back to env defaults
# ---------------------------------------------------------------------------


def _payload(**over):
    base = {
        "om_temperature_f": 78.0,
        "nws_temperature_f": 77.5,
        "tio_temperature_f": 78.4,
        "hrrr_t2m_f": 78.2,
        "hrrr_tmax_f": 84.0,
        "hrrr_tmin_f": 65.0,
        "clim_tmax_f": 80.0,
        "clim_tavg_f": 73.0,
    }
    base.update(over)
    return base


def test_predictor_uses_trained_record_when_db_returns_one(monkeypatch):
    """When ``weather_temp_trained_models`` has a row for the
    (city, metric), the predictor must use those coefficients and
    NOT fall back to env defaults."""
    weather_predictor._trained_cache_clear()
    trained = training.TrainedRecord(
        city="NYC", metric="HIGH",
        weights=gru_mod.baseline_weights(),
        # Push the coefficients far from the env defaults so we can
        # see them surface in the predictor's `raw` payload.
        coefficients=emos_mod.EMOSCoefficients(
            a=12.345, b=0.5, b_g=0.3, c=4.0, d=0.1, d_g=0.05,
        ),
        n_samples=42,
        baseline_brier=0.21,
        holdout_brier=0.16,
        fit_at=datetime(2026, 4, 24, tzinfo=timezone.utc),
    )

    async def fake_load_from_db(city, metric):
        if city == "NYC" and metric == "HIGH":
            return trained
        return None

    monkeypatch.setattr(training, "load_from_db", fake_load_from_db)
    pred = asyncio.run(weather_predictor.predict_temperature_binary(
        "KXHIGHNYC-26JUL15-T75",
        payload_override=_payload(),
    ))
    coefs = pred.raw["emos"]["coefficients"]
    assert pred.raw["emos"]["coefficients_source"] == "trained"
    assert coefs["a"] == pytest.approx(12.345)
    assert coefs["b"] == pytest.approx(0.5)
    assert coefs["b_g"] == pytest.approx(0.3)
    assert pred.raw["emos"]["trained_n_samples"] == 42
    assert pred.raw["emos"]["trained_holdout_brier"] == pytest.approx(0.16)
    assert pred.raw["gru"]["name"] == "weather_temp_trained_per_city_metric"


def test_predictor_falls_back_to_env_when_db_empty(monkeypatch):
    weather_predictor._trained_cache_clear()

    async def fake_load_from_db(city, metric):
        return None

    monkeypatch.setattr(training, "load_from_db", fake_load_from_db)
    pred = asyncio.run(weather_predictor.predict_temperature_binary(
        "KXHIGHNYC-26JUL15-T75",
        payload_override=_payload(),
    ))
    assert pred.raw["emos"]["coefficients_source"] == "env"
    # Env defaults match EMOSCoefficients() defaults.
    expected = emos_mod.EMOSCoefficients()
    assert pred.raw["emos"]["coefficients"]["b"] == pytest.approx(expected.b)


def test_predictor_caches_trained_record_for_ttl(monkeypatch):
    """``_resolve_trained_bundle`` must cache the DB lookup so the
    trading loop's per-cycle predictions don't trigger one DB
    fetchrow per call."""
    weather_predictor._trained_cache_clear()
    trained = training.TrainedRecord(
        city="LAX", metric="HIGH",
        weights=gru_mod.baseline_weights(),
        coefficients=emos_mod.EMOSCoefficients(),
        n_samples=20,
        baseline_brier=0.2,
        holdout_brier=0.18,
        fit_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )
    call_count = 0

    async def fake_load_from_db(city, metric):
        nonlocal call_count
        call_count += 1
        return trained

    monkeypatch.setattr(training, "load_from_db", fake_load_from_db)
    asyncio.run(weather_predictor._resolve_trained_bundle("LAX", "HIGH"))
    asyncio.run(weather_predictor._resolve_trained_bundle("LAX", "HIGH"))
    asyncio.run(weather_predictor._resolve_trained_bundle("LAX", "HIGH"))
    assert call_count == 1


# ---------------------------------------------------------------------------
# History loading: aggregates daily highs/lows from observations
# ---------------------------------------------------------------------------


def test_load_history_joins_hrrr_obs_and_climatology(monkeypatch):
    """End-to-end through ``load_history``: synthetic DB rows for
    HRRR + observations + climatology should produce a usable list
    of training samples."""
    candidates = [date(2026, 4, 1), date(2026, 4, 2), date(2026, 4, 3)]

    async def fake_fetch(query, *args):
        if "DISTINCT forecast_date" in query:
            return [{"forecast_date": d} for d in candidates]
        return []

    async def fake_fetchrow(query, *args):
        q = " ".join(query.split())
        if "FROM weather_observations" in q and "MAX(temperature_f)" in q:
            return {"v": 82.0}
        if "FROM weather_observations" in q and "MIN(temperature_f)" in q:
            return {"v": 60.0}
        if "FROM weather_observations" in q and "AVG(temperature_f)" in q:
            return {"v": 71.0}
        if "FROM weather_hrrr_forecasts" in q:
            return {
                "hrrr_high_f": 84.0,
                "hrrr_hourly": {
                    "t2m_f": 78.0,
                    "tmin_f": 62.0,
                    "samples": [{"t": "2026-04-01T12:00Z", "f": 80.0}],
                },
                "hrrr_run_time": datetime.now(timezone.utc),
            }
        if "FROM weather_observations" in q:
            # anchor obs lookups
            return {"temperature_f": 77.0}
        return None

    async def fake_clim(city, doy, metric):
        return {
            clim.METRIC_TMAX: 85.0,
            clim.METRIC_TMIN: 65.0,
            clim.METRIC_TAVG: 75.0,
        }.get(metric)

    monkeypatch.setattr(db, "fetch", fake_fetch)
    monkeypatch.setattr(db, "fetchrow", fake_fetchrow)
    monkeypatch.setattr(clim, "get_climatology", fake_clim)

    samples = asyncio.run(training.load_history("NYC", "HIGH", days=30))
    assert len(samples) == 3
    s = samples[0]
    assert s.city == "NYC" and s.metric == "HIGH"
    assert s.observed_f == pytest.approx(82.0)
    # The ensemble must carry the HRRR tmax + climatology + anchor obs.
    assert any(v == pytest.approx(84.0) for v in s.ensemble)
    assert any(v == pytest.approx(85.0) for v in s.ensemble)
    # Sequence must be at least one step long with the canonical 8-feature shape.
    assert len(s.sequence) >= 1
    assert all(len(step) == 8 for step in s.sequence)


def test_load_history_drops_dates_without_observed_target(monkeypatch):
    """Dates without an observed daily high/low can't be supervised;
    they must be silently dropped (no zero-target leakage)."""
    candidates = [date(2026, 4, 1), date(2026, 4, 2)]

    async def fake_fetch(query, *args):
        if "DISTINCT forecast_date" in query:
            return [{"forecast_date": d} for d in candidates]
        return []

    async def fake_fetchrow(query, *args):
        if "MAX(temperature_f)" in query:
            return {"v": None}  # no observations on either day
        if "weather_hrrr_forecasts" in query:
            return {
                "hrrr_high_f": 80.0, "hrrr_hourly": {},
                "hrrr_run_time": datetime.now(timezone.utc),
            }
        return None

    async def fake_clim(city, doy, metric):
        return 70.0

    monkeypatch.setattr(db, "fetch", fake_fetch)
    monkeypatch.setattr(db, "fetchrow", fake_fetchrow)
    monkeypatch.setattr(clim, "get_climatology", fake_clim)

    samples = asyncio.run(training.load_history("NYC", "HIGH", days=30))
    assert samples == []
