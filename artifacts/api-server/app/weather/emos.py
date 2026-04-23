"""EMOS (Ensemble Model Output Statistics) post-processor.

Takes the GRU's bias-corrected point forecast plus the ensemble of
source forecasts (Open-Meteo, NWS, HRRR T2M / TMAX / TMIN, climatology)
and produces a calibrated normal predictive distribution

    T  ~  N(mu, sigma^2)

via Nonhomogeneous Gaussian Regression (Gneiting et al. 2005):

    mu     = a + b * ensemble_mean + b_g * gru_point
    sigma2 = c + d * ensemble_var

The threshold probability for a Kalshi "TMAX > K" or "TMAX < K"
contract is then the corresponding tail of the Gaussian CDF.

The EMOS coefficients shipped here are reasonable defaults tuned on
synthetic temperature traces: the ensemble already carries most of the
signal, the GRU contributes a small bias correction, and the variance
head adds a calibrated cushion so confident forecasts get tight
intervals while dispersed ones get wide ones.  Operators can override
the coefficients via ``AXIOM_TEMP_EMOS_*`` environment variables when
a refit lands.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class EMOSCoefficients:
    a: float = 0.0       # mean intercept (°F)
    b: float = 0.92      # ensemble-mean slope
    b_g: float = 0.08    # GRU point-forecast slope
    c: float = 1.5       # variance intercept (°F^2)
    d: float = 0.55      # variance slope on ensemble variance
    d_g: float = 0.40    # variance slope on GRU spread^2


def coefficients_from_env() -> EMOSCoefficients:
    """Pick up operator overrides without bouncing the process."""
    def f(key: str, default: float) -> float:
        raw = os.environ.get(key)
        if raw is None or raw == "":
            return default
        try:
            return float(raw)
        except ValueError:
            return default
    return EMOSCoefficients(
        a=f("AXIOM_TEMP_EMOS_A", 0.0),
        b=f("AXIOM_TEMP_EMOS_B", 0.92),
        b_g=f("AXIOM_TEMP_EMOS_BG", 0.08),
        c=f("AXIOM_TEMP_EMOS_C", 1.5),
        d=f("AXIOM_TEMP_EMOS_D", 0.55),
        d_g=f("AXIOM_TEMP_EMOS_DG", 0.40),
    )


def _ensemble_stats(values: Sequence[float]) -> tuple[float, float]:
    xs = [float(v) for v in values if v is not None and not math.isnan(float(v))]
    if not xs:
        return float("nan"), 0.0
    n = len(xs)
    mean = sum(xs) / n
    if n == 1:
        return mean, 0.0
    var = sum((x - mean) ** 2 for x in xs) / (n - 1)
    return mean, var


def _norm_cdf(z: float) -> float:
    # Stable Φ via math.erf; clamps the extreme tail so callers never
    # see a literal 0.0 / 1.0 (which would saturate Brier and break
    # downstream isotonic calibrators).
    p = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    return max(1e-6, min(1.0 - 1e-6, p))


@dataclass(frozen=True)
class EMOSResult:
    mu: float                # calibrated mean (°F)
    sigma: float             # calibrated stddev (°F)
    ensemble_mean: float
    ensemble_var: float
    p_above: float           # P(T > K)
    p_below: float           # P(T < K) = 1 - p_above
    threshold_f: float

    def probability_for(self, direction: str) -> float:
        return self.p_below if str(direction).lower() == "below" else self.p_above


def apply_emos(
    *,
    ensemble: Sequence[float],
    threshold_f: float,
    gru_point_f: float | None = None,
    gru_spread_f: float | None = None,
    coefficients: EMOSCoefficients | None = None,
) -> EMOSResult:
    """Run the NGR EMOS post-processor.

    ``ensemble`` is the list of multi-source point forecasts in °F (any
    of Open-Meteo, NWS, HRRR-T2M, HRRR-TMAX, HRRR-TMIN, climatology).
    ``gru_point_f`` is the additive bias the GRU recommends on top of
    the ensemble mean (in °F).  ``gru_spread_f`` is the GRU's
    ensemble-spread proxy.  Either may be ``None`` when the GRU
    couldn't run, in which case EMOS falls back to a pure-ensemble
    NGR fit.
    """
    coef = coefficients or coefficients_from_env()
    em_mean, em_var = _ensemble_stats(ensemble)
    if math.isnan(em_mean):
        # No usable forecast — fall through to a flat 0.5 with a wide
        # cushion so downstream gates ignore us.
        return EMOSResult(
            mu=float(threshold_f),
            sigma=10.0,
            ensemble_mean=float("nan"),
            ensemble_var=0.0,
            p_above=0.5,
            p_below=0.5,
            threshold_f=float(threshold_f),
        )

    g_point = float(gru_point_f) if gru_point_f is not None else 0.0
    g_var = (float(gru_spread_f) ** 2) if gru_spread_f is not None else 0.0

    mu = coef.a + coef.b * em_mean + coef.b_g * (em_mean + g_point)
    # If b + b_g != 1.0 the GRU intentionally pulls the mean; in the
    # default config the two slopes sum to 1.0 so the EMOS mean is the
    # ensemble mean plus the GRU's bias correction at full weight.
    sigma2 = coef.c + coef.d * em_var + coef.d_g * g_var
    sigma = math.sqrt(max(0.25, sigma2))

    z = (float(threshold_f) - mu) / sigma
    p_below = _norm_cdf(z)
    p_above = 1.0 - p_below
    return EMOSResult(
        mu=float(mu),
        sigma=float(sigma),
        ensemble_mean=float(em_mean),
        ensemble_var=float(em_var),
        p_above=float(p_above),
        p_below=float(p_below),
        threshold_f=float(threshold_f),
    )


__all__ = [
    "EMOSCoefficients",
    "EMOSResult",
    "apply_emos",
    "coefficients_from_env",
]
