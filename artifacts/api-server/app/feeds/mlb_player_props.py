"""MLB player-props poller scaffold (task #829)."""
from __future__ import annotations

from ._player_props_common import build_player_props_feed


ensure_schema, register_mlb_player_props_feed = build_player_props_feed(
    feed_name="mlb_player_props",
    league="MLB",
    sportradar_sport="baseball_mlb",
    cadence_env="AXIOM_MLB_PROPS_CADENCE_SECS",
    default_cadence_secs=5 * 60.0,
)
