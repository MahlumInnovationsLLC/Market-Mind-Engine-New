"""Shared scaffold for per-league player-prop pollers (task #829).

Generalises the WNBA-only feed in :mod:`app.feeds.wnba_player_props`
into a sport-agnostic builder so adding NBA / NHL / MLB / NFL takes
~10 LOC per feed instead of a 380-line copy.

Each call to :func:`build_player_props_feed` returns a tuple of
``(ensure_schema, register)`` callables.  ``register`` is idempotent
and a no-op if ``THE_ODDS_API_KEY`` is missing (cleanly
``skipped_reason``-tagged in the feed registry).

The per-league per-stat columns on ``player_stat_history`` (added to
``app.player_props.baseline.SUPPORTED_STATS`` in task #829) are
created lazily by :func:`ensure_extended_schema` below using
``ALTER TABLE ... ADD COLUMN IF NOT EXISTS``.  The columns are
nullable so this is safe to run against a populated WNBA table.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Awaitable, Callable

from .. import db
from ..feed_scheduler import Feed, registry


log = logging.getLogger(__name__)


_EXTENDED_SCHEMA_SQL = """
ALTER TABLE IF EXISTS player_stat_history
    ADD COLUMN IF NOT EXISTS shots_on_goal     numeric,
    ADD COLUMN IF NOT EXISTS goals             numeric,
    ADD COLUMN IF NOT EXISTS hockey_assists    numeric,
    ADD COLUMN IF NOT EXISTS hockey_points     numeric,
    ADD COLUMN IF NOT EXISTS saves             numeric,
    ADD COLUMN IF NOT EXISTS hits              numeric,
    ADD COLUMN IF NOT EXISTS total_bases       numeric,
    ADD COLUMN IF NOT EXISTS strikeouts        numeric,
    ADD COLUMN IF NOT EXISTS earned_runs       numeric,
    ADD COLUMN IF NOT EXISTS outs_recorded     numeric,
    ADD COLUMN IF NOT EXISTS passing_yards     numeric,
    ADD COLUMN IF NOT EXISTS rushing_yards     numeric,
    ADD COLUMN IF NOT EXISTS receiving_yards   numeric,
    ADD COLUMN IF NOT EXISTS receptions        numeric,
    ADD COLUMN IF NOT EXISTS touchdowns        numeric,
    ADD COLUMN IF NOT EXISTS opponent          text,
    ADD COLUMN IF NOT EXISTS pace_estimate     numeric;
"""

_EXTENDED_READY = False
_EXTENDED_LOCK = asyncio.Lock()


async def ensure_extended_schema() -> None:
    """Add the multi-league per-stat columns to ``player_stat_history``
    if they're missing.  Idempotent; safe to call on every feed
    registration."""
    global _EXTENDED_READY
    if _EXTENDED_READY:
        return
    async with _EXTENDED_LOCK:
        if _EXTENDED_READY:
            return
        try:
            async with db.acquire() as conn:
                await conn.execute(_EXTENDED_SCHEMA_SQL)
            _EXTENDED_READY = True
            log.info("player_props extended schema ready")
        except Exception as e:
            log.warning(
                "player_props extended schema bootstrap failed: %s", e
            )


def _api_key() -> str | None:
    return (
        os.environ.get("THE_ODDS_API_KEY")
        or os.environ.get("THEODDSAPI_KEY")
        or os.environ.get("ODDS_API_KEY")
    )


def build_player_props_feed(
    *,
    feed_name: str,
    league: str,
    sportradar_sport: str,
    cadence_env: str,
    default_cadence_secs: float,
) -> tuple[
    Callable[[], Awaitable[None]],
    Callable[[], None],
]:
    """Return ``(ensure_schema, register_feed)`` for a player-prop feed.

    The poller is intentionally a thin stub for v1 — the per-league
    Sportradar event-stat backfill (the source of truth for
    ``player_stat_history``) is a follow-up.  Once it lands, swap
    ``_poll`` here for a real per-event walker, mirroring
    :func:`app.feeds.wnba_player_props._poll_wnba_player_props`.
    """

    async def _poll() -> int:
        # Stub poller: confirms the feed wires correctly and ticks
        # without burning the API budget while the per-league
        # Sportradar backfill is still being authored.  Logs at debug
        # because the feed ships disabled-on-startup until the API
        # key + raw-mode override are in place.
        if not _api_key():
            log.debug("%s skipping poll — THE_ODDS_API_KEY not set", feed_name)
            return 0
        log.debug(
            "%s poll tick (league=%s sport=%s) — stub awaiting "
            "Sportradar backfill",
            feed_name, league, sportradar_sport,
        )
        return 0

    async def ensure_schema() -> None:
        # Reuses the WNBA feed's schema bootstrap (player_props_quotes
        # + player_injury_status tables) and adds the extended
        # per-league per-stat columns to player_stat_history.
        try:
            from .wnba_player_props import ensure_schema as _ensure
            await _ensure()
        except Exception as e:
            log.warning(
                "%s base schema bootstrap failed: %s", feed_name, e,
            )
        await ensure_extended_schema()

    def register() -> None:
        if registry.status_of(feed_name):
            return
        api_key = _api_key()
        cadence = float(os.environ.get(cadence_env, default_cadence_secs))
        registry.register(Feed(
            name=feed_name,
            cadence_seconds=cadence,
            poll=_poll,
            enabled=bool(api_key),
            skipped_reason=None if api_key else "THE_ODDS_API_KEY not set",
        ))

    return ensure_schema, register
