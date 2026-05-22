"""Sport-agnostic player-prop predictor (task #829).

Generalisation of :mod:`app.wnba.predictor` that drives NBA / NHL /
MLB / NFL player props off the same baseline + matchup + calibrator
pipeline.  The WNBA predictor keeps its own entry point so the
existing (task #823) surface area is unchanged; this module is the
forward shape every new league plugs into.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Literal

from .. import calibrators
from ..predictor import Prediction
from .baseline import (
    DEFAULT_MIN_SAMPLE,
    BaselineDistribution,
    fetch_baseline,
)
from .league_specs import PlayerPropLeagueSpec, get_league_spec
from .matchup import (
    DEFAULT_MAX_MATCHUP_ADJ,
    MatchupAdjustment,
    fetch_adjustment,
)


log = logging.getLogger(__name__)


_DEFAULT_KALSHI_BLEND_WEIGHT: float = 0.4


class PlayerPropPredictionError(RuntimeError):
    """Raised when we can't produce a probability for a player prop."""


def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _normal_over_probability(
    *, projected: float, sigma: float, line: float
) -> float:
    if sigma <= 0:
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
    spec: PlayerPropLeagueSpec
    baseline: BaselineDistribution
    matchup: MatchupAdjustment | None
    line: float
    side: Literal["over", "under"]


def _why(inputs: _PredictionInputs, raw_prob: float) -> list[str]:
    b = inputs.baseline
    out: list[str] = []
    out.append(
        f"[{inputs.spec.league}] baseline (rolling-{b.rolling_window_used}-game) "
        f"for {b.player} {b.stat}: {b.rolling_mean:.2f}"
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


async def predict_player_prop(
    *,
    league: str,
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
    """Resolve baseline + matchup + market mid → calibrated Prediction
    for any league registered in
    :data:`app.player_props.league_specs.LEAGUE_SPECS`.

    Returns ``None`` (rather than a fabricated value) when the
    player's rolling sample is below ``min_sample`` — the dashboard
    and the orchestrator hook both treat None as "insufficient
    sample, surface to operator and skip".
    """
    spec = get_league_spec(league)
    if spec is None:
        raise PlayerPropPredictionError(
            f"league {league!r} not registered in LEAGUE_SPECS"
        )
    side_norm = side.lower().strip()
    if side_norm not in ("over", "under"):
        raise PlayerPropPredictionError(f"invalid side {side!r}")
    if stat not in spec.markets:
        raise PlayerPropPredictionError(
            f"{spec.league} player props do not price stat {stat!r}"
        )
    t0 = time.monotonic()

    baseline = await fetch_baseline(
        spec.league, player, stat, min_sample=min_sample
    )
    if baseline is None:
        return None

    matchup: MatchupAdjustment | None = None
    if opponent_team:
        try:
            matchup = await fetch_adjustment(
                league=spec.league,
                stat=stat,
                opponent_team=opponent_team,
                pace_estimate=pace_estimate,
                max_adj=max_adj,
            )
        except Exception as e:  # pragma: no cover — never fail predictor
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
    if (
        kalshi_mid is not None
        and 0.0 < kalshi_mid < 1.0
        and kalshi_blend_weight > 0.0
    ):
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

    cal_prob, cal_info = calibrators.apply(
        blended_prob, spec.calibrator_bucket
    )
    if cal_info.get("applied"):
        probability = float(cal_prob)
        cal_extra: dict[str, Any] = {
            "uncalibrated_probability": float(blended_prob),
        }
    else:
        probability = float(blended_prob)
        cal_extra = {}

    inputs = _PredictionInputs(
        spec=spec, baseline=baseline, matchup=matchup,
        line=float(line), side=side_norm,
    )

    raw: dict[str, Any] = {
        "league": spec.league,
        "vertical": spec.vertical,
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
        "calibrator_bucket": spec.calibrator_bucket,
        "why": _why(inputs, raw_prob),
    }

    elapsed_ms = (time.monotonic() - t0) * 1000.0
    confidence = min(
        1.0, 0.20 + 0.05 * max(0, baseline.sample_count - min_sample)
    )
    return Prediction(
        probability=float(probability),
        confidence=float(confidence),
        model_name=spec.vertical,
        model_version="1",
        model_type="PlayerProps",
        elapsed_ms=round(elapsed_ms, 3),
        raw=raw,
    )
