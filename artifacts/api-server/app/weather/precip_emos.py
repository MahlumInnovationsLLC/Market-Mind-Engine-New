"""EMOS-style post-processor for daily precipitation forecasts.

Mirrors the temperature ``emos`` module but swaps the normal CDF tail
for a *gamma / logistic* link, since precipitation accumulation is a
non-negative, right-skewed quantity:

* The ensemble of source point forecasts (Open-Meteo precip-1h,
  Tomorrow.io precip-1h, AccuWeather minutecast intensity, optional
  HRRR daily QPF) is summarised as ensemble-mean and ensemble-variance
  in inches.
* Linear EMOS correction:
      mu     = a + b * ensemble_mean
      sigma2 = c + d * ensemble_var      (clamped to a small floor)
* For the K=0 contract ("any precipitation today"), use a *logistic*
  link on ``mu`` so a small but non-zero forecast still maps to a
  bounded probability rather than collapsing to 0/1.
* For K>0, fit a Gamma(shape=alpha, scale=theta) by method of moments
  to (mu, sigma2) and read off the survival function::

      P(Y > K) = 1 - F_Gamma(K; alpha, theta)

  The regularised lower incomplete gamma is computed via the standard
  Numerical-Recipes series + continued-fraction expansions so the call
  stays pure-Python (no SciPy dependency in the trading loop).

Operators can override the EMOS coefficients via
``AXIOM_PRECIP_EMOS_*`` environment variables when a refit lands.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Sequence


# ---------------------------------------------------------------------------
# Coefficients
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrecipEMOSCoefficients:
    a: float = 0.0       # mean intercept (inches)
    b: float = 1.0       # ensemble-mean slope
    c: float = 0.04      # variance intercept (inches^2) — small but non-zero
    d: float = 1.20      # variance slope on ensemble variance
    # Logistic-link scale used for the K=0 ("any precip") contract.
    # Chosen so a 0.05" forecast already maps to ~0.62 P(any precip)
    # — i.e. the operator-perceptible "rain is on the way" threshold.
    logistic_scale: float = 5.0
    # Floor on EMOS sigma^2 so a degenerate (single-source, agreeing-
    # forecasts) ensemble still produces a non-saturating gamma tail.
    sigma2_floor: float = 0.01


def coefficients_from_env() -> PrecipEMOSCoefficients:
    def f(key: str, default: float) -> float:
        raw = os.environ.get(key)
        if raw is None or raw == "":
            return default
        try:
            return float(raw)
        except ValueError:
            return default
    return PrecipEMOSCoefficients(
        a=f("AXIOM_PRECIP_EMOS_A", 0.0),
        b=f("AXIOM_PRECIP_EMOS_B", 1.0),
        c=f("AXIOM_PRECIP_EMOS_C", 0.04),
        d=f("AXIOM_PRECIP_EMOS_D", 1.20),
        logistic_scale=f("AXIOM_PRECIP_EMOS_LOGISTIC", 5.0),
        sigma2_floor=f("AXIOM_PRECIP_EMOS_SIGMA2_FLOOR", 0.01),
    )


# ---------------------------------------------------------------------------
# Numerical helpers
# ---------------------------------------------------------------------------


def _ensemble_stats(values: Sequence[float]) -> tuple[float, float]:
    xs: list[float] = []
    for v in values:
        if v is None:
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if math.isnan(fv) or fv < 0.0:
            # Negative precipitation is a data error; NaN is an
            # explicit "no value" — drop both rather than silently
            # clamping (which would bias the ensemble mean down).
            continue
        xs.append(fv)
    if not xs:
        return float("nan"), 0.0
    n = len(xs)
    mean = sum(xs) / n
    if n == 1:
        return mean, 0.0
    var = sum((x - mean) ** 2 for x in xs) / (n - 1)
    return mean, var


def _sigmoid(z: float) -> float:
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    ez = math.exp(z)
    return ez / (1.0 + ez)


def _clamp_prob(p: float) -> float:
    return max(1e-6, min(1.0 - 1e-6, p))


def gamma_sf(k: float, alpha: float, theta: float) -> float:
    """Survival function P(X > k) for X ~ Gamma(shape=alpha, scale=theta).

    Pure-Python regularised incomplete gamma via Numerical-Recipes
    series / continued-fraction split.  Defends against the degenerate
    ``alpha <= 0`` / ``theta <= 0`` shapes that fall out of a
    near-zero ensemble.
    """
    if alpha <= 0.0 or theta <= 0.0:
        # No mass / no spread — treat as a delta at 0; any K > 0 means
        # P(X > K) = 0.
        return 1.0 if k <= 0.0 else 0.0
    if k <= 0.0:
        return 1.0
    x = k / theta
    if x < alpha + 1.0:
        return 1.0 - _gser(alpha, x)
    return _gcf(alpha, x)


def _gser(a: float, x: float, itmax: int = 200, eps: float = 1e-9) -> float:
    """Lower regularised incomplete gamma via series expansion."""
    if x <= 0.0:
        return 0.0
    gln = math.lgamma(a)
    ap = a
    summ = 1.0 / a
    delta = summ
    for _ in range(itmax):
        ap += 1.0
        delta *= x / ap
        summ += delta
        if abs(delta) < abs(summ) * eps:
            break
    return summ * math.exp(-x + a * math.log(x) - gln)


def _gcf(a: float, x: float, itmax: int = 200, eps: float = 1e-9) -> float:
    """Upper regularised incomplete gamma via Lentz continued fraction."""
    gln = math.lgamma(a)
    tiny = 1e-300
    b = x + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, itmax + 1):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return math.exp(-x + a * math.log(x) - gln) * h


# ---------------------------------------------------------------------------
# Public EMOS apply
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrecipEMOSResult:
    mu: float                 # calibrated mean (inches)
    sigma: float              # calibrated stddev (inches)
    ensemble_mean: float
    ensemble_var: float
    threshold_in: float
    p_above: float            # P(Y > K)
    p_below: float            # P(Y < K) = 1 - p_above
    link: str                 # "logistic" (K==0) or "gamma" (K>0)

    def probability_for(self, direction: str) -> float:
        return self.p_below if str(direction).lower() == "below" else self.p_above


def apply_precip_emos(
    *,
    ensemble: Sequence[float],
    threshold_in: float,
    coefficients: PrecipEMOSCoefficients | None = None,
) -> PrecipEMOSResult:
    """Run the gamma / logistic EMOS on a precipitation ensemble.

    ``ensemble`` is the list of multi-source daily-accumulation point
    forecasts in inches.  Negative or NaN values are dropped.  Returns
    a flat, EMOS-style result so the predictor downstream looks
    identical to the temperature path.
    """
    coef = coefficients or coefficients_from_env()
    em_mean, em_var = _ensemble_stats(ensemble)
    if math.isnan(em_mean):
        # No usable forecast — flat 0.5 with wide cushion so gates ignore us.
        return PrecipEMOSResult(
            mu=float(max(0.0, threshold_in)),
            sigma=1.0,
            ensemble_mean=float("nan"),
            ensemble_var=0.0,
            threshold_in=float(threshold_in),
            p_above=0.5,
            p_below=0.5,
            link="degenerate",
        )

    mu = max(0.0, coef.a + coef.b * em_mean)
    sigma2 = max(coef.sigma2_floor, coef.c + coef.d * em_var)
    sigma = math.sqrt(sigma2)

    if threshold_in <= 0.0:
        # "Any precipitation today" contract — gamma SF is 1.0 for
        # K=0, which would saturate the YES side; the operator
        # actually wants a probability that scales with mu.  Use a
        # logistic link on the calibrated mean.
        z = mu * coef.logistic_scale
        p_above = _clamp_prob(_sigmoid(z))
        p_below = 1.0 - p_above
        link = "logistic"
    else:
        # Method-of-moments Gamma fit: mu = alpha*theta, sigma2 = alpha*theta^2.
        theta = sigma2 / mu if mu > 0 else 0.0
        alpha = (mu * mu) / sigma2 if sigma2 > 0 else 0.0
        p_above = _clamp_prob(gamma_sf(threshold_in, alpha, theta))
        p_below = 1.0 - p_above
        link = "gamma"

    return PrecipEMOSResult(
        mu=float(mu),
        sigma=float(sigma),
        ensemble_mean=float(em_mean),
        ensemble_var=float(em_var),
        threshold_in=float(threshold_in),
        p_above=float(p_above),
        p_below=float(p_below),
        link=link,
    )


__all__ = [
    "PrecipEMOSCoefficients",
    "PrecipEMOSResult",
    "apply_precip_emos",
    "coefficients_from_env",
    "gamma_sf",
]
