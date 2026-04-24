"""NFL snap-count cache (sportradar `extended_box_score` per game).

The persona prompt for NFL games needs *real* snap-count percentages
for the top skill players, not just an exhortation to "consider snap
counts" — see :mod:`app.sports.nfl_context`.  Sportradar exposes per-
game snap counts inside its ``extended_box_score`` payload; we fan out
across the most recent completed games for each NFL team and persist
the rows we care about into ``nfl_snap_counts``.

The fetcher is intentionally lightweight — schema bootstrap + an
``upsert_snap_counts`` helper so :mod:`app.sports.nfl_context` can
read from the same table whether the rows came from this poller, an
operator backfill script, or the integration tests.

Polling itself is wired into the feed registry as a daily job (NFL
games settle on Sun/Mon/Thu evenings, so once a day is more than
enough to keep snap counts fresh for the next slate).  Without an
SPORTRADAR API key the feed registers as ``enabled=false`` with a
clear ``skipped_reason`` so the operator console flags the gap.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .. import db
from ..http_retry import retry_request
from .registry import Feed, registry
from .scheduler import RateLimited


log = logging.getLogger("axiom.feeds.nfl_snap_counts")


_EXT_BOX_URL = (
    "https://api.sportradar.us/nfl/official/trial/v7/en/games/"
    "{game_id}/extended_box_score.json"
)
# Look back two weeks to catch Thu/Sun/Mon late-finishers + bye-week
# carry-over.  Anything older has already been fetched on a previous run
# (rows are upserted by (season,week,team_name,player_id)).
_LOOKBACK_DAYS = 14
# Cap fan-out per poll so a transient quota issue can't blast through
# our entire weekly Sportradar budget.
_MAX_GAMES_PER_POLL = max(1, int(os.environ.get("AXIOM_NFL_SNAP_MAX_GAMES") or "16"))


_SCHEMA_LOCK = asyncio.Lock()
_SCHEMA_READY = False

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS nfl_snap_counts (
    season       integer     NOT NULL,
    week         integer     NOT NULL,
    team_id      text,
    team_name    text        NOT NULL,
    player_id    text        NOT NULL,
    player_name  text,
    position     text,
    snap_pct     numeric,
    snaps        integer,
    total_snaps  integer,
    fetched_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (season, week, team_name, player_id)
);
CREATE INDEX IF NOT EXISTS nfl_snap_counts_team_idx
    ON nfl_snap_counts (team_name, season DESC, week DESC);
"""


async def ensure_schema() -> None:
    """Create the snap-counts cache table if missing.  Idempotent."""
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    async with _SCHEMA_LOCK:
        if _SCHEMA_READY:
            return
        try:
            async with db.acquire() as conn:
                await conn.execute(_SCHEMA_SQL)
            _SCHEMA_READY = True
            log.info("nfl_snap_counts schema ready")
        except Exception as e:
            log.warning("nfl_snap_counts schema bootstrap failed: %s", e)


async def upsert_snap_counts(rows: list[dict[str, Any]]) -> int:
    """Bulk-upsert ``rows`` into ``nfl_snap_counts``.

    Each row dict must carry ``season`` (int), ``week`` (int),
    ``team_name`` (str), ``player_id`` (str), and may carry
    ``team_id``, ``player_name``, ``position``, ``snap_pct``,
    ``snaps``, ``total_snaps``.

    Returns the count of rows written.  Best-effort schema bootstrap
    runs first so callers don't need to remember to call
    :func:`ensure_schema` themselves.
    """
    if not rows:
        return 0
    await ensure_schema()
    db_rows = [
        (
            int(r.get("season") or 0),
            int(r.get("week") or 0),
            (r.get("team_id") or None),
            str(r.get("team_name") or ""),
            str(r.get("player_id") or ""),
            (r.get("player_name") or None),
            (r.get("position") or None),
            (float(r["snap_pct"]) if r.get("snap_pct") is not None else None),
            (int(r["snaps"]) if r.get("snaps") is not None else None),
            (int(r["total_snaps"]) if r.get("total_snaps") is not None else None),
        )
        for r in rows
        if r.get("player_id") and r.get("team_name")
    ]
    if not db_rows:
        return 0
    try:
        async with db.acquire() as conn, conn.transaction():
            await conn.executemany(
                """
                INSERT INTO nfl_snap_counts
                  (season, week, team_id, team_name, player_id,
                   player_name, position, snap_pct, snaps, total_snaps,
                   fetched_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NOW())
                ON CONFLICT (season, week, team_name, player_id) DO UPDATE SET
                  team_id      = EXCLUDED.team_id,
                  player_name  = EXCLUDED.player_name,
                  position     = EXCLUDED.position,
                  snap_pct     = EXCLUDED.snap_pct,
                  snaps        = EXCLUDED.snaps,
                  total_snaps  = EXCLUDED.total_snaps,
                  fetched_at   = NOW()
                """,
                db_rows,
            )
    except Exception as e:
        log.warning("nfl_snap_counts upsert failed: %s", e)
        raise
    return len(db_rows)


def has_api_key() -> bool:
    """True iff a Sportradar key is configured for the NFL endpoint."""
    return bool(_api_key())


def _api_key() -> str | None:
    return (
        os.environ.get("SPORTRADAR_NFL_KEY")
        or os.environ.get("SPORTRADAR_API_KEY")
    )


# ---------------------------------------------------------------------------
# Sportradar `extended_box_score` poller
# ---------------------------------------------------------------------------


def _player_snap_row(
    *,
    season: int,
    week: int,
    team_id: str | None,
    team_name: str,
    player: dict[str, Any],
) -> dict[str, Any] | None:
    """Pluck the snap-count-relevant fields out of one player dict.

    Sportradar nests participation either at the top of the player dict or
    under a ``statistics.participation`` block; we tolerate both.  Returns
    ``None`` if the player has no usable snap data so callers can drop them.
    """
    pid = str(player.get("id") or "")
    if not pid:
        return None
    pname = str(player.get("name") or player.get("full_name") or "") or None
    pos = str(player.get("position") or "") or None

    # Find the participation sub-dict — Sportradar has shipped this in a
    # few different locations across schema revisions.
    candidates = [
        player,
        player.get("statistics") if isinstance(player.get("statistics"), dict) else None,
        (player.get("statistics") or {}).get("participation")
            if isinstance(player.get("statistics"), dict) else None,
        player.get("participation") if isinstance(player.get("participation"), dict) else None,
    ]
    snap_pct: float | None = None
    snaps: int | None = None
    total: int | None = None
    for src in candidates:
        if not isinstance(src, dict):
            continue
        # Pick whichever side (offense / defense / st) has the highest %.
        for key_pct, key_snaps in (
            ("offense_snaps_pct", "offense_snaps"),
            ("defense_snaps_pct", "defense_snaps"),
            ("st_snaps_pct", "st_snaps"),
        ):
            v = src.get(key_pct)
            try:
                v_pct = float(v) if v is not None else None
            except (TypeError, ValueError):
                v_pct = None
            if v_pct is None:
                continue
            # Sportradar returns whole-number percentages (e.g. 87.0); some
            # operators normalise to 0..1.  Normalise everything to 0..100.
            if 0.0 <= v_pct <= 1.0:
                v_pct *= 100.0
            if snap_pct is None or v_pct > snap_pct:
                snap_pct = v_pct
                try:
                    snaps = int(src.get(key_snaps)) if src.get(key_snaps) is not None else None
                except (TypeError, ValueError):
                    snaps = None
        for total_key in ("total_snaps", "snaps_played"):
            v = src.get(total_key)
            try:
                total = int(v) if v is not None else total
            except (TypeError, ValueError):
                pass

    if snap_pct is None and snaps is None:
        return None
    return {
        "season": season,
        "week": week,
        "team_id": team_id,
        "team_name": team_name,
        "player_id": pid,
        "player_name": pname,
        "position": pos,
        "snap_pct": snap_pct,
        "snaps": snaps,
        "total_snaps": total,
    }


def parse_extended_box_score(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten one Sportradar ``extended_box_score`` payload into snap-count
    rows.  Tolerant of missing keys — anything we can't decode is dropped
    silently rather than raising."""
    if not isinstance(payload, dict):
        return []
    season = payload.get("season") or {}
    week = payload.get("week") or {}
    season_year = 0
    week_n = 0
    try:
        season_year = int(season.get("year") if isinstance(season, dict) else season or 0)
    except (TypeError, ValueError):
        season_year = 0
    try:
        week_n = int(week.get("sequence") if isinstance(week, dict) else week or 0)
    except (TypeError, ValueError):
        week_n = 0

    rows: list[dict[str, Any]] = []
    for side_key in ("home", "away"):
        side = payload.get(side_key) or {}
        if not isinstance(side, dict):
            continue
        team_id = str(side.get("id") or "") or None
        team_name = str(side.get("name") or side.get("market") or "")
        if not team_name:
            continue
        players = side.get("players") or []
        if not isinstance(players, list):
            continue
        for p in players:
            if not isinstance(p, dict):
                continue
            row = _player_snap_row(
                season=season_year,
                week=week_n,
                team_id=team_id,
                team_name=team_name,
                player=p,
            )
            if row:
                rows.append(row)
    return rows


async def _get_json(url: str, api_key: str) -> dict[str, Any] | None:
    """GET + JSON-decode one Sportradar URL.  Mirrors `sportradar._get_json`
    but kept local to avoid a circular import."""
    timeout = httpx.Timeout(10.0, connect=5.0)

    async def _do() -> httpx.Response:
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.get(url, params={"api_key": api_key})

    r = await retry_request(
        _do,
        client_name="sportradar:nfl:snap_counts",
        max_attempts=3,
        retryable_statuses=frozenset({500, 502, 503, 504}),
    )
    if r.status_code in (401, 403):
        raise RuntimeError(f"sportradar nfl auth rejected ({r.status_code})")
    if r.status_code == 429:
        # Honour Sportradar's Retry-After when present, else 60s.
        ra = r.headers.get("Retry-After")
        try:
            wait = float(ra) if ra else 60.0
        except (TypeError, ValueError):
            wait = 60.0
        raise RateLimited("sportradar:nfl:snap_counts", retry_after=wait)
    if r.status_code == 404:
        return None
    if r.status_code >= 400:
        raise RuntimeError(f"sportradar nfl HTTP {r.status_code}")
    try:
        return r.json() or {}
    except Exception:
        return None


async def _recent_closed_nfl_game_ids(limit: int) -> list[str]:
    """Pull recently-completed NFL game IDs from `sportradar_schedules`.

    Returns the most recent ``limit`` closed games inside the lookback
    window.  Best-effort — returns ``[]`` if the schedules table isn't
    populated yet (e.g. fresh install).
    """
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=_LOOKBACK_DAYS)
    try:
        async with db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT game_id
                  FROM sportradar_schedules
                 WHERE league = 'nfl'
                   AND status IN ('closed', 'complete', 'completed', 'final')
                   AND scheduled >= $1
                 ORDER BY scheduled DESC
                 LIMIT $2
                """,
                cutoff,
                int(limit),
            )
    except Exception as e:
        log.info("nfl_snap_counts: schedules lookup skipped (%s)", e)
        return []
    return [str(r["game_id"]) for r in rows if r["game_id"]]


async def poll_snap_counts() -> int:
    """Fetch + persist snap counts for recently-closed NFL games.

    Returns the number of player rows written across all games processed.
    Designed to be called from the feed scheduler; raises ``RateLimited``
    on Sportradar 429 so the scheduler can apply per-provider cooldown,
    and otherwise swallows per-game errors so one bad game can't stall
    the whole batch.
    """
    api_key = _api_key()
    if not api_key:
        raise RuntimeError("SPORTRADAR_NFL_KEY not set")
    await ensure_schema()
    game_ids = await _recent_closed_nfl_game_ids(_MAX_GAMES_PER_POLL)
    if not game_ids:
        return 0
    total = 0
    for gid in game_ids:
        url = _EXT_BOX_URL.format(game_id=gid)
        try:
            payload = await _get_json(url, api_key)
        except RateLimited:
            # Re-raise so the scheduler applies provider cooldown — we'll
            # pick up where we left off on the next poll cycle.
            raise
        except Exception as e:
            log.info("nfl_snap_counts: skip game %s (%s)", gid, e)
            continue
        if not payload:
            continue
        rows = parse_extended_box_score(payload)
        if not rows:
            continue
        try:
            total += await upsert_snap_counts(rows)
        except Exception as e:
            log.info("nfl_snap_counts: upsert failed for %s (%s)", gid, e)
            continue
    return total


# Daily by default — NFL games settle on Sun/Mon/Thu evenings, so a 24h
# cadence is more than enough to keep the cache fresh for the next slate.
SNAP_COUNTS_CADENCE_SECONDS = max(
    300,
    int(os.environ.get("AXIOM_NFL_SNAP_CADENCE") or str(60 * 60 * 24)),
)


def register_nfl_snap_counts_feed() -> None:
    """Register the daily NFL snap-count poller.

    Idempotent: re-registration is a no-op so the lifespan + tests can both
    call it.  When the API key is missing the feed is registered DISABLED
    with a clear ``skipped_reason`` so the operator console flags the gap
    on the Health page rather than failing silently.
    """
    name = "sportradar_nfl_snap_counts"
    if registry.status_of(name) is not None:
        return
    have_key = has_api_key()
    registry.register(Feed(
        name=name,
        cadence_seconds=float(SNAP_COUNTS_CADENCE_SECONDS),
        poll=poll_snap_counts,
        enabled=have_key,
        skipped_reason=None if have_key else "SPORTRADAR_NFL_KEY not set",
    ))


__all__ = [
    "ensure_schema",
    "upsert_snap_counts",
    "has_api_key",
    "parse_extended_box_score",
    "poll_snap_counts",
    "register_nfl_snap_counts_feed",
]
