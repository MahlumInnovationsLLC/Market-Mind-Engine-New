"""WNBA player-props vertical (task #823).

This package owns the WNBA-specific pieces of the player-props
primitive:

* :mod:`.matchup` — pace + opponent-allowed adjustment to the baseline
  projection.
* :mod:`.predictor` — combines the sport-agnostic per-player baseline
  with the WNBA matchup adjustment to produce a calibrated
  over/under probability.

The sport-agnostic baseline + injury primitive lives in
:mod:`app.player_props` so future verticals (NBA / NFL / MLB / NHL
player props) reuse the same schema and helpers.
"""
from __future__ import annotations

from .matchup import (
    DEFAULT_MAX_MATCHUP_ADJ,
    MatchupAdjustment,
    compute_adjustment,
    league_average,
)
from .predictor import (
    PROP_MARKETS,
    WnbaPropPredictionError,
    predict_wnba_player_prop,
)


__all__ = [
    "DEFAULT_MAX_MATCHUP_ADJ",
    "MatchupAdjustment",
    "compute_adjustment",
    "league_average",
    "PROP_MARKETS",
    "WnbaPropPredictionError",
    "predict_wnba_player_prop",
]
