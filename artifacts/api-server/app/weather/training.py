"""Train + persist per-(city, metric) GRU + EMOS coefficients for the
temperature vertical (task #254).

The shipped predictor used to combine deterministic baseline GRU
weights with EMOS coefficients read from ``AXIOM_TEMP_EMOS_*``
environment variables.  Both are now refitted offline (or via the
settlement retrain hook) on real history:

  * ``load_history(city, metric, days=...)`` joins
    ``weather_hrrr_forecasts`` against per-day high/low aggregates
    of ``weather_observations`` and per-day climatology lookups
    (``app.feeds.weather_climatology``) to produce a list of
    training samples.  Each sample carries the ensemble of
    point-forecast values, the GRU input sequence, and the
    observed target temperature for that (city, metric, day).

  * ``train_one(city, metric, samples)`` fits the GRU output head
    (``W_out`` + ``b_out``) via closed-form least-squares on top of
    the deterministic baseline hidden weights — the rest of the
    GRU stays at baseline (the hidden layer is only a low-pass
    smoother) — and fits the EMOS regression coefficients
    (``a, b, b_g, c, d, d_g``) on the residual structure of the
    ensemble.

  * Held-out Brier is computed against the same baseline that the
    pre-training pipeline produced and is written into the snapshot
    row alongside the trained coefficients.  The trainer refuses to
    promote a fit whose held-out Brier doesn't beat the deterministic
    baseline (so a degenerate refit can never silently regress the
    live predictor).

  * ``save_to_db`` / ``load_from_db`` round-trip the trained
    artefacts through ``weather_temp_trained_models`` (created by
    migration ``0017_weather_temp_trained_models``).  The live
    predictor consults this table at request time.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

import numpy as np

from .. import db
from ..feeds import weather_climatology as clim
from . import emos as emos_mod
from . import gru as gru_mod
from .tickers import SUPPORTED_CITIES, SUPPORTED_METRICS


log = logging.getLogger(__name__)


# Minimum number of joined samples we require before fitting per
# (city, metric).  Below this the regression is too noisy to
# meaningfully beat the deterministic baseline.
MIN_TRAINING_SAMPLES = 10
DEFAULT_HISTORY_DAYS = 90
DEFAULT_HOLDOUT_FRAC = 0.25


# ---------------------------------------------------------------------------
# History loading
# ---------------------------------------------------------------------------


@dataclass
class TrainingSample:
    """A single (city, metric, forecast_date) training example."""

    city: str
    metric: str
    forecast_date: date
    ensemble: list[float]            # multi-source point forecasts (°F)
    sequence: list[list[float]]      # GRU input sequence (T x 8)
    observed_f: float                # ground-truth daily high / low / temp (°F)
    threshold_f: float               # synthetic threshold for Brier eval


def _decode_jsonb(blob: Any) -> Any:
    if blob is None:
        return None
    if isinstance(blob, (dict, list)):
        return blob
    if isinstance(blob, str):
        try:
            return json.loads(blob)
        except json.JSONDecodeError:
            return None
    return None


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


async def _aggregate_daily_observed(
    city: str, target_date: date, *, metric: str,
) -> float | None:
    """Aggregate observed temperatures for ``target_date`` into a daily
    high / low / mean.  We pull every ``weather_observations`` row
    whose ``observed_at`` falls inside the local-day window
    [target_date 00Z, target_date+1 00Z); UTC is good enough — the
    Kalshi contracts are settled on the local day but the resulting
    bias is a small offset that EMOS absorbs into ``a``.
    """
    start = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    metric = metric.upper()
    if metric == "HIGH":
        agg = "MAX"
    elif metric == "LOW":
        agg = "MIN"
    else:
        agg = "AVG"
    try:
        row = await db.fetchrow(
            f"""
            SELECT {agg}(temperature_f)::float AS v
              FROM weather_observations
             WHERE city_code = $1
               AND temperature_f IS NOT NULL
               AND observed_at >= $2
               AND observed_at <  $3
            """,
            city, start, end,
        )
    except Exception as e:
        log.debug("[temp-train] daily-obs aggregate failed (%s/%s/%s): %s",
                  city, metric, target_date, e)
        return None
    if row is None:
        return None
    return _safe_float(row["v"])


async def _hrrr_snapshot(
    city: str, forecast_date: date,
) -> dict[str, Any] | None:
    """Pull the most-recent HRRR forecast snapshot stored for the
    given (city, forecast_date)."""
    try:
        row = await db.fetchrow(
            """
            SELECT hrrr_high_f, hrrr_hourly, hrrr_run_time, fetched_at
              FROM weather_hrrr_forecasts
             WHERE city_code = $1 AND forecast_date = $2
             ORDER BY fetched_at DESC
             LIMIT 1
            """,
            city, forecast_date,
        )
    except Exception as e:
        log.debug("[temp-train] hrrr snapshot failed (%s/%s): %s",
                  city, forecast_date, e)
        return None
    if row is None:
        return None
    body = _decode_jsonb(row["hrrr_hourly"]) or {}
    return {
        "hrrr_tmax_f": _safe_float(row["hrrr_high_f"]),
        "hrrr_t2m_f": _safe_float(body.get("t2m_f")),
        "hrrr_tmin_f": _safe_float(body.get("tmin_f")),
        "samples": [s for s in (body.get("samples") or [])
                    if isinstance(s, Mapping) and s.get("f") is not None],
        "run_time": row["hrrr_run_time"],
    }


async def _anchor_observation(
    city: str, source: str, target_date: date,
) -> float | None:
    """Find the observation for ``source`` closest to ``target_date``
    (looking back up to 36h), used as the "current" anchor at
    forecast time."""
    cutoff = datetime.combine(
        target_date, datetime.min.time(), tzinfo=timezone.utc,
    )
    floor = cutoff - timedelta(hours=36)
    try:
        row = await db.fetchrow(
            """
            SELECT temperature_f
              FROM weather_observations
             WHERE city_code = $1 AND source = $2
               AND temperature_f IS NOT NULL
               AND observed_at >= $3 AND observed_at < $4
             ORDER BY observed_at DESC
             LIMIT 1
            """,
            city, source, floor, cutoff,
        )
    except Exception as e:
        log.debug("[temp-train] anchor obs failed (%s/%s): %s", city, source, e)
        return None
    if row is None:
        return None
    return _safe_float(row["temperature_f"])


def _ensemble_for_metric(
    metric: str,
    *,
    hrrr: dict[str, Any] | None,
    om: float | None,
    nws: float | None,
    tio: float | None,
    clim_tmax: float | None,
    clim_tmin: float | None,
    clim_tavg: float | None,
) -> list[float]:
    """Mirror ``predictor._ensemble_for_metric`` so training and
    inference share exactly the same ensemble composition."""
    metric = metric.upper()
    h_tmax = (hrrr or {}).get("hrrr_tmax_f")
    h_t2m = (hrrr or {}).get("hrrr_t2m_f")
    h_tmin = (hrrr or {}).get("hrrr_tmin_f")
    if metric == "HIGH":
        candidates = [h_tmax, clim_tmax, om, nws, tio]
    elif metric == "LOW":
        candidates = [h_tmin, clim_tmin, om, nws, tio]
    else:
        candidates = [om, nws, h_t2m, tio, clim_tavg]
    return [float(v) for v in candidates if v is not None]


def _build_sequence(
    *,
    forecast_date: date,
    hrrr: dict[str, Any] | None,
    om: float | None,
    nws: float | None,
    clim_tmax: float | None,
    clim_tavg: float | None,
) -> list[list[float]]:
    """Build the GRU input sequence consistent with
    ``predictor._build_sequence`` so a sample's training-time features
    match the at-inference features."""
    hour_n = 0.5  # daily contracts: anchor mid-day
    doy_n = (int(forecast_date.strftime("%j")) - 1) / 365.0
    h_t2m = (hrrr or {}).get("hrrr_t2m_f")
    h_tmax = (hrrr or {}).get("hrrr_tmax_f")
    h_tmin = (hrrr or {}).get("hrrr_tmin_f")
    base_now = next(
        (v for v in (om, nws, h_t2m, clim_tavg, 50.0) if v is not None),
        50.0,
    )
    samples = (hrrr or {}).get("samples") or []
    if samples:
        seq: list[list[float]] = []
        for s in samples[:12]:
            t_step = _safe_float(s.get("f")) or base_now
            seq.append([
                float(t_step),
                float(h_t2m if h_t2m is not None else t_step),
                float(h_tmax if h_tmax is not None else 60.0),
                float(h_tmin if h_tmin is not None else 40.0),
                float(clim_tmax if clim_tmax is not None else 60.0),
                float(clim_tavg if clim_tavg is not None else 50.0),
                hour_n,
                doy_n,
            ])
        return seq
    return [[
        float(base_now),
        float(h_t2m if h_t2m is not None else base_now),
        float(h_tmax if h_tmax is not None else 60.0),
        float(h_tmin if h_tmin is not None else 40.0),
        float(clim_tmax if clim_tmax is not None else 60.0),
        float(clim_tavg if clim_tavg is not None else 50.0),
        hour_n,
        doy_n,
    ]]


async def _candidate_dates(city: str, days: int) -> list[date]:
    """Return the set of forecast_dates for which we have at least one
    HRRR snapshot for ``city`` in the trailing ``days`` window."""
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)
    try:
        rows = await db.fetch(
            """
            SELECT DISTINCT forecast_date
              FROM weather_hrrr_forecasts
             WHERE city_code = $1
               AND forecast_date >= $2
               AND forecast_date <= $3
             ORDER BY forecast_date ASC
            """,
            city, start, end,
        )
    except Exception as e:
        log.debug("[temp-train] candidate dates failed (%s): %s", city, e)
        return []
    return [r["forecast_date"] for r in rows]


async def load_history(
    city: str,
    metric: str,
    *,
    days: int = DEFAULT_HISTORY_DAYS,
) -> list[TrainingSample]:
    """Build a chronological list of joined training samples for
    (city, metric).  Samples without an HRRR snapshot, climatology
    baseline, or observed daily high/low are dropped silently."""
    metric = metric.upper()
    if metric not in SUPPORTED_METRICS:
        raise ValueError(f"unsupported metric {metric!r}")
    if city not in SUPPORTED_CITIES:
        raise ValueError(f"unsupported city {city!r}")
    candidate_dates = await _candidate_dates(city, days)
    samples: list[TrainingSample] = []
    for d in candidate_dates:
        observed = await _aggregate_daily_observed(city, d, metric=metric)
        if observed is None:
            continue
        hrrr = await _hrrr_snapshot(city, d)
        om = await _anchor_observation(city, "open-meteo", d)
        nws = await _anchor_observation(city, "nws", d)
        tio = await _anchor_observation(city, "tomorrow.io", d)
        doy = int(d.strftime("%j"))
        c_tmax = await clim.get_climatology(city, doy, clim.METRIC_TMAX)
        c_tmin = await clim.get_climatology(city, doy, clim.METRIC_TMIN)
        c_tavg = await clim.get_climatology(city, doy, clim.METRIC_TAVG)
        ensemble = _ensemble_for_metric(
            metric,
            hrrr=hrrr, om=om, nws=nws, tio=tio,
            clim_tmax=_safe_float(c_tmax),
            clim_tmin=_safe_float(c_tmin),
            clim_tavg=_safe_float(c_tavg),
        )
        if not ensemble:
            continue
        sequence = _build_sequence(
            forecast_date=d, hrrr=hrrr, om=om, nws=nws,
            clim_tmax=_safe_float(c_tmax),
            clim_tavg=_safe_float(c_tavg),
        )
        threshold = float(observed) + ((doy % 7) - 3) * 0.5
        samples.append(TrainingSample(
            city=city,
            metric=metric,
            forecast_date=d,
            ensemble=[float(v) for v in ensemble],
            sequence=[[float(x) for x in row] for row in sequence],
            observed_f=float(observed),
            threshold_f=threshold,
        ))
    return samples


# ---------------------------------------------------------------------------
# GRU output-head fit (closed-form least-squares on top of baseline)
# ---------------------------------------------------------------------------


def _ensemble_mean(values: Sequence[float]) -> float:
    n = len(values)
    return float(sum(values) / n) if n else float("nan")


def _ensemble_var(values: Sequence[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    m = _ensemble_mean(values)
    return float(sum((v - m) ** 2 for v in values) / (n - 1))


def _final_hidden(sequence: Sequence[Sequence[float]],
                  weights: gru_mod.GRUWeights) -> list[float]:
    """Forward the GRU and return only the final hidden state — used
    as features for the linear regression of the output head."""
    h: list[float] = [0.0] * weights.n_hidden
    for x in sequence:
        if len(x) != weights.n_input:
            x = list(x)[: weights.n_input] + [0.0] * max(
                0, weights.n_input - len(x))
        h = gru_mod._step(x, h, weights)  # noqa: SLF001 (intentional reuse)
    return h


def _fit_gru_output_head(
    samples: Sequence[TrainingSample],
    base_weights: gru_mod.GRUWeights,
) -> gru_mod.GRUWeights:
    """Fit ``W_out`` (2 x H) and ``b_out`` (2,) as a closed-form
    linear regression on top of the baseline GRU.

    ``forward()`` does ``point_bias_f = clamp(out[0] * 3.0, ±5)`` and
    ``spread_f = clamp(exp(out[1]), [0.5, 12])``; we therefore fit
    ``out[0]`` against ``(observed - ensemble_mean) / 3.0`` and
    ``out[1]`` against ``log(max(0.5, |observed - ensemble_mean|))``.
    All non-output GRU weights stay at their deterministic baseline
    — the hidden layer is a low-pass smoother and re-fitting it
    closed-form would require a real backprop pass we explicitly
    avoid (see ``app.weather.gru`` module docstring).
    """
    H = base_weights.n_hidden
    h_rows: list[list[float]] = []
    point_targets: list[float] = []
    spread_targets: list[float] = []
    for s in samples:
        h = _final_hidden(s.sequence, base_weights)
        h_rows.append(h)
        em_mean = _ensemble_mean(s.ensemble)
        residual = s.observed_f - em_mean
        point_targets.append(residual / 3.0)
        spread_targets.append(math.log(max(0.5, abs(residual))))

    # Design matrix: [1, h_1, ..., h_H]
    X = np.hstack([np.ones((len(samples), 1)), np.array(h_rows)])
    yp = np.array(point_targets)
    ys = np.array(spread_targets)
    # Tiny ridge — the GRU outputs of nearly-equal samples can leave
    # X poorly conditioned.
    reg = 1e-4 * np.eye(X.shape[1])
    A = X.T @ X + reg
    coef_p = np.linalg.solve(A, X.T @ yp)
    coef_s = np.linalg.solve(A, X.T @ ys)

    # coef = [intercept, w_1, ..., w_H]
    new_b = [float(coef_p[0]), float(coef_s[0])]
    new_W = [
        [float(coef_p[i + 1]) for i in range(H)],
        [float(coef_s[i + 1]) for i in range(H)],
    ]
    trained = gru_mod.GRUWeights(
        n_input=base_weights.n_input,
        n_hidden=H,
        W_ir=base_weights.W_ir, W_iz=base_weights.W_iz, W_in=base_weights.W_in,
        W_hr=base_weights.W_hr, W_hz=base_weights.W_hz, W_hn=base_weights.W_hn,
        b_ir=base_weights.b_ir, b_iz=base_weights.b_iz, b_in=base_weights.b_in,
        b_hr=base_weights.b_hr, b_hz=base_weights.b_hz, b_hn=base_weights.b_hn,
        W_out=new_W,
        b_out=new_b,
        input_mean=list(base_weights.input_mean),
        input_std=list(base_weights.input_std),
    )
    return trained


# ---------------------------------------------------------------------------
# EMOS (NGR) coefficient fit
# ---------------------------------------------------------------------------


def _fit_emos(
    samples: Sequence[TrainingSample],
    gru_outputs: Sequence[dict[str, float]],
) -> emos_mod.EMOSCoefficients:
    """Fit (a, b, b_g, c, d, d_g) via closed-form OLS.

    Mean: ``mu = a + b * em_mean + b_g * (em_mean + g_point)``
    rearranges to ``mu = a + (b + b_g) * em_mean + b_g * g_point``.
    We regress observed onto ``[1, em_mean, g_point]`` to recover
    (a, β1, β2) and back out b = β1 - β2, b_g = β2.

    Variance: residual^2 ≈ c + d * em_var + d_g * g_var, fit by OLS
    with positivity floors so the live predictor never sees a
    negative ``sigma2``.
    """
    n = len(samples)
    em_means = np.array([_ensemble_mean(s.ensemble) for s in samples])
    em_vars = np.array([_ensemble_var(s.ensemble) for s in samples])
    g_points = np.array([float(o["point_bias_f"]) for o in gru_outputs])
    g_vars = np.array([float(o["spread_f"]) ** 2 for o in gru_outputs])
    obs = np.array([s.observed_f for s in samples])

    Xm = np.column_stack([np.ones(n), em_means, g_points])
    reg = 1e-4 * np.eye(Xm.shape[1])
    coef_m = np.linalg.solve(Xm.T @ Xm + reg, Xm.T @ obs)
    a = float(coef_m[0])
    beta1 = float(coef_m[1])
    beta2 = float(coef_m[2])
    b = beta1 - beta2
    b_g = beta2

    mu_pred = a + (b + b_g) * em_means + b_g * g_points
    sq_resid = (obs - mu_pred) ** 2
    Xv = np.column_stack([np.ones(n), em_vars, g_vars])
    coef_v = np.linalg.solve(Xv.T @ Xv + reg, Xv.T @ sq_resid)
    c, d, d_g = (float(coef_v[0]), float(coef_v[1]), float(coef_v[2]))
    # Clamp to sane positive ranges — the live predictor floors sigma2
    # at 0.25 anyway, but a negative slope would make the variance
    # *shrink* on noisier ensembles which is the wrong sign.
    c = max(0.5, c)
    d = max(0.0, min(d, 5.0))
    d_g = max(0.0, min(d_g, 5.0))
    return emos_mod.EMOSCoefficients(a=a, b=b, b_g=b_g, c=c, d=d, d_g=d_g)


# ---------------------------------------------------------------------------
# Brier evaluation
# ---------------------------------------------------------------------------


def _outcome_above(observed: float, threshold: float) -> int:
    return 1 if observed > threshold else 0


def evaluate_brier(
    samples: Sequence[TrainingSample],
    weights: gru_mod.GRUWeights,
    coefficients: emos_mod.EMOSCoefficients,
) -> float:
    """Brier on ``samples`` using the supplied (gru, emos) pair."""
    if not samples:
        return float("nan")
    total = 0.0
    for s in samples:
        gru_out = gru_mod.forward(s.sequence, weights)
        em = emos_mod.apply_emos(
            ensemble=s.ensemble,
            threshold_f=s.threshold_f,
            gru_point_f=gru_out["point_bias_f"],
            gru_spread_f=gru_out["spread_f"],
            coefficients=coefficients,
        )
        y = _outcome_above(s.observed_f, s.threshold_f)
        total += (em.p_above - y) ** 2
    return total / len(samples)


def baseline_brier(samples: Sequence[TrainingSample]) -> float:
    """Brier of the deterministic-baseline pipeline that this trainer
    is replacing — baseline GRU + env-default EMOS coefficients."""
    base_weights = gru_mod.baseline_weights()
    base_coef = emos_mod.EMOSCoefficients()  # dataclass defaults match env defaults
    return evaluate_brier(samples, base_weights, base_coef)


# ---------------------------------------------------------------------------
# End-to-end training entry point
# ---------------------------------------------------------------------------


@dataclass
class TrainResult:
    city: str
    metric: str
    n_samples: int
    n_train: int
    n_holdout: int
    weights: gru_mod.GRUWeights | None = None
    coefficients: emos_mod.EMOSCoefficients | None = None
    baseline_brier: float | None = None
    holdout_brier: float | None = None
    train_brier: float | None = None
    promoted: bool = False
    error: str | None = None
    fit_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def train_one(
    samples: Sequence[TrainingSample],
    *,
    holdout_frac: float = DEFAULT_HOLDOUT_FRAC,
) -> TrainResult:
    """Fit the GRU output head + EMOS coefficients on the head of
    ``samples``, evaluate on the tail, and return a ``TrainResult``."""
    if not samples:
        raise ValueError("train_one called with no samples")
    city = samples[0].city
    metric = samples[0].metric
    n = len(samples)
    if n < MIN_TRAINING_SAMPLES:
        return TrainResult(
            city=city, metric=metric, n_samples=n, n_train=0, n_holdout=0,
            error=f"too few samples ({n} < {MIN_TRAINING_SAMPLES})",
        )

    n_holdout = max(2, int(round(n * holdout_frac)))
    n_train = max(MIN_TRAINING_SAMPLES, n - n_holdout)
    if n_train + n_holdout > n:
        n_holdout = n - n_train
    train = list(samples[:n_train])
    holdout = list(samples[n_train: n_train + n_holdout])

    base = gru_mod.baseline_weights()
    weights = _fit_gru_output_head(train, base)
    gru_outs = [gru_mod.forward(s.sequence, weights) for s in train]
    coef = _fit_emos(train, gru_outs)

    train_brier = evaluate_brier(train, weights, coef)
    hold_brier = evaluate_brier(holdout, weights, coef) if holdout else float("nan")
    base_brier = baseline_brier(holdout) if holdout else baseline_brier(train)

    # Refuse to promote a regression — the trainer must prove a strict
    # improvement on the held-out tail before the live predictor swaps
    # the coefficients.
    promoted = (
        not math.isnan(hold_brier)
        and not math.isnan(base_brier)
        and hold_brier < base_brier
    )

    return TrainResult(
        city=city, metric=metric,
        n_samples=n, n_train=n_train, n_holdout=len(holdout),
        weights=weights,
        coefficients=coef,
        baseline_brier=float(base_brier) if not math.isnan(base_brier) else None,
        holdout_brier=float(hold_brier) if not math.isnan(hold_brier) else None,
        train_brier=float(train_brier) if not math.isnan(train_brier) else None,
        promoted=promoted,
    )


# ---------------------------------------------------------------------------
# Persistence (DB round-trip)
# ---------------------------------------------------------------------------


def _gru_to_dict(w: gru_mod.GRUWeights) -> dict[str, Any]:
    return {
        "n_input": int(w.n_input),
        "n_hidden": int(w.n_hidden),
        "W_ir": w.W_ir, "W_iz": w.W_iz, "W_in": w.W_in,
        "W_hr": w.W_hr, "W_hz": w.W_hz, "W_hn": w.W_hn,
        "b_ir": w.b_ir, "b_iz": w.b_iz, "b_in": w.b_in,
        "b_hr": w.b_hr, "b_hz": w.b_hz, "b_hn": w.b_hn,
        "W_out": w.W_out, "b_out": w.b_out,
        "input_mean": w.input_mean, "input_std": w.input_std,
    }


def _gru_from_dict(d: Mapping[str, Any]) -> gru_mod.GRUWeights:
    return gru_mod.GRUWeights(
        n_input=int(d["n_input"]),
        n_hidden=int(d["n_hidden"]),
        W_ir=[[float(x) for x in row] for row in d["W_ir"]],
        W_iz=[[float(x) for x in row] for row in d["W_iz"]],
        W_in=[[float(x) for x in row] for row in d["W_in"]],
        W_hr=[[float(x) for x in row] for row in d["W_hr"]],
        W_hz=[[float(x) for x in row] for row in d["W_hz"]],
        W_hn=[[float(x) for x in row] for row in d["W_hn"]],
        b_ir=[float(x) for x in d["b_ir"]],
        b_iz=[float(x) for x in d["b_iz"]],
        b_in=[float(x) for x in d["b_in"]],
        b_hr=[float(x) for x in d["b_hr"]],
        b_hz=[float(x) for x in d["b_hz"]],
        b_hn=[float(x) for x in d["b_hn"]],
        W_out=[[float(x) for x in row] for row in d["W_out"]],
        b_out=[float(x) for x in d["b_out"]],
        input_mean=[float(x) for x in d["input_mean"]],
        input_std=[float(x) for x in d["input_std"]],
    )


def _coef_to_dict(c: emos_mod.EMOSCoefficients) -> dict[str, float]:
    return {"a": c.a, "b": c.b, "b_g": c.b_g,
            "c": c.c, "d": c.d, "d_g": c.d_g}


def _coef_from_dict(d: Mapping[str, Any]) -> emos_mod.EMOSCoefficients:
    return emos_mod.EMOSCoefficients(
        a=float(d.get("a", 0.0)),
        b=float(d.get("b", 0.92)),
        b_g=float(d.get("b_g", 0.08)),
        c=float(d.get("c", 1.5)),
        d=float(d.get("d", 0.55)),
        d_g=float(d.get("d_g", 0.40)),
    )


async def save_to_db(result: TrainResult) -> None:
    """Upsert a ``TrainResult`` into ``weather_temp_trained_models``.

    Refuses to write a row that wasn't promoted (failed the held-out
    Brier gate) so the live predictor never reads a regression."""
    if not result.promoted or result.weights is None or result.coefficients is None:
        log.info(
            "[temp-train] %s/%s not promoted (holdout=%.4f baseline=%.4f) — "
            "skipping DB write",
            result.city, result.metric,
            result.holdout_brier or float("nan"),
            result.baseline_brier or float("nan"),
        )
        return
    metrics = {
        "n_train": result.n_train,
        "n_holdout": result.n_holdout,
        "train_brier": result.train_brier,
        "holdout_brier": result.holdout_brier,
        "baseline_brier": result.baseline_brier,
        "fit_at": result.fit_at,
    }
    await db.execute(
        """
        INSERT INTO weather_temp_trained_models
            (city_code, metric, gru_weights, emos_coef, fit_metrics,
             n_samples, n_holdout, baseline_brier, holdout_brier, fit_at)
        VALUES ($1, $2, $3::jsonb, $4::jsonb, $5::jsonb, $6, $7, $8, $9, now())
        ON CONFLICT (city_code, metric) DO UPDATE SET
            gru_weights    = EXCLUDED.gru_weights,
            emos_coef      = EXCLUDED.emos_coef,
            fit_metrics    = EXCLUDED.fit_metrics,
            n_samples      = EXCLUDED.n_samples,
            n_holdout      = EXCLUDED.n_holdout,
            baseline_brier = EXCLUDED.baseline_brier,
            holdout_brier  = EXCLUDED.holdout_brier,
            fit_at         = now()
        """,
        result.city,
        result.metric,
        json.dumps(_gru_to_dict(result.weights)),
        json.dumps(_coef_to_dict(result.coefficients)),
        json.dumps(metrics, default=str),
        int(result.n_samples),
        int(result.n_holdout),
        result.baseline_brier,
        result.holdout_brier,
    )
    log.info(
        "[temp-train] persisted %s/%s — holdout=%.4f vs baseline=%.4f "
        "(%.1f%% improvement, n=%d)",
        result.city, result.metric,
        result.holdout_brier, result.baseline_brier,
        100.0 * (result.baseline_brier - result.holdout_brier)
        / max(1e-9, result.baseline_brier),
        result.n_samples,
    )


@dataclass(frozen=True)
class TrainedRecord:
    city: str
    metric: str
    weights: gru_mod.GRUWeights
    coefficients: emos_mod.EMOSCoefficients
    n_samples: int
    baseline_brier: float | None
    holdout_brier: float | None
    fit_at: datetime | None


async def load_from_db(city: str, metric: str) -> TrainedRecord | None:
    """Read a trained (GRUWeights, EMOSCoefficients) pair from the DB."""
    try:
        row = await db.fetchrow(
            """
            SELECT gru_weights, emos_coef, n_samples,
                   baseline_brier, holdout_brier, fit_at
              FROM weather_temp_trained_models
             WHERE city_code = $1 AND metric = $2
            """,
            city, metric.upper(),
        )
    except Exception as e:
        log.debug("[temp-train] load_from_db failed (%s/%s): %s",
                  city, metric, e)
        return None
    if row is None:
        return None
    gru_dict = _decode_jsonb(row["gru_weights"])
    coef_dict = _decode_jsonb(row["emos_coef"])
    if not isinstance(gru_dict, Mapping) or not isinstance(coef_dict, Mapping):
        log.warning(
            "[temp-train] %s/%s row has malformed JSONB — falling back to "
            "untrained predictor", city, metric,
        )
        return None
    try:
        weights = _gru_from_dict(gru_dict)
        coef = _coef_from_dict(coef_dict)
    except Exception as e:  # pragma: no cover — defensive
        log.warning("[temp-train] %s/%s deserialize failed: %s",
                    city, metric, e)
        return None
    return TrainedRecord(
        city=city,
        metric=metric.upper(),
        weights=weights,
        coefficients=coef,
        n_samples=int(row["n_samples"] or 0),
        baseline_brier=_safe_float(row["baseline_brier"]),
        holdout_brier=_safe_float(row["holdout_brier"]),
        fit_at=row["fit_at"],
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def train_and_persist(
    city: str, metric: str,
    *,
    days: int = DEFAULT_HISTORY_DAYS,
    holdout_frac: float = DEFAULT_HOLDOUT_FRAC,
) -> TrainResult:
    samples = await load_history(city, metric, days=days)
    if len(samples) < MIN_TRAINING_SAMPLES:
        return TrainResult(
            city=city, metric=metric.upper(),
            n_samples=len(samples), n_train=0, n_holdout=0,
            error=f"too few joined samples ({len(samples)} < "
                  f"{MIN_TRAINING_SAMPLES})",
        )
    result = train_one(samples, holdout_frac=holdout_frac)
    if result.promoted:
        await save_to_db(result)
    return result


async def train_all(
    *,
    cities: Sequence[str] | None = None,
    metrics: Sequence[str] | None = None,
    days: int = DEFAULT_HISTORY_DAYS,
    holdout_frac: float = DEFAULT_HOLDOUT_FRAC,
) -> list[TrainResult]:
    """Train every supported (city, metric) pair (or the supplied
    subset)."""
    cities = list(cities) if cities else list(SUPPORTED_CITIES)
    metrics = list(metrics) if metrics else list(SUPPORTED_METRICS)
    out: list[TrainResult] = []
    for city in cities:
        for metric in metrics:
            try:
                res = await train_and_persist(
                    city, metric, days=days, holdout_frac=holdout_frac,
                )
            except Exception as e:
                log.warning("[temp-train] %s/%s raised: %s", city, metric, e)
                res = TrainResult(
                    city=city, metric=metric.upper(),
                    n_samples=0, n_train=0, n_holdout=0,
                    error=f"{type(e).__name__}: {e}",
                )
            out.append(res)
    return out


__all__ = [
    "DEFAULT_HISTORY_DAYS",
    "DEFAULT_HOLDOUT_FRAC",
    "MIN_TRAINING_SAMPLES",
    "TrainingSample",
    "TrainResult",
    "TrainedRecord",
    "baseline_brier",
    "evaluate_brier",
    "load_from_db",
    "load_history",
    "save_to_db",
    "train_all",
    "train_and_persist",
    "train_one",
]
