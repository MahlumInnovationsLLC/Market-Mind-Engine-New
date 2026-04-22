"""Pure numeric helpers used by the AutoResearch mining job.

Kept dependency-free (stdlib only) so they're cheap to import inside
admin/probe paths and trivially unit-testable.

Three metrics, all "the bigger the better" (in absolute value):

* **IC** — Pearson correlation between the candidate signal value and
  the per-trade *residual* ``y - p`` (where ``y`` is the realised
  outcome and ``p`` is the meta-model's predicted probability for the
  taken side). A positive IC means "the bigger this signal was, the
  more the model under-predicted the outcome" — i.e. uncaptured edge.
* **Sharpe** — mean(value * residual) / std(value * residual). Treats
  each trade as a one-shot bet sized by the candidate signal and asks
  "what's the signal-to-noise of the realised P&L proxy?".
* **Monotonicity** — Spearman correlation between the rank of the
  signal value and the bin-mean residual. Captures whether the
  relationship is consistently directional rather than driven by a
  handful of outliers.
"""
from __future__ import annotations

from math import sqrt
from typing import Sequence


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: Sequence[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    var = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    return sqrt(max(0.0, var))


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Pearson correlation. Returns 0.0 when undefined (n<2 or zero variance)."""
    n = min(len(xs), len(ys))
    if n < 2:
        return 0.0
    mx, my = _mean(xs[:n]), _mean(ys[:n])
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    dx = sum((xs[i] - mx) ** 2 for i in range(n))
    dy = sum((ys[i] - my) ** 2 for i in range(n))
    denom = sqrt(dx * dy)
    if denom <= 0.0:
        return 0.0
    return num / denom


def _ranks(xs: Sequence[float]) -> list[float]:
    """Average-rank for ties — matches scipy.stats.rankdata default."""
    n = len(xs)
    order = sorted(range(n), key=lambda i: xs[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0  # 1-based average rank
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Spearman rank correlation."""
    n = min(len(xs), len(ys))
    if n < 2:
        return 0.0
    return pearson(_ranks(xs[:n]), _ranks(ys[:n]))


def sharpe(values: Sequence[float], residuals: Sequence[float]) -> float:
    """Per-trade signal-to-noise of ``value * residual``.

    Uses sample std (Bessel-corrected) so it matches the textbook
    Sharpe ratio. Returns 0.0 when std is zero or sample is too small.
    """
    n = min(len(values), len(residuals))
    if n < 2:
        return 0.0
    pnl = [values[i] * residuals[i] for i in range(n)]
    s = _std(pnl)
    if s <= 0.0:
        return 0.0
    return _mean(pnl) / s


def monotonicity(
    values: Sequence[float],
    residuals: Sequence[float],
    *,
    bins: int = 5,
) -> float:
    """Spearman of (bin index) vs (bin-mean residual).

    We bucket the candidate values into ``bins`` equal-frequency
    quantiles; if all values collapse into a single bucket the result is
    0.0 (no monotone signal). The intent is to catch "yes, IC is
    positive *and* the relationship is roughly linear in rank-space",
    which is what we want before promoting a feature to production.
    """
    n = min(len(values), len(residuals))
    if n < bins * 2:
        return 0.0
    pairs = sorted(zip(values[:n], residuals[:n]), key=lambda p: p[0])
    bin_size = n // bins
    bin_indices: list[float] = []
    bin_means: list[float] = []
    for b in range(bins):
        start = b * bin_size
        end = n if b == bins - 1 else (b + 1) * bin_size
        chunk = pairs[start:end]
        if not chunk:
            continue
        bin_indices.append(float(b))
        bin_means.append(_mean([r for _, r in chunk]))
    if len(bin_indices) < 2:
        return 0.0
    return spearman(bin_indices, bin_means)


def evaluate(
    values: Sequence[float], residuals: Sequence[float]
) -> dict[str, float | int]:
    """One-call summary used by the mining job."""
    return {
        "n": int(min(len(values), len(residuals))),
        "ic": pearson(values, residuals),
        "sharpe": sharpe(values, residuals),
        "monotonicity": monotonicity(values, residuals),
    }
