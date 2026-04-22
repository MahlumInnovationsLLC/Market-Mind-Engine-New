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

__all__ = [
    "LeagueConfig",
    "LEAGUE_CONFIGS",
    "get_league_config",
    "league_blackout_until",
    "is_sunday_window",
    "persona_context_augment",
]
