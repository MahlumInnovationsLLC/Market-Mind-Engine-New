"""NBA player-props poller scaffold (task #829).  Wraps the shared
builder in :mod:`app.feeds._player_props_common`; the real per-event
Sportradar walker lands in the per-league backfill follow-up."""
from __future__ import annotations

from ._player_props_common import build_player_props_feed


ensure_schema, register_nba_player_props_feed = build_player_props_feed(
    feed_name="nba_player_props",
    league="NBA",
    sportradar_sport="basketball_nba",
    cadence_env="AXIOM_NBA_PROPS_CADENCE_SECS",
    default_cadence_secs=5 * 60.0,
)
