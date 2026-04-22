"""Hybrid model for the entertainment vertical (task #203).

Two model heads share a single dispatch entry point:

  * **Box-office head** (signal_type == BOX, RT)
    Log-normal opening-weekend prior fitted from settled history.
    Threshold tokens (ABOVE150, BELOW50) are interpreted in the
    settled value's natural unit (USD millions for BOX, percentage
    points for RT) and the YES probability is computed via the
    log-normal CDF (or normal CDF for RT scores).

  * **Awards head** (signal_type ∈ {OSCAR, EMMY, GRAMMY})
    Bayesian winner update — the upcoming signal row's `value`
    carries a precursor-derived prior probability (e.g. Globes /
    SAG / Critics' Choice winner share for the candidate title).  We
    smooth it with a category base rate from settled history then
    clamp to [0.05, 0.95].

  * **Charts head** (signal_type ∈ {SPOTIFY, STREAM})
    Threshold-style: positions are integer ranks (TOP10, TOP5,
    TOP1).  The probability is the fraction of recent settled rows
    for the same title that landed inside the requested rank window.

The model is deliberately thin — the swarm carries the qualitative
nuance.  This layer's job is to translate signal value + history into
a calibrated YES probability for a binary entertainment market.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable

from .signals import SignalRow
from .tickers import EntertainmentTicker


# Default sigma (log-space for BOX, raw for RT) when history is too
# thin to estimate one.  Tuned so a cold-start BOX market lands in the
# right ballpark (~0.6 in log-space ≈ 80% spread on the median).
_DEFAULT_LOG_SIGMA: dict[str, float] = {
    "BOX": 0.60,
    "RT":  10.0,   # RT score points
}


_MIN_SIGMA = 1e-3


# Threshold token parsers.
_DIR_ABOVE = re.compile(
    r"^(?:ABOVE|OVER|GE|GT|GTE|UP)([0-9_]+)$",
    re.IGNORECASE,
)
_DIR_BELOW = re.compile(
    r"^(?:BELOW|UNDER|LE|LT|LTE|DOWN)([0-9_]+)$",
    re.IGNORECASE,
)
# Chart-rank tokens: TOP10 / TOP5 / TOP1 — YES iff rank <= K.
_DIR_TOP = re.compile(r"^TOP([0-9]+)$", re.IGNORECASE)


@dataclass(frozen=True)
class HybridResult:
    probability: float          # YES probability after clamping
    head: str                   # 'box_office' | 'awards' | 'charts' | 'reviews' | 'unknown'
    threshold: float | None
    direction: str              # 'above' | 'below' | 'top' | 'unknown'
    sigma: float                # std-dev / scale used in the prior
    contributing_history: int   # count of historical rows used
    feature_summary: dict[str, float]


def _parse_threshold_token(token: str | None) -> tuple[float | None, str]:
    """Return (numeric threshold, direction) parsed from a ticker token.

    Direction "top" is reserved for chart-rank tokens (TOPK -> rank <= K).
    Returns ``(None, 'unknown')`` when the token is non-numeric.
    """
    if not token:
        return None, "unknown"
    t = token.upper().strip()
    m = _DIR_TOP.match(t)
    if m:
        try:
            return float(m.group(1)), "top"
        except ValueError:
            return None, "unknown"
    m = _DIR_ABOVE.match(t)
    if m:
        raw = m.group(1)
        try:
            return float(raw.replace("_", ".")), "above"
        except ValueError:
            return None, "unknown"
    m = _DIR_BELOW.match(t)
    if m:
        raw = m.group(1)
        try:
            return float(raw.replace("_", ".")), "below"
        except ValueError:
            return None, "unknown"
    m_bare = re.match(r"^([0-9_]+)$", t)
    if m_bare:
        try:
            return float(m_bare.group(1).replace("_", ".")), "above"
        except ValueError:
            return None, "unknown"
    return None, "unknown"


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _clamp(p: float, lo: float = 0.05, hi: float = 0.95) -> float:
    return max(lo, min(hi, p))


def _settled_values(history: Iterable[SignalRow]) -> list[float]:
    return [
        float(r.value)
        for r in history
        if r.settled and r.value is not None and r.value > 0.0
    ]


def _lognormal_fit(history: Iterable[SignalRow]) -> tuple[float, float, int]:
    """Return (mu, sigma, n) of log(value) over settled history."""
    values = _settled_values(history)
    if len(values) < 3:
        return 0.0, 0.0, len(values)
    logs = [math.log(v) for v in values]
    mu = sum(logs) / len(logs)
    var = sum((x - mu) ** 2 for x in logs) / max(1, len(logs) - 1)
    return mu, math.sqrt(max(0.0, var)), len(values)


def _normal_fit(history: Iterable[SignalRow]) -> tuple[float, float, int]:
    values = [
        float(r.value) for r in history
        if r.settled and r.value is not None
    ]
    if len(values) < 3:
        return 0.0, 0.0, len(values)
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / max(1, len(values) - 1)
    return mean, math.sqrt(max(0.0, var)), len(values)


def _box_office_conditioning(
    ticker: EntertainmentTicker,
    history_list: list[SignalRow],
) -> tuple[list[SignalRow], dict[str, float]]:
    """Pick the conditioning subset of settled history for the box-office head.

    Conditions on (in priority order):
      1. **Genre**: when an upcoming row carrying genre metadata exists
         for this title, we restrict history to the same genre tag.
      2. **Release window**: same calendar quarter / month bucket as
         the ticker's ``when`` (e.g. summer tentpoles vs winter
         drama).  Falls back to the unrestricted history when fewer
         than 3 rows survive.
      3. **Competitor load**: when an upcoming row carries a
         ``competitors_in_window`` payload field, we down-weight rows
         whose competitor load was lower than the upcoming release's.
    """
    settled = [r for r in history_list if r.settled and r.value is not None and r.value > 0.0]
    feats: dict[str, float] = {}

    upcoming = next(
        (r for r in history_list if not r.settled), None,
    )
    pl = (upcoming.payload or {}) if upcoming else {}
    genre = str(pl.get("genre", "")).lower() or None
    if genre:
        genre_subset = [
            r for r in settled
            if str((r.payload or {}).get("genre", "")).lower() == genre
        ]
        if len(genre_subset) >= 3:
            settled = genre_subset
            feats["cond_genre"] = 1.0

    when_token = ticker.when.upper()
    month_match = re.match(r"^\d{2}([A-Z]{3})$", when_token)
    if month_match:
        month_abbr = month_match.group(1)
        win_subset = [
            r for r in settled
            if r.event_at is not None
            and r.event_at.strftime("%b").upper() == month_abbr
        ]
        if len(win_subset) >= 3:
            settled = win_subset
            feats["cond_release_window"] = 1.0
    else:
        quarter_match = re.match(r"^\d{2}Q([1-4])$", when_token)
        if quarter_match:
            q = int(quarter_match.group(1))
            months = {1: (1, 2, 3), 2: (4, 5, 6), 3: (7, 8, 9), 4: (10, 11, 12)}[q]
            win_subset = [
                r for r in settled
                if r.event_at is not None and r.event_at.month in months
            ]
            if len(win_subset) >= 3:
                settled = win_subset
                feats["cond_release_window"] = 1.0

    competitors = pl.get("competitors_in_window")
    if isinstance(competitors, (int, float)) and competitors >= 0:
        feats["competitors_in_window"] = float(competitors)
        # Heavy competitor load (>=3) — penalise the central tendency
        # by 5% per extra competitor (capped 25%).
        feats["competitor_penalty"] = max(0.0, min(0.25, (float(competitors) - 2.0) * 0.05))

    return settled, feats


def _box_office_probability(
    ticker: EntertainmentTicker,
    history_list: list[SignalRow],
) -> HybridResult:
    threshold, direction = _parse_threshold_token(ticker.threshold)
    cond_history, cond_feats = _box_office_conditioning(ticker, history_list)
    mu, sigma, n_hist = _lognormal_fit(cond_history)
    if sigma <= 0.0:
        sigma = _DEFAULT_LOG_SIGMA["BOX"]
    sigma = max(_MIN_SIGMA, sigma)
    # Apply competitor-load penalty by shifting mu downward (heavier
    # competitor week => lower expected take => lower P(ABOVE)).
    competitor_penalty = cond_feats.get("competitor_penalty", 0.0)
    mu_eff = mu - competitor_penalty
    if threshold is None or threshold <= 0.0 or mu_eff <= 0.0:
        return HybridResult(
            probability=0.5, head="box_office", threshold=threshold,
            direction=direction, sigma=sigma, contributing_history=n_hist,
            feature_summary={"history": float(n_hist), "mu_log": mu_eff, "sigma_log": sigma,
                             "has_threshold": 0.0 if threshold is None else 1.0,
                             **cond_feats},
        )
    z = (math.log(threshold) - mu_eff) / sigma
    p_above = 1.0 - _norm_cdf(z)
    p_yes = p_above if direction != "below" else 1.0 - p_above
    return HybridResult(
        probability=_clamp(p_yes), head="box_office", threshold=threshold,
        direction=direction, sigma=sigma, contributing_history=n_hist,
        feature_summary={"history": float(n_hist), "mu_log": mu_eff, "sigma_log": sigma,
                         "z": z, "has_threshold": 1.0, **cond_feats},
    )


def _reviews_probability(
    ticker: EntertainmentTicker,
    history_list: list[SignalRow],
) -> HybridResult:
    threshold, direction = _parse_threshold_token(ticker.threshold)
    mean, sigma, n_hist = _normal_fit(history_list)
    if sigma <= 0.0:
        sigma = _DEFAULT_LOG_SIGMA["RT"]
    sigma = max(_MIN_SIGMA, sigma)
    if threshold is None or mean <= 0.0:
        return HybridResult(
            probability=0.5, head="reviews", threshold=threshold,
            direction=direction, sigma=sigma, contributing_history=n_hist,
            feature_summary={"history": float(n_hist), "mean": mean, "sigma": sigma,
                             "has_threshold": 0.0 if threshold is None else 1.0},
        )
    z = (threshold - mean) / sigma
    p_above = 1.0 - _norm_cdf(z)
    p_yes = p_above if direction != "below" else 1.0 - p_above
    return HybridResult(
        probability=_clamp(p_yes), head="reviews", threshold=threshold,
        direction=direction, sigma=sigma, contributing_history=n_hist,
        feature_summary={"history": float(n_hist), "mean": mean, "sigma": sigma,
                         "z": z, "has_threshold": 1.0},
    )


def _awards_probability(
    ticker: EntertainmentTicker,
    history_list: list[SignalRow],
) -> HybridResult:
    """Race-scoped discrete winner probability for an awards market.

    For an awards binary (e.g. KXOSCAR-25-BESTPIC-OPPENHEIMER):

      1. Identify the race field — every unsettled signal row of the
         same ``signal_type`` whose payload ``race_category`` matches
         the ticker's race (when the ticker carries one).  Each
         candidate row's ``value`` is the precursor-derived prior
         (Golden Globes / SAG / Critics' Choice / DGA winner share for
         that candidate).
      2. Match the ticker's candidate name against the field exactly
         (no fall-through to an unrelated row — that would price an
         unrelated candidate's signal into our market).
      3. Normalise across the field so the posteriors over the race
         sum to 1.  Our candidate's posterior is its share of that
         total.

    A ticker whose candidate has no representative row in the race
    field returns a small floor probability (5%) — we don't have
    evidence to support a YES.
    """
    st = ticker.signal_type.upper()
    race_lc = (ticker.race_category or "").lower()
    needle = (ticker.candidate or ticker.title).lower()

    field: list[SignalRow] = []
    for r in history_list:
        if r.settled or r.value is None or r.value <= 0.0:
            continue
        if r.signal_type.upper() != st:
            continue
        r_race = str((r.payload or {}).get("race_category", "")).lower()
        # Race-scoped tickers must match payload race_category when the
        # row carries one.  Rows missing race_category are admitted so
        # legacy variety/HR RSS placeholder rows still contribute.
        if race_lc and r_race and r_race != race_lc:
            continue
        field.append(r)

    def _is_us(r: SignalRow) -> bool:
        if r.title.lower() == needle:
            return True
        cand = str((r.payload or {}).get("candidate", "")).lower()
        return bool(cand) and cand == needle

    our_row = next((r for r in field if _is_us(r)), None)

    settled_field_wins = sum(
        1 for r in history_list
        if r.settled and (r.outcome or "").lower() == "win"
        and r.signal_type.upper() == st
        and (not race_lc
             or str((r.payload or {}).get("race_category", "")).lower() == race_lc)
    )

    if our_row is None:
        # No representative row for our candidate in the race — return
        # a small floor probability so we don't price unrelated rows
        # into this market.
        return HybridResult(
            probability=_clamp(0.05),
            head="awards", threshold=None, direction="unknown", sigma=0.0,
            contributing_history=settled_field_wins,
            feature_summary={"field_size": float(len(field)),
                             "matched_candidate": 0.0,
                             "race_category": float(1.0 if race_lc else 0.0)},
        )

    total = sum(float(r.value) for r in field)
    if total <= 0.0:
        # Degenerate field — fall back to the raw prior (clamped).
        posterior = float(our_row.value)
    else:
        posterior = float(our_row.value) / total
    return HybridResult(
        probability=_clamp(posterior),
        head="awards", threshold=None, direction="unknown", sigma=0.0,
        contributing_history=settled_field_wins,
        feature_summary={"field_size": float(len(field)),
                         "matched_candidate": 1.0,
                         "candidate_value": float(our_row.value),
                         "field_total": float(total),
                         "posterior": posterior,
                         "race_category": float(1.0 if race_lc else 0.0)},
    )


def _charts_probability(
    ticker: EntertainmentTicker,
    history_list: list[SignalRow],
) -> HybridResult:
    threshold, direction = _parse_threshold_token(ticker.threshold)
    title_lc = ticker.title.lower()
    same_title = [
        r for r in history_list
        if r.settled and r.value is not None
        and (title_lc in r.title.lower() or r.title.lower() in title_lc)
    ]
    universe = same_title if len(same_title) >= 3 else [
        r for r in history_list if r.settled and r.value is not None
    ]
    n_hist = len(universe)
    if threshold is None or n_hist == 0:
        return HybridResult(
            probability=0.5, head="charts", threshold=threshold,
            direction=direction, sigma=0.0, contributing_history=n_hist,
            feature_summary={"history": float(n_hist),
                             "has_threshold": 0.0 if threshold is None else 1.0},
        )
    if direction == "top":
        hits = sum(1 for r in universe if r.value is not None and r.value <= threshold)
    elif direction == "below":
        hits = sum(1 for r in universe if r.value is not None and r.value < threshold)
    else:  # default treat as ABOVE
        hits = sum(1 for r in universe if r.value is not None and r.value >= threshold)
    raw = hits / max(1, n_hist)
    return HybridResult(
        probability=_clamp(raw), head="charts", threshold=threshold,
        direction=direction, sigma=0.0, contributing_history=n_hist,
        feature_summary={"history": float(n_hist), "hits": float(hits),
                         "share": raw, "has_threshold": 1.0},
    )


def hybrid_probability(
    ticker: EntertainmentTicker,
    history: Iterable[SignalRow],
) -> HybridResult:
    """Dispatch to the head matching the ticker's signal_type.

    Falls through to a 0.5 prior with head='unknown' for ticker shapes
    we don't yet route — keeps the cycle alive on a swarm-only signal.
    """
    history_list = list(history)
    st = ticker.signal_type.upper()
    if st == "BOX":
        return _box_office_probability(ticker, history_list)
    if st == "RT":
        return _reviews_probability(ticker, history_list)
    if st in {"OSCAR", "EMMY", "GRAMMY"}:
        return _awards_probability(ticker, history_list)
    if st in {"SPOTIFY", "STREAM"}:
        return _charts_probability(ticker, history_list)
    return HybridResult(
        probability=0.5, head="unknown", threshold=None,
        direction="unknown", sigma=0.0, contributing_history=0,
        feature_summary={"history": 0.0},
    )


def at_baseline_probability(
    row: "object",
    history: list,
) -> "HybridResult":
    """Compute a deterministic baseline probability for a calendar row.

    Used by the public calendar route (no ticker needed): we return
    the at-median probability for box-office rows (0.5 — exactly at the
    fitted log-normal median) and the precursor-implied probability
    for awards rows.  Always deterministic so the JSON payload is
    snapshot-stable.
    """
    signal_type = str(getattr(row, "signal_type", "")).upper()
    settled = [r for r in history if getattr(r, "settled", False)]
    if signal_type == "BOX":
        _, sigma, n = _lognormal_fit(settled)
        if sigma <= 0.0:
            sigma = _DEFAULT_LOG_SIGMA["BOX"]
        return HybridResult(
            probability=0.5, head="box_office", threshold=None,
            direction="above", sigma=sigma, contributing_history=n,
            feature_summary={"history": float(n), "sigma_log": sigma,
                             "has_threshold": 0.0},
        )
    if signal_type == "RT":
        _, sigma, n = _normal_fit(settled)
        if sigma <= 0.0:
            sigma = _DEFAULT_LOG_SIGMA["RT"]
        return HybridResult(
            probability=0.5, head="reviews", threshold=None,
            direction="above", sigma=sigma, contributing_history=n,
            feature_summary={"history": float(n), "sigma": sigma,
                             "has_threshold": 0.0},
        )
    if signal_type in {"OSCAR", "EMMY", "GRAMMY"}:
        prior_raw = getattr(row, "value", None)
        prior = _clamp(float(prior_raw) if prior_raw is not None else 0.5, 0.01, 0.99)
        winners = [
            float(getattr(r, "value", 0.0))
            for r in settled
            if (getattr(r, "outcome", "") or "").lower() == "win"
            and getattr(r, "value", None) is not None
        ]
        base_rate = sum(winners) / len(winners) if len(winners) >= 3 else 0.5
        posterior = 0.7 * prior + 0.3 * base_rate
        return HybridResult(
            probability=_clamp(posterior), head="awards", threshold=None,
            direction="unknown", sigma=0.0, contributing_history=len(winners),
            feature_summary={"history": float(len(winners)),
                             "prior": prior, "base_rate": base_rate,
                             "posterior": posterior},
        )
    return HybridResult(
        probability=0.5, head="unknown", threshold=None,
        direction="unknown", sigma=0.0, contributing_history=0,
        feature_summary={"history": 0.0},
    )
