"""Consensus-prior model for the events-econ vertical (task #202).

Design:
  * Anchor on the upcoming release's *consensus* forecast.  Parse the
    ticker's threshold token (e.g. ``ABOVE3`` -> threshold=3.0) and
    compute the probability ``actual >= threshold`` under a Normal
    distribution centred on consensus with sigma estimated from the
    rolling forecast-error variance of recent same-type releases.
  * When no consensus is available, fall back to the most recent
    `previous` print as the centre.
  * Output is clamped to [0.05, 0.95] so a sparse-history ticker
    can't yield a degenerate 0/1 the gates would reject anyway.

The model is deliberately thin — the swarm carries the qualitative
nuance.  This layer's job is to translate consensus + variance into a
calibrated YES probability for a binary release threshold.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable

from .calendar import ReleaseRow
from .tickers import EventsEconTicker


# Default sigma when the rolling history is too thin to estimate one.
# Tuned so the prior probability lands in a reasonable range for the
# canonical CPI / NFP releases (~0.3 for CPI, ~50 for NFP) — operators
# can tune via system_settings if needed.
_DEFAULT_SIGMA_BY_TYPE: dict[str, float] = {
    "CPI":     0.20,    # m/m % surprise
    "PPI":     0.30,
    "GDP":     0.40,
    "NFP":     50.0,    # thousands of jobs
    "JOBLESS": 25.0,    # thousands of claims
    "RETAIL":  0.50,
    "PCE":     0.20,
    "FOMC":    0.10,    # rate change in pp (e.g. 25bp)
    "ISM":     1.50,
    "CONFIDENCE": 5.0,
    "HOUSING": 0.05,
}


# Floor on sigma so a small sample with zero variance can't produce a
# degenerate point mass at consensus.
_MIN_SIGMA = 1e-3


# Threshold token parsers. Order matters — the first matching pattern
# wins. Direction is captured so ABOVE / OVER / GE -> "above" and
# BELOW / UNDER / LE -> "below"; the model inverts the probability for
# below-thresholds.
_DIR_ABOVE = re.compile(
    r"^(?:ABOVE|OVER|GE|GT|GTE|UP|HIKE)([0-9_]+)$",
    re.IGNORECASE,
)
_DIR_BELOW = re.compile(
    r"^(?:BELOW|UNDER|LE|LT|LTE|DOWN|CUT)([0-9_]+)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ConsensusPriorResult:
    probability: float          # YES probability after clamping
    consensus: float | None
    threshold: float | None
    direction: str              # 'above' | 'below' | 'unknown'
    sigma: float                # standard deviation used in the Normal prior
    contributing_history: int   # count of historical release rows used
    feature_summary: dict[str, float]


def _parse_threshold_token(token: str) -> tuple[float | None, str]:
    """Return (numeric threshold, direction) parsed from a ticker token.

    Direction is "above" by default when only a number is present
    (e.g. ``HIKE25`` is treated as a 25bp above-threshold).  Returns
    ``(None, 'unknown')`` when the token is non-numeric.
    """
    if not token:
        return None, "unknown"
    t = token.upper().strip()
    m = _DIR_ABOVE.match(t)
    direction = "above"
    if not m:
        m = _DIR_BELOW.match(t)
        direction = "below" if m else "above"
    if m is None:
        # Bare numeric token (e.g. "25", "3_5") -> default to above.
        m_bare = re.match(r"^([0-9_]+)$", t)
        if not m_bare:
            return None, "unknown"
        raw = m_bare.group(1)
    else:
        raw = m.group(1)
    try:
        return float(raw.replace("_", ".")), direction
    except ValueError:
        return None, "unknown"


def _norm_cdf(x: float) -> float:
    """Standard-normal CDF via erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _clamp(p: float, lo: float = 0.05, hi: float = 0.95) -> float:
    return max(lo, min(hi, p))


def _rolling_forecast_error_sigma(
    history: Iterable[ReleaseRow],
) -> tuple[float, int]:
    """Return (sigma, n) over recent settled (consensus, actual) pairs.

    Skips rows without both consensus + actual.  When fewer than 3
    pairs are available, returns (0.0, n) so the caller falls back to
    the per-release-type default sigma.
    """
    diffs: list[float] = []
    for r in history:
        if r.consensus is None or r.actual is None:
            continue
        diffs.append(float(r.actual) - float(r.consensus))
    n = len(diffs)
    if n < 3:
        return 0.0, n
    mean = sum(diffs) / n
    var = sum((d - mean) ** 2 for d in diffs) / max(1, n - 1)
    return math.sqrt(max(0.0, var)), n


def consensus_prior_probability(
    ticker: EventsEconTicker,
    history: Iterable[ReleaseRow],
) -> ConsensusPriorResult:
    """Translate consensus + rolling forecast variance -> YES probability.

    `history` should include the upcoming release as the most recent
    row (carrying `consensus` but `actual=None`); historical rows
    (with both `consensus` and `actual`) drive the variance estimate.
    """
    history_list = list(history)
    threshold, direction = _parse_threshold_token(ticker.threshold)

    # Find the upcoming release row (actual IS NULL) for consensus.
    upcoming = next(
        (r for r in history_list if r.actual is None and r.consensus is not None),
        None,
    )
    consensus: float | None = upcoming.consensus if upcoming else None
    if consensus is None:
        # Fall back to the most recent `previous` from settled rows.
        for r in history_list:
            if r.previous is not None:
                consensus = float(r.previous)
                break

    sigma, n_hist = _rolling_forecast_error_sigma(history_list)
    if sigma <= 0.0:
        sigma = _DEFAULT_SIGMA_BY_TYPE.get(ticker.release_type.upper(), 0.5)
    sigma = max(_MIN_SIGMA, sigma)

    if consensus is None or threshold is None:
        # No consensus AND no threshold => prior 0.5.
        return ConsensusPriorResult(
            probability=0.5,
            consensus=consensus,
            threshold=threshold,
            direction=direction,
            sigma=sigma,
            contributing_history=n_hist,
            feature_summary={
                "history": float(n_hist),
                "has_consensus": 0.0 if consensus is None else 1.0,
                "has_threshold": 0.0 if threshold is None else 1.0,
            },
        )

    z = (threshold - consensus) / sigma
    # P(actual >= threshold) = 1 - CDF(z)
    p_above = 1.0 - _norm_cdf(z)
    p_yes = p_above if direction != "below" else 1.0 - p_above
    p_yes = _clamp(p_yes)

    return ConsensusPriorResult(
        probability=p_yes,
        consensus=consensus,
        threshold=threshold,
        direction=direction,
        sigma=sigma,
        contributing_history=n_hist,
        feature_summary={
            "history": float(n_hist),
            "has_consensus": 1.0,
            "has_threshold": 1.0,
            "z": z,
            "sigma": sigma,
        },
    )
