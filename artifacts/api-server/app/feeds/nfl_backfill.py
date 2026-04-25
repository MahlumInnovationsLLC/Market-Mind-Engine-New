"""Self-healing NFL season backfill.

The one-shot operator CLI in ``scripts/backfill_nfl_games.py`` had to be
remembered and run by hand every off-season — if nobody did, the NFL
margin regressor and per-league calibrator silently kept training on a
stale window.  This module wraps that same backfill flow as a periodic
:class:`~app.feeds.registry.Feed` so the existing ``FeedScheduler``
ticks it weekly.

Per-poll behaviour:

  1.  Compute the most recent completed NFL season — March 1 is treated
      as the cutoff because the Super Bowl is always held on the second
      Sunday of February.  In March/April/.../December of year ``Y``
      the latest completed season is ``Y - 1``; in January/February
      it's ``Y - 2`` (this season's Super Bowl hasn't happened yet).
  2.  Count NFL rows in the canonical ``games`` table whose
      ``scheduled_start`` falls inside that season's window
      (``Aug Y`` through ``Jul Y+1``, wide enough to catch the
      Super Bowl in February of ``Y+1``).
  3.  If the count is zero **and** a Sportradar API key is configured,
      pull the season schedule (REG + PST) and upsert it through the
      same helpers the manual CLI uses.  When the season is already
      present the poll is a single SQL count and a no-op.

The Feed runs through the standard registry, so its outcome (last
poll time, rows written, last error) is exposed on
``/api/feeds/status`` next to every other periodic data source — no
separate dashboard surface is needed.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from .registry import Feed, registry


# Make the operator CLI in `scripts/` importable from inside the app
# process.  The CLI itself only depends on `app.db`, so pulling it in
# carries no side effects beyond loading httpx.
_API_SERVER_ROOT = Path(__file__).resolve().parents[2]
if str(_API_SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_SERVER_ROOT))


log = logging.getLogger("axiom.feeds.nfl_backfill")


# Default cadence: once a week.  The check is dirt-cheap on the
# steady-state no-op path (one indexed count) so weekly is plenty of
# headroom during the long off-season window without burning Sportradar
# quota.
DEFAULT_CADENCE_SECONDS: float = 7.0 * 24 * 3600


def _has_sportradar_key() -> bool:
    return bool(
        os.environ.get("SPORTRADAR_NFL_KEY")
        or os.environ.get("SPORTRADAR_API_KEY")
    )


def latest_completed_nfl_season(now: datetime | None = None) -> int:
    """Return the season year of the most recently finished NFL season.

    NFL season ``Y`` runs September of ``Y`` through the Super Bowl in
    February of ``Y + 1``.  We treat March 1 as the cutoff: the Super
    Bowl is always played on the second Sunday of February (well before
    March 1), so this is a conservative trigger that never tries to
    backfill a season whose post-season is still in flight.
    """
    now = now or datetime.now(tz=timezone.utc)
    if now.month >= 3:
        return now.year - 1
    return now.year - 2


def season_window_iso(season: int) -> tuple[str, str]:
    """ISO ``[start, end)`` bounds for an NFL season's ``scheduled_start``.

    Wide enough to include the Super Bowl in February of ``season + 1``
    but tight enough to never overlap with the *next* season's
    preseason kickoff in August.
    """
    start = datetime(season, 8, 1, tzinfo=timezone.utc).isoformat()
    end = datetime(season + 1, 8, 1, tzinfo=timezone.utc).isoformat()
    return start, end


async def count_nfl_games_for_season(season: int) -> int | None:
    """Count NFL rows in ``games`` whose schedule falls inside ``season``.

    Returns ``None`` when the presence check itself failed (sick DB,
    bad row shape, etc.) so callers can distinguish "season is truly
    absent" from "we couldn't tell" and avoid kicking off an
    unnecessary Sportradar fetch on a transient outage.
    """
    from .. import db  # local import: avoid circulars at module import time

    start_iso, end_iso = season_window_iso(season)
    sql = """
    SELECT count(*) AS n
      FROM games
     WHERE league = 'NFL'
       AND scheduled_start >= $1
       AND scheduled_start <  $2
    """
    try:
        rows = await db.fetch(sql, start_iso, end_iso)
    except Exception as e:
        # A sick DB shouldn't kill the loop; the scheduler will retry
        # next tick.  Return None so the poll skips backfill instead
        # of treating the failure as "missing season".
        log.warning("nfl_backfill: count query failed: %s", e)
        return None
    if not rows:
        return None
    r = rows[0]
    try:
        val = r["n"] if isinstance(r, dict) or hasattr(r, "__getitem__") else r
        return int(val or 0)
    except Exception as e:
        log.warning("nfl_backfill: count row coercion failed: %s", e)
        return None


async def backfill_season(season: int) -> int:
    """Pull the season from Sportradar + upsert into ``games``.

    Returns the number of rows written.  Delegates to the same helpers
    as the operator CLI so behaviour stays identical.
    """
    from scripts import backfill_nfl_games as backfill  # noqa: E402

    games = await backfill.fetch_from_sportradar(
        season, list(backfill.DEFAULT_SEASON_TYPES),
    )
    return await backfill.upsert_games(games)


async def poll_nfl_backfill_once(now: datetime | None = None) -> int:
    """Single-tick body.  Returns rows upserted (0 on no-op).

    Decision tree:
      * Season already represented in ``games``  -> 0 rows, no API call.
      * Season missing **and** SPORTRADAR_NFL_KEY (or SPORTRADAR_API_KEY)
        is set -> trigger the backfill, return rows written.
      * Season missing but no API key configured -> log + 0 rows.
        (The Feed is registered as ``enabled=False`` in that case so
        this branch only runs if a key was added after registration.)
    """
    season = latest_completed_nfl_season(now)
    existing = await count_nfl_games_for_season(season)
    if existing is None:
        # Presence check failed (sick DB / bad row shape).  Skip this
        # tick — the scheduler will try again next cadence rather than
        # spending a Sportradar fetch we can't justify.
        log.warning(
            "nfl_backfill: season %d presence check unavailable; "
            "skipping this tick", season,
        )
        return 0
    if existing > 0:
        log.debug(
            "nfl_backfill: season %d already has %d games; no-op",
            season, existing,
        )
        return 0
    if not _has_sportradar_key():
        log.warning(
            "nfl_backfill: season %d missing from `games` but neither "
            "SPORTRADAR_NFL_KEY nor SPORTRADAR_API_KEY is set — skipping",
            season,
        )
        return 0
    log.info(
        "nfl_backfill: season %d missing from `games`; "
        "pulling from Sportradar", season,
    )
    written = await backfill_season(season)
    log.info("nfl_backfill: season %d upserted %d rows", season, written)
    return int(written or 0)


async def _poll() -> int:
    """Feed-shaped poll function (no args)."""
    return await poll_nfl_backfill_once()


def register_nfl_backfill_feed() -> None:
    """Register the off-season NFL backfill check as a periodic Feed.

    Disabled (with an explicit ``skipped_reason``) when the operator
    has either turned it off via ``AXIOM_NFL_BACKFILL_DISABLED`` or
    not configured a Sportradar API key.  Disabled feeds still appear
    on ``/api/feeds/status`` so the gap is visible.
    """
    if registry.status_of("nfl_backfill"):
        return  # already registered
    cadence = max(
        3600,
        int(
            os.environ.get("AXIOM_NFL_BACKFILL_CADENCE_SECONDS")
            or str(int(DEFAULT_CADENCE_SECONDS))
        ),
    )
    disabled_flag = os.environ.get(
        "AXIOM_NFL_BACKFILL_DISABLED", ""
    ).strip().lower() in ("1", "true", "yes", "on")
    has_key = _has_sportradar_key()
    skipped_reason: str | None = None
    if disabled_flag:
        skipped_reason = "AXIOM_NFL_BACKFILL_DISABLED set"
    elif not has_key:
        skipped_reason = "SPORTRADAR_NFL_KEY/SPORTRADAR_API_KEY not set"
    registry.register(Feed(
        name="nfl_backfill",
        cadence_seconds=float(cadence),
        poll=_poll,
        enabled=(not disabled_flag) and has_key,
        skipped_reason=skipped_reason,
    ))


__all__ = [
    "register_nfl_backfill_feed",
    "poll_nfl_backfill_once",
    "latest_completed_nfl_season",
    "season_window_iso",
    "count_nfl_games_for_season",
    "backfill_season",
    "DEFAULT_CADENCE_SECONDS",
]
