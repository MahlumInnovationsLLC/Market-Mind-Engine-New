"""NHL in-game live predictor (task #885).

Third per-sport wiring for the in-game live prediction loop scaffolded
in :mod:`app.learning.in_game_loop`.  Mirrors the shape of
:mod:`app.learning.nba_live` (task #878) — same feature-builder +
``predict_live_yes`` contract so the dispatcher in
``_predict_live_for_market`` only has to add a single ``elif`` branch.

Design notes
------------

* NHL regulation = three 20-minute periods (60 min total).  The
  Sportradar play-by-play feed ticks ~15s, populates ``period`` with
  the period number (``1`` / ``2`` / ``3``, ``OT`` / ``ot`` for
  overtime) and ``clock`` as a COUNT-DOWN string (e.g. ``"05:00"`` =
  5 minutes remaining in the period — same convention as NBA, opposite
  of soccer which counts UP).
* Like the NBA predictor we stay leg-agnostic (the predictions table
  doesn't carry a HOME/AWAY leg orientation on each row), so we blend
  the pre-game prior toward the current Kalshi mid as game time
  elapses.  Both inputs already encode leg orientation (the pre-game
  prob was produced for THIS market, ``kalshi_price`` is THIS
  market's mid), so the blend is correct whichever leg the row
  represents.
* The blend weight grows monotonically from 0 (opening face-off) to a
  capped ceiling at the final buzzer.  NHL goals are scarcer than NBA
  points, so the per-goal score-gap kicker is meaningfully larger
  per unit of lead than NBA's per-point kicker — a 2-goal lead with
  five minutes left should pull the predictor strongly toward the
  market.
* Output is passed through :mod:`app.calibrators` ``apply_both`` so
  it matches the per-league calibration the pre-game predictor uses
  — the daily honesty report can then compare pre-game vs in-game
  Brier on the SAME calibration footing (otherwise a calibration
  mismatch would mask the model's actual drift).

Ships in **shadow mode**: the per-sport enable flag
``risk.live_loop_enabled_nhl`` is False by default, so the operator
console drives the flip once Section 9 of the daily honesty report
shows the NHL pre-vs-in-game Brier delta is within the 0.02 ship
criterion (per the task brief).
"""
from __future__ import annotations

import logging
import re
from typing import Any

from .. import calibrators, db


log = logging.getLogger(__name__)


# Canonical NHL league name(s) on ``predictions.league``.  Frozenset
# keeps the membership check trivial and symmetrical with
# ``nba_live.NBA_LEAGUES``.
NHL_LEAGUES: frozenset[str] = frozenset({"NHL"})


# Regulation length used to estimate "fraction of the match elapsed".
# Overtime pushes past 60 — we cap at 1.0 so an OT reading doesn't
# over-weight the market consensus beyond the ceiling below.
_REGULATION_MINUTES = 60.0
_PERIOD_MINUTES = 20.0
_OT_MINUTES = 5.0

# Ceiling on the market-consensus blend weight.  At the final buzzer
# the predictor is mostly tracking the market but still keeps a sliver
# of the pre-game prior so a flash mispricing during an OT scramble
# can't single-handedly swing the shadow log.
_MAX_MARKET_WEIGHT = 0.85

# Per-goal score-state confidence kicker (probability points).  NHL
# goals are scarcer than NBA points (a 2-goal lead with five minutes
# left is decisive in a way a 2-point NBA lead never is), so the
# per-goal kicker and cap are both substantially larger than the
# NBA per-point figures.
_SCORE_GAP_KICKER_PP = 0.05  # per goal of lead
_MAX_SCORE_GAP_KICKER_PP = 0.15


def is_nhl_league(league: str | None) -> bool:
    """Case-insensitive membership check used by the in-game loop
    dispatcher so callers don't have to upper-case + frozenset-test
    inline.  Mirrors :func:`nba_live.is_nba_league`.
    """
    if not league:
        return False
    return str(league).strip().upper() in NHL_LEAGUES


# ---------------------------------------------------------------------------
# Feature builder
# ---------------------------------------------------------------------------


_CLOCK_RE = re.compile(r"^\s*(\d{1,3})(?::(\d{1,2}))?\s*$")


def _parse_clock_remaining_minutes(clock: str | None) -> float:
    """Parse a Sportradar NHL clock string into REMAINING minutes
    within the current period.

    NHL clocks count DOWN from 20:00 each period (or 5:00 each OT
    period) — so "05:00" means 5m left in the period, NOT 5m elapsed.
    Returns 0.0 on any parse failure so a malformed feed never blows
    up the cycle.
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


def _normalize_period(period: str | None) -> str:
    return (period or "").strip().lower()


def _period_index(period: str | None) -> tuple[int, bool]:
    """Translate a period label into ``(period_number, is_overtime)``.

    Returns ``(0, False)`` for anything we can't parse, which in
    practice means the predictor will fall back to the pre-game prior
    (the safest behaviour when the live feed is wedged or pre-puck-
    drop).  Recognised forms cover the values the Sportradar feed
    actually writes (numeric strings ``"1"``..``"3"``, the literal
    ``"ot"``, plus a handful of long-form variants for defensiveness).
    """
    p = _normalize_period(period)
    if not p:
        return 0, False
    if p in ("ot", "overtime", "1ot", "2ot", "3ot"):
        try:
            n = int(p[0]) if p[0].isdigit() else 1
        except ValueError:
            n = 1
        return max(1, n), True
    if p in ("so", "shootout"):
        # Shootout — treat as deep into OT (capped fraction = 1.0).
        return 1, True
    # Numeric period (``"1"`` / ``"2"`` / ``"3"``) or long-form
    # (``"1st_period"`` etc).
    if p[0].isdigit():
        try:
            n = int(re.match(r"\d+", p).group(0))
            if 1 <= n <= 3:
                return n, False
            if n > 3:
                # ``"4"`` from a numeric-only feed = first OT.
                return n - 3, True
        except (AttributeError, ValueError):
            return 0, False
    if "period" in p:
        m = re.match(r"(\d+)", p)
        if m:
            try:
                n = int(m.group(1))
                if 1 <= n <= 3:
                    return n, False
            except ValueError:
                pass
    if p in ("intermission", "intermission1", "intermission2"):
        # Between periods — pin elapsed to exactly end-of-prior-period
        # via the sentinel so a missing/garbage clock can't inflate
        # elapsed minutes.  ``_estimate_minutes_elapsed`` reads the
        # trailing digit out of the label to figure out which one.
        return -1, False
    return 0, False


def _estimate_minutes_elapsed(period: str | None, clock: str | None) -> float:
    """Estimate total minutes elapsed in the game from period + clock.

    Returns 0.0 for an unparseable period AND an unparseable clock so
    the predictor degenerates to the pre-game prior (the safest
    behaviour when the live feed is wedged or pre-puck-drop).
    """
    period_num, is_ot = _period_index(period)
    if period_num == 0:
        return 0.0
    if period_num == -1:
        # Intermission sentinel: pin to exactly end-of-prior-period
        # regardless of clock.  Default to end-of-period-1 unless the
        # label carries a digit (``"intermission2"`` → end of P2).
        label = _normalize_period(period)
        m = re.search(r"(\d+)", label)
        which = int(m.group(1)) if m else 1
        which = max(1, min(2, which))
        return which * _PERIOD_MINUTES
    remaining = _parse_clock_remaining_minutes(clock)
    if is_ot:
        # All three regulation periods complete + (period_num - 1)
        # prior OTs done + (OT_MINUTES - remaining) into the current
        # OT.
        prior_ot = max(0, period_num - 1) * _OT_MINUTES
        in_current = max(0.0, _OT_MINUTES - remaining)
        return _REGULATION_MINUTES + prior_ot + in_current
    # Regulation: (period_num - 1) full periods done + elapsed within
    # the current period (20:00 - remaining).
    prior_p = (period_num - 1) * _PERIOD_MINUTES
    in_current = max(0.0, _PERIOD_MINUTES - remaining)
    return prior_p + in_current


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
            "nhl_live live-state lookup failed for %s/%s: %s",
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

    Same shape as :func:`nba_live.build_live_features` so anything
    that introspects the feature blob (the metadata persisted on
    ``predictions.metadata.live_prediction`` and the honesty report's
    forensic dump) gets a consistent key set across sports.  When
    ``live_state`` is None all of the live-derived features collapse
    to 0.0 so the predictor falls back to the pre-game prior.
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

    Leg-agnostic: we don't know whether YES = HOME / AWAY, but
    ``market`` (the Kalshi mid) is leg-aware, so we kick the blended
    probability TOWARD the market (above 0.5 → push up, below 0.5 →
    push down) proportional to the absolute goal gap and how late
    we are in the game.
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
        # before puck drop is confirmed).
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
    """Compute a calibrated YES probability for one NHL market row.

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
        "source": "nhl_live",
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
            "nhl_live calibration bypass for %s: %s", league, e,
        )
        cal_prob = float(raw)
        cal_info = {"applied": False, "reason": f"error: {type(e).__name__}"}
    meta["raw_probability"] = float(raw)
    meta["calibration"] = cal_info
    return float(cal_prob), meta


__all__ = [
    "NHL_LEAGUES",
    "is_nhl_league",
    "build_live_features",
    "predict_live_yes",
]
