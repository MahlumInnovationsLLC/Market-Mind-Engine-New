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
    fetch_baseline,
    summarize,
)
from .injuries import (
    PlayerStatus,
    fetch_player_status,
    is_blocking_status,
)


__all__ = [
    "BaselineDistribution",
    "DEFAULT_MIN_SAMPLE",
    "fetch_baseline",
    "summarize",
    "PlayerStatus",
    "fetch_player_status",
    "is_blocking_status",
]
