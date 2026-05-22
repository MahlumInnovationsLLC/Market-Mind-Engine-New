"""Sport-agnostic player-prop primitive (task #823).

Holds the per-player baseline distribution helper and the player
availability lookup used by every player-level vertical.  The seed
caller is the WNBA vertical; future verticals (NBA / NFL / MLB / NHL
/ NCAAB / soccer / golf player props) plug in by filtering on
``league`` against the same tables.
"""
from __future__ import annotations

from .baseline import (
    BaselineDistribution,
    DEFAULT_MIN_SAMPLE,
    SUPPORTED_STATS,
    fetch_baseline,
    summarize,
)
from .injuries import (
    PlayerStatus,
    fetch_player_status,
    is_blocking_status,
)
from .league_specs import (
    LEAGUE_SPECS,
    PlayerPropLeagueSpec,
    calibrator_buckets,
    get_league_spec,
    get_vertical_spec,
)
from .matchup import (
    DEFAULT_MAX_MATCHUP_ADJ,
    MatchupAdjustment,
    compute_adjustment,
    fetch_adjustment,
)
from .predictor import (
    PlayerPropPredictionError,
    predict_player_prop,
)


__all__ = [
    "BaselineDistribution",
    "DEFAULT_MIN_SAMPLE",
    "DEFAULT_MAX_MATCHUP_ADJ",
    "LEAGUE_SPECS",
    "MatchupAdjustment",
    "PlayerPropLeagueSpec",
    "PlayerPropPredictionError",
    "PlayerStatus",
    "SUPPORTED_STATS",
    "calibrator_buckets",
    "compute_adjustment",
    "fetch_adjustment",
    "fetch_baseline",
    "fetch_player_status",
    "get_league_spec",
    "get_vertical_spec",
    "is_blocking_status",
    "predict_player_prop",
    "summarize",
]
