"""WNBA player-props predictor (task #823).

Mirrors the calibration shape used by ``app.entertainment.predictor`` so
the orchestrator's hook-dispatch can route ``predictor_hook =
'wnba_player_props'`` candidates through here without a custom path.

The prediction is built in three steps:

1. **Baseline**: per-player rolling-mean projection + robust σ from
   ``app.player_props.baseline``.
2. **Matchup adjustment**: pace × opponent-allowed multiplicative
   factor from ``app.wnba.matchup`` (bounded ±25%).
3. **Calibrated probability**: integrate the resulting normal
   approximation against the over/under threshold; bound to
   ``[0.05, 0.95]``; optional Kalshi/sportsbook mid blend; optional
   isotonic calibrator from ``app.calibrators`` if a fitted snapshot
   exists.

Honest defaults: when sample is below the configured minimum, this
returns ``None`` rather than fabricating a probability.  Operators
must surface "insufficient sample" to the trader instead of routing a
zero-confidence ticket through the gates.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Literal

from .. import calibrators
from ..predictor import Prediction
from ..player_props.baseline import (
    DEFAULT_MIN_SAMPLE,
    BaselineDistribution,
    fetch_baseline,
)
from .matchup import (
    DEFAULT_MAX_MATCHUP_ADJ,
    MatchupAdjustment,
    fetch_adjustment,
)


log = logging.getLogger(__name__)


# Markets the WNBA player-props vertical knows how to price.  The
# string form matches TheOddsAPI's `markets=` query param suffix.
PROP_MARKETS: tuple[str, ...] = (
    "points",
    "rebounds",
    "assists",
    "threes",
    "blocks",
    "steals",
    "turnovers",
    "points_rebounds_assists",
    "points_rebounds",
    "points_assists",
    "rebounds_assists",
)


_DEFAULT_KALSHI_BLEND_WEIGHT: float = 0.4
_LEAGUE: str = "WNBA"


class WnbaPropPredictionError(RuntimeError):
    """Raised when we can't produce a probability for a WNBA prop."""


def _phi(z: float) -> float:
    """Standard-normal CDF."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _normal_over_probability(
    *, projected: float, sigma: float, line: float
) -> float:
    """P(X > line) under a normal approximation.  ``sigma`` is the
    standard deviation of the player's per-game stat distribution."""
    if sigma <= 0:
        # Degenerate σ — treat the mean as a point mass.
        return 1.0 if projected > line else 0.0
    z = (projected - line) / sigma
    return _phi(z)


def _logit(p: float) -> float:
    p = min(0.999, max(0.001, p))
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def _bound(p: float, *, lo: float = 0.05, hi: float = 0.95) -> float:
    return max(lo, min(hi, p))


@dataclass
class _PredictionInputs:
    baseline: BaselineDistribution
    matchup: MatchupAdjustment | None
    line: float
    side: Literal["over", "under"]


def _why(inputs: _PredictionInputs, raw_prob: float) -> list[str]:
    """Human-readable trail the dashboard surfaces verbatim."""
    b = inputs.baseline
    out: list[str] = []
    out.append(
        f"Baseline projection (rolling-{b.rolling_window_used}-game mean) for "
        f"{b.player} {b.stat}: {b.rolling_mean:.1f}"
    )
    out.append(
        f"Robust σ (MAD-based) over last {b.sample_count} games: "
        f"{b.mad_sigma:.2f}"
    )
    if inputs.matchup is not None:
        m = inputs.matchup
        out.append(
            f"Matchup adjustment ×{m.factor:.3f} "
            f"(pace ×{m.pace_factor:.3f}, opp-allowed ×{m.opp_allowed_factor:.3f}"
            f"{', BOUNDED' if m.bounded else ''})"
        )
    out.append(
        f"Side {inputs.side.upper()} {inputs.line}: "
        f"raw probability {raw_prob:.3f}"
    )
    return out


async def predict_wnba_player_prop(
    *,
    player: str,
    stat: str,
    line: float,
    side: Literal["over", "under"] = "over",
    opponent_team: str | None = None,
    pace_estimate: float | None = None,
    kalshi_mid: float | None = None,
    kalshi_blend_weight: float = _DEFAULT_KALSHI_BLEND_WEIGHT,
    min_sample: int = DEFAULT_MIN_SAMPLE,
    max_adj: float = DEFAULT_MAX_MATCHUP_ADJ,
) -> Prediction | None:
    """Resolve baseline + matchup + market mid → calibrated Prediction.

    Returns ``None`` (rather than a fabricated value) when the player's
    rolling sample is below ``min_sample``.  The dashboard endpoint and
    the orchestrator hook both treat None as "insufficient sample,
    surface to operator and skip".
    """
    side_norm = side.lower().strip()
    if side_norm not in ("over", "under"):
        raise WnbaPropPredictionError(f"invalid side {side!r}")
    if stat not in PROP_MARKETS:
        raise WnbaPropPredictionError(f"unsupported prop market {stat!r}")
    t0 = time.monotonic()

    baseline = await fetch_baseline(_LEAGUE, player, stat, min_sample=min_sample)
    if baseline is None:
        return None

    matchup: MatchupAdjustment | None = None
    if opponent_team:
        try:
            matchup = await fetch_adjustment(
                league=_LEAGUE,
                stat=stat,
                opponent_team=opponent_team,
                pace_estimate=pace_estimate,
                max_adj=max_adj,
            )
        except Exception as e:  # pragma: no cover — never fail the predictor
            log.debug("matchup adjustment failed for %s: %s", opponent_team, e)
            matchup = None

    projected = baseline.projected()
    if matchup is not None:
        projected *= matchup.factor

    over_p = _normal_over_probability(
        projected=projected, sigma=baseline.sigma(), line=float(line)
    )
    raw_prob = over_p if side_norm == "over" else (1.0 - over_p)
    raw_prob = _bound(raw_prob)

    blended_prob = raw_prob
    blend_info: dict[str, Any] = {"applied": False, "weight": 0.0}
    if kalshi_mid is not None and 0.0 < kalshi_mid < 1.0 and kalshi_blend_weight > 0.0:
        w = max(0.0, min(1.0, kalshi_blend_weight))
        blended_prob = _sigmoid(
            (1.0 - w) * _logit(raw_prob) + w * _logit(float(kalshi_mid))
        )
        blended_prob = _bound(blended_prob)
        blend_info = {
            "applied": True,
            "weight": w,
            "kalshi_mid": float(kalshi_mid),
            "model_prob_pre_blend": raw_prob,
        }

    cal_prob, cal_info = calibrators.apply(blended_prob, _LEAGUE)
    if cal_info.get("applied"):
        probability = float(cal_prob)
        cal_extra: dict[str, Any] = {"uncalibrated_probability": float(blended_prob)}
    else:
        probability = float(blended_prob)
        cal_extra = {}

    inputs = _PredictionInputs(
        baseline=baseline, matchup=matchup, line=float(line), side=side_norm
    )

    raw: dict[str, Any] = {
        "league": _LEAGUE,
        "player": player,
        "stat": stat,
        "line": float(line),
        "side": side_norm,
        "projected": float(projected),
        "baseline": baseline.to_dict(),
        "matchup": matchup.to_dict() if matchup is not None else None,
        "raw_probability": raw_prob,
        "kalshi_blend": blend_info,
        "calibration": {**cal_info, **cal_extra},
        "why": _why(inputs, raw_prob),
    }

    elapsed_ms = (time.monotonic() - t0) * 1000.0
    confidence = min(1.0, 0.20 + 0.05 * max(0, baseline.sample_count - min_sample))
    return Prediction(
        probability=float(probability),
        confidence=float(confidence),
        model_name="wnba_player_props",
        model_version="1",
        model_type="WnbaPlayerProps",
        elapsed_ms=round(elapsed_ms, 3),
        raw=raw,
    )
