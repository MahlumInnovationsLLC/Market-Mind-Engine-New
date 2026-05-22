"""NHL player-props poller scaffold (task #829)."""
from __future__ import annotations

from ._player_props_common import build_player_props_feed


ensure_schema, register_nhl_player_props_feed = build_player_props_feed(
    feed_name="nhl_player_props",
    league="NHL",
    sportradar_sport="icehockey_nhl",
    cadence_env="AXIOM_NHL_PROPS_CADENCE_SECS",
    default_cadence_secs=5 * 60.0,
)
