"""Poll-of-polls + state-bias-correction model for elections (task #201).

Design:
  * Aggregate every recent ``poll`` row scoped to the ticker into a
    weighted support % for the requested candidate.  Weights combine
    sample-size (`sqrt(n)/sqrt(800)`) with a recency decay
    (half-life=14 days).
  * Apply a per-region historical bias correction (state polls have
    persistent house effects — most pronounced in WI/PA/MI/AZ).
  * Map the corrected support % onto a YES probability via a logistic
    transformation centred on 50%.  An explicit Polymarket cross-ref
    row (kind=``polymarket_xref``), when present, is folded in as a
    further log-odds nudge so wide arb opportunities pull the model
    estimate toward the prediction-market consensus.

Output is clamped to [0.05, 0.95] so a sparse-poll prediction can
never yield a degenerate 0/1 that the gates would reject anyway.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from .polls import PollRow
from .tickers import ElectionTicker


# Per-region historical polling bias (additive percentage-points
# applied to the *Democratic* candidate before mapping to probability).
# Positive => polls historically over-state Democrat support, so we
# subtract.  Negative => polls historically under-state Dem support.
# Conservative defaults derived from 2016-2024 cycles; operators can
# tune via system_settings if needed.
_STATE_BIAS_PP: dict[str, float] = {
    "WI": -2.5, "PA": -1.8, "MI": -1.2, "OH": -2.4, "IA": -3.0,
    "FL": -1.0, "NC": -0.8, "AZ": -0.5, "GA": +0.4, "NV": +0.6,
    "TX": -1.4, "VA": +0.9, "CO": +0.6, "MN": -0.4,
}


# Half-life for recency decay (days).
_HALF_LIFE_DAYS = 14.0


# Polymarket cross-reference nudge.  Each xref row contributes a small
# log-odds nudge toward the implied Polymarket probability so wide arb
# spreads pull the model estimate without overriding the underlying
# poll signal.
_POLYMARKET_NUDGE_WEIGHT = 0.30


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


# Known person-name → party-label map used to normalize poll rows
# whose `candidate` field carries a person name against tickers
# whose candidate slot carries a party label (DEM / REP / etc).  Kept
# small + extensible; operators can append via runtime config later.
_NAME_TO_PARTY: dict[str, str] = {
    "TRUMP":     "REP",
    "VANCE":     "REP",
    "DESANTIS":  "REP",
    "HALEY":     "REP",
    "RAMASWAMY": "REP",
    "PENCE":     "REP",
    "CRUZ":      "REP",
    "BIDEN":     "DEM",
    "HARRIS":    "DEM",
    "NEWSOM":    "DEM",
    "WHITMER":   "DEM",
    "SHAPIRO":   "DEM",
    "BUTTIGIEG": "DEM",
    "WARNOCK":   "DEM",
    "FETTERMAN": "DEM",
}


def _party_of(label: str) -> str | None:
    """Best-effort party label for a candidate string.

    Handles party-label tickers (DEM/REP/GOP/IND), party-prefixed
    candidate codes (REP_TRUMP), and known person names via
    `_NAME_TO_PARTY`.  Returns None for unknown labels.
    """
    if not label:
        return None
    s = label.upper().strip()
    if s in {"DEM", "DEMOCRAT", "D"}:
        return "DEM"
    if s in {"REP", "GOP", "REPUBLICAN", "R"}:
        return "REP"
    if s.startswith("REP_") or s.startswith("R_"):
        return "REP"
    if s.startswith("DEM_") or s.startswith("D_"):
        return "DEM"
    return _NAME_TO_PARTY.get(s)


def _candidate_matches(ticker_cand: str, row_cand: str) -> bool:
    """True iff a poll row's candidate label is the same person/party
    as the ticker's candidate slot.

    Exact match wins.  Otherwise both labels are normalized to a party
    via `_party_of` and matched on party — so a ticker like
    `KXPRES-28-DEM` is satisfied by a "Harris (D) 47" poll row, and
    a ticker like `KXPRES-28-TRUMP` is satisfied by a `REP` row from
    the Cook ratings poller.
    """
    a = (ticker_cand or "").upper().strip()
    b = (row_cand or "").upper().strip()
    if not a or not b:
        return False
    if a == b:
        return True
    pa, pb = _party_of(a), _party_of(b)
    return pa is not None and pa == pb


def _bias_for_region(region: str | None, candidate: str) -> float:
    """Return the bias (in pp) to *add* to the candidate's reported support.

    Bias rows are stored as Democrat-side adjustments; a Republican
    candidate gets the inverse sign.  Unknown regions/candidates
    default to 0.0.  Person names are mapped to a party via
    `_NAME_TO_PARTY`; party-prefixed codes (REP_*) and bare labels
    (REP/GOP/D/R) are handled too.
    """
    if not region:
        return 0.0
    base = _STATE_BIAS_PP.get(region.upper(), 0.0)
    party = _party_of(candidate or "")
    if party == "REP":
        return -base
    return base


@dataclass(frozen=True)
class PollOfPollsResult:
    probability: float           # YES probability after clamping
    weighted_support_pct: float  # candidate support (0..100) after bias correction
    raw_support_pct: float       # before bias correction
    contributing_polls: int
    state_bias_pp: float
    polymarket_implied_prob: float | None
    feature_summary: dict[str, float]


def poll_of_polls_probability(
    ticker: ElectionTicker,
    polls: Iterable[PollRow],
    *,
    now: datetime | None = None,
) -> PollOfPollsResult:
    """Combine poll rows into a calibrated-ish probability for the YES side.

    Returns a `PollOfPollsResult` that the predictor wraps in the
    standard `Prediction` shape.  Empty input → prior 0.5 with no
    contributing polls.
    """
    now = now or datetime.now(timezone.utc)
    cand = (ticker.candidate or "").upper()

    weighted_support_num = 0.0
    weighted_support_den = 0.0
    contributing = 0
    polymarket_log_odds = 0.0
    polymarket_implied: float | None = None
    polymarket_count = 0
    polymarket_acc = 0.0

    feature_summary: dict[str, float] = {
        "polls": 0.0,
        "polymarket_xref": 0.0,
        "samples": 0.0,
    }

    for row in polls:
        kind = (row.kind or "").lower()
        if kind == "polymarket_xref":
            # Multi-outcome Polymarket markets emit one xref row per
            # outcome (DEM + REP + IND), so we must align on candidate
            # before folding the row into the log-odds nudge — otherwise
            # a Republican xref row would push a Democrat-side ticker's
            # probability the wrong way.  Mirrors the dashboard arb
            # endpoint's filtering.
            if cand and not _candidate_matches(cand, row.candidate or ""):
                continue
            try:
                implied = float(row.support_pct) / 100.0 if row.support_pct is not None else None
            except Exception:
                implied = None
            if implied is None:
                continue
            implied = _clamp(implied, 1e-3, 1 - 1e-3)
            decay = _decay_factor(row.observed_at, now)
            polymarket_log_odds += _logit(implied) * decay * _POLYMARKET_NUDGE_WEIGHT
            polymarket_acc += implied * decay
            polymarket_count += 1
            feature_summary["polymarket_xref"] += decay
            continue
        # Poll row: weight by sqrt(sample_size/800) * recency decay.
        # Polls without a sample size fall back to weight=1.0 so they
        # still count, just less than a 1k-sample national poll.
        if row.support_pct is None:
            continue
        # Polls store a *full* candidate-vs-opponent breakdown in the
        # payload; the resolver only carries the row when the
        # candidate name matches.  As a defensive guard, skip rows
        # whose candidate field doesn't match the ticker's candidate
        # (so a bundled multi-candidate row doesn't double-count).
        if cand and not _candidate_matches(cand, row.candidate or ""):
            continue
        decay = _decay_factor(row.observed_at, now)
        n = row.sample_size or 800
        size_w = math.sqrt(max(1, n) / 800.0)
        # explicit row weight from the poller (e.g. lower for
        # partisan-leaning pollsters) multiplies in.
        explicit = max(0.0, float(row.weight or 1.0))
        w = decay * size_w * explicit
        weighted_support_num += float(row.support_pct) * w
        weighted_support_den += w
        contributing += 1
        feature_summary["polls"] += w
        feature_summary["samples"] += float(n)

    if weighted_support_den <= 0.0 and polymarket_count == 0:
        # No polls AND no Polymarket cross-ref → prior 0.5.
        return PollOfPollsResult(
            probability=0.5,
            weighted_support_pct=50.0,
            raw_support_pct=50.0,
            contributing_polls=0,
            state_bias_pp=0.0,
            polymarket_implied_prob=None,
            feature_summary=feature_summary,
        )

    if weighted_support_den > 0.0:
        raw_support = weighted_support_num / weighted_support_den
    else:
        raw_support = 50.0

    bias_pp = _bias_for_region(ticker.region, cand)
    corrected_support = max(0.0, min(100.0, raw_support + bias_pp))

    # Map support % onto a YES probability via a logistic transform
    # centred on 50%.  Calibration constant tuned so a 5pp lead maps
    # to ~0.70 YES probability — close to the empirical 2008-2024
    # calibration of state polls inside the final 30 days.
    log_odds_from_polls = (corrected_support - 50.0) * 0.20

    log_odds = log_odds_from_polls + polymarket_log_odds
    raw_prob = _sigmoid(log_odds)
    p = _clamp(raw_prob)

    if polymarket_count > 0:
        polymarket_implied = polymarket_acc / max(
            1e-6, sum(
                _decay_factor(r.observed_at, now)
                for r in polls
                if (r.kind or "").lower() == "polymarket_xref"
            )
        )

    return PollOfPollsResult(
        probability=p,
        weighted_support_pct=corrected_support,
        raw_support_pct=raw_support,
        contributing_polls=contributing,
        state_bias_pp=bias_pp,
        polymarket_implied_prob=polymarket_implied,
        feature_summary=feature_summary,
    )
