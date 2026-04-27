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

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from .. import db
from ..http_retry import retry_request
from .odds_api import (
    SPORTRADAR_SPORTS,
    _SPORTRADAR_LEAGUE,
    _retry_after_seconds,
)
from .registry import Feed, registry
from .scheduler import RateLimited


def _utc_iso(epoch: float | None = None) -> str:
    """Render an epoch (defaults to now) as an ISO-8601 UTC string,
    matching the format the rest of FeedStatus uses (seconds resolution)."""
    ts = epoch if epoch is not None else time.time()
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


log = logging.getLogger("axiom.feeds.sportradar")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _api_key_for(sport: str) -> str | None:
    return (
        os.environ.get(f"SPORTRADAR_{sport.upper()}_KEY")
        or os.environ.get("SPORTRADAR_API_KEY")
    )


class _NotFound:
    """Singleton sentinel returned by ``_get_json(..., distinguish_404=True)``
    so callers that need to react to a 404 (e.g. the soccer pollers, which
    must invalidate a stale cached season id) can tell a "not found"
    response apart from a successful-but-empty payload."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging only
        return "<NOT_FOUND>"


_NOT_FOUND = _NotFound()


async def _get_json(
    provider: str,
    url: str,
    api_key: str,
    *,
    distinguish_404: bool = False,
) -> dict[str, Any] | None | _NotFound:
    """Fetch + JSON-decode a Sportradar URL.  Raises RateLimited on 429.

    Returns ``None`` on 4xx other than 429 so callers can degrade gracefully
    (some leagues don't expose every endpoint on the trial tier).  When
    ``distinguish_404=True`` is passed, a 404 response returns the
    ``_NOT_FOUND`` sentinel instead of ``None`` so the caller can
    differentiate "endpoint not in tier / season rolled over" from a
    decode failure.
    """
    timeout = httpx.Timeout(10.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        # 429 stays the caller's problem (we re-raise as RateLimited
        # below so the scheduler can apply per-provider cooldown), but
        # transient 5xx / network errors retry up to 3 times.
        async def _do() -> httpx.Response:
            return await client.get(url, params={"api_key": api_key})

        r = await retry_request(
            _do,
            client_name=f"sportradar:{provider}",
            max_attempts=3,
            retryable_statuses=frozenset({500, 502, 503, 504}),
        )
    if r.status_code in (401, 403):
        raise RuntimeError(f"{provider} auth rejected ({r.status_code})")
    if r.status_code == 429:
        raise RateLimited(provider, retry_after=_retry_after_seconds(r) or 60.0)
    if r.status_code == 404:
        # Endpoint not in this sport's tier — caller treats as 0 records.
        return _NOT_FOUND if distinguish_404 else None
    if r.status_code >= 400:
        raise RuntimeError(f"{provider} HTTP {r.status_code}: {r.text[:200]}")
    try:
        return r.json() or {}
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Injuries
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Soccer-extended (task #211): injury / roster / live-state endpoints share
# a different URL family than the NA leagues.  The soccer-extended /v4 API
# is keyed by ``sr:tournament:<id>`` (per competition) and ``sr:season:<id>``
# (per season).  Roster + injury endpoints require a season id which we
# discover at runtime from ``tournaments/{tid}/info.json``.  We cache the
# resolved season id for the process lifetime since soccer seasons turn
# over only a couple of times per year.
# ---------------------------------------------------------------------------

_SOCCER_TOURNAMENTS: dict[str, str] = {
    "epl":    "sr:tournament:17",
    "laliga": "sr:tournament:8",
    "seriea": "sr:tournament:23",
    "bundes": "sr:tournament:35",
    "ligue1": "sr:tournament:34",
    "mls":    "sr:tournament:242",
    "ucl":    "sr:tournament:7",
}

_SOCCER_SET = frozenset(_SOCCER_TOURNAMENTS)

_SOCCER_TOURNAMENT_INFO_TPL = (
    "https://api.sportradar.us/soccer-extended/trial/v4/en/"
    "tournaments/{tid}/info.json"
)
_SOCCER_SEASON_COMPETITORS_TPL = (
    "https://api.sportradar.us/soccer-extended/trial/v4/en/"
    "seasons/{season_id}/competitors.json"
)
_SOCCER_SEASON_MISSING_TPL = (
    "https://api.sportradar.us/soccer-extended/trial/v4/en/"
    "seasons/{season_id}/missing_players.json"
)
_SOCCER_COMPETITOR_PROFILE_TPL = (
    "https://api.sportradar.us/soccer-extended/trial/v4/en/"
    "competitors/{team_id}/profile.json"
)
_SOCCER_DAILY_SCHEDULE_TPL = (
    "https://api.sportradar.us/soccer-extended/trial/v4/en/"
    "schedules/{y}-{m:02d}-{d:02d}/schedule.json?tournament={tid}"
)

# Process-level cache: tournament_id -> season_id.  Refreshed lazily if a
# downstream call returns 404 (season turned over).
_SOCCER_SEASON_CACHE: dict[str, str] = {}

# Parallel cache: tournament_id -> ISO timestamp of when the *current*
# cached season id was first resolved (either initial lookup or after a
# 404-driven refresh).  Surfaced in the operator console so humans can
# see when a competition rolled into a new season.
_SOCCER_SEASON_RESOLVED_AT: dict[str, str] = {}


_INJURY_PATHS: dict[str, str] = {
    "nba":   "https://api.sportradar.us/nba/trial/v8/en/league/injuries.json",
    "nhl":   "https://api.sportradar.us/nhl/trial/v7/en/league/injuries.json",
    "mlb":   "https://api.sportradar.us/mlb/trial/v7/en/league/injuries.json",
    "ncaab": "https://api.sportradar.us/ncaamb/trial/v8/en/league/injuries.json",
    "nfl":   "https://api.sportradar.us/nfl/official/trial/v7/en/league/injuries.json",
    "ncaaf": "https://api.sportradar.us/ncaafb/trial/v1/en/league/injuries.json",
}
# Soccer "injury" endpoint requires runtime season id; we record the
# tournament-info URL here so existing `has_injury_path` checks succeed and
# the actual two-step fetch is performed in `_poll_soccer_injuries`.
for _code, _tid in _SOCCER_TOURNAMENTS.items():
    _INJURY_PATHS[_code] = _SOCCER_TOURNAMENT_INFO_TPL.format(tid=_tid)


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
    if sport in _SOCCER_SET:
        return await _poll_soccer_injuries(sport, api_key)
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


_ROSTER_PATHS: dict[str, str] = {
    "nba":   "https://api.sportradar.us/nba/trial/v8/en/league/hierarchy.json",
    "nhl":   "https://api.sportradar.us/nhl/trial/v7/en/league/hierarchy.json",
    "mlb":   "https://api.sportradar.us/mlb/trial/v7/en/league/hierarchy.json",
    "ncaab": "https://api.sportradar.us/ncaamb/trial/v8/en/league/hierarchy.json",
    "nfl":   "https://api.sportradar.us/nfl/official/trial/v7/en/league/hierarchy.json",
    "ncaaf": "https://api.sportradar.us/ncaafb/trial/v1/en/league/hierarchy.json",
}
# Soccer rosters require a runtime season-id lookup; record the
# tournament-info URL so existing `has_roster_path` checks succeed.
for _code, _tid in _SOCCER_TOURNAMENTS.items():
    _ROSTER_PATHS[_code] = _SOCCER_TOURNAMENT_INFO_TPL.format(tid=_tid)

# Per-team roster: tail-appended per team_id; some leagues nest the
# players directly inside the hierarchy payload, so the per-team call is
# only used as a fallback when the hierarchy has empty `players` lists.
_TEAM_PROFILE_PATHS: dict[str, str] = {
    "nba":   "https://api.sportradar.us/nba/trial/v8/en/teams/{team_id}/profile.json",
    "nhl":   "https://api.sportradar.us/nhl/trial/v7/en/teams/{team_id}/profile.json",
    "mlb":   "https://api.sportradar.us/mlb/trial/v7/en/teams/{team_id}/profile.json",
    "ncaab": "https://api.sportradar.us/ncaamb/trial/v8/en/teams/{team_id}/profile.json",
    "nfl":   "https://api.sportradar.us/nfl/official/trial/v7/en/teams/{team_id}/full_roster.json",
    "ncaaf": "https://api.sportradar.us/ncaafb/trial/v1/en/teams/{team_id}/profile.json",
}
for _code in _SOCCER_TOURNAMENTS:
    _TEAM_PROFILE_PATHS[_code] = _SOCCER_COMPETITOR_PROFILE_TPL


# ---------------------------------------------------------------------------
# Soccer-extended helpers
# ---------------------------------------------------------------------------


async def _resolve_soccer_season_id(
    sport: str, api_key: str, *, force: bool = False
) -> str | None:
    """Return the active season id for a soccer competition, caching it
    for the process lifetime.  Returns None if Sportradar can't tell us.
    """
    tid = _SOCCER_TOURNAMENTS.get(sport)
    if not tid:
        return None
    if not force:
        cached = _SOCCER_SEASON_CACHE.get(tid)
        if cached:
            return cached
    info = await _get_json(
        f"sportradar:{sport}:tournament_info",
        _SOCCER_TOURNAMENT_INFO_TPL.format(tid=tid),
        api_key,
    )
    if not info:
        return None
    # Soccer-extended /v4 returns either {"season": {...}} (active
    # season) or {"seasons": [...]} (list).  Prefer the explicit
    # `season` block when present.
    season = info.get("season") if isinstance(info.get("season"), dict) else None
    season_id = (season or {}).get("id")
    if not season_id:
        seasons = info.get("seasons") or []
        # Pick the most recent season whose start_date is in the past
        # and end_date in the future, falling back to the last entry.
        if isinstance(seasons, list) and seasons:
            now_iso = datetime.now(tz=timezone.utc).isoformat()
            active = [
                s for s in seasons
                if isinstance(s, dict)
                and str(s.get("start_date") or "") <= now_iso
                and str(s.get("end_date") or "9999") >= now_iso
            ]
            chosen = active[-1] if active else seasons[-1]
            if isinstance(chosen, dict):
                season_id = chosen.get("id")
    if not season_id:
        return None
    _SOCCER_SEASON_CACHE[tid] = str(season_id)
    _SOCCER_SEASON_RESOLVED_AT[tid] = _utc_iso()
    return _SOCCER_SEASON_CACHE[tid]


async def _fetch_soccer_seasoned(
    sport: str,
    api_key: str,
    provider: str,
    url_template: str,
    *,
    feed_name: str | None = None,
) -> dict[str, Any] | None:
    """Fetch a soccer-extended endpoint that's keyed by ``{season_id}``,
    transparently refreshing the cached season id if the cached one has
    expired (Sportradar returns 404 for ``seasons/{stale_id}/...`` once
    the competition rolls into a new season).

    Returns ``None`` on definitive failure (no season available, repeat
    404 after refresh, etc.) so callers can short-circuit to 0 rows.
    """
    tid = _SOCCER_TOURNAMENTS.get(sport)
    if not tid:
        return None
    season_id = await _resolve_soccer_season_id(sport, api_key)
    if not season_id:
        if feed_name:
            registry.set_metadata(
                feed_name,
                season_id=None,
                season_id_resolved_at=None,
                tournament_id=tid,
                season_stuck=True,
                season_last_refresh_at=_utc_iso(),
                season_last_refresh_outcome="no_id_available",
            )
        return None
    # Always advertise the currently-cached season id so the operator
    # console can show *which* season the feed is pinned to.  Reset
    # any previous "stuck" flag whenever we have a usable id; the 404
    # branch below re-flips it if the refresh yields nothing new.
    if feed_name:
        registry.set_metadata(
            feed_name,
            season_id=season_id,
            season_id_resolved_at=_SOCCER_SEASON_RESOLVED_AT.get(tid),
            tournament_id=tid,
            season_stuck=False,
        )
    payload = await _get_json(
        provider,
        url_template.format(season_id=season_id),
        api_key,
        distinguish_404=True,
    )
    if not isinstance(payload, _NotFound):
        # _get_json is typed as `dict | None | _NotFound`; the
        # isinstance narrow leaves the union of `dict | None`, both
        # of which are valid return values for this helper.
        return payload
    # 404 — the cached season id has likely rolled over.  Drop it from
    # the cache, re-resolve via tournament info, and retry once.  If the
    # refreshed id matches what we had (or still 404s) we give up rather
    # than thrash on every poll.
    log.info(
        "sportradar %s: cached season id %s returned 404, refreshing", sport,
        season_id,
    )
    _SOCCER_SEASON_CACHE.pop(tid, None)
    refresh_at = _utc_iso()
    new_season_id = await _resolve_soccer_season_id(sport, api_key, force=True)
    if not new_season_id or new_season_id == season_id:
        # Refresh yielded nothing new — Sportradar's tournament info is
        # also stale, or it pointed back at the same id we just got a
        # 404 on.  Flag the feed as stuck so the operator console
        # surfaces a "season id stuck" warning instead of letting the
        # poll look like a green zero-row success.
        if feed_name:
            registry.set_metadata(
                feed_name,
                season_id=season_id,
                season_previous_id=season_id,
                tournament_id=tid,
                season_stuck=True,
                season_last_refresh_at=refresh_at,
                season_last_refresh_outcome=(
                    "same_id" if new_season_id == season_id else "no_new_id"
                ),
            )
        # Restore the cached id so we don't thrash tournament_info on
        # every subsequent poll: we'll keep retrying the seasoned
        # endpoint with the same (still 404'ing) id and reporting
        # "stuck" each cycle.
        _SOCCER_SEASON_CACHE[tid] = season_id
        return None
    retry = await _get_json(
        provider,
        url_template.format(season_id=new_season_id),
        api_key,
        distinguish_404=True,
    )
    refresh_outcome = "rotated"
    refresh_payload: dict[str, Any] | None
    if isinstance(retry, _NotFound):
        # New id 404s too — we found a fresh season id but the
        # downstream endpoint still doesn't have it.  Treat as stuck.
        refresh_outcome = "rotated_but_404"
        refresh_payload = None
    else:
        refresh_payload = retry
    if feed_name:
        registry.set_metadata(
            feed_name,
            season_id=new_season_id,
            season_previous_id=season_id,
            season_id_resolved_at=_SOCCER_SEASON_RESOLVED_AT.get(tid),
            tournament_id=tid,
            season_stuck=(refresh_payload is None),
            season_last_refresh_at=refresh_at,
            season_last_refresh_outcome=refresh_outcome,
        )
    return refresh_payload


def _flatten_soccer_injuries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Soccer-extended `seasons/{season_id}/missing_players.json` payload:

        {"competitors": [
            {"id": "sr:competitor:1", "name": "Arsenal",
             "players": [{
                "id": "sr:player:1", "name": "Bukayo Saka",
                "type": "type",  # "missing", "doubtful"
                "status": "missing",
                "reason": "knee injury",
                "start_date": "2026-04-01"}]}]}
    """
    out: list[dict[str, Any]] = []
    for team in payload.get("competitors") or []:
        if not isinstance(team, dict):
            continue
        team_id = str(team.get("id") or "")
        team_name = team.get("name") or ""
        for player in team.get("players") or team.get("missing_players") or []:
            pid = str(player.get("id") or "")
            if not pid:
                continue
            out.append({
                "player_id": pid,
                "player_name": player.get("name") or player.get("full_name") or "",
                "team_id": team_id,
                "team_name": team_name,
                "position": player.get("type") or player.get("position") or "",
                "status": player.get("status") or player.get("type") or "missing",
                "description": (
                    player.get("reason")
                    or player.get("description")
                    or player.get("desc")
                    or ""
                ),
                "updated_at": (
                    player.get("start_date")
                    or player.get("update_date")
                ),
            })
    return out


def _flatten_soccer_competitor_profile(
    profile: dict[str, Any],
) -> tuple[str, str, list[dict[str, Any]]]:
    """Pull (team_id, team_name, players[]) out of a soccer-extended
    competitor profile payload."""
    competitor = profile.get("competitor") or {}
    team_id = str(competitor.get("id") or "")
    team_name = competitor.get("name") or competitor.get("short_name") or ""
    players = profile.get("players") or competitor.get("players") or []
    return team_id, team_name, [p for p in players if isinstance(p, dict)]


async def _poll_soccer_rosters(sport: str, api_key: str) -> int:
    """Two-step soccer roster fetch: season competitors -> per-team profile.

    Uses :func:`_fetch_soccer_seasoned` so a stale cached season id is
    transparently refreshed when the season rolls over (otherwise the
    cached id would 404 ``seasons/{id}/competitors.json`` for the rest
    of the process lifetime, silently dropping rosters to zero rows).
    """
    season_payload = await _fetch_soccer_seasoned(
        sport,
        api_key,
        f"sportradar:{sport}:rosters",
        _SOCCER_SEASON_COMPETITORS_TPL,
        feed_name=f"sportradar_{sport}_rosters",
    )
    if not season_payload:
        log.info("sportradar %s rosters: no season payload available", sport)
        return 0
    competitors = (
        season_payload.get("season_competitors")
        or season_payload.get("competitors")
        or []
    )
    league = _SPORTRADAR_LEAGUE[sport]
    rows: list[tuple] = []
    teams_with_players = 0
    for team in competitors:
        if not isinstance(team, dict):
            continue
        tid = str(team.get("id") or "")
        if not tid:
            continue
        try:
            profile = await _get_json(
                f"sportradar:{sport}:roster:{tid}",
                _SOCCER_COMPETITOR_PROFILE_TPL.format(team_id=tid),
                api_key,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "sportradar %s competitor %s profile fetch failed: %s",
                sport, tid, e,
            )
            continue
        if not profile:
            continue
        cid, cname, players = _flatten_soccer_competitor_profile(profile)
        team_id = cid or tid
        team_name = cname or team.get("name") or ""
        if players:
            teams_with_players += 1
        for player in players:
            pid = str(player.get("id") or "")
            if not pid:
                continue
            rows.append((
                league,
                team_id,
                team_name,
                pid,
                player.get("name") or player.get("full_name") or "",
                player.get("type") or player.get("position") or "",
                str(player.get("jersey_number") or player.get("jersey") or ""),
                player.get("status") or "",
                str(player.get("height") or ""),
                str(player.get("weight") or ""),
            ))
    log.info(
        "sportradar %s soccer rosters: %d competitors, %d with players, %d player rows",
        sport, len(competitors), teams_with_players, len(rows),
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
        log.warning("sportradar %s soccer rosters upsert failed: %s", sport, e)
        raise
    return len(rows)


async def _poll_soccer_injuries(sport: str, api_key: str) -> int:
    """Fetch league-wide missing-player list for a soccer competition.

    Uses :func:`_fetch_soccer_seasoned` so a stale cached season id is
    transparently refreshed when the competition rolls into a new season
    (otherwise the cached id would 404 ``seasons/{id}/missing_players``
    for the rest of the process lifetime, silently dropping injury rows).
    """
    payload = await _fetch_soccer_seasoned(
        sport,
        api_key,
        f"sportradar:{sport}:injuries",
        _SOCCER_SEASON_MISSING_TPL,
        feed_name=f"sportradar_{sport}_injuries",
    )
    if not payload:
        return 0
    rows = _flatten_soccer_injuries(payload)
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
        log.warning("sportradar %s soccer injuries upsert failed: %s", sport, e)
        raise
    return len(db_rows)


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
    if sport in _SOCCER_SET:
        return await _poll_soccer_rosters(sport, api_key)
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


_DAILY_SCHEDULE_PATHS: dict[str, str] = {
    "nba":   "https://api.sportradar.us/nba/trial/v8/en/games/{y}/{m:02d}/{d:02d}/schedule.json",
    "nhl":   "https://api.sportradar.us/nhl/trial/v7/en/games/{y}/{m:02d}/{d:02d}/schedule.json",
    "mlb":   "https://api.sportradar.us/mlb/trial/v7/en/games/{y}/{m:02d}/{d:02d}/schedule.json",
    "ncaab": "https://api.sportradar.us/ncaamb/trial/v8/en/games/{y}/{m:02d}/{d:02d}/schedule.json",
    # NFL has no daily endpoint — current_week covers any in-progress game.
    "nfl":   "https://api.sportradar.us/nfl/official/trial/v7/en/games/current_week/schedule.json",
    "ncaaf": "https://api.sportradar.us/ncaafb/trial/v1/en/games/{y}/{m:02d}/{d:02d}/schedule.json",
}
# Soccer live state: hit the per-competition daily schedule (already
# scoped by ``tournament=`` query param) and parse the soccer-extended
# {"schedules": [{"sport_event": {...}, "sport_event_status": {...}}]}
# envelope.  Sportradar updates `sport_event_status.match_status` /
# `clock` in near-real time on this endpoint, so a single 60s poll per
# league mirrors what the NA leagues get out of their schedule call.
for _code, _tid in _SOCCER_TOURNAMENTS.items():
    _DAILY_SCHEDULE_PATHS[_code] = _SOCCER_DAILY_SCHEDULE_TPL.replace("{tid}", _tid)

_LIVE_STATUSES = {"inprogress", "halftime", "delayed", "scheduled.live"}
# Soccer-extended uses different status / match_status enums; treat any
# of these as "live enough to persist".
_SOCCER_LIVE_STATUSES = {
    "live", "started", "1st_half", "2nd_half", "halftime", "pause",
    "extra_time", "penalties", "interrupted", "suspended", "delayed",
    "awaiting_extra_time", "awaiting_penalties",
}


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


def _extract_soccer_live_rows(
    league: str, payload: dict[str, Any],
) -> list[tuple]:
    """Walk a soccer-extended schedule payload and produce live-state
    rows.  Soccer-extended wraps each match as
    ``{"sport_event": {...}, "sport_event_status": {...}}`` under either
    ``schedules`` (per-day endpoint) or ``sport_events`` (live endpoint).
    """
    import json as _json
    entries = (
        payload.get("schedules")
        or payload.get("sport_events")
        or []
    )
    rows: list[tuple] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        ev = entry.get("sport_event") or entry
        if not isinstance(ev, dict):
            continue
        st = entry.get("sport_event_status") or {}
        status = str(st.get("status") or "").lower()
        match_status = str(st.get("match_status") or "").lower()
        # Persist any match that's currently live or finished today
        # (so post-match scores show up in operator views) — matches
        # whose status is just "not_started" / "scheduled" are skipped
        # to keep the table small.
        is_live = (
            status in _SOCCER_LIVE_STATUSES
            or match_status in _SOCCER_LIVE_STATUSES
            or status in {"closed", "ended", "complete", "finished"}
        )
        if not is_live:
            continue
        gid = str(ev.get("id") or "")
        if not gid:
            continue
        home_score = st.get("home_score")
        away_score = st.get("away_score")
        period = str(match_status or st.get("period") or "")
        clock = str(st.get("clock") or st.get("match_time") or "")
        scheduled = _parse_ts(ev.get("start_time") or ev.get("scheduled"))
        merged = {**ev, "sport_event_status": st}
        rows.append((
            league,
            gid,
            status or match_status or "live",
            int(home_score) if home_score is not None else None,
            int(away_score) if away_score is not None else None,
            period,
            clock,
            scheduled,
            _json.dumps(merged, default=str),
        ))
    return rows


async def _poll_live_state(sport: str) -> int:
    api_key = _api_key_for(sport)
    if not api_key:
        raise RuntimeError(f"SPORTRADAR_{sport.upper()}_KEY not set")
    url = _today_url(sport)
    payload = await _get_json(f"sportradar:{sport}:live", url, api_key)
    if not payload:
        return 0

    league = _SPORTRADAR_LEAGUE[sport]

    # Soccer-extended payloads are shaped completely differently from
    # the NA-league daily schedule (sport_event + sport_event_status),
    # so route them through the soccer-aware row builder.
    if sport in _SOCCER_SET:
        rows = _extract_soccer_live_rows(league, payload)
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
            log.warning("sportradar %s soccer live-state upsert failed: %s", sport, e)
            raise
        return len(rows)

    games = _extract_games(payload)
    if not games:
        return 0

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
# Tournament leaderboards (task #449)
# ---------------------------------------------------------------------------
#
# Sportradar exposes per-event leaderboards for golf and motorsport that
# stream while the event is live.  We poll those endpoints for any PGA
# tournament / F1 race weekend whose ``sportradar_schedules.status`` is
# in-progress, parse the ordered field, and overwrite the per-event
# entry in ``app.sports.ratings`` so the orchestrator's Plackett-Luce
# branch prices off live position instead of pre-event seed.  The
# live-action blackout (``app.sports.leagues.league_blackout_until``)
# still gates *new* trades on these leagues — refreshed ratings only
# affect monitoring / pricing.


_PGA_LEADERBOARD_TPL = (
    "https://api.sportradar.us/golf/trial/v3/en/tournaments/"
    "{tournament_id}/leaderboard.json"
)
_F1_RACE_SUMMARY_TPL = (
    "https://api.sportradar.us/formula1/trial/v2/en/sport_events/"
    "{event_id}/summary.json"
)

# Statuses on `sportradar_schedules` that should drive a leaderboard
# refresh.  Sportradar's golf v3 uses "inprogress" while it's playing
# and "closed" when the round wraps; F1 v2 uses similar enums.  The
# poller treats all live-ish enums as eligible so a multi-day PGA
# tournament gets its leaderboard updated every poll cycle.
_LEADERBOARD_LIVE_STATUSES = frozenset({
    "inprogress", "in_progress", "live", "started",
    "delayed", "halftime", "interrupted",
})


def _extract_pga_leaderboard(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull an ordered ``[{name, position}]`` list out of a Sportradar
    PGA leaderboard payload.

    Golf v3 leaderboard shape is roughly::

        {"tournament": {...},
         "leaderboard": [
            {"position": 1, "tied": false,
             "player": {"id": "sr:player:1", "first_name": "Scottie",
                        "last_name": "Scheffler"}},
            ...]}

    We tolerate both ``player.first_name`` + ``player.last_name`` and a
    pre-formatted ``player.full_name`` / ``player.name`` so a future
    schema tweak doesn't silently regress to seed-based ratings.
    Players who have withdrawn / been cut (``status`` not in the
    "active" set) are dropped so they don't dilute the field.
    """
    if not isinstance(payload, dict):
        return []
    rows = payload.get("leaderboard") or payload.get("players") or []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "").lower()
        if status in {"withdrew", "wd", "cut", "disqualified", "dq"}:
            continue
        player = row.get("player") if isinstance(row.get("player"), dict) else row
        name = (
            player.get("full_name")
            or player.get("name")
            or " ".join(
                part for part in (
                    player.get("first_name"),
                    player.get("last_name"),
                ) if part
            ).strip()
        )
        if not name:
            continue
        position = row.get("position") or player.get("position")
        out.append({"name": str(name), "position": position})
    return out


def _extract_f1_leaderboard(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull an ordered ``[{name, position}]`` list out of a Sportradar
    F1 race summary payload.

    F1 v2 summary shape during a live race is roughly::

        {"sport_event": {...},
         "sport_event_status": {"status": "live", "competitors": [
            {"id": "sr:competitor:1", "name": "Verstappen",
             "position": 1, "result": {...}},
            ...]}}

    We accept ``competitors`` either at the top level or nested under
    ``sport_event_status`` (race) / ``stage`` (race weekend rollup) so
    every public summary shape works without bespoke routing.
    """
    if not isinstance(payload, dict):
        return []
    candidates: list[Any] = []
    status = payload.get("sport_event_status") or {}
    if isinstance(status, dict):
        candidates.append(status.get("competitors"))
        candidates.append(status.get("results"))
    stage = payload.get("stage") or {}
    if isinstance(stage, dict):
        candidates.append(stage.get("competitors"))
    candidates.append(payload.get("competitors"))
    candidates.append(payload.get("results"))

    rows: list[dict[str, Any]] = []
    for raw in candidates:
        if isinstance(raw, list) and raw:
            rows = raw
            break
    out: list[dict[str, Any]] = []
    for entry in rows:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or (entry.get("competitor") or {}).get("name")
        if not name:
            continue
        position = (
            entry.get("position")
            or (entry.get("result") or {}).get("position")
            or entry.get("rank")
        )
        out.append({"name": str(name), "position": position})
    return out


async def _live_leaderboard_event_ids(
    league: str, statuses: frozenset[str] = _LEADERBOARD_LIVE_STATUSES,
) -> list[str]:
    """Pull live PGA/F1 event IDs from ``sportradar_schedules``.

    Returns ``[]`` when the table isn't populated yet (fresh install)
    or when no events are live — best-effort, never raises.
    """
    try:
        async with db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT game_id
                  FROM sportradar_schedules
                 WHERE league = $1
                   AND lower(status) = ANY($2::text[])
                 ORDER BY scheduled DESC
                """,
                league,
                list(statuses),
            )
    except Exception as e:
        log.info(
            "sportradar leaderboard %s: schedules lookup skipped (%s)",
            league, e,
        )
        return []
    return [str(r["game_id"]) for r in rows if r["game_id"]]


async def _persist_leaderboard_snapshot(
    event_id: str,
    entries: list[dict[str, Any]],
    fetched_at: datetime | None = None,
) -> None:
    """Append one snapshot of a tournament leaderboard to
    ``sportradar_tournament_leaderboards`` (task #623).

    All rows from a single fetch share an identical ``fetched_at``
    timestamp so callers can recover a coherent snapshot via
    ``WHERE event_id = $1 AND fetched_at = MAX(fetched_at)``.

    Best-effort: persistence failures are logged and swallowed so a
    transient DB hiccup never poisons the in-memory ratings cache
    refresh that the orchestrator's pricing depends on.  The caller
    has already extracted ``entries`` from a live HTTP payload —
    losing the snapshot is strictly less bad than losing the live
    rating refresh.
    """
    if not event_id or not entries:
        return
    ts = fetched_at or datetime.now(tz=timezone.utc)
    rows: list[tuple[str, int | None, str, datetime]] = []
    for e in entries:
        name = e.get("name")
        if not name:
            continue
        raw_pos = e.get("position")
        pos: int | None
        if raw_pos is None:
            pos = None
        else:
            try:
                pos = int(float(raw_pos))
            except (TypeError, ValueError):
                pos = None
        rows.append((str(event_id), pos, str(name), ts))
    if not rows:
        return
    try:
        async with db.acquire() as conn, conn.transaction():
            await conn.executemany(
                """
                INSERT INTO sportradar_tournament_leaderboards
                  (event_id, position, name, fetched_at)
                VALUES ($1, $2, $3, $4)
                """,
                rows,
            )
    except Exception as e:  # noqa: BLE001 — never block the rating refresh
        log.warning(
            "sportradar leaderboard %s snapshot persist failed: %s",
            event_id, e,
        )


async def latest_tournament_leaderboard(
    event_id: str,
) -> list[dict[str, Any]]:
    """Return the most recent leaderboard snapshot for ``event_id`` so
    dashboards / replays can reconstruct the field at the time we
    last polled (task #623).

    Each row carries ``{event_id, position, name, fetched_at}`` and the
    list is ordered by position (NULLs last so unranked / withdrawn
    entries don't crowd the leader).  Returns ``[]`` when no snapshot
    has been recorded for the event yet — callers should treat this as
    "fall back to the seed-time ratings" rather than as an error.
    """
    if not event_id:
        return []
    try:
        async with db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT event_id, position, name, fetched_at
                  FROM sportradar_tournament_leaderboards
                 WHERE event_id = $1
                   AND fetched_at = (
                       SELECT MAX(fetched_at)
                         FROM sportradar_tournament_leaderboards
                        WHERE event_id = $1
                   )
                 ORDER BY position NULLS LAST, name
                """,
                str(event_id),
            )
    except Exception as e:  # noqa: BLE001
        log.info(
            "sportradar leaderboard %s history lookup skipped (%s)",
            event_id, e,
        )
        return []
    return [
        {
            "event_id": r["event_id"],
            "position": r["position"],
            "name": r["name"],
            "fetched_at": r["fetched_at"],
        }
        for r in rows
    ]


async def _refresh_pga_leaderboard(api_key: str, tournament_id: str) -> int:
    """Fetch one PGA tournament's leaderboard and refresh the per-event
    ratings cache.  Returns the entrant count written (0 on failure).

    Each successful fetch also appends a snapshot to
    ``sportradar_tournament_leaderboards`` (task #623) so historical
    pricing can be replayed.
    """
    from ..sports import set_field_from_leaderboard
    payload = await _get_json(
        f"sportradar:pga:leaderboard:{tournament_id}",
        _PGA_LEADERBOARD_TPL.format(tournament_id=tournament_id),
        api_key,
    )
    if not payload:
        return 0
    entries = _extract_pga_leaderboard(payload)
    if len(entries) < 2:
        return 0
    written = set_field_from_leaderboard(tournament_id, entries)
    if written:
        await _persist_leaderboard_snapshot(tournament_id, entries)
    return written


async def _refresh_f1_leaderboard(api_key: str, event_id: str) -> int:
    """Fetch one F1 race summary and refresh the per-event ratings
    cache.  Returns the entrant count written (0 on failure).

    Each successful fetch also appends a snapshot to
    ``sportradar_tournament_leaderboards`` (task #623) so historical
    pricing can be replayed.
    """
    from ..sports import set_field_from_leaderboard
    payload = await _get_json(
        f"sportradar:f1:leaderboard:{event_id}",
        _F1_RACE_SUMMARY_TPL.format(event_id=event_id),
        api_key,
    )
    if not payload:
        return 0
    entries = _extract_f1_leaderboard(payload)
    if len(entries) < 2:
        return 0
    written = set_field_from_leaderboard(event_id, entries)
    if written:
        await _persist_leaderboard_snapshot(event_id, entries)
    return written


async def _poll_tournament_leaderboards() -> int:
    """Refresh ratings for every in-progress PGA tournament + F1 race
    weekend (task #449).

    Returns the total number of entrants whose live rating got
    refreshed across all events — exposed via ``Feed.poll`` so the
    operator console reports a real number per cycle (instead of a
    binary success/fail).  Per-event failures are isolated so one
    flaky tournament doesn't blank out the rest.
    """
    pga_key = _api_key_for("pga")
    f1_key = _api_key_for("f1")
    if not pga_key and not f1_key:
        raise RuntimeError(
            "neither SPORTRADAR_PGA_KEY nor SPORTRADAR_F1_KEY is set"
        )

    refreshed = 0

    if pga_key:
        for tid in await _live_leaderboard_event_ids("PGA"):
            try:
                refreshed += await _refresh_pga_leaderboard(pga_key, tid)
            except RateLimited:
                # Re-raise so the scheduler records the cooldown — we
                # can't keep hitting Sportradar after a 429.
                raise
            except Exception as e:  # noqa: BLE001 — don't let one event poison the rest
                log.warning(
                    "sportradar pga leaderboard %s refresh failed: %s", tid, e,
                )

    if f1_key:
        for eid in await _live_leaderboard_event_ids("F1"):
            try:
                refreshed += await _refresh_f1_leaderboard(f1_key, eid)
            except RateLimited:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "sportradar f1 leaderboard %s refresh failed: %s", eid, e,
                )

    return refreshed


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
# Leaderboards drive intra-event pricing for PGA / F1.  120s by default
# matches Sportradar's published live-data refresh cadence and keeps us
# well inside the trial-tier quota even with a multi-day tournament.
LEADERBOARD_CADENCE_SECONDS = max(
    30, int(os.environ.get("AXIOM_SPORTRADAR_LEADERBOARD_CADENCE") or "120")
)


def register_sportradar_extra_feeds() -> None:
    """Register injuries + rosters + live-state pollers for every Sportradar
    sport.

    Idempotent: if any feed name is already in the registry we skip — this
    lets the function be called from both lifespan and tests without
    raising "already registered".  Each sport that ships with rosters /
    injuries / live-state path templates contributes 3 feeds.  As of
    task #211 all 7 soccer competitions added under task #196 (EPL,
    LALIGA, SERIEA, BUNDES, LIGUE1, MLS, UCL) have their soccer-extended
    endpoints wired and register as enabled when an API key is present.
    Sports without path templates yet (combat / individual sports added
    under task #197) still register *disabled* with a clear
    `skipped_reason`.  Schedules themselves are registered alongside The
    Odds API in `odds_api.register_odds_feeds`.
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

    # Cross-sport tournament leaderboard refresher (task #449).  One
    # poller covers both PGA tournaments and F1 race weekends so the
    # operator console gets a single status row + cadence per cycle
    # instead of two near-identical entries.
    leaderboard_name = "sportradar_tournament_leaderboards"
    if registry.status_of(leaderboard_name) is None:
        pga_key = _api_key_for("pga")
        f1_key = _api_key_for("f1")
        if pga_key or f1_key:
            leaderboard_skipped: str | None = None
        else:
            leaderboard_skipped = (
                "neither SPORTRADAR_PGA_KEY nor SPORTRADAR_F1_KEY is set"
            )
        registry.register(Feed(
            name=leaderboard_name,
            cadence_seconds=float(LEADERBOARD_CADENCE_SECONDS),
            poll=_poll_tournament_leaderboards,
            enabled=bool(pga_key or f1_key),
            skipped_reason=leaderboard_skipped,
        ))
