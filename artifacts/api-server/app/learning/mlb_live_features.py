"""MLB-specific in-game live-feature builders.

Provides two distinct APIs that coexist as the MLB live path evolves
(mirrors :mod:`app.learning.nhl_live_features`):

* ``build_mlb_live`` (task #875) — vector-style builder used by the
  ``_predict_live_via_registry`` path in
  :mod:`app.learning.in_game_loop`.  Returns ``(names, values)``
  aligned to :data:`MLB_LIVE_FEATURE_NAMES`.
* ``build_mlb_live_candidate`` (task #881) — meta-model candidate
  builder mirroring :mod:`app.learning.nba_live_features`.  Returns a
  candidate dict scored via ``predictor.predict(..., league="MLB")``
  through ``_predict_live_for_market_meta``.

Both paths run during a cycle but write through different persistence
helpers so they do not double-stamp the same column on the same row.

----------------------------------------------------------------------
``build_mlb_live`` (task #875) — vector contract

Mirrors the niche-sports / NHL builder contract:

  * Takes a free-form candidate dict (the row produced by
    :func:`app.learning.in_game_loop._fetch_live_eligible`) and returns
    ``(names, values)`` where ``len(names) == len(values)`` and every
    value is a finite float in a bounded range.
  * Null-safe: a missing/garbled live-state field MUST NOT raise and
    MUST NOT emit ``nan`` / ``inf``.  Defaults are deterministic so
    the meta-model + per-league calibrator can replay shadow runs.
  * Column order is the trained-model contract — changing it without
    bumping the model version would silently mis-feed the calibrator.

MLB-specific signal (informed by Section 9 of the daily honesty
report's MLB pre-vs-in-game Brier split):

  * ``score_diff_home_minus_away`` — signed run margin; the single
    strongest in-game predictor once past the 5th inning.
  * ``abs_score_diff`` — magnitude of the lead.
  * ``inning_index`` — 1..12 (10+ is extras).  Late innings carry
    much more leverage than early ones.
  * ``regulation_innings_remaining_frac`` — fraction of the 9-inning
    regulation game still ahead, in [0, 1].  Extras clamp to 0.
  * ``is_extras`` — sudden-death-ish flag (any extra inning is a
    walk-off regime, no ties in MLB).
  * ``pre_game_model_yes_prob`` — pre-game baseline from the
    ``predictions`` row.  Anchors the live re-eval so cold starts
    (no live signal yet) still produce a sane probability.
  * ``kalshi_yes_price`` — current market mid; lets the calibrator
    learn whether the book is leading or lagging our model.
  * ``live_state_age_seconds`` — freshness of the sportradar pull.
    Already gated by :func:`app.risk.gates.gate_stale_live_state`, but
    the model gets it as a feature so it can soft-discount stale rows
    instead of hard-rejecting at the gate boundary.

MLB has no game clock the way NHL/NBA do — the inning index itself is
the progress signal, and the fractional "regulation remaining" is
derived purely from how many full innings are left of the 9.
"""
from __future__ import annotations

import math
import re
from typing import Any, Mapping


# Public column order — DO NOT REORDER without bumping the model
# version.  Append-only is safe; reorder is not.
MLB_LIVE_FEATURE_NAMES: tuple[str, ...] = (
    "score_diff_home_minus_away",
    "abs_score_diff",
    "inning_index",
    "regulation_innings_remaining_frac",
    "is_extras",
    "pre_game_model_yes_prob",
    "kalshi_yes_price",
    "live_state_age_seconds",
)


_REGULATION_INNINGS = 9


def _as_float(value: Any, default: float) -> float:
    """Coerce to a finite float; fall back on ``default`` for any of
    ``None``, non-numeric strings, ``NaN``, or ``inf``.
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return float(default)
    if f != f or f in (float("inf"), float("-inf")):
        return float(default)
    return f


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _regulation_innings_remaining_frac(inning_idx: int) -> float:
    """Fraction of regulation (9 innings) still ahead, in [0, 1].

    Extras (inning >= 10) clamp to 0 — regulation is over.  The
    fraction is computed from full innings remaining BEFORE the
    current one starts: top of the 1st = 9/9 = 1.0, start of the
    9th = 1/9.
    """
    if inning_idx <= 0:
        return 1.0
    if inning_idx > _REGULATION_INNINGS:
        return 0.0
    remaining = _REGULATION_INNINGS - (inning_idx - 1)
    return _clamp(remaining / float(_REGULATION_INNINGS), 0.0, 1.0)


def build_mlb_live(cand: dict[str, Any]) -> tuple[list[str], list[float]]:
    """Build the MLB live feature vector for ``cand``.

    Inputs (all optional — defaults are deterministic):
      * ``home_score``, ``away_score`` — integer runs.
      * ``period`` — sportradar inning hint (``"1"``..``"9"``,
        ``"inning_5"``, ``"top_3"`` / ``"bottom_8"``, ``"extras"``).
      * ``model_yes_prob`` — pre-game baseline; defaults to 0.5.
      * ``kalshi_price`` — current YES mid; defaults to 0.5.
      * ``state_age_seconds`` — sportradar freshness; defaults to 0.

    Returns ``(names, values)`` aligned to
    :data:`MLB_LIVE_FEATURE_NAMES`.
    """
    home = _as_float(cand.get("home_score"), 0.0)
    away = _as_float(cand.get("away_score"), 0.0)
    diff = home - away

    inning_idx = _parse_period(cand.get("period"))
    reg_frac = _regulation_innings_remaining_frac(inning_idx)
    is_extras = inning_idx > _REGULATION_INNINGS

    pre_prob = _clamp(_as_float(cand.get("model_yes_prob"), 0.5), 0.0, 1.0)
    yes_price = _clamp(_as_float(cand.get("kalshi_price"), 0.5), 0.0, 1.0)
    # Cap state-age so a one-off stale outlier doesn't blow out the
    # feature scale.  The risk gate hard-rejects past the per-league
    # freshness budget; the feature just needs a bounded soft-signal.
    age_secs = _clamp(_as_float(cand.get("state_age_seconds"), 0.0), 0.0, 600.0)

    values = [
        diff,
        abs(diff),
        float(inning_idx),
        reg_frac,
        1.0 if is_extras else 0.0,
        pre_prob,
        yes_price,
        age_secs,
    ]
    return list(MLB_LIVE_FEATURE_NAMES), values


# ---------------------------------------------------------------------------
# Meta-model candidate builder (task #881).
# ---------------------------------------------------------------------------


_LOGISTIC_SCALE = 2.5

_PERIOD_WEIGHTS: dict[int, float] = {
    1: 0.10,
    2: 0.20,
    3: 0.30,
    4: 0.45,
    5: 0.60,
    6: 0.75,
    7: 0.90,
    8: 1.00,
    9: 1.05,
    10: 1.15,
    11: 1.20,
    12: 1.20,
}

_PERIOD_LOOKUP_RE = re.compile(r"(\d+)")


def _parse_period(period: Any) -> int:
    """Coerce a Sportradar inning hint into an integer 1..12.

    MLB feeds write inning numbers a few different ways: ``"5"``,
    ``"inning_5"``, ``"top_3"`` / ``"bottom_8"`` (top/bottom-of-inning),
    or ``"extras"`` for any inning past 9.  Defaults to 1 for missing
    / unparseable inputs.
    """
    if period is None:
        return 1
    s = str(period).strip().lower()
    if not s:
        return 1
    if "extra" in s:
        return 10
    m = _PERIOD_LOOKUP_RE.search(s)
    if m:
        try:
            n = int(m.group(1))
            return max(1, min(n, 12))
        except ValueError:
            pass
    return 1


def _score_diff_yes_prob(
    *, score_diff: int, period_idx: int, pre_game_prob: float | None
) -> float:
    weight = _PERIOD_WEIGHTS.get(period_idx, 1.0)
    z = (weight * float(score_diff)) / _LOGISTIC_SCALE
    live_p = 1.0 / (1.0 + math.exp(-z))
    if pre_game_prob is None:
        anchor = live_p
    else:
        blend = max(0.0, min(weight, 1.0))
        anchor = blend * live_p + (1.0 - blend) * float(pre_game_prob)
    return max(0.01, min(0.99, anchor))


def build_mlb_live_candidate(
    row: Mapping[str, Any],
    *,
    live_state: Mapping[str, Any] | None,
) -> dict[str, Any]:
    league = (row.get("league") or "MLB").upper()
    pre_game_prob = row.get("model_yes_prob")
    try:
        pre_p = float(pre_game_prob) if pre_game_prob is not None else None
    except (TypeError, ValueError):
        pre_p = None
    kalshi_price = row.get("kalshi_price")
    try:
        kp = float(kalshi_price) if kalshi_price is not None else None
    except (TypeError, ValueError):
        kp = None

    home_score = 0
    away_score = 0
    period_idx = 1
    clock_str = ""
    status = ""
    if live_state:
        try:
            home_score = int(live_state.get("home_score") or 0)
        except (TypeError, ValueError):
            home_score = 0
        try:
            away_score = int(live_state.get("away_score") or 0)
        except (TypeError, ValueError):
            away_score = 0
        period_idx = _parse_period(live_state.get("period"))
        clock_str = str(live_state.get("clock") or "")
        status = str(live_state.get("status") or "")

    score_diff = home_score - away_score
    live_anchor = _score_diff_yes_prob(
        score_diff=score_diff,
        period_idx=period_idx,
        pre_game_prob=pre_p,
    )

    return {
        "market_id": row.get("market_id"),
        "kalshi_market_id": row.get("market_id"),
        "league": league,
        "market_type": "moneyline",
        "side": "YES",
        "recommended_action": "BUY_YES",
        "consensus_probability": live_anchor,
        "kalshi_price": kp,
        "market_prob": kp,
        "game_id": row.get("game_id"),
        "game_time": row.get("game_time"),
        "live_state": {
            "home_score": home_score,
            "away_score": away_score,
            "score_diff": score_diff,
            "period_idx": period_idx,
            "clock": clock_str,
            "status": status,
            "pre_game_prob": pre_p,
            "live_anchor": live_anchor,
        },
    }


__all__ = [
    "build_mlb_live_candidate",
    "_parse_period",
    "_score_diff_yes_prob",
]
