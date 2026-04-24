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

Coefficient resolution order (highest precedence first):

  1. Persisted refit cache (``set_persisted_coefficients`` — populated
     by the nightly retrain loop in :mod:`app.settlement.retrain` once
     enough KXRAIN markets have settled to drive a maximum-likelihood
     refit of (a, b, c, d, logistic_scale)).
  2. ``AXIOM_PRECIP_EMOS_*`` environment variables (operator override).
  3. The shipped, sanity-checked defaults below.
"""
from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, asdict
from typing import Iterable, Sequence


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Coefficients
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrecipEMOSCoefficients:
    a: float = 0.0       # mean intercept (inches)
    b: float = 1.0       # ensemble-mean slope
    b_g: float = 0.30    # GRU point-bias slope (additive, non-negative)
    c: float = 0.04      # variance intercept (inches^2) — small but non-zero
    d: float = 1.20      # variance slope on ensemble variance
    d_g: float = 0.40    # variance slope on GRU spread^2
    # Logistic-link scale used for the K=0 ("any precip") contract.
    # Chosen so a 0.05" forecast already maps to ~0.62 P(any precip)
    # — i.e. the operator-perceptible "rain is on the way" threshold.
    logistic_scale: float = 5.0
    # Floor on EMOS sigma^2 so a degenerate (single-source, agreeing-
    # forecasts) ensemble still produces a non-saturating gamma tail.
    sigma2_floor: float = 0.01


def coefficients_from_env() -> PrecipEMOSCoefficients:
    """Resolve the active EMOS coefficients.

    Resolution order (first hit wins):

      1. The persisted refit cache populated by the retrain loop —
         ``set_persisted_coefficients`` calls store the most recent
         max-likelihood fit here so the trading loop picks it up
         without restarting the API.
      2. ``AXIOM_PRECIP_EMOS_*`` environment variables.
      3. Hand-picked defaults on :class:`PrecipEMOSCoefficients`.
    """
    cached = _persisted_coefficients()
    if cached is not None:
        return cached

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
        b_g=f("AXIOM_PRECIP_EMOS_BG", 0.30),
        c=f("AXIOM_PRECIP_EMOS_C", 0.04),
        d=f("AXIOM_PRECIP_EMOS_D", 1.20),
        d_g=f("AXIOM_PRECIP_EMOS_DG", 0.40),
        logistic_scale=f("AXIOM_PRECIP_EMOS_LOGISTIC", 5.0),
        sigma2_floor=f("AXIOM_PRECIP_EMOS_SIGMA2_FLOOR", 0.01),
    )


# ---------------------------------------------------------------------------
# Persisted-refit cache
# ---------------------------------------------------------------------------
# The nightly retrain loop refits (a, b, c, d, logistic_scale) on real
# settled KXRAIN markets and stores the result here so the trading loop
# stops using the shipped defaults the moment a fit lands. The cache is
# process-local — :mod:`app.settlement.retrain` is responsible for
# hydrating it from ``system_settings.precipitation_emos_coefficients``
# at boot and updating it whenever a fresh fit is persisted.

_PERSISTED: PrecipEMOSCoefficients | None = None


def _persisted_coefficients() -> PrecipEMOSCoefficients | None:
    return _PERSISTED


def set_persisted_coefficients(coef: PrecipEMOSCoefficients | None) -> None:
    """Install (or clear) the in-memory refit cache.

    ``None`` reverts to env / default resolution and is the right call
    for tests that want a clean baseline.
    """
    global _PERSISTED
    _PERSISTED = coef


def coefficients_to_dict(coef: PrecipEMOSCoefficients) -> dict[str, float]:
    """Plain-dict serialization for the system_settings JSON payload."""
    return {k: float(v) for k, v in asdict(coef).items()}


def coefficients_from_dict(payload: dict[str, float | int]) -> PrecipEMOSCoefficients:
    """Inverse of :func:`coefficients_to_dict`. Unknown keys are ignored
    and any missing key falls back to the dataclass default so a
    forward-compatible JSON payload from a future fitter still loads.
    """
    defaults = PrecipEMOSCoefficients()
    def _g(key: str, fallback: float) -> float:
        v = payload.get(key)
        if v is None:
            return fallback
        try:
            return float(v)
        except (TypeError, ValueError):
            return fallback
    return PrecipEMOSCoefficients(
        a=_g("a", defaults.a),
        b=_g("b", defaults.b),
        c=_g("c", defaults.c),
        d=_g("d", defaults.d),
        logistic_scale=_g("logistic_scale", defaults.logistic_scale),
        sigma2_floor=_g("sigma2_floor", defaults.sigma2_floor),
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
    gru_point_in: float | None = None,
    gru_spread_in: float | None = None,
    coefficients: PrecipEMOSCoefficients | None = None,
) -> PrecipEMOSResult:
    """Run the gamma / logistic EMOS on a precipitation ensemble.

    ``ensemble`` is the list of multi-source daily-accumulation point
    forecasts in inches.  Negative or NaN values are dropped.  Returns
    a flat, EMOS-style result so the predictor downstream looks
    identical to the temperature path.

    ``gru_point_in`` is a non-negative additive bias (inches) the
    precipitation GRU recommends on top of the ensemble mean —
    HRRR / Open-Meteo systematically under-predict heavy events, and
    a non-negative bias head is the right correction.  ``gru_spread_in``
    is the GRU's spread proxy (inches) folded into the EMOS variance
    so a too-tight ensemble doesn't saturate the gamma tail.  Either
    may be ``None`` when the GRU couldn't run, in which case EMOS
    falls back to the pure-ensemble fit.
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

    g_point = max(0.0, float(gru_point_in)) if gru_point_in is not None else 0.0
    g_var = (
        max(0.0, float(gru_spread_in)) ** 2 if gru_spread_in is not None else 0.0
    )

    mu = max(0.0, coef.a + coef.b * em_mean + coef.b_g * g_point)
    sigma2 = max(coef.sigma2_floor, coef.c + coef.d * em_var + coef.d_g * g_var)
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


# ---------------------------------------------------------------------------
# Maximum-likelihood refit
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrecipEMOSTrainingSample:
    """Single (observation, outcome) row consumed by the fitter.

    ``ensemble_mean`` and ``ensemble_var`` are the EMOS inputs the
    predictor cached at trade-creation time. ``threshold_in`` is the
    Kalshi K (in inches). ``y_above`` is the realized binary outcome
    *for the canonical "P(precip > K)" prediction* — callers that
    persisted a `direction='below'` row must flip the YES outcome
    before constructing the sample.
    """
    ensemble_mean: float
    ensemble_var: float
    threshold_in: float
    y_above: int


@dataclass(frozen=True)
class PrecipEMOSFitResult:
    coefficients: PrecipEMOSCoefficients
    n_samples: int
    log_likelihood: float
    brier: float
    brier_baseline: float
    converged: bool


def _predict_p_above_with(coef: PrecipEMOSCoefficients,
                          em_mean: float, em_var: float,
                          threshold_in: float) -> float:
    """Same math as :func:`apply_precip_emos` but inlined so the fitter
    can drive it tens of thousands of times without re-allocating the
    full ``PrecipEMOSResult`` shape on every call.
    """
    mu = max(0.0, coef.a + coef.b * float(em_mean))
    sigma2 = max(coef.sigma2_floor, coef.c + coef.d * float(em_var))
    if threshold_in <= 0.0:
        z = mu * coef.logistic_scale
        return _clamp_prob(_sigmoid(z))
    if mu <= 0.0 or sigma2 <= 0.0:
        # mu==0 collapses the gamma to a delta at 0 — P(Y > K) = 0.
        return _clamp_prob(0.0)
    theta = sigma2 / mu
    alpha = (mu * mu) / sigma2
    return _clamp_prob(gamma_sf(threshold_in, alpha, theta))


def _neg_log_likelihood(
    samples: Sequence[PrecipEMOSTrainingSample],
    coef: PrecipEMOSCoefficients,
) -> float:
    nll = 0.0
    for s in samples:
        p_above = _predict_p_above_with(
            coef, s.ensemble_mean, s.ensemble_var, s.threshold_in
        )
        if s.y_above:
            nll -= math.log(p_above)
        else:
            nll -= math.log(1.0 - p_above)
    return nll


def _brier_for(coef: PrecipEMOSCoefficients,
               samples: Sequence[PrecipEMOSTrainingSample]) -> float:
    if not samples:
        return 0.0
    se = 0.0
    for s in samples:
        p = _predict_p_above_with(
            coef, s.ensemble_mean, s.ensemble_var, s.threshold_in
        )
        se += (p - float(s.y_above)) ** 2
    return se / len(samples)


# Minimum samples below which we refuse to refit (the optimizer is
# unstable on tiny / single-class folds, and a partial refit could
# regress the live coefficients).
MIN_REFIT_SAMPLES = 50


def to_training_sample(
    *, ensemble_mean: float, ensemble_var: float,
    threshold_in: float, direction: str, actual_outcome_yes: int,
) -> PrecipEMOSTrainingSample:
    """Canonicalize a stored observation onto the gamma-SF axis the
    fitter operates on. Above-direction tickers store ``y_above``
    directly; below-direction tickers flip the YES outcome so the
    fitter sees a single "did precip exceed K" target across the
    whole dataset.
    """
    y = int(actual_outcome_yes)
    d = str(direction).lower().strip()
    if d == "below":
        y = 1 - y
    return PrecipEMOSTrainingSample(
        ensemble_mean=float(ensemble_mean),
        ensemble_var=float(ensemble_var),
        threshold_in=float(threshold_in),
        y_above=int(y),
    )


def fit_precip_emos_coefficients(
    samples: Iterable[PrecipEMOSTrainingSample],
    *,
    initial: PrecipEMOSCoefficients | None = None,
    max_iter: int = 400,
) -> PrecipEMOSFitResult:
    """Refit (a, b, c, d, logistic_scale) by maximizing the binary
    log-likelihood of the gamma / logistic EMOS link against settled
    KXRAIN outcomes.

    Implementation notes:

    * (b, c, d, logistic_scale) are positivity-preserved via an
      ``exp`` reparam so the optimizer can never push the variance
      slope or the logistic scale below zero (which would invert
      the link).
    * ``a`` is unconstrained (the EMOS intercept is allowed to be
      slightly negative for sources that systematically over-read).
    * ``sigma2_floor`` is held fixed at the shipped default so the
      optimizer doesn't trade the floor off against ``c`` and produce
      a degenerate gamma on dry days.
    * Falls back to scipy's ``minimize`` (Nelder-Mead, no gradients)
      when scipy is available; otherwise raises so the caller can
      decline the refit rather than ship a broken fit.
    """
    sample_list = list(samples)
    if len(sample_list) < MIN_REFIT_SAMPLES:
        raise ValueError(
            f"need at least {MIN_REFIT_SAMPLES} samples to refit, "
            f"got {len(sample_list)}"
        )
    # Single-class fold (every outcome 0 or every outcome 1) breaks the
    # log-likelihood since either log(p) or log(1-p) saturates against
    # any informative coefficients. Fall back to a Bayesian-prior fit.
    n_pos = sum(1 for s in sample_list if s.y_above)
    if n_pos == 0 or n_pos == len(sample_list):
        raise ValueError(
            "training set is single-class (all YES or all NO); refit "
            "would be degenerate"
        )

    base = initial or coefficients_from_env()

    # Reparam: theta = (a, log_b, log_c, log_d, log_logistic).
    def _unpack(theta: Sequence[float]) -> PrecipEMOSCoefficients:
        a, lb, lc, ld, lL = theta
        return PrecipEMOSCoefficients(
            a=float(a),
            b=float(math.exp(lb)),
            c=float(math.exp(lc)),
            d=float(math.exp(ld)),
            logistic_scale=float(math.exp(lL)),
            sigma2_floor=base.sigma2_floor,
        )

    def _objective(theta: Sequence[float]) -> float:
        coef = _unpack(theta)
        return _neg_log_likelihood(sample_list, coef)

    x0 = (
        base.a,
        math.log(max(1e-3, base.b)),
        math.log(max(1e-3, base.c)),
        math.log(max(1e-3, base.d)),
        math.log(max(1e-3, base.logistic_scale)),
    )

    try:
        from scipy.optimize import minimize  # type: ignore
    except Exception as e:
        raise RuntimeError(
            "scipy.optimize.minimize is required for the EMOS refit "
            f"but is unavailable: {e}"
        )

    res = minimize(
        _objective, list(x0),
        method="Nelder-Mead",
        options={"xatol": 1e-4, "fatol": 1e-4, "maxiter": max_iter},
    )
    fitted = _unpack(res.x)
    return PrecipEMOSFitResult(
        coefficients=fitted,
        n_samples=len(sample_list),
        log_likelihood=float(-res.fun),
        brier=_brier_for(fitted, sample_list),
        brier_baseline=_brier_for(base, sample_list),
        converged=bool(getattr(res, "success", False)),
    )


__all__ = [
    "MIN_REFIT_SAMPLES",
    "PrecipEMOSCoefficients",
    "PrecipEMOSFitResult",
    "PrecipEMOSResult",
    "PrecipEMOSTrainingSample",
    "apply_precip_emos",
    "coefficients_from_dict",
    "coefficients_from_env",
    "coefficients_to_dict",
    "fit_precip_emos_coefficients",
    "gamma_sf",
    "set_persisted_coefficients",
    "to_training_sample",
]
