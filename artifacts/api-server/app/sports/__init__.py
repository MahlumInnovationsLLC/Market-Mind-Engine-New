"""Sports-vertical helpers (per-league config, blackout windows, persona
context augmentation).

Kept under `app.sports` so per-vertical helpers can sit alongside the
generic verticals registry without bloating it.  The registry stays
vertical-scoped; this module is league-scoped (NFL / NBA / MLB / NHL
/ NCAAB / NCAAF / MLS / EPL).
"""
from .leagues import (
    LeagueConfig,
    LEAGUE_CONFIGS,
    get_league_config,
    league_blackout_until,
    is_sunday_window,
    persona_context_augment,
)
from .nfl_context import (
    build_game_context as build_nfl_game_context,
    fetch_player_status as fetch_nfl_player_status,
    fetch_weather as fetch_nfl_weather,
)
from .nfl_stadiums import resolve_stadium as resolve_nfl_stadium
from .ratings import (
    coerce_to_ratings,
    derive_field_ratings,
    get_field,
    set_field,
)

__all__ = [
    "LeagueConfig",
    "LEAGUE_CONFIGS",
    "get_league_config",
    "league_blackout_until",
    "is_sunday_window",
    "persona_context_augment",
    "build_nfl_game_context",
    "fetch_nfl_player_status",
    "fetch_nfl_weather",
    "resolve_nfl_stadium",
    "coerce_to_ratings",
    "derive_field_ratings",
    "get_field",
    "set_field",
]
