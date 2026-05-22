"""MLB-specific in-game live-feature builder (task #881).

Mirrors :mod:`app.learning.nba_live_features` but with baseball-specific
period structure (9 innings + extras) and a run-margin logistic scale.
A 2-run lead after the 7th is roughly a 75% win probability so the
logistic scale sits around 2.5 runs.  Period weights ramp gradually
through the first 5 innings (early-game runs are noisy) then steepen
sharply through the late innings.

Ships behind ``risk.live_loop_enabled_mlb`` (default False) per the
shadow-mode rollout in :mod:`app.learning.in_game_loop`.
"""
from __future__ import annotations

import math
import re
from typing import Any, Mapping


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
