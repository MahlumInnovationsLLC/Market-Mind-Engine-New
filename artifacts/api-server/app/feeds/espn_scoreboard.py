"""ESPN public-scoreboard schedule poller.

Sportradar's trial expires and the existing ``sportradar_schedules``
table stops getting refreshed.  Without a backstop, the chatbot's
"Today's Games" lookahead and the per-game pre-game context bundle
both empty out as the cached window rolls past.

This module fills that gap by polling ESPN's free public scoreboard
JSON for the four major US leagues (NFL / NBA / MLB / NHL).  No API
key is required — these endpoints have served the espn.com site for
years.  The poller upserts each event into the *same*
``sportradar_schedules`` table the Sportradar pipeline writes to, so
no downstream code (``dashboard.get_upcoming_games``,
``dashboard.get_upcoming_game_context``, ``/api/upcoming-games``) needs
to change.

Game-ids are namespaced ``espn:<league>:<event_id>`` so they can never
collide with native Sportradar ids and so a future settler can tell
which provider wrote a row.

The module sits behind an env flag (``AXIOM_ESPN_SCOREBOARD_DISABLED=1``
opts out) and registers itself as ``enabled=False`` with a clear
``skipped_reason`` when so configured — matching how the other feeds
advertise themselves on the operator console.
"""
from __future__ import annotations

import json as _json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Iterable

import httpx

from .. import db
from .registry import Feed, registry
from .scheduler import RateLimited


log = logging.getLogger("axiom.feeds.espn_scoreboard")


# ---------------------------------------------------------------------------
# Provider config
# ---------------------------------------------------------------------------

# The ESPN site API is free, key-less, and stable.  Each entry maps a
# canonical ``sportradar_schedules.league`` value to the URL path
# segment ESPN uses for that sport.  Adding a new league here is a one
# line change because the JSON shape is identical across sports.
_ESPN_LEAGUES: dict[str, tuple[str, str]] = {
    # league_label : (sport_path, league_path)
    "NFL": ("football", "nfl"),
    "NBA": ("basketball", "nba"),
    "MLB": ("baseball",  "mlb"),
    "NHL": ("hockey",    "nhl"),
}

_BASE_URL = "https://site.api.espn.com/apis/site/v2/sports"
_DEFAULT_CADENCE_SECS: float = 600.0  # 10 minutes; scoreboards barely change pre-game.
_HTTP_TIMEOUT_SECS: float = 8.0


def _scoreboard_url(league_label: str) -> str | None:
    pair = _ESPN_LEAGUES.get(league_label.upper())
    if pair is None:
        return None
    sport, league = pair
    return f"{_BASE_URL}/{sport}/{league}/scoreboard"


def _is_enabled() -> bool:
    """Allow operators to disable the ESPN backstop via env flag."""
    raw = (os.environ.get("AXIOM_ESPN_SCOREBOARD_DISABLED") or "").strip().lower()
    return raw not in {"1", "true", "yes", "on"}


def _cadence_seconds() -> float:
    raw = os.environ.get("AXIOM_ESPN_SCOREBOARD_CADENCE_SECS")
    try:
        v = float(raw) if raw is not None else _DEFAULT_CADENCE_SECS
    except (TypeError, ValueError):
        return _DEFAULT_CADENCE_SECS
    # Floor at 60s so a misconfigured "0" can't melt the upstream.
    return max(60.0, v)


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def _parse_iso8601(raw: Any) -> datetime | None:
    """Coerce ESPN's ISO timestamp ("...Z") into an aware UTC datetime."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if not isinstance(raw, str) or not raw.strip():
        return None
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _competitor_team_name(c: dict[str, Any]) -> str | None:
    """ESPN nests the team display name under ``team.displayName``.
    Fall back to short name or location to avoid empty strings."""
    team = c.get("team") if isinstance(c, dict) else None
    if not isinstance(team, dict):
        return None
    for k in ("displayName", "shortDisplayName", "name", "location"):
        v = team.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _competition_status(comp: dict[str, Any], event: dict[str, Any]) -> str | None:
    """Pull ESPN's coarse status enum (``pre`` / ``in`` / ``post``)
    from the competition status, then the event status as a fallback."""
    for src in (comp, event):
        s = src.get("status") if isinstance(src, dict) else None
        if isinstance(s, dict):
            t = s.get("type")
            if isinstance(t, dict):
                v = t.get("state") or t.get("name")
                if isinstance(v, str) and v.strip():
                    return v.strip().lower()
    return None


def _venue_name(comp: dict[str, Any]) -> str | None:
    v = comp.get("venue") if isinstance(comp, dict) else None
    if not isinstance(v, dict):
        return None
    name = v.get("fullName") or v.get("name")
    return name.strip() if isinstance(name, str) and name.strip() else None


def _row_from_event(league: str, event: dict[str, Any]) -> tuple | None:
    """Convert one ESPN event into a ``sportradar_schedules`` row tuple.

    Returns ``None`` when the event is unparseable (missing id, both
    teams, or scheduled time) so a single bad row can't poison the
    batch upsert.
    """
    if not isinstance(event, dict):
        return None
    raw_id = event.get("id") or event.get("uid")
    if not raw_id:
        return None
    eid = str(raw_id).strip()
    if not eid:
        return None
    game_id = f"espn:{league.lower()}:{eid}"

    comps = event.get("competitions") or []
    comp = comps[0] if isinstance(comps, list) and comps else {}
    competitors = comp.get("competitors") if isinstance(comp, dict) else None
    if not isinstance(competitors, list):
        competitors = []

    home_team: str | None = None
    away_team: str | None = None
    for c in competitors:
        if not isinstance(c, dict):
            continue
        side = (c.get("homeAway") or "").strip().lower()
        name = _competitor_team_name(c)
        if not name:
            continue
        if side == "home":
            home_team = name
        elif side == "away":
            away_team = name
    # Fall back to ``shortName`` ("PHI @ BUF") if homeAway flags are
    # missing — better to surface a row with a degraded team than to
    # skip it entirely.
    if (not home_team or not away_team) and isinstance(event.get("shortName"), str):
        parts = [p.strip() for p in event["shortName"].replace(" vs ", " @ ").split(" @ ")]
        if len(parts) == 2:
            away_team = away_team or parts[0]
            home_team = home_team or parts[1]
    if not home_team or not away_team:
        return None

    scheduled = _parse_iso8601(event.get("date") or comp.get("date"))
    if scheduled is None:
        return None

    status = _competition_status(comp, event)
    venue = _venue_name(comp)
    payload = {
        "source": "espn",
        "espn_event_id": eid,
        "league": league,
        "name": event.get("name"),
        "short_name": event.get("shortName"),
        "status_state": status,
    }
    return (
        league,
        game_id,
        home_team,
        away_team,
        scheduled,
        status,
        venue,
        _json.dumps(payload, default=str),
    )


def _rows_from_scoreboard(league: str, body: dict[str, Any]) -> list[tuple]:
    events = body.get("events") if isinstance(body, dict) else None
    if not isinstance(events, list):
        return []
    out: list[tuple] = []
    for ev in events:
        row = _row_from_event(league, ev)
        if row is not None:
            out.append(row)
    return out


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

async def _upsert_rows(rows: Iterable[tuple]) -> int:
    """Upsert rows into ``sportradar_schedules``.

    Mirrors the existing Sportradar writer's ON CONFLICT clause
    verbatim so a Sportradar row and an ESPN row for the same matchup
    coexist (they have different ``game_id`` namespaces) without
    fighting over the primary key.
    """
    rows = list(rows)
    if not rows:
        return 0
    # Let DB failures propagate: the scheduler catches and records them
    # in FeedStatus so a broken writer can't impersonate a healthy feed.
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
    return len(rows)


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------

async def _fetch_scoreboard(client: httpx.AsyncClient, league: str) -> dict[str, Any] | None:
    """Fetch one league's scoreboard.

    Returns a parsed JSON dict, ``None`` for a recoverable failure
    (network error, non-JSON body, etc.) so the rest of the leagues
    keep polling, and **raises** :class:`RateLimited` on a 429 so the
    scheduler enforces a real cooldown.
    """
    url = _scoreboard_url(league)
    if url is None:
        return None
    try:
        resp = await client.get(url)
        if resp.status_code == 429:
            retry_after_raw = resp.headers.get("Retry-After")
            try:
                retry_after = float(retry_after_raw) if retry_after_raw else 60.0
            except (TypeError, ValueError):
                retry_after = 60.0
            raise RateLimited("espn_scoreboard", retry_after=max(30.0, retry_after))
        resp.raise_for_status()
    except RateLimited:
        raise
    except httpx.HTTPError as e:
        log.info("espn scoreboard fetch failed for %s: %s", league, e)
        return None
    try:
        body = resp.json()
    except ValueError as e:
        log.info("espn scoreboard JSON decode failed for %s: %s", league, e)
        return None
    return body if isinstance(body, dict) else None


async def poll_espn_scoreboard() -> int:
    """One poll of every supported ESPN scoreboard.

    Returns the total number of ``sportradar_schedules`` rows touched.
    Per-league failures are isolated — an ESPN outage in NHL never
    blocks NFL.  Always closes the HTTP client so the asyncio loop
    doesn't leak file descriptors on long-running operator sessions.
    """
    if not _is_enabled():
        return 0
    timeout = httpx.Timeout(_HTTP_TIMEOUT_SECS, connect=3.0)
    leagues_seen: list[str] = []
    leagues_with_rows: list[str] = []
    total = 0
    async with httpx.AsyncClient(timeout=timeout, headers={
        "User-Agent": "axiom-predict/1.0 (+schedule-backstop)",
        "Accept": "application/json",
    }) as client:
        for league in _ESPN_LEAGUES:
            leagues_seen.append(league)
            body = await _fetch_scoreboard(client, league)
            if body is None:
                continue
            rows = _rows_from_scoreboard(league, body)
            if not rows:
                continue
            written = await _upsert_rows(rows)
            if written:
                leagues_with_rows.append(league)
                total += written
    # Stash a per-league summary on the FeedStatus metadata so the
    # operator console can show which leagues actually got a fresh
    # batch on the last tick.  Mirrors the metadata pattern used by
    # the soccer Sportradar pollers.
    registry.set_metadata(
        "espn_scoreboard",
        leagues_polled=leagues_seen,
        leagues_with_rows=leagues_with_rows,
        last_total_rows=total,
    )
    return total


def register_espn_scoreboard_feed() -> None:
    """Register the ESPN scoreboard backstop with the global feed
    registry.

    Idempotent on hot reload; advertises a clear ``skipped_reason``
    when the operator has explicitly disabled the feed via env flag so
    the operator console renders the row as ``enabled=false`` rather
    than silently dropping the registration.
    """
    name = "espn_scoreboard"
    if registry.status_of(name):
        return
    enabled = _is_enabled()
    skipped = (
        None if enabled
        else "AXIOM_ESPN_SCOREBOARD_DISABLED set"
    )
    registry.register(Feed(
        name=name,
        cadence_seconds=_cadence_seconds(),
        poll=poll_espn_scoreboard,
        enabled=enabled,
        skipped_reason=skipped,
    ))
    log.info(
        "espn scoreboard feed registered (enabled=%s, cadence=%.1fs)",
        enabled, _cadence_seconds(),
    )
