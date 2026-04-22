"""Thin Bayesian model that combines tech-AI evidence rows into a YES
probability for a Kalshi binary market (task #199).

Design: prior = 0.5 (uninformative).  Each evidence row contributes a
log-odds nudge keyed off `kind`:

    release          : recent release in the last 14 days = +ve
                       evidence the symbol is "active" — pushes YES
                       up for "release-by date" markets.
    trending         : Hugging Face trending row, weighted by
                       download volume.
    arxiv            : new submission in cs.LG/cs.CL — small +ve nudge.
    benchmark        : MLPerf-style benchmark row.
    earnings         : NVDA/TSMC earnings within window.

Output is clamped to [0.05, 0.95] so a sparse-evidence prediction can
never yield a degenerate 0/1 that the gates would reject anyway.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from .evidence import EvidenceRow
from .tickers import TechAITicker


# Per-kind log-odds nudges per *unit weight* of supportive evidence.
# Conservative defaults — paper-grade until trained data accumulates.
_KIND_NUDGE: dict[str, float] = {
    "release":   0.40,
    "trending":  0.30,
    "arxiv":     0.10,
    "benchmark": 0.50,
    "earnings":  0.25,
}


# Time decay (half-life in days) so a release from 6 months ago doesn't
# carry the same weight as one from this week.
_HALF_LIFE_DAYS = 14.0


def _decay_factor(observed_at: datetime, now: datetime) -> float:
    delta_days = max(0.0, (now - observed_at).total_seconds() / 86400.0)
    return 0.5 ** (delta_days / _HALF_LIFE_DAYS)


def _clamp(p: float, lo: float = 0.05, hi: float = 0.95) -> float:
    return max(lo, min(hi, p))


def _logit(p: float) -> float:
    p = _clamp(p, 1e-6, 1 - 1e-6)
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


@dataclass(frozen=True)
class BayesianResult:
    probability: float            # YES probability after clamping
    prior: float                  # starting prior (always 0.5 here)
    contributing_rows: int        # how many evidence rows nudged
    total_log_odds_shift: float   # cumulative log-odds applied
    feature_summary: dict[str, float]  # per-kind aggregated weight


def bayesian_probability(
    ticker: TechAITicker,
    evidence: Iterable[EvidenceRow],
    *,
    now: datetime | None = None,
) -> BayesianResult:
    """Combine evidence rows into a calibrated-ish probability for the YES side."""
    now = now or datetime.now(timezone.utc)
    prior = 0.5
    log_odds = _logit(prior)

    feature_summary: dict[str, float] = {k: 0.0 for k in _KIND_NUDGE}
    contributing = 0

    for row in evidence:
        nudge = _KIND_NUDGE.get(row.kind)
        if nudge is None:
            continue
        decay = _decay_factor(row.observed_at, now)
        weight = max(0.0, float(row.weight))
        # Each row contributes nudge * weight * decay to the log-odds.
        delta = nudge * weight * decay
        if delta <= 0.0:
            continue
        log_odds += delta
        feature_summary[row.kind] += delta
        contributing += 1

    # For "below"-direction binaries the evidence (which is generally
    # supportive of activity) should push the YES probability DOWN
    # rather than up — a release-by-date "below threshold" market
    # resolves YES when the symbol stays inactive.
    if ticker.direction == "below":
        log_odds = _logit(prior) * 2 - log_odds

    raw = _sigmoid(log_odds)
    p = _clamp(raw)
    return BayesianResult(
        probability=p,
        prior=prior,
        contributing_rows=contributing,
        total_log_odds_shift=log_odds - _logit(prior),
        feature_summary=feature_summary,
    )
