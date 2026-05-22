"""NBA-specific in-game live-feature builder (task #855).

Produces the candidate dict that :func:`app.features.build_vector`
needs in order to score an NBA market that is currently in-progress.

Why not just call the pre-game feature builder unchanged?
    The meta-model's feature schema is centered on pre-game inputs
    (consensus / market price / signed edge / league / market type).
    A pure pre-game score against a market whose price has already
    moved through 3 quarters of game action would silently re-use
    the stale pre-game consensus.  The live builder here:

      * Carries the *current* Kalshi YES price (from the candidate
        row) so the market-prob feature reflects the live mid.
      * Derives an updated YES-side "consensus" anchor from the live
        score differential + game-state (period, clock, OT) so the
        edge feature reflects the in-game state of the game rather
        than the pre-game line.
      * Tags the candidate so the dashboard's live tile can render
        the inputs that went into the prediction.

The score-diff → YES-probability mapping below is intentionally
*simple* (logistic on score-margin with a per-period scale).  The
loop ships in shadow mode behind the per-sport flag so the daily
honesty report's pre-vs-in-game Brier split (Section 9) is the
gating signal that decides whether the multiplier on this anchor
needs to grow before the operator flips
``risk.live_loop_enabled_nba``.
"""
from __future__ import annotations

import math
import re
from typing import Any, Mapping


# Logistic scale parameter (points-of-margin -> log-odds).  Calibrated
# off the NBA win-probability literature: a 10-point lead with a full
# quarter remaining converts to ~85% win probability, which corresponds
# to a scale of ~5.0 in a vanilla logistic.  This is deliberately on
# the conservative side so the live anchor doesn't dominate the
# pre-game consensus when game-state is thin.
_LOGISTIC_SCALE = 5.0


# Period weights: how much each remaining period contributes to the
# margin signal.  Earlier in the game the score-diff is noisier so the
# anchor weighs the pre-game prior more heavily; in the 4th / OT the
# score-diff is close to dispositive.
_PERIOD_WEIGHTS: dict[int, float] = {
    1: 0.30,
    2: 0.55,
    3: 0.80,
    4: 1.00,
    5: 1.10,  # OT / 2OT etc.
}


_PERIOD_LOOKUP_RE = re.compile(r"(\d+)")


def _parse_period(period: Any) -> int:
    """Best-effort coerce a Sportradar ``period`` string into an int.

    The feed writes a mix of shapes — ``"1"``, ``"2nd_quarter"``,
    ``"OT"``, etc — so we extract the first integer when present and
    bump to 5 for any string mentioning overtime.  Defaults to 1 when
    the value is missing or unparseable (worst-case the anchor falls
    back to its lowest weight).
    """
    if period is None:
        return 1
    s = str(period).strip().lower()
    if not s:
        return 1
    if "ot" in s or "overtime" in s:
        return 5
    if "half" in s and "1" in s:
        return 2
    if "half" in s and "2" in s:
        return 3
    m = _PERIOD_LOOKUP_RE.search(s)
    if m:
        try:
            n = int(m.group(1))
            return max(1, min(n, 6))
        except ValueError:
            pass
    return 1


def _score_diff_yes_prob(
    *, score_diff: int, period_idx: int, pre_game_prob: float | None
) -> float:
    """Blend the pre-game prior with a logistic on (period-weighted)
    score margin.  Returns a YES-side probability in [0.01, 0.99].

    ``score_diff`` is YES-perspective: positive means the YES side is
    ahead.  ``period_idx`` is 1..5+ (4=4th, 5=OT).  ``pre_game_prob``
    is the calibrated pre-game model probability (used as a Bayesian
    prior when game-state is thin).
    """
    weight = _PERIOD_WEIGHTS.get(period_idx, 1.0)
    # Logistic on weighted margin.
    z = (weight * float(score_diff)) / _LOGISTIC_SCALE
    live_p = 1.0 / (1.0 + math.exp(-z))
    if pre_game_prob is None:
        anchor = live_p
    else:
        # Blend: as the game progresses (period weight grows) we trust
        # the live anchor more and the pre-game prior less.
        blend = max(0.0, min(weight, 1.0))
        anchor = blend * live_p + (1.0 - blend) * float(pre_game_prob)
    return max(0.01, min(0.99, anchor))


def build_nba_live_candidate(
    row: Mapping[str, Any],
    *,
    live_state: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build the candidate dict :func:`build_vector` needs from a
    live-loop row + the latest ``sportradar_live_state`` snapshot.

    The shape mirrors the candidate dicts the orchestrator's
    `_predict_for_candidate` consumes (consensus_probability /
    kalshi_price / league / market_type) so the existing 32-feature
    meta-model schema scores the live snapshot cleanly without a
    bespoke schema.

    ``row`` comes from ``in_game_loop._fetch_live_eligible`` and
    carries the candidate's market_id, league, model_yes_prob (the
    persisted pre-game prior) and kalshi_price (the current YES
    side mid).  ``live_state`` carries home_score / away_score /
    period / clock from sportradar_live_state.
    """
    league = (row.get("league") or "NBA").upper()
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

    # YES-side score perspective.  The orchestrator stamps the
    # YES-side onto the candidate via ``side``; for the simple
    # moneyline case we treat the home team as the YES side
    # (matches the orchestrator's pre-game default when no explicit
    # side is carried).  A future per-side mapping lands as a
    # follow-up — the shadow-mode flip guards against bad calls.
    score_diff = home_score - away_score
    live_anchor = _score_diff_yes_prob(
        score_diff=score_diff,
        period_idx=period_idx,
        pre_game_prob=pre_p,
    )

    cand: dict[str, Any] = {
        "market_id": row.get("market_id"),
        "kalshi_market_id": row.get("market_id"),
        "league": league,
        "market_type": "moneyline",
        "side": "YES",
        "recommended_action": "BUY_YES",
        # The "consensus" for the live snapshot is the live anchor.
        "consensus_probability": live_anchor,
        # Pricing feature inputs.
        "kalshi_price": kp,
        "market_prob": kp,
        # Game-state metadata the dashboard tile renders.
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
    return cand


__all__ = [
    "build_nba_live_candidate",
    "_parse_period",
    "_score_diff_yes_prob",
]
