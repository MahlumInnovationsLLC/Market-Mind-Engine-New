"""Tiny pure-numpy GRU bias head for the precipitation vertical (task #281).

Mirrors ``app.weather.gru`` (the temperature GRU) but tuned for daily
precipitation forecasts:

* The feature trace is a per-step vector of multi-source point
  forecasts in inches (Open-Meteo + Tomorrow.io + HRRR + AccuWeather)
  plus a running ensemble mean / variance and the cycle's hour-of-day
  / day-of-year normalisers.
* The output head emits

      {"point_bias_in": float,    # non-negative additive bias (inches)
       "spread_in":     float,    # non-negative spread proxy (inches)
       "h":             list[float]}  # final hidden state (diagnostics)

  ``point_bias_in`` is intentionally non-negative — HRRR / Open-Meteo
  systematically *under*-read heavy precipitation events, and modelling
  the residual as an additive log-domain mass keeps the EMOS gamma
  shape sane.  ``spread_in`` is exp-activated so the EMOS variance
  layer always sees a positive spread proxy.

Production training happens offline (drop a
``models/saved/precip_gru.pkl`` carrying ``PrecipGRUWeights`` and the
loader will swap it in transparently); the in-process baseline shipped
here is a deterministic LCG-seeded init that supplies a calibrated
default bias (~0.10") and spread (~0.30") so the EMOS post-processor
sees a sensible non-degenerate distribution even with a tight ensemble
(see ``tests/test_weather_precip_predictor.py``).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from .gru import _matvec, _sigmoid, _tanh, _vadd, _vmul


# ---------------------------------------------------------------------------
# Weight container + deterministic baseline initialiser.
# ---------------------------------------------------------------------------


@dataclass
class PrecipGRUWeights:
    """Single-layer GRU + 2-output head, shaped for precipitation.

    Shapes mirror ``gru.GRUWeights``:
      W_ir, W_iz, W_in:  hidden x input
      W_hr, W_hz, W_hn:  hidden x hidden
      b_ir, b_iz, b_in:  hidden
      b_hr, b_hz, b_hn:  hidden
      W_out:             2 x hidden     (point bias pre-act, spread pre-act)
      b_out:             2
      input_mean / std:  per-feature input normalisation (inches scale)
    """
    n_input: int
    n_hidden: int
    W_ir: list[list[float]]
    W_iz: list[list[float]]
    W_in: list[list[float]]
    W_hr: list[list[float]]
    W_hz: list[list[float]]
    W_hn: list[list[float]]
    b_ir: list[float]
    b_iz: list[float]
    b_in: list[float]
    b_hr: list[float]
    b_hz: list[float]
    b_hn: list[float]
    W_out: list[list[float]]
    b_out: list[float]
    input_mean: list[float]
    input_std: list[float]


# Defaults chosen so a fresh-install GRU adds a small but operator-
# perceptible correction on top of the EMOS ensemble:
#   softplus(-2.2) ~ 0.105  -> default bias addition of ~0.10"
#   exp(log(0.3))  =  0.30  -> default spread proxy of ~0.30"
# The training loop in `scripts/train_precip_gru.py` shifts these as it
# learns the per-city HRRR / Open-Meteo bias from settled outcomes.
_DEFAULT_POINT_BIAS_PRE: float = -2.2
_DEFAULT_SPREAD_LOG: float = math.log(0.3)


def baseline_weights(n_input: int = 8, n_hidden: int = 6) -> PrecipGRUWeights:
    """Deterministic, dependency-free baseline weights.

    The hidden weights are tiny (sub-unit) so the GRU acts as a low-pass
    smoother on the precipitation feature trace.  The output head is
    wired so that

        point_bias_in  = softplus(out[0])         in [0, ~2] inches
        spread_in      = exp(out[1])              in [~0.05, ~3] inches

    is added on top of the EMOS ensemble mean (and not used to override
    it entirely).  This gives the GRU a measurable but bounded say in
    the final probability — exactly what a small online-trained model
    should look like before a real checkpoint is dropped in.

    A different LCG seed than ``gru.baseline_weights`` so the two
    verticals don't accidentally end up with identical hidden states
    on otherwise-symmetric inputs.
    """
    state = [54321]

    def rnd() -> float:
        state[0] = (1103515245 * state[0] + 12345) & 0x7FFFFFFF
        # Map to [-0.1, +0.1] — same scale as the temperature GRU so
        # the hidden state magnitudes are in a familiar range.
        return ((state[0] / 0x7FFFFFFF) - 0.5) * 0.2

    def mat(rows: int, cols: int) -> list[list[float]]:
        return [[rnd() for _ in range(cols)] for _ in range(rows)]

    def vec(n: int) -> list[float]:
        return [rnd() for _ in range(n)]

    # Per-feature normalisation centred on light-precip values; std=0.5"
    # keeps the GRU input within ~[-1, 4] for typical daily totals.
    # Feature order (matches ``precip_predictor._build_precip_sequence``):
    #   [om_in, tio_in, hrrr_in, accu_in, em_mean_in, em_var_in,
    #    hour_norm, doy_norm]
    input_mean = [0.05, 0.05, 0.05, 0.05, 0.05, 0.0, 0.5, 0.5][:n_input]
    input_std = [0.5, 0.5, 0.5, 0.5, 0.5, 0.1, 0.3, 0.3][:n_input]
    if len(input_mean) < n_input:
        input_mean = input_mean + [0.0] * (n_input - len(input_mean))
        input_std = input_std + [1.0] * (n_input - len(input_std))

    return PrecipGRUWeights(
        n_input=n_input,
        n_hidden=n_hidden,
        W_ir=mat(n_hidden, n_input),
        W_iz=mat(n_hidden, n_input),
        W_in=mat(n_hidden, n_input),
        W_hr=mat(n_hidden, n_hidden),
        W_hz=mat(n_hidden, n_hidden),
        W_hn=mat(n_hidden, n_hidden),
        b_ir=vec(n_hidden),
        b_iz=vec(n_hidden),
        b_in=vec(n_hidden),
        b_hr=vec(n_hidden),
        b_hz=vec(n_hidden),
        b_hn=vec(n_hidden),
        # 2-output head: [point_bias_pre (softplus), log_spread_pre (exp)]
        W_out=mat(2, n_hidden),
        b_out=[_DEFAULT_POINT_BIAS_PRE, _DEFAULT_SPREAD_LOG],
        input_mean=input_mean,
        input_std=input_std,
    )


# ---------------------------------------------------------------------------
# Forward pass.
# ---------------------------------------------------------------------------


def _normalise(x: Sequence[float], w: PrecipGRUWeights) -> list[float]:
    return [
        (x[i] - w.input_mean[i]) / max(1e-6, w.input_std[i])
        for i in range(w.n_input)
    ]


def _step(x: Sequence[float], h: list[float], w: PrecipGRUWeights) -> list[float]:
    """One GRU cell step (PyTorch's GRUCell formulation)."""
    xn = _normalise(x, w)
    r = [_sigmoid(v) for v in _vadd(_vadd(_matvec(w.W_ir, xn), w.b_ir),
                                    _vadd(_matvec(w.W_hr, h),  w.b_hr))]
    z = [_sigmoid(v) for v in _vadd(_vadd(_matvec(w.W_iz, xn), w.b_iz),
                                    _vadd(_matvec(w.W_hz, h),  w.b_hz))]
    n_pre = _vadd(_matvec(w.W_in, xn), w.b_in)
    n_h   = _vadd(_matvec(w.W_hn, h),  w.b_hn)
    n     = [_tanh(n_pre[i] + r[i] * n_h[i]) for i in range(w.n_hidden)]
    one_minus_z = [1.0 - zi for zi in z]
    return _vadd(_vmul(one_minus_z, n), _vmul(z, h))


def _softplus(x: float) -> float:
    """Numerically-stable softplus: log(1 + exp(x))."""
    if x > 30.0:
        return x  # exp(30) overflows; softplus(x) ~ x for large x
    if x < -30.0:
        return 0.0
    return math.log1p(math.exp(x))


# Output clamps — keep the GRU's contribution operator-bounded so a
# rogue checkpoint can't push a 50-inch bias into the EMOS layer.
_POINT_BIAS_MAX_IN: float = 2.0
_SPREAD_MIN_IN: float = 0.05
_SPREAD_MAX_IN: float = 3.0
_DEFAULT_POINT_BIAS_IN: float = _softplus(_DEFAULT_POINT_BIAS_PRE)
_DEFAULT_SPREAD_IN: float = math.exp(_DEFAULT_SPREAD_LOG)


def forward(
    sequence: Sequence[Sequence[float]],
    weights: PrecipGRUWeights | None = None,
) -> dict:
    """Run the GRU over ``sequence`` and emit a non-negative bias + spread.

    ``sequence`` is ``[step0_features, step1_features, ...]`` with each
    inner vector matching ``weights.n_input``.  Empty / malformed
    sequences return the default (calibrated) head outputs so callers
    never see ``None`` — falling back to the bare ensemble mean is
    handled in the EMOS layer when the GRU output is suppressed.
    """
    if not sequence:
        return {
            "point_bias_in": _DEFAULT_POINT_BIAS_IN,
            "spread_in": _DEFAULT_SPREAD_IN,
            "h": [],
        }
    w = weights or baseline_weights()
    h: list[float] = [0.0] * w.n_hidden
    for x in sequence:
        if len(x) != w.n_input:
            x = list(x)[: w.n_input] + [0.0] * max(0, w.n_input - len(x))
        h = _step(x, h, w)
    out = _vadd(_matvec(w.W_out, h), w.b_out)
    point_bias_in = max(0.0, min(_POINT_BIAS_MAX_IN, _softplus(float(out[0]))))
    spread_in = max(_SPREAD_MIN_IN, min(_SPREAD_MAX_IN, math.exp(float(out[1]))))
    return {"point_bias_in": point_bias_in, "spread_in": spread_in, "h": h}


__all__ = ["PrecipGRUWeights", "baseline_weights", "forward"]
