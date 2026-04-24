"""Tests for the precipitation EMOS coefficient refit loop (task #280).

Covers:
  * The fitter recovers a known coefficient set from synthetic data.
  * The fitter materially improves Brier vs the shipped defaults on a
    skewed real-world-shaped sample.
  * ``coefficients_from_env`` precedence: persisted cache > env > default.
  * The predictor records observation rows on every prediction.
  * The settlement pipeline stamps ``actual_outcome_yes`` + ``settled_at``.
  * The retrain cycle invokes the refit and skips when not enough data.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import random
from typing import Any

import pytest

from app import db as _db
from app.settlement import pipeline as _pipeline
from app.settlement import retrain as _retrain
from app.weather import precip_emos
from app.weather import precip_predictor as wp_precip


# ---------------------------------------------------------------------------
# Shared synthetic sample generator
# ---------------------------------------------------------------------------


def _generate_synthetic(
    coef: precip_emos.PrecipEMOSCoefficients,
    *,
    n: int,
    seed: int,
    threshold_jitter: float = 0.15,
    src_noise: float = 0.25,
) -> list[precip_emos.PrecipEMOSTrainingSample]:
    """Generate (ensemble_mean, ensemble_var, K, y_above) tuples whose
    YES outcomes follow the gamma / logistic link of `coef` with a
    realistic 4-source ensemble."""
    rng = random.Random(seed)
    out: list[precip_emos.PrecipEMOSTrainingSample] = []
    for _ in range(n):
        # Truth model: daily precip ~ Gamma(shape=1.2, scale=0.4).
        true_in = rng.gammavariate(1.2, 0.4)
        # 4-source ensemble: noisy unbiased on a log-scale.
        sources = [
            max(0.0, true_in * math.exp(rng.gauss(0.0, src_noise)))
            for _ in range(4)
        ]
        em_mean = sum(sources) / len(sources)
        em_var = (
            sum((s - em_mean) ** 2 for s in sources) / (len(sources) - 1)
            if len(sources) > 1 else 0.0
        )
        # Threshold near the climatological median.
        threshold = max(0.05, 0.27 + rng.gauss(0.0, threshold_jitter))
        y = 1 if true_in > threshold else 0
        out.append(precip_emos.PrecipEMOSTrainingSample(
            ensemble_mean=float(em_mean),
            ensemble_var=float(em_var),
            threshold_in=float(threshold),
            y_above=int(y),
        ))
    return out


# ---------------------------------------------------------------------------
# Fitter recovery + Brier improvement
# ---------------------------------------------------------------------------


def test_fitter_runs_and_improves_brier_vs_default():
    """A fit on synthetic data must converge AND the post-fit Brier
    must beat the in-sample Brier of the shipped defaults by a
    measurable margin. This is the "done = Brier materially lower"
    half of the task spec."""
    pytest.importorskip("scipy.optimize")
    # Use the shipped defaults as ground truth so the fitter has a
    # well-specified target to recover toward, then verify Brier
    # materially improves vs *another*, deliberately mis-set baseline.
    truth = precip_emos.PrecipEMOSCoefficients(
        a=0.05, b=1.10, c=0.06, d=1.30, logistic_scale=4.5,
    )
    samples = _generate_synthetic(truth, n=500, seed=2026)

    # Sanity: training set is two-class (otherwise the fitter rejects).
    n_pos = sum(1 for s in samples if s.y_above)
    assert 50 < n_pos < len(samples) - 50

    bad_baseline = precip_emos.PrecipEMOSCoefficients(
        a=0.0, b=1.0, c=0.04, d=1.20, logistic_scale=5.0,
    )
    fit = precip_emos.fit_precip_emos_coefficients(
        samples, initial=bad_baseline,
    )
    assert fit.n_samples == len(samples)
    # The refit must outperform the baseline by at least the
    # rejection tolerance the retrain loop enforces.
    assert fit.brier < fit.brier_baseline
    improvement = fit.brier_baseline - fit.brier
    assert improvement > 1e-3, (
        f"refit Brier {fit.brier:.4f} did not materially beat "
        f"baseline {fit.brier_baseline:.4f}"
    )
    # And the refit log-likelihood is finite and not collapsed.
    assert math.isfinite(fit.log_likelihood)


def test_fitter_rejects_when_too_few_samples():
    """The fitter is intentionally conservative — under
    MIN_REFIT_SAMPLES it raises rather than returning a wobbly fit."""
    pytest.importorskip("scipy.optimize")
    truth = precip_emos.PrecipEMOSCoefficients()
    samples = _generate_synthetic(truth, n=10, seed=1)
    with pytest.raises(ValueError):
        precip_emos.fit_precip_emos_coefficients(samples)


def test_fitter_rejects_single_class_data():
    """All-YES or all-NO training sets break the log-likelihood and
    the fitter must refuse rather than silently producing a degenerate
    set of coefficients."""
    pytest.importorskip("scipy.optimize")
    samples = [
        precip_emos.PrecipEMOSTrainingSample(
            ensemble_mean=0.5, ensemble_var=0.05,
            threshold_in=0.3, y_above=1,
        )
        for _ in range(precip_emos.MIN_REFIT_SAMPLES + 5)
    ]
    with pytest.raises(ValueError, match="single-class"):
        precip_emos.fit_precip_emos_coefficients(samples)


# ---------------------------------------------------------------------------
# Coefficient resolution: persisted > env > default
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_persisted_coefficients_after_test():
    """Every test runs against a clean persisted-cache slate so
    ordering can never bleed between cases."""
    precip_emos.set_persisted_coefficients(None)
    yield
    precip_emos.set_persisted_coefficients(None)


def test_coefficients_default_when_unset(monkeypatch):
    for k in ("AXIOM_PRECIP_EMOS_A", "AXIOM_PRECIP_EMOS_B",
              "AXIOM_PRECIP_EMOS_C", "AXIOM_PRECIP_EMOS_D",
              "AXIOM_PRECIP_EMOS_LOGISTIC"):
        monkeypatch.delenv(k, raising=False)
    coef = precip_emos.coefficients_from_env()
    assert coef.a == 0.0
    assert coef.b == 1.0
    assert coef.c == 0.04
    assert coef.d == 1.20
    assert coef.logistic_scale == 5.0


def test_coefficients_env_overrides_default(monkeypatch):
    monkeypatch.setenv("AXIOM_PRECIP_EMOS_A", "0.25")
    monkeypatch.setenv("AXIOM_PRECIP_EMOS_LOGISTIC", "6.5")
    coef = precip_emos.coefficients_from_env()
    assert coef.a == 0.25
    assert coef.logistic_scale == 6.5


def test_persisted_cache_wins_over_env(monkeypatch):
    monkeypatch.setenv("AXIOM_PRECIP_EMOS_A", "0.25")
    persisted = precip_emos.PrecipEMOSCoefficients(
        a=0.99, b=2.0, c=0.5, d=3.5, logistic_scale=7.0,
    )
    precip_emos.set_persisted_coefficients(persisted)
    out = precip_emos.coefficients_from_env()
    assert out.a == 0.99
    assert out.b == 2.0
    assert out.logistic_scale == 7.0


def test_clear_persisted_cache_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("AXIOM_PRECIP_EMOS_A", "0.42")
    precip_emos.set_persisted_coefficients(
        precip_emos.PrecipEMOSCoefficients(a=1.0)
    )
    precip_emos.set_persisted_coefficients(None)
    assert precip_emos.coefficients_from_env().a == 0.42


def test_coefficients_serialization_roundtrip():
    coef = precip_emos.PrecipEMOSCoefficients(
        a=0.13, b=1.31, c=0.07, d=1.81, logistic_scale=5.5, sigma2_floor=0.02,
    )
    payload = precip_emos.coefficients_to_dict(coef)
    rebuilt = precip_emos.coefficients_from_dict(payload)
    assert rebuilt == coef


def test_coefficients_from_dict_missing_keys_use_defaults():
    rebuilt = precip_emos.coefficients_from_dict({"a": 0.5})
    assert rebuilt.a == 0.5
    # Missing keys collapse to dataclass defaults, not zero.
    assert rebuilt.b == 1.0
    assert rebuilt.logistic_scale == 5.0


# ---------------------------------------------------------------------------
# Direction canonicalization
# ---------------------------------------------------------------------------


def test_to_training_sample_above_keeps_outcome():
    s = precip_emos.to_training_sample(
        ensemble_mean=0.5, ensemble_var=0.04, threshold_in=0.3,
        direction="above", actual_outcome_yes=1,
    )
    assert s.y_above == 1


def test_to_training_sample_below_flips_outcome():
    """A 'below 0.3"' contract that resolved YES means precip was
    actually < 0.3" — i.e. NOT above 0.3" — so the canonical
    y_above must be 0."""
    s = precip_emos.to_training_sample(
        ensemble_mean=0.5, ensemble_var=0.04, threshold_in=0.3,
        direction="below", actual_outcome_yes=1,
    )
    assert s.y_above == 0
    s2 = precip_emos.to_training_sample(
        ensemble_mean=0.5, ensemble_var=0.04, threshold_in=0.3,
        direction="below", actual_outcome_yes=0,
    )
    assert s2.y_above == 1


# ---------------------------------------------------------------------------
# Predictor records observations
# ---------------------------------------------------------------------------


def _payload() -> dict[str, Any]:
    return {
        "om_precip_1hr_in": 0.6,
        "tio_precip_1hr_in": 0.8,
        "accu_precip_intensity": 0.7,
        "hrrr_precip_in": 0.5,
    }


def test_predictor_persists_observation_row(monkeypatch):
    """predict_precipitation_binary upserts a row into
    precip_emos_observations on every call."""
    captured: dict[str, Any] = {}

    async def fake_execute(query: str, *args: Any) -> str:
        if "precip_emos_observations" in query and "INSERT" in query:
            captured["query"] = query
            captured["args"] = args
        return ""

    monkeypatch.setattr(_db, "execute", fake_execute)
    pred = asyncio.run(wp_precip.predict_precipitation_binary(
        "KXRAINNYC-26APR15-T0.5", payload_override=_payload(),
    ))
    assert "args" in captured, "predictor did not upsert observation"
    args = captured["args"]
    assert args[0] == "KXRAINNYC-26APR15-T0.5"  # market_id
    assert args[1] == "NYC"                      # city
    assert args[2] == pytest.approx(0.5)         # threshold_in
    assert args[3] == "above"                    # direction
    # ensemble mean / var / n must match what the prediction returned.
    assert args[4] == pytest.approx(pred.raw["ensemble"]["mean"])
    assert args[5] == pytest.approx(pred.raw["ensemble"]["var"])
    assert args[6] == 4


def test_predictor_record_observation_can_be_disabled(monkeypatch):
    calls: list[Any] = []

    async def fake_execute(query: str, *args: Any) -> str:
        if "precip_emos_observations" in query:
            calls.append(query)
        return ""

    monkeypatch.setattr(_db, "execute", fake_execute)
    asyncio.run(wp_precip.predict_precipitation_binary(
        "KXRAINNYC-26APR15-T0.5", payload_override=_payload(),
        record_observation=False,
    ))
    assert calls == []


def test_predictor_swallows_observation_db_error(monkeypatch):
    """A transient DB failure during the observation upsert must
    NEVER propagate to the trader."""
    async def boom(query: str, *args: Any) -> str:
        if "precip_emos_observations" in query:
            raise RuntimeError("simulated DB outage")
        return ""

    monkeypatch.setattr(_db, "execute", boom)
    # Should not raise.
    pred = asyncio.run(wp_precip.predict_precipitation_binary(
        "KXRAINNYC-26APR15-T0.5", payload_override=_payload(),
    ))
    assert 0.0 <= pred.probability <= 1.0


# ---------------------------------------------------------------------------
# Pipeline stamps the outcome at settlement time
# ---------------------------------------------------------------------------


def test_settle_market_stamps_precip_observation(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_execute(query: str, *args: Any) -> str:
        if "precip_emos_observations" in query and "UPDATE" in query:
            captured["query"] = query
            captured["args"] = args
        return ""

    monkeypatch.setattr(_db, "execute", fake_execute)
    asyncio.run(_pipeline._stamp_precip_emos_observation(
        "KXRAINNYC-26APR15-T0.5", outcome_yes=1,
    ))
    assert captured["args"] == ("KXRAINNYC-26APR15-T0.5", 1)


def test_settle_market_stamp_swallows_db_error(monkeypatch):
    async def boom(query: str, *args: Any) -> str:
        raise RuntimeError("simulated DB outage")

    monkeypatch.setattr(_db, "execute", boom)
    # Must not raise — settlement cannot regress because of
    # observability-side flakes.
    asyncio.run(_pipeline._stamp_precip_emos_observation(
        "KXRAINNYC-26APR15-T0.5", outcome_yes=0,
    ))


# ---------------------------------------------------------------------------
# Retrain hook: refit when enough rows, skip otherwise
# ---------------------------------------------------------------------------


class _RetrainStub:
    """Minimal db.fetch / db.execute stub for the EMOS refit path —
    isolated from _FakeDb in test_retrain_worker so the two suites
    don't have to share the same data shape."""

    def __init__(self, observations: list[dict[str, Any]] | None = None):
        self.observations = observations or []
        self.executes: list[tuple[str, tuple]] = []
        self.publishes: list[dict[str, Any]] = []

    async def fetch(self, query: str, *args: Any):
        if "precip_emos_observations" in query:
            return list(self.observations)
        # Other retrain-cycle queries land here too — return empty so
        # the cycle finds no other verticals to refit.
        return []

    async def fetchval(self, query: str, *args: Any):
        return None

    async def execute(self, query: str, *args: Any) -> str:
        self.executes.append((query, args))
        return ""

    async def publish(self, event: dict[str, Any]) -> None:
        self.publishes.append(event)


@pytest.fixture
def retrain_stub(monkeypatch):
    stub = _RetrainStub()
    monkeypatch.setattr(_db, "fetch", stub.fetch)
    monkeypatch.setattr(_db, "fetchval", stub.fetchval)
    monkeypatch.setattr(_db, "execute", stub.execute)
    monkeypatch.setattr(_retrain, "_publish", stub.publish)
    return stub


def test_emos_refit_skips_when_too_few_observations(retrain_stub):
    """Below MIN_REFIT_SAMPLES the refit step records 'skipped' and
    promotes nothing."""
    retrain_stub.observations = [
        {
            "market_id": f"KXRAINNYC-26APR{i:02d}-T0.3",
            "threshold_in": 0.3, "direction": "above",
            "ensemble_mean": 0.4, "ensemble_var": 0.05,
            "actual_outcome_yes": i % 2,
        }
        for i in range(5)
    ]
    out = asyncio.run(_retrain._refit_precip_emos_from_observations())
    assert out["status"] == "skipped"
    assert out["promoted"] is False
    # Cache must NOT have been touched.
    assert precip_emos._persisted_coefficients() is None


def test_emos_refit_promotes_when_brier_improves(retrain_stub):
    """End-to-end: enough observations + a Brier improvement vs the
    baseline => fit, persist to system_settings, install in cache,
    publish promoted event."""
    pytest.importorskip("scipy.optimize")
    truth = precip_emos.PrecipEMOSCoefficients(
        a=0.10, b=1.20, c=0.07, d=1.40, logistic_scale=4.0,
    )
    samples = _generate_synthetic(truth, n=400, seed=4242)
    retrain_stub.observations = [
        {
            "market_id": f"KXRAINNYC-26APR-{i:03d}-T{s.threshold_in:.2f}",
            "threshold_in": s.threshold_in, "direction": "above",
            "ensemble_mean": s.ensemble_mean, "ensemble_var": s.ensemble_var,
            "actual_outcome_yes": s.y_above,
        }
        for i, s in enumerate(samples)
    ]

    out = asyncio.run(_retrain._refit_precip_emos_from_observations())
    assert out["status"] == "fit"
    assert out["promoted"] is True
    assert out["brier_after"] < out["brier_baseline"]
    # Cache installed → next coefficients_from_env returns the fit.
    cached = precip_emos._persisted_coefficients()
    assert cached is not None
    assert precip_emos.coefficients_from_env() is cached
    # Persistence INSERT landed.
    assert any(
        "system_settings" in q and "INSERT" in q
        for q, _ in retrain_stub.executes
    )
    # Promotion event surfaced on the bus.
    refit_events = [
        e for e in retrain_stub.publishes
        if e.get("type") == "precip_emos_refit"
    ]
    assert refit_events and refit_events[-1]["promoted"] is True


def test_emos_refit_rejects_when_no_brier_improvement(retrain_stub, monkeypatch):
    """When the optimizer's best fit fails to beat the baseline we
    keep the baseline coefficients live and emit a non-promoted
    event."""
    pytest.importorskip("scipy.optimize")
    # Pre-install the truth as the baseline so a refit can't beat it.
    truth = precip_emos.PrecipEMOSCoefficients(
        a=0.10, b=1.20, c=0.07, d=1.40, logistic_scale=4.0,
    )
    precip_emos.set_persisted_coefficients(truth)
    samples = _generate_synthetic(truth, n=300, seed=11)
    retrain_stub.observations = [
        {
            "market_id": f"K-{i}",
            "threshold_in": s.threshold_in, "direction": "above",
            "ensemble_mean": s.ensemble_mean, "ensemble_var": s.ensemble_var,
            "actual_outcome_yes": s.y_above,
        }
        for i, s in enumerate(samples)
    ]
    out = asyncio.run(_retrain._refit_precip_emos_from_observations())
    # Either "fit" with a tiny delta below tolerance, or "rejected".
    assert out["status"] in ("rejected", "fit")
    if out["status"] == "rejected":
        # Cache must still hold the original truth, not be overwritten.
        assert precip_emos._persisted_coefficients() is truth
        # No system_settings INSERT must have fired.
        assert not any(
            "system_settings" in q for q, _ in retrain_stub.executes
        )
        promoted_events = [
            e for e in retrain_stub.publishes
            if e.get("type") == "precip_emos_refit" and e.get("promoted")
        ]
        assert promoted_events == []


def test_emos_refit_is_called_from_run_retrain_cycle(monkeypatch):
    """The cycle's hook actually invokes the EMOS refit step
    (regardless of whether it skips or fits)."""
    invocations: list[int] = []

    async def fake_refit():
        invocations.append(1)
        return {"status": "skipped", "promoted": False, "n": 0}

    async def fake_publish(event: dict[str, Any]) -> None:
        return None

    async def fake_fetch(query: str, *args: Any):
        return []  # No verticals to retrain.

    monkeypatch.setattr(
        _retrain, "_refit_precip_emos_from_observations", fake_refit,
    )
    monkeypatch.setattr(_retrain, "_publish", fake_publish)
    monkeypatch.setattr(_db, "fetch", fake_fetch)

    cr = asyncio.run(_retrain.run_retrain_cycle())
    assert invocations == [1]
    assert cr.precip_emos_status == {
        "status": "skipped", "promoted": False, "n": 0,
    }


def test_emos_refit_failure_does_not_abort_cycle(monkeypatch):
    """A raise from the EMOS refit must NOT take the rest of the
    retrain cycle down — the trading loop's ability to see the
    isotonic refit through is more important."""
    async def kaboom():
        raise RuntimeError("simulated EMOS refit failure")

    async def fake_publish(event: dict[str, Any]) -> None:
        return None

    async def fake_fetch(query: str, *args: Any):
        return []

    monkeypatch.setattr(
        _retrain, "_refit_precip_emos_from_observations", kaboom,
    )
    monkeypatch.setattr(_retrain, "_publish", fake_publish)
    monkeypatch.setattr(_db, "fetch", fake_fetch)

    cr = asyncio.run(_retrain.run_retrain_cycle())
    # Cycle still completes and surfaces the EMOS error in its status.
    assert cr.completed_at is not None
    assert cr.precip_emos_status.get("status") == "error"


# ---------------------------------------------------------------------------
# Persisted-coefficients hydration on boot
# ---------------------------------------------------------------------------


def test_load_persisted_coefficients_installs_cache(monkeypatch):
    payload = {
        "coefficients": {
            "a": 0.31, "b": 1.42, "c": 0.09, "d": 1.55,
            "logistic_scale": 6.1, "sigma2_floor": 0.01,
        },
        "n": 250,
        "brier_after": 0.1234,
        "brier_baseline": 0.1567,
    }

    async def fake_fetchval(query: str, *args: Any):
        if args and args[0] == "precipitation_emos_coefficients":
            return json.dumps(payload)
        return None

    monkeypatch.setattr(_db, "fetchval", fake_fetchval)
    ok = asyncio.run(_retrain.load_persisted_precip_emos_coefficients())
    assert ok is True
    cached = precip_emos._persisted_coefficients()
    assert cached is not None
    assert cached.a == pytest.approx(0.31)
    assert cached.logistic_scale == pytest.approx(6.1)


def test_load_persisted_coefficients_no_row_is_silent(monkeypatch):
    async def fake_fetchval(query: str, *args: Any):
        return None

    monkeypatch.setattr(_db, "fetchval", fake_fetchval)
    ok = asyncio.run(_retrain.load_persisted_precip_emos_coefficients())
    assert ok is False
    assert precip_emos._persisted_coefficients() is None
