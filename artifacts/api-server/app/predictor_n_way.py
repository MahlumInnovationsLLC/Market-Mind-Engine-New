"""N-way winner-market probability utilities (task #197).

Combat & individual sports ship a different market shape than team
sports: a single market resolves to one of N entrants (UFC fight-card
"who wins the main event"-style multi-fight bundles, ATP/WTA tournament
outright, F1 race winner, PGA tournament winner).  The generic 2-way
meta-model is undefined on these — we need a closed-form that converts
per-entrant rolling rating priors (Elo for UFC, ATP/WTA points for
tennis, FIA-weighted driver rating for F1, OWGR for PGA) into a proper
probability simplex.

Two related models live here:

* `bradley_terry_pairwise(rating_a, rating_b)` — closed-form pairwise
  win-probability under a logistic Bradley-Terry on log-strengths.
  Uses the standard 400-point Elo scale so callers can pass raw Elo
  ratings unchanged.
* `plackett_luce(ratings)` — Plackett-Luce extension over `n>=2`
  entrants.  Each entrant's win probability is its strength divided
  by the total-strength normaliser.  Equivalent to a single-draw
  multinomial parameterised by `softmax(ratings / scale)`.

Both functions are pure, deterministic and dependency-free so the
existing predictor + swarm pipelines can route N-way candidates
through them without dragging numpy in for a hot path that only
manipulates short vectors.

The functions return:

* `bradley_terry_pairwise` — `float` in (0, 1), the probability that
  side A wins.
* `plackett_luce` — `list[float]` summing to 1.0 in the same order as
  the input ratings list / mapping.
* `plackett_luce_from_mapping` — same as above but indexed by entrant
  name so callers can drop the dict on a candidate's `n_way_field`
  without re-ordering.

Also exposes a sanity-check helper `is_normalised(probabilities)` so
callers can assert the simplex constraint in tests.
"""
from __future__ import annotations

import math
from typing import Iterable, Mapping


# Elo-style log-strength scale.  Same constant the team-sport Elo
# module (`app.elo`) uses so callers can mix Bradley-Terry / PL with
# the existing rolling-rating store without a units mismatch.
ELO_SCALE: float = 400.0


def bradley_terry_pairwise(
    rating_a: float, rating_b: float, *, scale: float = ELO_SCALE,
) -> float:
    """Closed-form Bradley-Terry win probability for side A vs side B.

    Parameterised on the Elo log-strength scale (400 ratings points =
    10x relative strength) so a 200-point Elo gap produces the same
    probability the team-sport Elo module would compute.

    Returns a probability in the open interval (0, 1).  Callers that
    need a strict 0..1 must clip; we keep the open interval here so
    Plackett-Luce extensions never divide by zero.
    """
    diff = (float(rating_b) - float(rating_a)) / float(scale)
    # Standard logistic — equivalent to `1 / (1 + 10**diff)` on the
    # Elo scale; written via math.exp to avoid the small overhead of
    # repeated `pow`.
    try:
        return 1.0 / (1.0 + math.exp(diff * math.log(10.0)))
    except OverflowError:
        return 0.0 if diff > 0 else 1.0


def plackett_luce(
    ratings: Iterable[float], *, scale: float = ELO_SCALE,
) -> list[float]:
    """Plackett-Luce winner probabilities over `n>=1` entrants.

    Each entrant `i` carries strength `s_i = 10 ** (rating_i / scale)`
    and the winner probability is `s_i / sum(s)`.  Equivalent to the
    softmax of `ratings / (scale / log10(e))` so callers can think in
    Elo-points throughout.

    Returns a list of probabilities in the same order as `ratings`,
    summing to 1.0 (modulo floating-point noise).  Empty input
    returns `[]`.

    Raises `ValueError` if any rating is non-finite (NaN/inf) — these
    typically come from a malformed feed payload and should be
    surfaced loudly rather than silently turned into a degenerate
    distribution.
    """
    rs = list(ratings)
    if not rs:
        return []
    if any(not math.isfinite(r) for r in rs):
        raise ValueError("plackett_luce requires finite ratings")
    # Subtract the max for numerical stability — keeps the largest
    # exponent at exp(0)=1 so we never overflow on a 3000-point Elo
    # gap (rare but possible at the top of the OWGR / driver-rating
    # spread).
    max_r = max(rs)
    weights = [math.exp((float(r) - max_r) / scale * math.log(10.0)) for r in rs]
    total = sum(weights)
    if total <= 0:  # pragma: no cover — numerically impossible after exp
        n = len(rs)
        return [1.0 / n] * n
    return [w / total for w in weights]


def plackett_luce_from_mapping(
    ratings: Mapping[str, float], *, scale: float = ELO_SCALE,
) -> dict[str, float]:
    """Mapping-flavoured Plackett-Luce: returns `{entrant: prob}` so
    callers can drop the result on `candidate['n_way_field']` without
    keeping a parallel order list.

    Entrants are processed in the mapping's iteration order so dicts
    constructed via the same iteration order on the caller side stay
    aligned with downstream tests / logs.
    """
    keys = list(ratings.keys())
    probs = plackett_luce([ratings[k] for k in keys], scale=scale)
    return dict(zip(keys, probs))


def is_normalised(probabilities: Iterable[float], *, tol: float = 1e-9) -> bool:
    """True iff `probabilities` form a valid probability simplex
    (each in [0, 1] and sums to 1.0 within `tol`).  Used by tests +
    risk audit code to reject malformed candidates."""
    ps = list(probabilities)
    if not ps:
        return False
    total = 0.0
    for p in ps:
        if not math.isfinite(p) or p < -tol or p > 1.0 + tol:
            return False
        total += float(p)
    return abs(total - 1.0) <= tol


__all__ = [
    "ELO_SCALE",
    "bradley_terry_pairwise",
    "plackett_luce",
    "plackett_luce_from_mapping",
    "is_normalised",
]
