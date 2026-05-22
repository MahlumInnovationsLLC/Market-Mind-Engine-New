"""NFL player-props poller scaffold (task #829)."""
from __future__ import annotations

from ._player_props_common import build_player_props_feed


ensure_schema, register_nfl_player_props_feed = build_player_props_feed(
    feed_name="nfl_player_props",
    league="NFL",
    sportradar_sport="americanfootball_nfl",
    cadence_env="AXIOM_NFL_PROPS_CADENCE_SECS",
    default_cadence_secs=10 * 60.0,
)
