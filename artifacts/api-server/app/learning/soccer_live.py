"""Soccer in-game live predictor (task #857).

First per-sport wiring for the in-game live prediction loop scaffolded
in :mod:`app.learning.in_game_loop`.  Shared across all seven soccer
leagues the orchestrator trades (EPL / LALIGA / SERIEA / BUNDES /
LIGUE1 / MLS / UCL — canonical league names match
``app.sports.soccer_context.SOCCER_LEAGUES``).

Design notes
------------

* The live feature builder reads the freshest ``sportradar_live_state``
  row for the row's ``game_id`` so the predictor sees current score,
  period, and clock without re-pulling the Sportradar feed.
* Without per-row leg orientation (a Kalshi soccer 3-way market does
  not carry "is this the HOME/DRAW/AWAY leg?" on the predictions
  table), the live predictor stays leg-agnostic by blending the
  pre-game calibrated probability toward the current Kalshi mid as
  the match progresses.  Both inputs already encode leg orientation
  (the pre-game model probability was produced for THIS market, and
  ``kalshi_price`` is THIS market's mid) so blending them is correct
  whichever leg the row represents.
* The blend weight grows monotonically from 0 (kickoff) to a capped
  ceiling (90 minutes), so early-match readings stay close to the
  pre-game prior and late-match readings track the market consensus.
  A small score-state confidence kicker biases the result toward the
  market direction when the score gap is wide — a leg-agnostic
  proxy for "the live state has resolved the prior" since the market
  has already absorbed the score swing.
* Output is passed through :mod:`app.calibrators` ``apply_both`` so
  the value matches the same per-league calibration the pre-game
  predictor uses — the daily honesty report can then compare
  pre-game vs in-game Brier on the SAME calibration footing
  (otherwise a calibration mismatch would mask the model's actual
  drift).

Ships in **shadow mode**: the per-sport enable flag
``risk.live_loop_enabled_soccer`` is False by default, so the
operator console drives the flip once Section 9 of the daily
honesty report shows the soccer pre-vs-in-game Brier delta is
within the 0.02 ship criterion (per the task brief).
"""
from __future__ import annotations

import logging
import re
from typing import Any

from .. import calibrators, db


log = logging.getLogger(__name__)


# Canonical soccer league names that match ``predictions.league``
# (mirrors :data:`app.sports.soccer_context.SOCCER_LEAGUES` — the
# in_game_loop's SPORT_LEAGUES["soccer"] tuple is rewritten to match
# these so the fetch query actually finds rows).
SOCCER_LEAGUES: frozenset[str] = frozenset({
    "EPL", "LALIGA", "SERIEA", "BUNDES", "LIGUE1", "MLS", "UCL",
})


# Regulation length used to estimate "fraction of the match elapsed".
# Stoppage time and extra time push past 90 — we cap at 1.0 so a
# 95-minute reading doesn't over-weight the market consensus beyond
# the ceiling below.
_REGULATION_MINUTES = 90.0

# Ceiling on the market-consensus blend weight.  At full-time the
# predictor is mostly tracking the market but still keeps a sliver
# of the pre-game prior so a flash mispricing during stoppage time
# can't single-handedly swing the shadow log.
_MAX_MARKET_WEIGHT = 0.85

# Per-goal score-state confidence kicker (probability points).  A
# clean two-goal lead late in the match pulls the predictor a touch
# further toward the market direction than the time-blend alone —
# matches the empirical pattern that wide leads converge faster
# than the linear time prior would suggest.
_SCORE_GAP_KICKER_PP = 0.02
_MAX_SCORE_GAP_KICKER_PP = 0.06


def is_soccer_league(league: str | None) -> bool:
    """Case-insensitive membership check used by the in-game loop
    dispatcher so callers don't have to upper-case + frozenset-test
    inline.
    """
    if not league:
        return False
    return str(league).strip().upper() in SOCCER_LEAGUES


# ---------------------------------------------------------------------------
# Feature builder
# ---------------------------------------------------------------------------


_CLOCK_RE = re.compile(r"^\s*(\d{1,3})(?::(\d{1,2}))?\s*$")

# Period name → minutes already elapsed at the START of the period.
# Anything we don't recognise falls through to 0 (the clock will then
# carry the elapsed minutes on its own).
_PERIOD_BASE_MINUTES: dict[str, float] = {
    "1st_half": 0.0,
    "first_half": 0.0,
    "1": 0.0,
    "halftime": 45.0,
    "half_time": 45.0,
    "ht": 45.0,
    "2nd_half": 45.0,
    "second_half": 45.0,
    "2": 45.0,
    "fulltime": 90.0,
    "full_time": 90.0,
    "ft": 90.0,
    "ot": 90.0,
    "extra_time": 90.0,
    "1st_extra": 90.0,
    "2nd_extra": 105.0,
    "penalties": 120.0,
}


def _parse_clock_minutes(clock: str | None) -> float:
    """Parse a Sportradar soccer clock string into elapsed minutes.

    The soccer clock counts UP from 0 each half (so "37:12" in the
    2nd half is minute 37 of that half, NOT minute 82 of the match —
    the period base handles the cross-half offset).  Returns 0.0 on
    any parse failure so a malformed feed never blows up the cycle.
    """
    if not clock:
        return 0.0
    m = _CLOCK_RE.match(str(clock))
    if not m:
        return 0.0
    try:
        mins = int(m.group(1))
        secs = int(m.group(2) or 0)
    except (TypeError, ValueError):
        return 0.0
    return float(mins) + float(secs) / 60.0


def _estimate_minutes_elapsed(period: str | None, clock: str | None) -> float:
    """Estimate total minutes elapsed in the match from period + clock.

    Returns 0.0 for an unparseable period AND an unparseable clock so
    the predictor degenerates to the pre-game prior (the safest
    behaviour when the live feed is wedged or pre-kickoff).
    """
    base = _PERIOD_BASE_MINUTES.get(
        (period or "").strip().lower(), 0.0
    )
    return base + _parse_clock_minutes(clock)


async def _fetch_live_state(
    league: str, game_id: str | None
) -> dict[str, Any] | None:
    """Pull the freshest ``sportradar_live_state`` row for this game.

    Returns ``None`` on any DB error or when no live row exists yet
    so the caller can degrade to the pre-game prior instead of
    blocking the cycle.
    """
    if not game_id:
        return None
    try:
        row = await db.fetchrow(
            """
            SELECT status, home_score, away_score, period, clock,
                   extract(epoch from (now() - fetched_at))::float
                       AS state_age_seconds
              FROM sportradar_live_state
             WHERE league = $1 AND game_id = $2
             ORDER BY fetched_at DESC
             LIMIT 1
            """,
            league,
            str(game_id),
        )
    except Exception as e:  # pragma: no cover — defensive
        log.debug(
            "soccer_live live-state lookup failed for %s/%s: %s",
            league, game_id, e,
        )
        return None
    if row is None:
        return None
    return dict(row)


def build_live_features(
    *, row: dict[str, Any], live_state: dict[str, Any] | None
) -> dict[str, float]:
    """Assemble the feature dict the predictor will consume.

    Inputs:
      * ``row`` — one open-prediction row as returned by the in_game_loop
        fetch (carries pre-game ``model_yes_prob``, ``kalshi_price``,
        ``league``, etc).
      * ``live_state`` — the freshest ``sportradar_live_state`` row
        for this row's ``game_id``, or ``None`` when the feed has
        no entry yet.

    Returned dict keys (all floats — the downstream code paths and
    the daily honesty report's metadata blob both expect a flat
    numeric shape).  When ``live_state`` is None all of the live-
    derived features collapse to 0.0 so the predictor falls back to
    the pre-game prior.
    """
    pregame = _coerce_prob(row.get("model_yes_prob"))
    yes_price = _coerce_prob(row.get("kalshi_price"))
    feats: dict[str, float] = {
        "pregame_prob": pregame if pregame is not None else 0.5,
        "kalshi_price": yes_price if yes_price is not None else 0.5,
        "has_pregame": 1.0 if pregame is not None else 0.0,
        "has_market_price": 1.0 if yes_price is not None else 0.0,
    }
    if not live_state:
        feats.update({
            "home_score": 0.0,
            "away_score": 0.0,
            "score_diff": 0.0,
            "score_total": 0.0,
            "minutes_elapsed": 0.0,
            "minutes_fraction": 0.0,
            "has_live_state": 0.0,
            "state_age_seconds": 0.0,
        })
        return feats
    hs = _coerce_int(live_state.get("home_score"))
    as_ = _coerce_int(live_state.get("away_score"))
    minutes = _estimate_minutes_elapsed(
        live_state.get("period"), live_state.get("clock"),
    )
    fraction = max(0.0, min(1.0, minutes / _REGULATION_MINUTES))
    age = live_state.get("state_age_seconds")
    feats.update({
        "home_score": float(hs),
        "away_score": float(as_),
        "score_diff": float(hs - as_),
        "score_total": float(hs + as_),
        "minutes_elapsed": float(minutes),
        "minutes_fraction": float(fraction),
        "has_live_state": 1.0,
        "state_age_seconds": float(age) if age is not None else 0.0,
    })
    return feats


def _coerce_prob(value: Any) -> float | None:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v < 0.0 or v > 1.0:
        return None
    return v


def _coerce_int(value: Any) -> int:
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Prediction
# ---------------------------------------------------------------------------


def _blend(
    pregame: float, market: float, minutes_fraction: float
) -> float:
    """Time-weighted blend of the pre-game prior and the market mid.

    ``minutes_fraction`` in [0, 1].  Returns the blended probability
    in [0, 1].
    """
    w = max(0.0, min(_MAX_MARKET_WEIGHT, minutes_fraction * _MAX_MARKET_WEIGHT))
    return (1.0 - w) * pregame + w * market


def _score_gap_kicker(
    score_diff: float, market: float, minutes_fraction: float
) -> float:
    """Probability-point bias toward the market direction when the
    score gap is wide.  Returns a signed delta to add to the blend.

    Leg-agnostic: we don't know whether YES = HOME / DRAW / AWAY,
    but ``market`` (the Kalshi mid) is leg-aware, so we kick the
    blended probability TOWARD the market (above 0.5 → push up,
    below 0.5 → push down) proportional to the absolute score gap
    and how late we are in the match.
    """
    gap = abs(score_diff)
    if gap < 1.0 or minutes_fraction <= 0.0:
        return 0.0
    magnitude = min(_MAX_SCORE_GAP_KICKER_PP, gap * _SCORE_GAP_KICKER_PP)
    magnitude *= minutes_fraction
    return magnitude if market >= 0.5 else -magnitude


def _raw_live_probability(features: dict[str, float]) -> float | None:
    """Closed-form combination of features into a raw YES probability.

    Returns ``None`` when neither a pre-game prior nor a market price
    is available — the in_game_loop treats None as "no live update
    this cycle" and leaves the existing ``model_yes_prob`` untouched.
    """
    has_pre = features.get("has_pregame", 0.0) > 0.5
    has_mkt = features.get("has_market_price", 0.0) > 0.5
    if not (has_pre or has_mkt):
        return None
    pregame = features.get("pregame_prob", 0.5)
    market = features.get("kalshi_price", pregame)
    if not has_mkt:
        # No market price — fall back to the pre-game prior, no live
        # information to blend in.
        return pregame
    if not has_pre:
        # No pre-game prior — the market is our best estimate.
        return market
    if features.get("has_live_state", 0.0) <= 0.5:
        # Live feed isn't carrying a row for this game yet → degrade
        # to the pre-game prior (safer than over-weighting the market
        # before kickoff state is confirmed).
        return pregame
    fraction = features.get("minutes_fraction", 0.0)
    blended = _blend(pregame, market, fraction)
    blended += _score_gap_kicker(
        features.get("score_diff", 0.0), market, fraction,
    )
    return max(0.0, min(1.0, blended))


async def predict_live_yes(
    row: dict[str, Any],
) -> tuple[float | None, dict[str, Any]]:
    """Compute a calibrated YES probability for one soccer market row.

    Returns ``(probability, metadata)``.  ``probability`` is None
    when there isn't enough signal to produce a reading (no
    pre-game prior AND no market price) — the in_game_loop treats
    this as "no live update this cycle".  ``metadata`` is the
    feature blob + calibration info the loop persists for forensics.
    """
    league = (row.get("league") or "").strip().upper() or None
    live_state = await _fetch_live_state(league or "", row.get("game_id"))
    features = build_live_features(row=row, live_state=live_state)
    raw = _raw_live_probability(features)
    meta: dict[str, Any] = {
        "source": "soccer_live",
        "league": league,
        "features": features,
    }
    if raw is None:
        meta["skipped_reason"] = "no pre-game prior and no market price"
        return None, meta
    # Per-league calibration so pre-game and in-game Brier are compared
    # on the same calibration footing in Section 9 of the daily report.
    try:
        cal_prob, cal_info = calibrators.apply_both(float(raw), league)
    except Exception as e:  # pragma: no cover — defensive
        log.debug(
            "soccer_live calibration bypass for %s: %s", league, e,
        )
        cal_prob = float(raw)
        cal_info = {"applied": False, "reason": f"error: {type(e).__name__}"}
    meta["raw_probability"] = float(raw)
    meta["calibration"] = cal_info
    return float(cal_prob), meta


__all__ = [
    "SOCCER_LEAGUES",
    "is_soccer_league",
    "build_live_features",
    "predict_live_yes",
]
