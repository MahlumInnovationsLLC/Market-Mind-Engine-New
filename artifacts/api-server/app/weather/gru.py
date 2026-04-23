"""Tiny pure-numpy GRU sequence model for the temperature vertical.

We deliberately avoid pulling in PyTorch / TensorFlow at runtime — the
fleet has no GPU, the numbers we move are tiny (a 12-step trace per
city per cycle), and a hand-rolled GRU keeps the dependency surface
flat.  Production training happens offline (we can drop a
``models/saved/temperature_gru.pkl`` carrying ``GRUWeights`` and the
loader will swap it in transparently); the in-process baseline shipped
here is a reasonable, deterministic seed that already beats a
naive single-source point-forecast on synthetic + replayed data
(see ``tests/test_weather_predictor.py``).

The model consumes a sequence of per-step feature vectors:

    [t2m_now_f, hrrr_t2m_f, hrrr_tmax_f, hrrr_tmin_f,
     clim_tmax_f, clim_tavg_f, hour_norm, doy_norm]

and emits

    {"point_f": float,    # bias-corrected point forecast (Fahrenheit)
     "spread_f": float,   # ensemble standard-deviation proxy
     "h": list[float]}    # final hidden state (for diagnostics)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import math


# ---------------------------------------------------------------------------
# Tiny linear-algebra primitives (pure-Python so tests are dep-free).
# ---------------------------------------------------------------------------


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def _tanh(x: float) -> float:
    return math.tanh(x)


def _matvec(W: list[list[float]], x: Sequence[float]) -> list[float]:
    return [sum(W[i][j] * x[j] for j in range(len(x))) for i in range(len(W))]


def _vadd(a: Sequence[float], b: Sequence[float]) -> list[float]:
    return [a[i] + b[i] for i in range(len(a))]


def _vmul(a: Sequence[float], b: Sequence[float]) -> list[float]:
    return [a[i] * b[i] for i in range(len(a))]


def _vsub1(a: Sequence[float], b: Sequence[float]) -> list[float]:
    return [a[i] - b[i] for i in range(len(a))]


# ---------------------------------------------------------------------------
# Weight container + deterministic baseline initialiser.
# ---------------------------------------------------------------------------


@dataclass
class GRUWeights:
    """Single-layer GRU + 2-output head.

    Shapes:
      W_ir, W_iz, W_in:  hidden x input
      W_hr, W_hz, W_hn:  hidden x hidden
      b_ir, b_iz, b_in:  hidden
      b_hr, b_hz, b_hn:  hidden
      W_out:             2 x hidden    (point bias, spread scale)
      b_out:             2
      input_mean / std:  per-feature input normalisation
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


def baseline_weights(n_input: int = 8, n_hidden: int = 6) -> GRUWeights:
    """Deterministic, dependency-free baseline.

    Hidden weights are tiny (sub-unit) so the GRU acts as a low-pass
    smoother over the feature trace.  The output head is wired so that

        point_bias  ~  small correction in Fahrenheit ([-3, +3])
        spread_pre  ~  log of a positive ensemble-spread proxy

    is added on top of the ensemble mean (and not used to override it
    entirely).  This gives the GRU a measurable but bounded say in the
    final probability — exactly what a small online-trained model
    should look like before a real checkpoint is dropped in.
    """
    # Deterministic pseudo-random seed via a tiny LCG so we don't pull
    # in numpy at module import time.
    state = [12345]

    def rnd() -> float:
        state[0] = (1103515245 * state[0] + 12345) & 0x7FFFFFFF
        # Map to [-0.1, +0.1]
        return ((state[0] / 0x7FFFFFFF) - 0.5) * 0.2

    def mat(rows: int, cols: int) -> list[list[float]]:
        return [[rnd() for _ in range(cols)] for _ in range(rows)]

    def vec(n: int) -> list[float]:
        return [rnd() for _ in range(n)]

    # Reasonable per-feature normalisation centred on Fahrenheit
    # mid-range; std=20 keeps the GRU input within ~[-2, 2].
    input_mean = [55.0, 55.0, 65.0, 45.0, 65.0, 55.0, 0.5, 0.5][:n_input]
    input_std = [20.0, 20.0, 20.0, 20.0, 20.0, 20.0, 0.3, 0.3][:n_input]
    if len(input_mean) < n_input:
        input_mean = input_mean + [0.0] * (n_input - len(input_mean))
        input_std = input_std + [1.0] * (n_input - len(input_std))

    return GRUWeights(
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
        # 2-output head: [point_bias_F, log_spread_proxy]
        W_out=mat(2, n_hidden),
        b_out=[0.0, math.log(2.5)],  # default spread ~ 2.5°F
        input_mean=input_mean,
        input_std=input_std,
    )


# ---------------------------------------------------------------------------
# Forward pass.
# ---------------------------------------------------------------------------


def _normalise(x: Sequence[float], w: GRUWeights) -> list[float]:
    return [
        (x[i] - w.input_mean[i]) / max(1e-6, w.input_std[i])
        for i in range(w.n_input)
    ]


def _step(x: Sequence[float], h: list[float], w: GRUWeights) -> list[float]:
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


def forward(
    sequence: Sequence[Sequence[float]],
    weights: GRUWeights | None = None,
) -> dict:
    """Run the GRU over ``sequence`` and emit a point + spread head.

    ``sequence`` is ``[step0_features, step1_features, ...]`` with each
    inner vector matching ``weights.n_input``.  Empty / malformed
    sequences return a zeroed payload — callers should fall back to the
    raw ensemble mean rather than abort.
    """
    if not sequence:
        return {"point_bias_f": 0.0, "spread_f": 2.5, "h": []}
    w = weights or baseline_weights()
    h: list[float] = [0.0] * w.n_hidden
    for x in sequence:
        if len(x) != w.n_input:
            x = list(x)[: w.n_input] + [0.0] * max(0, w.n_input - len(x))
        h = _step(x, h, w)
    out = _vadd(_matvec(w.W_out, h), w.b_out)
    point_bias_f = max(-5.0, min(5.0, float(out[0]) * 3.0))
    # exp(out[1]) is positive — clamp [0.5°F, 12°F] so EMOS sees a sane sigma.
    spread_f = max(0.5, min(12.0, math.exp(float(out[1]))))
    return {"point_bias_f": point_bias_f, "spread_f": spread_f, "h": h}


__all__ = ["GRUWeights", "baseline_weights", "forward"]
