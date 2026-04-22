"""The Odds API + Sportradar pollers.

The Odds API: a public aggregator across many sportsbooks (key required,
free tier exists).  We poll a small set of in-season leagues and persist
per-book per-market quotes to `odds_changes` (lightweight per-tick log)
and a snapshot row to `line_snapshots` (richer schema used for backtests).

Sportradar: per-sport feeds (NBA/NHL/MLB/NCAAB).  Each requires its own
key.  This file registers placeholder pollers that skip cleanly when the
key for a given sport isn't configured — they still appear in
`/api/feeds/status` so the operator sees the missing-key reason.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from .. import db
from .registry import Feed, registry
from .scheduler import RateLimited


log = logging.getLogger("axiom.feeds.odds")


THE_ODDS_API_BASE = "https://api.the-odds-api.com/v4"
# In-season default — operator can override via env.
DEFAULT_ODDS_SPORTS = os.environ.get(
    "AXIOM_ODDS_SPORTS",
    "basketball_nba,baseball_mlb,icehockey_nhl,basketball_ncaab,americanfootball_nfl",
).split(",")
DEFAULT_ODDS_REGIONS = os.environ.get("AXIOM_ODDS_REGIONS", "us")
DEFAULT_ODDS_MARKETS = os.environ.get("AXIOM_ODDS_MARKETS", "h2h,spreads,totals")


async def _poll_the_odds_api() -> int:
    api_key = os.environ.get("THE_ODDS_API_KEY") or os.environ.get("ODDS_API_KEY")
    if not api_key:
        # Defensive guard — registration should already have skipped us.
        raise RuntimeError("THE_ODDS_API_KEY not set")

    written = 0
    timeout = httpx.Timeout(10.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for sport in [s.strip() for s in DEFAULT_ODDS_SPORTS if s.strip()]:
            url = f"{THE_ODDS_API_BASE}/sports/{sport}/odds"
            params = {
                "apiKey": api_key,
                "regions": DEFAULT_ODDS_REGIONS,
                "markets": DEFAULT_ODDS_MARKETS,
                "oddsFormat": "decimal",
            }
            try:
                r = await client.get(url, params=params)
            except httpx.HTTPError as e:
                log.warning("odds api fetch failed for %s: %s", sport, e)
                continue
            # Surface rate-limit + auth errors loudly without failing the whole sweep.
            if r.status_code == 401:
                raise RuntimeError("the-odds-api auth rejected — bad key")
            if r.status_code == 429:
                # Surface to the scheduler so it can apply a backoff window.
                # The Odds API uses the standard `Retry-After` header.
                retry = _retry_after_seconds(r) or 60.0
                raise RateLimited("the-odds-api", retry_after=retry)
            if r.status_code >= 400:
                log.warning("the-odds-api %s returned %s: %s", sport, r.status_code, r.text[:200])
                continue
            try:
                games: list[dict[str, Any]] = r.json()
            except Exception:
                continue

            written += await _persist_odds_payload(sport, games)
    return written


async def _persist_odds_payload(sport: str, games: list[dict[str, Any]]) -> int:
    """Write per-book per-market quotes into odds_changes + line_snapshots."""
    if not games:
        return 0
    snapshot_rows: list[tuple] = []
    change_rows: list[tuple] = []
    from datetime import datetime, timezone
    now_iso = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")

    for g in games:
        gid = str(g.get("id") or "")
        home = g.get("home_team")
        away = g.get("away_team")
        for bk in g.get("bookmakers", []) or []:
            book = bk.get("key") or bk.get("title") or "unknown"
            for mk in bk.get("markets", []) or []:
                mtype = mk.get("key") or "unknown"
                for outcome in mk.get("outcomes", []) or []:
                    line = outcome.get("point")
                    price = outcome.get("price")
                    snapshot_rows.append((
                        gid, sport, book, mtype,
                        float(line) if line is not None else None,
                        float(price) if price is not None else None,
                        home, away, False,
                    ))
                    change_rows.append((
                        gid, book, mtype,
                        str(line) if line is not None else None,
                        float(price) if price is not None else None,
                        now_iso,
                    ))

    if not change_rows:
        return 0

    written = 0
    try:
        async with db.acquire() as conn, conn.transaction():
            await conn.executemany(
                """
                INSERT INTO line_snapshots
                  (game_id, sport, book, market_type, line_value, odds_value,
                   home_team, away_team, is_opening, timestamp)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW())
                """,
                snapshot_rows,
            )
            await conn.executemany(
                """
                INSERT INTO odds_changes
                  (game_id, book, market_type, line, odds, timestamp)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                change_rows,
            )
            written = len(change_rows)
    except Exception as e:
        log.warning("persist odds failed for sport=%s: %s", sport, e)
    return written


_SPORTRADAR_SCHEDULE_PATHS = {
    # Per-sport schedule endpoint templates (year/month/day are filled in at call time).
    "nba":   "https://api.sportradar.us/nba/trial/v8/en/games/{y}/{m:02d}/{d:02d}/schedule.json",
    "nhl":   "https://api.sportradar.us/nhl/trial/v7/en/games/{y}/{m:02d}/{d:02d}/schedule.json",
    "mlb":   "https://api.sportradar.us/mlb/trial/v7/en/games/{y}/{m:02d}/{d:02d}/schedule.json",
    "ncaab": "https://api.sportradar.us/ncaamb/trial/v8/en/games/{y}/{m:02d}/{d:02d}/schedule.json",
    # NFL has no daily-schedule endpoint; current-week is the closest equivalent.
    "nfl":    "https://api.sportradar.us/nfl/official/trial/v7/en/games/current_week/schedule.json",
    "ncaaf":  "https://api.sportradar.us/ncaafb/trial/v1/en/games/{y}/{m:02d}/{d:02d}/schedule.json",
}
_SPORTRADAR_LEAGUE = {
    "nba": "NBA",
    "nhl": "NHL",
    "mlb": "MLB",
    "ncaab": "NCAAB",
    "nfl": "NFL",
    "ncaaf": "NCAAF",
}
SPORTRADAR_SPORTS: tuple[str, ...] = tuple(_SPORTRADAR_SCHEDULE_PATHS.keys())


async def _poll_sportradar(sport: str) -> int:
    """Fetch today's schedule from Sportradar and upsert into `games`."""
    key_var = f"SPORTRADAR_{sport.upper()}_KEY"
    api_key = os.environ.get(key_var) or os.environ.get("SPORTRADAR_API_KEY")
    if not api_key:
        raise RuntimeError(f"{key_var} not set")
    template = _SPORTRADAR_SCHEDULE_PATHS.get(sport)
    if not template:
        raise RuntimeError(f"unknown sportradar sport: {sport}")

    from datetime import datetime, timezone
    today = datetime.now(tz=timezone.utc)
    url = template.format(y=today.year, m=today.month, d=today.day)

    timeout = httpx.Timeout(10.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(url, params={"api_key": api_key})
        if r.status_code in (401, 403):
            raise RuntimeError(f"sportradar {sport} auth rejected")
        if r.status_code == 429:
            raise RateLimited(f"sportradar:{sport}", retry_after=_retry_after_seconds(r) or 60.0)
        if r.status_code >= 400:
            raise RuntimeError(f"sportradar {sport} HTTP {r.status_code}")
        try:
            payload = r.json() or {}
        except Exception:
            return 0

    # Most leagues return either {"games": [...]} or {"league": {"games": [...]}};
    # NFL's current_week endpoint nests them under {"week": {"games": [...]}}.
    games = (
        payload.get("games")
        or (payload.get("league") or {}).get("games")
        or (payload.get("week") or {}).get("games")
        or []
    )
    if not games:
        return 0

    league = _SPORTRADAR_LEAGUE[sport]
    rows: list[tuple] = []
    from datetime import datetime as _dt, timezone as _tz
    now_iso = _dt.now(tz=_tz.utc).isoformat(timespec="seconds")
    for g in games:
        gid = str(g.get("id") or "")
        if not gid:
            continue
        home = (g.get("home") or {}).get("name") or g.get("home_team") or ""
        away = (g.get("away") or {}).get("name") or g.get("away_team") or ""
        scheduled = str(g.get("scheduled") or "")
        status = str(g.get("status") or "scheduled")
        home_score = (g.get("home_points") if g.get("home_points") is not None
                      else (g.get("home") or {}).get("runs"))
        away_score = (g.get("away_points") if g.get("away_points") is not None
                      else (g.get("away") or {}).get("runs"))
        rows.append((
            gid, sport, league, home, away, scheduled, status,
            int(home_score) if home_score is not None else None,
            int(away_score) if away_score is not None else None,
            now_iso, now_iso,
        ))

    if not rows:
        return 0
    try:
        async with db.acquire() as conn, conn.transaction():
            await conn.executemany(
                """
                INSERT INTO games
                  (game_id, sport, league, home_team, away_team, scheduled_start,
                   status, home_score, away_score, last_updated, inserted_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                ON CONFLICT (game_id) DO UPDATE SET
                  status = EXCLUDED.status,
                  home_score = EXCLUDED.home_score,
                  away_score = EXCLUDED.away_score,
                  last_updated = EXCLUDED.last_updated
                """,
                rows,
            )
    except Exception as e:
        # If the table doesn't have a unique index on game_id the upsert will
        # raise.  Re-raise so the scheduler records a real failure and the
        # health page surfaces the error rather than reporting "0 rows OK".
        log.warning("sportradar %s upsert failed: %s", sport, e)
        raise
    # Also mirror to the dedicated sportradar_schedules cache so analytics
    # queries can hit a clean per-source table.  Failures here are logged
    # but never poison the primary `games` write — the scheduler still
    # treats the poll as a success because the canonical row landed.
    try:
        from .sportradar import cache_schedule_rows
        await cache_schedule_rows(league, games)
    except Exception as e:
        log.warning("sportradar %s schedule-cache mirror failed: %s", sport, e)
    return len(rows)


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    """Best-effort parse of the standard `Retry-After` header (seconds or HTTP-date)."""
    raw = resp.headers.get("retry-after")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        from email.utils import parsedate_to_datetime
        from datetime import datetime, timezone
        try:
            dt = parsedate_to_datetime(raw)
            return max(1.0, (dt - datetime.now(tz=timezone.utc)).total_seconds())
        except Exception:
            return None


def register_odds_feeds() -> None:
    """Register the odds + sportradar feeds (skips cleanly when keys missing)."""
    if registry.status_of("the_odds_api"):
        return  # already registered

    odds_key = os.environ.get("THE_ODDS_API_KEY") or os.environ.get("ODDS_API_KEY")
    registry.register(Feed(
        name="the_odds_api",
        cadence_seconds=30.0,
        poll=_poll_the_odds_api,
        enabled=bool(odds_key),
        skipped_reason=None if odds_key else "THE_ODDS_API_KEY not set",
    ))

    # Schedules change slowly (kickoff times, lineups) — default to 4h to be
    # gentle on the trial-tier quota.  Operators can override per-deployment.
    schedule_cadence = max(
        60, int(os.environ.get("AXIOM_SPORTRADAR_SCHEDULE_CADENCE") or str(60 * 60 * 4))
    )
    for sport in SPORTRADAR_SPORTS:
        key_var = f"SPORTRADAR_{sport.upper()}_KEY"
        sport_key = os.environ.get(key_var) or os.environ.get("SPORTRADAR_API_KEY")
        # Bind sport into the closure so each feed polls its own URL.
        async def _poll(sport: str = sport) -> int:
            return await _poll_sportradar(sport)
        registry.register(Feed(
            name=f"sportradar_{sport}",
            cadence_seconds=float(schedule_cadence),
            poll=_poll,
            enabled=bool(sport_key),
            skipped_reason=None if sport_key else f"{key_var} not set",
        ))
