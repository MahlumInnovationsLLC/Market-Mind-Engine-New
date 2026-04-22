"""Sportradar premium feeds: injuries + live in-game state.

Schedules for the six leagues we cover (NBA/NHL/MLB/NCAAB/NFL/NCAAF) live
in :mod:`odds_api` alongside The Odds API poller.  This module adds the
two complementary signals the spec calls for:

  * **Injuries** — the league-wide injury list refreshed every 15 minutes.
    Stored in `sportradar_injuries`, one row per (league, player_id).
  * **Live state** — score / clock / status for any games currently
    in-progress, refreshed every 60 seconds.  Stored in
    `sportradar_live_state`, one row per (league, game_id).

Each league/feed type is registered as its own pollable Feed so the
operator console shows per-league last-poll, error, and rate-limit info.
Feeds whose API key is missing register as `enabled=false` with a
`skipped_reason`; they never poll but still appear on the Health page.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

from .. import db
from .odds_api import (
    SPORTRADAR_SPORTS,
    _SPORTRADAR_LEAGUE,
    _retry_after_seconds,
)
from .registry import Feed, registry
from .scheduler import RateLimited


log = logging.getLogger("axiom.feeds.sportradar")


# ---------------------------------------------------------------------------
# Schema bootstrap (idempotent — called once at app startup)
# ---------------------------------------------------------------------------


_SCHEMA_LOCK = asyncio.Lock()
_SCHEMA_READY = False

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sportradar_schedules (
    league       text        NOT NULL,
    game_id      text        NOT NULL,
    home_team    text,
    away_team    text,
    scheduled    timestamptz,
    status       text,
    venue        text,
    payload      jsonb,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (league, game_id)
);
CREATE INDEX IF NOT EXISTS sportradar_schedules_when_idx
    ON sportradar_schedules (league, scheduled);

CREATE TABLE IF NOT EXISTS sportradar_rosters (
    league       text        NOT NULL,
    team_id      text        NOT NULL,
    team_name    text,
    player_id    text        NOT NULL,
    player_name  text,
    position     text,
    jersey       text,
    status       text,
    height       text,
    weight       text,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (league, team_id, player_id)
);
CREATE INDEX IF NOT EXISTS sportradar_rosters_team_idx
    ON sportradar_rosters (league, team_id);

CREATE TABLE IF NOT EXISTS sportradar_injuries (
    league       text        NOT NULL,
    player_id    text        NOT NULL,
    player_name  text,
    team_id      text,
    team_name    text,
    position     text,
    status       text,
    description  text,
    updated_at   timestamptz,
    fetched_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (league, player_id)
);
CREATE INDEX IF NOT EXISTS sportradar_injuries_team_idx
    ON sportradar_injuries (league, team_id);

CREATE TABLE IF NOT EXISTS sportradar_live_state (
    league       text        NOT NULL,
    game_id      text        NOT NULL,
    status       text,
    home_score   integer,
    away_score   integer,
    period       text,
    clock        text,
    scheduled    timestamptz,
    payload      jsonb,
    fetched_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (league, game_id)
);
CREATE INDEX IF NOT EXISTS sportradar_live_state_status_idx
    ON sportradar_live_state (league, status);
"""


async def ensure_schema() -> None:
    """Create the two cache tables if missing.  Idempotent and best-effort."""
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
            log.info("sportradar_* schema ready")
        except Exception as e:
            log.warning("sportradar schema bootstrap failed: %s", e)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _api_key_for(sport: str) -> str | None:
    return (
        os.environ.get(f"SPORTRADAR_{sport.upper()}_KEY")
        or os.environ.get("SPORTRADAR_API_KEY")
    )


async def _get_json(provider: str, url: str, api_key: str) -> dict[str, Any] | None:
    """Fetch + JSON-decode a Sportradar URL.  Raises RateLimited on 429.

    Returns ``None`` on 4xx other than 429 so callers can degrade gracefully
    (some leagues don't expose every endpoint on the trial tier).
    """
    timeout = httpx.Timeout(10.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(url, params={"api_key": api_key})
    if r.status_code in (401, 403):
        raise RuntimeError(f"{provider} auth rejected ({r.status_code})")
    if r.status_code == 429:
        raise RateLimited(provider, retry_after=_retry_after_seconds(r) or 60.0)
    if r.status_code == 404:
        # Endpoint not in this sport's tier — caller treats as 0 records.
        return None
    if r.status_code >= 400:
        raise RuntimeError(f"{provider} HTTP {r.status_code}: {r.text[:200]}")
    try:
        return r.json() or {}
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Injuries
# ---------------------------------------------------------------------------


_INJURY_PATHS = {
    "nba":   "https://api.sportradar.us/nba/trial/v8/en/league/injuries.json",
    "nhl":   "https://api.sportradar.us/nhl/trial/v7/en/league/injuries.json",
    "mlb":   "https://api.sportradar.us/mlb/trial/v7/en/league/injuries.json",
    "ncaab": "https://api.sportradar.us/ncaamb/trial/v8/en/league/injuries.json",
    "nfl":   "https://api.sportradar.us/nfl/official/trial/v7/en/league/injuries.json",
    "ncaaf": "https://api.sportradar.us/ncaafb/trial/v1/en/league/injuries.json",
}


def _flatten_injuries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Sportradar injury payloads vary across leagues; normalise to one row
    per (team, player).  Tolerant of missing keys — anything we can't
    decode is skipped silently rather than crashing the poll."""
    out: list[dict[str, Any]] = []
    teams = payload.get("teams") or []
    for team in teams:
        team_id = str(team.get("id") or "")
        team_name = team.get("name") or team.get("market") or ""
        for player in team.get("players", []) or []:
            pid = str(player.get("id") or "")
            if not pid:
                continue
            inj = (player.get("injuries") or [{}])[0] if player.get("injuries") else {}
            out.append({
                "player_id": pid,
                "player_name": player.get("full_name") or player.get("name") or "",
                "team_id": team_id,
                "team_name": team_name,
                "position": player.get("position") or "",
                "status": inj.get("status") or player.get("status") or "",
                "description": inj.get("desc") or inj.get("description") or "",
                "updated_at": inj.get("update_date") or inj.get("start_date"),
            })
    return out


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return None


async def _poll_injuries(sport: str) -> int:
    api_key = _api_key_for(sport)
    if not api_key:
        raise RuntimeError(f"SPORTRADAR_{sport.upper()}_KEY not set")
    url = _INJURY_PATHS.get(sport)
    if not url:
        return 0
    payload = await _get_json(f"sportradar:{sport}:injuries", url, api_key)
    if not payload:
        return 0
    rows = _flatten_injuries(payload)
    if not rows:
        return 0

    league = _SPORTRADAR_LEAGUE[sport]
    db_rows = [
        (
            league,
            r["player_id"],
            r["player_name"],
            r["team_id"],
            r["team_name"],
            r["position"],
            r["status"],
            r["description"],
            _parse_ts(r["updated_at"]),
        )
        for r in rows
    ]

    try:
        async with db.acquire() as conn, conn.transaction():
            await conn.executemany(
                """
                INSERT INTO sportradar_injuries
                  (league, player_id, player_name, team_id, team_name,
                   position, status, description, updated_at, fetched_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW())
                ON CONFLICT (league, player_id) DO UPDATE SET
                  player_name = EXCLUDED.player_name,
                  team_id     = EXCLUDED.team_id,
                  team_name   = EXCLUDED.team_name,
                  position    = EXCLUDED.position,
                  status      = EXCLUDED.status,
                  description = EXCLUDED.description,
                  updated_at  = EXCLUDED.updated_at,
                  fetched_at  = NOW()
                """,
                db_rows,
            )
    except Exception as e:
        # Surface persistence failures to the scheduler so feed-status /
        # error-rate reflect reality instead of a silent zero-row "success".
        log.warning("sportradar %s injuries upsert failed: %s", sport, e)
        raise
    return len(db_rows)


# ---------------------------------------------------------------------------
# Schedule cache (mirror of `games` keyed per-league for backtests/queries)
# ---------------------------------------------------------------------------


async def cache_schedule_rows(
    league: str, games: list[dict[str, Any]]
) -> int:
    """Mirror today's schedule into `sportradar_schedules` for analytics
    queries that don't want to touch the cross-source `games` table."""
    if not games:
        return 0
    import json as _json
    rows: list[tuple] = []
    for g in games:
        gid = str(g.get("id") or "")
        if not gid:
            continue
        home = (g.get("home") or {}).get("name") or g.get("home_team") or ""
        away = (g.get("away") or {}).get("name") or g.get("away_team") or ""
        venue = (g.get("venue") or {}).get("name") if isinstance(g.get("venue"), dict) else ""
        rows.append((
            league, gid, home, away,
            _parse_ts(g.get("scheduled")),
            str(g.get("status") or "scheduled"),
            venue,
            _json.dumps(g, default=str),
        ))
    if not rows:
        return 0
    try:
        async with db.acquire() as conn, conn.transaction():
            await conn.executemany(
                """
                INSERT INTO sportradar_schedules
                  (league, game_id, home_team, away_team, scheduled,
                   status, venue, payload, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, NOW())
                ON CONFLICT (league, game_id) DO UPDATE SET
                  home_team  = EXCLUDED.home_team,
                  away_team  = EXCLUDED.away_team,
                  scheduled  = EXCLUDED.scheduled,
                  status     = EXCLUDED.status,
                  venue      = EXCLUDED.venue,
                  payload    = EXCLUDED.payload,
                  updated_at = NOW()
                """,
                rows,
            )
    except Exception as e:
        log.warning("sportradar_schedules upsert failed for %s: %s", league, e)
        raise
    return len(rows)


# ---------------------------------------------------------------------------
# Rosters
# ---------------------------------------------------------------------------


_ROSTER_PATHS = {
    "nba":   "https://api.sportradar.us/nba/trial/v8/en/league/hierarchy.json",
    "nhl":   "https://api.sportradar.us/nhl/trial/v7/en/league/hierarchy.json",
    "mlb":   "https://api.sportradar.us/mlb/trial/v7/en/league/hierarchy.json",
    "ncaab": "https://api.sportradar.us/ncaamb/trial/v8/en/league/hierarchy.json",
    "nfl":   "https://api.sportradar.us/nfl/official/trial/v7/en/league/hierarchy.json",
    "ncaaf": "https://api.sportradar.us/ncaafb/trial/v1/en/league/hierarchy.json",
}

# Per-team roster: tail-appended per team_id; some leagues nest the
# players directly inside the hierarchy payload, so the per-team call is
# only used as a fallback when the hierarchy has empty `players` lists.
_TEAM_PROFILE_PATHS = {
    "nba":   "https://api.sportradar.us/nba/trial/v8/en/teams/{team_id}/profile.json",
    "nhl":   "https://api.sportradar.us/nhl/trial/v7/en/teams/{team_id}/profile.json",
    "mlb":   "https://api.sportradar.us/mlb/trial/v7/en/teams/{team_id}/profile.json",
    "ncaab": "https://api.sportradar.us/ncaamb/trial/v8/en/teams/{team_id}/profile.json",
    "nfl":   "https://api.sportradar.us/nfl/official/trial/v7/en/teams/{team_id}/full_roster.json",
    "ncaaf": "https://api.sportradar.us/ncaafb/trial/v1/en/teams/{team_id}/profile.json",
}


def _walk_teams(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Walk the league hierarchy and yield every team dict regardless of
    league-specific nesting (conferences -> divisions -> teams, or
    leagues -> conferences -> teams)."""
    out: list[dict[str, Any]] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("players") is not None or (
                node.get("id") and node.get("name") and "market" in node
            ):
                out.append(node)
            for v in node.values():
                visit(v)
        elif isinstance(node, list):
            for v in node:
                visit(v)

    visit(payload)
    # De-dupe by team id, keeping the version with the most players.
    by_id: dict[str, dict[str, Any]] = {}
    for t in out:
        tid = str(t.get("id") or "")
        if not tid:
            continue
        prev = by_id.get(tid)
        if prev is None or len(t.get("players") or []) > len(prev.get("players") or []):
            by_id[tid] = t
    return list(by_id.values())


async def _poll_rosters(sport: str) -> int:
    api_key = _api_key_for(sport)
    if not api_key:
        raise RuntimeError(f"SPORTRADAR_{sport.upper()}_KEY not set")
    url = _ROSTER_PATHS.get(sport)
    if not url:
        return 0
    payload = await _get_json(f"sportradar:{sport}:rosters", url, api_key)
    if not payload:
        return 0
    teams = _walk_teams(payload)
    league = _SPORTRADAR_LEAGUE[sport]
    profile_url_tpl = _TEAM_PROFILE_PATHS.get(sport)

    def _player_row(team_id: str, team_name: str, player: dict[str, Any]) -> tuple | None:
        pid = str(player.get("id") or "")
        if not pid:
            return None
        return (
            league,
            team_id,
            team_name,
            pid,
            player.get("full_name") or player.get("name") or "",
            player.get("position") or "",
            str(player.get("jersey_number") or player.get("jersey") or ""),
            player.get("status") or "",
            str(player.get("height") or ""),
            str(player.get("weight") or ""),
        )

    rows: list[tuple] = []
    teams_with_players = 0
    teams_via_fallback = 0
    for team in teams:
        tid = str(team.get("id") or "")
        if not tid:
            continue
        tname = team.get("name") or team.get("market") or ""
        inline_players = team.get("players") or []

        # Fallback: hierarchy returned no inline players for this team.
        # Hit the per-team profile/full_roster endpoint and merge.
        if not inline_players and profile_url_tpl:
            try:
                prof = await _get_json(
                    f"sportradar:{sport}:roster:{tid}",
                    profile_url_tpl.format(team_id=tid),
                    api_key,
                )
            except Exception as e:  # noqa: BLE001 — log + continue, don't poison whole poll
                log.warning("sportradar %s team %s profile fetch failed: %s", sport, tid, e)
                prof = None
            if prof:
                inline_players = prof.get("players") or []
                if inline_players:
                    teams_via_fallback += 1

        if inline_players:
            teams_with_players += 1
        for player in inline_players:
            row = _player_row(tid, tname, player)
            if row is not None:
                rows.append(row)

    log.info(
        "sportradar %s rosters: %d teams scanned, %d with players (%d via per-team fallback), %d player rows",
        sport, len(teams), teams_with_players, teams_via_fallback, len(rows),
    )
    if not rows:
        return 0
    try:
        async with db.acquire() as conn, conn.transaction():
            await conn.executemany(
                """
                INSERT INTO sportradar_rosters
                  (league, team_id, team_name, player_id, player_name,
                   position, jersey, status, height, weight, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NOW())
                ON CONFLICT (league, team_id, player_id) DO UPDATE SET
                  team_name   = EXCLUDED.team_name,
                  player_name = EXCLUDED.player_name,
                  position    = EXCLUDED.position,
                  jersey      = EXCLUDED.jersey,
                  status      = EXCLUDED.status,
                  height      = EXCLUDED.height,
                  weight      = EXCLUDED.weight,
                  updated_at  = NOW()
                """,
                rows,
            )
    except Exception as e:
        log.warning("sportradar %s rosters upsert failed: %s", sport, e)
        raise
    return len(rows)


# ---------------------------------------------------------------------------
# Live in-game state
# ---------------------------------------------------------------------------


_DAILY_SCHEDULE_PATHS = {
    "nba":   "https://api.sportradar.us/nba/trial/v8/en/games/{y}/{m:02d}/{d:02d}/schedule.json",
    "nhl":   "https://api.sportradar.us/nhl/trial/v7/en/games/{y}/{m:02d}/{d:02d}/schedule.json",
    "mlb":   "https://api.sportradar.us/mlb/trial/v7/en/games/{y}/{m:02d}/{d:02d}/schedule.json",
    "ncaab": "https://api.sportradar.us/ncaamb/trial/v8/en/games/{y}/{m:02d}/{d:02d}/schedule.json",
    # NFL has no daily endpoint — current_week covers any in-progress game.
    "nfl":   "https://api.sportradar.us/nfl/official/trial/v7/en/games/current_week/schedule.json",
    "ncaaf": "https://api.sportradar.us/ncaafb/trial/v1/en/games/{y}/{m:02d}/{d:02d}/schedule.json",
    # Soccer leagues use the soccer-extended /v4 API which has a
    # different (competition-keyed) payload shape than the NA-league
    # endpoints above.  Rather than registering live-state pollers
    # that would mis-attribute events across competitions, soccer
    # live/roster/injury feeds are intentionally registered DISABLED
    # with a clear `skipped_reason` until the soccer-extended payload
    # parser ships (follow-up task #211).  Schedules themselves are
    # pulled via the dedicated `_SPORTRADAR_SCHEDULE_PATHS` map in
    # `app/feeds/odds_api.py`, which has the soccer-extended URLs
    # and a soccer-aware normalizer.
}

_LIVE_STATUSES = {"inprogress", "halftime", "delayed", "scheduled.live"}


def _today_url(sport: str) -> str:
    template = _DAILY_SCHEDULE_PATHS[sport]
    today = datetime.now(tz=timezone.utc)
    return template.format(y=today.year, m=today.month, d=today.day)


def _extract_games(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not payload:
        return []
    if isinstance(payload.get("games"), list):
        return payload["games"]
    league = payload.get("league") or {}
    if isinstance(league.get("games"), list):
        return league["games"]
    # NFL current_week wraps games inside week.games
    week = payload.get("week") or {}
    if isinstance(week.get("games"), list):
        return week["games"]
    return []


async def _poll_live_state(sport: str) -> int:
    api_key = _api_key_for(sport)
    if not api_key:
        raise RuntimeError(f"SPORTRADAR_{sport.upper()}_KEY not set")
    url = _today_url(sport)
    payload = await _get_json(f"sportradar:{sport}:live", url, api_key)
    if not payload:
        return 0
    games = _extract_games(payload)
    if not games:
        return 0

    league = _SPORTRADAR_LEAGUE[sport]
    rows: list[tuple] = []
    import json as _json
    for g in games:
        status = str(g.get("status") or "").lower()
        # Only persist live-ish games to keep the table small.
        if status not in _LIVE_STATUSES and "progress" not in status:
            continue
        gid = str(g.get("id") or "")
        if not gid:
            continue
        home_score = (
            g.get("home_points")
            if g.get("home_points") is not None
            else (g.get("home") or {}).get("runs") or (g.get("home") or {}).get("points")
        )
        away_score = (
            g.get("away_points")
            if g.get("away_points") is not None
            else (g.get("away") or {}).get("runs") or (g.get("away") or {}).get("points")
        )
        period = str(
            g.get("quarter")
            or g.get("period")
            or g.get("inning")
            or g.get("half")
            or ""
        )
        clock = str(g.get("clock") or g.get("time_remaining") or "")
        scheduled = _parse_ts(g.get("scheduled"))
        rows.append((
            league,
            gid,
            status,
            int(home_score) if home_score is not None else None,
            int(away_score) if away_score is not None else None,
            period,
            clock,
            scheduled,
            _json.dumps(g, default=str),
        ))

    if not rows:
        return 0
    try:
        async with db.acquire() as conn, conn.transaction():
            await conn.executemany(
                """
                INSERT INTO sportradar_live_state
                  (league, game_id, status, home_score, away_score,
                   period, clock, scheduled, payload, fetched_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, NOW())
                ON CONFLICT (league, game_id) DO UPDATE SET
                  status     = EXCLUDED.status,
                  home_score = EXCLUDED.home_score,
                  away_score = EXCLUDED.away_score,
                  period     = EXCLUDED.period,
                  clock      = EXCLUDED.clock,
                  scheduled  = EXCLUDED.scheduled,
                  payload    = EXCLUDED.payload,
                  fetched_at = NOW()
                """,
                rows,
            )
    except Exception as e:
        log.warning("sportradar %s live-state upsert failed: %s", sport, e)
        raise
    return len(rows)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


# Per-feed cadence overrides via env so an operator can dial things down
# on a tight quota.
INJURIES_CADENCE_SECONDS = max(60, int(os.environ.get("AXIOM_SPORTRADAR_INJURIES_CADENCE") or "900"))
LIVE_STATE_CADENCE_SECONDS = max(15, int(os.environ.get("AXIOM_SPORTRADAR_LIVE_CADENCE") or "60"))
# Rosters change rarely (transactions / waiver wire) — refresh every 24h
# by default.
ROSTERS_CADENCE_SECONDS = max(
    300, int(os.environ.get("AXIOM_SPORTRADAR_ROSTERS_CADENCE") or str(60 * 60 * 24))
)


def register_sportradar_extra_feeds() -> None:
    """Register injuries + rosters + live-state pollers for every Sportradar
    sport.

    Idempotent: if any feed name is already in the registry we skip — this
    lets the function be called from both lifespan and tests without
    raising "already registered".  Each sport that ships with rosters /
    injuries / live-state path templates contributes 3 feeds.  Soccer
    leagues (task #196) only ship the daily schedule today and so are
    registered with the three extra feeds *disabled* with a clear
    `skipped_reason` — wiring the soccer endpoints is tracked under a
    separate follow-up task.  Schedules themselves are registered
    alongside The Odds API in `odds_api.register_odds_feeds`.
    """
    for sport in SPORTRADAR_SPORTS:
        api_key = _api_key_for(sport)
        has_roster_path = sport in _ROSTER_PATHS
        has_injury_path = sport in _INJURY_PATHS
        has_live_path = sport in _DAILY_SCHEDULE_PATHS

        def _reason(have_path: bool, kind: str) -> str | None:
            if not have_path:
                return f"sportradar {kind} endpoint not wired for {sport}"
            if not api_key:
                return f"SPORTRADAR_{sport.upper()}_KEY not set"
            return None

        ros_name = f"sportradar_{sport}_rosters"
        if registry.status_of(ros_name) is None:
            async def _ros(sport: str = sport) -> int:
                return await _poll_rosters(sport)
            registry.register(Feed(
                name=ros_name,
                cadence_seconds=float(ROSTERS_CADENCE_SECONDS),
                poll=_ros,
                enabled=bool(api_key) and has_roster_path,
                skipped_reason=_reason(has_roster_path, "rosters"),
            ))

        inj_name = f"sportradar_{sport}_injuries"
        if registry.status_of(inj_name) is None:
            async def _inj(sport: str = sport) -> int:
                return await _poll_injuries(sport)
            registry.register(Feed(
                name=inj_name,
                cadence_seconds=float(INJURIES_CADENCE_SECONDS),
                poll=_inj,
                enabled=bool(api_key) and has_injury_path,
                skipped_reason=_reason(has_injury_path, "injuries"),
            ))

        live_name = f"sportradar_{sport}_live"
        if registry.status_of(live_name) is None:
            async def _live(sport: str = sport) -> int:
                return await _poll_live_state(sport)
            registry.register(Feed(
                name=live_name,
                cadence_seconds=float(LIVE_STATE_CADENCE_SECONDS),
                poll=_live,
                enabled=bool(api_key) and has_live_path,
                skipped_reason=_reason(has_live_path, "live"),
            ))
