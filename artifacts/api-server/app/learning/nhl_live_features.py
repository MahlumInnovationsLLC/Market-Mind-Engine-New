"""NHL live-feature builder for the in-game prediction loop (task #856).

Per-sport live builders mirror the niche-sports builder contract in
:mod:`app.niche_sports.features`:

  * Takes a free-form candidate dict (the row produced by
    :func:`app.learning.in_game_loop._fetch_live_eligible`) and returns
    ``(names, values)`` where ``len(names) == len(values)`` and every
    value is a finite float in a bounded range.
  * Null-safe: a missing/garbled live-state field MUST NOT raise and
    MUST NOT emit ``nan`` / ``inf``.  Defaults are deterministic so
    the meta-model + per-league calibrator can replay shadow runs.
  * Column order is the trained-model contract — changing it without
    bumping the model version would silently mis-feed the calibrator.

NHL-specific signal (informed by Section 9 of the daily honesty
report's NHL pre-vs-in-game Brier split):

  * ``score_diff_home_minus_away`` — signed score margin; the single
    strongest predictor of an NHL game's eventual winner once the 1st
    period is over.
  * ``abs_score_diff`` — magnitude of the lead.
  * ``period_index`` — 1, 2, 3, 4 (OT), 5 (SO).  Late-game possessions
    are worth more than 1st-period ones.
  * ``regulation_time_remaining_frac`` — fraction of the 60-minute
    regulation clock still on the board, in [0, 1].  Combined with
    ``score_diff`` this is the live-state's leverage signal.
  * ``is_overtime``, ``is_shootout`` — sudden-death flags so the
    calibrator can pick up the regime change (one-goal wins, no ties).
  * ``pre_game_model_yes_prob`` — pre-game baseline from the
    ``predictions`` row.  Anchors the live re-eval so cold starts
    (no live signal yet) still produce a sane probability.
  * ``kalshi_yes_price`` — current market mid; lets the calibrator
    learn whether the book is leading or lagging our model.
  * ``live_state_age_seconds`` — freshness of the sportradar pull.
    Already gated by :func:`app.risk.gates.gate_stale_live_state`, but
    the model gets it as a feature so it can soft-discount stale rows
    instead of hard-rejecting at the gate boundary.

Regulation time is 3 x 20:00 = 60:00.  The sportradar ``clock`` field
is ``mm:ss`` and counts DOWN within each period.  We compute total
elapsed regulation time as
``20 * (period_idx - 1) + (20 - clock_minutes_remaining)``.  Periods
past 3 (OT / SO) clamp to a regulation_remaining_frac of 0.
"""
from __future__ import annotations

from typing import Any


# Public column order — DO NOT REORDER without bumping the model
# version.  Append-only is safe; reorder is not.
NHL_LIVE_FEATURE_NAMES: tuple[str, ...] = (
    "score_diff_home_minus_away",
    "abs_score_diff",
    "period_index",
    "regulation_time_remaining_frac",
    "is_overtime",
    "is_shootout",
    "pre_game_model_yes_prob",
    "kalshi_yes_price",
    "live_state_age_seconds",
)


_REGULATION_PERIOD_SECONDS = 20.0 * 60.0  # 20:00 minutes per period
_REGULATION_PERIODS = 3
_REGULATION_TOTAL_SECONDS = _REGULATION_PERIOD_SECONDS * _REGULATION_PERIODS


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


def _parse_period(raw: Any) -> tuple[int, bool, bool]:
    """Return ``(period_index, is_overtime, is_shootout)``.

    Sportradar's NHL ``period`` field is a string — typically ``"1"``,
    ``"2"``, ``"3"``, ``"OT"``, or ``"SO"``.  Some feeds use integer
    period numbers (4 = OT, 5 = SO).  Defaults to period 1, regulation
    on any unparseable value so the builder stays null-safe.
    """
    if raw is None:
        return 1, False, False
    s = str(raw).strip().lower()
    if not s:
        return 1, False, False
    if s in ("ot", "overtime", "ot1"):
        return 4, True, False
    if s.startswith("ot"):  # OT2, OT3 in playoffs
        return 4, True, False
    if s in ("so", "shootout"):
        return 5, False, True
    try:
        n = int(s)
    except ValueError:
        return 1, False, False
    if n <= 0:
        return 1, False, False
    if n >= 5:
        return 5, False, True
    if n == 4:
        return 4, True, False
    return n, False, False


def _parse_clock_seconds_remaining(raw: Any) -> float:
    """Parse a ``mm:ss`` (or ``m:ss``) clock string into seconds left
    in the current period.  Returns ``_REGULATION_PERIOD_SECONDS``
    (full 20:00) when the clock is missing or unparseable — a fresh
    period with no clock yet is "all time remaining".
    """
    if raw is None:
        return _REGULATION_PERIOD_SECONDS
    s = str(raw).strip()
    if not s:
        return _REGULATION_PERIOD_SECONDS
    parts = s.split(":")
    if len(parts) != 2:
        # Some feeds send seconds-only (e.g. "1200").
        try:
            v = float(s)
        except ValueError:
            return _REGULATION_PERIOD_SECONDS
        return _clamp(v, 0.0, _REGULATION_PERIOD_SECONDS)
    try:
        mins = int(parts[0])
        secs = int(parts[1])
    except ValueError:
        return _REGULATION_PERIOD_SECONDS
    total = mins * 60.0 + secs
    return _clamp(total, 0.0, _REGULATION_PERIOD_SECONDS)


def _regulation_remaining_frac(period_idx: int, period_secs_left: float) -> float:
    """Fraction of regulation (60:00) still on the clock, in [0, 1].

    Periods 4 (OT) and 5 (SO) clamp to 0 — regulation is over.
    """
    if period_idx <= 0:
        return 1.0
    if period_idx > _REGULATION_PERIODS:
        return 0.0
    full_periods_remaining = _REGULATION_PERIODS - period_idx
    secs_remaining = full_periods_remaining * _REGULATION_PERIOD_SECONDS + period_secs_left
    return _clamp(secs_remaining / _REGULATION_TOTAL_SECONDS, 0.0, 1.0)


def build_nhl_live(cand: dict[str, Any]) -> tuple[list[str], list[float]]:
    """Build the NHL live feature vector for ``cand``.

    Inputs (all optional — defaults are deterministic):
      * ``home_score``, ``away_score`` — integer goals.
      * ``period`` — sportradar period string (``"1"``..``"3"``,
        ``"OT"``, ``"SO"``) or integer.
      * ``clock`` — ``mm:ss`` remaining in the current period.
      * ``model_yes_prob`` — pre-game baseline; defaults to 0.5.
      * ``kalshi_price`` — current YES mid; defaults to 0.5.
      * ``state_age_seconds`` — sportradar freshness; defaults to 0.

    Returns ``(names, values)`` aligned to
    :data:`NHL_LIVE_FEATURE_NAMES`.
    """
    home = _as_float(cand.get("home_score"), 0.0)
    away = _as_float(cand.get("away_score"), 0.0)
    diff = home - away

    period_idx, is_ot, is_so = _parse_period(cand.get("period"))
    period_secs_left = _parse_clock_seconds_remaining(cand.get("clock"))
    reg_frac = _regulation_remaining_frac(period_idx, period_secs_left)

    pre_prob = _clamp(_as_float(cand.get("model_yes_prob"), 0.5), 0.0, 1.0)
    yes_price = _clamp(_as_float(cand.get("kalshi_price"), 0.5), 0.0, 1.0)
    # Cap state-age so a one-off stale outlier doesn't blow out the
    # feature scale.  The risk gate hard-rejects past the per-league
    # freshness budget; the feature just needs a bounded soft-signal.
    age_secs = _clamp(_as_float(cand.get("state_age_seconds"), 0.0), 0.0, 600.0)

    values = [
        diff,
        abs(diff),
        float(period_idx),
        reg_frac,
        1.0 if is_ot else 0.0,
        1.0 if is_so else 0.0,
        pre_prob,
        yes_price,
        age_secs,
    ]
    return list(NHL_LIVE_FEATURE_NAMES), values
