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
    # NA sports + international soccer (task #196).  Soccer keys map
    # 1:1 to The-Odds-API's `sports/{key}/odds` endpoint and produce
    # 3-way h2h markets (home/draw/away) which the persistor branches
    # on via LeagueConfig.three_way_market.
    "basketball_nba,baseball_mlb,icehockey_nhl,basketball_ncaab,"
    "americanfootball_nfl,soccer_epl,soccer_spain_la_liga,"
    "soccer_italy_serie_a,soccer_germany_bundesliga,"
    "soccer_france_ligue_one,soccer_usa_mls,soccer_uefa_champs_league",
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
    # International soccer (task #196).  Sportradar splits soccer by
    # competition under the soccer-extended package; the daily schedule
    # endpoint accepts a competition tournament_id baked into the path.
    # Operators must set per-league SPORTRADAR_<LEAGUE>_KEY env vars.
    "epl":    "https://api.sportradar.us/soccer-extended/trial/v4/en/schedules/{y}-{m:02d}-{d:02d}/schedule.json?tournament=sr:tournament:17",
    "laliga": "https://api.sportradar.us/soccer-extended/trial/v4/en/schedules/{y}-{m:02d}-{d:02d}/schedule.json?tournament=sr:tournament:8",
    "seriea": "https://api.sportradar.us/soccer-extended/trial/v4/en/schedules/{y}-{m:02d}-{d:02d}/schedule.json?tournament=sr:tournament:23",
    "bundes": "https://api.sportradar.us/soccer-extended/trial/v4/en/schedules/{y}-{m:02d}-{d:02d}/schedule.json?tournament=sr:tournament:35",
    "ligue1": "https://api.sportradar.us/soccer-extended/trial/v4/en/schedules/{y}-{m:02d}-{d:02d}/schedule.json?tournament=sr:tournament:34",
    "mls":    "https://api.sportradar.us/soccer-extended/trial/v4/en/schedules/{y}-{m:02d}-{d:02d}/schedule.json?tournament=sr:tournament:242",
    "ucl":    "https://api.sportradar.us/soccer-extended/trial/v4/en/schedules/{y}-{m:02d}-{d:02d}/schedule.json?tournament=sr:tournament:7",
    # Combat & individual sports (task #197).  Each discipline ships
    # under its own Sportradar package with a discipline-specific
    # daily-schedule endpoint.  Operators must set the matching
    # SPORTRADAR_<LEAGUE>_KEY env var; missing keys leave the feed
    # registered-but-skipped so /api/feeds/status surfaces the gap.
    "ufc":    "https://api.sportradar.us/mma/trial/v2/en/schedules/{y}-{m:02d}-{d:02d}/schedule.json",
    "atp":    "https://api.sportradar.us/tennis/trial/v3/en/schedules/{y}-{m:02d}-{d:02d}/schedule.json?gender=men",
    "wta":    "https://api.sportradar.us/tennis/trial/v3/en/schedules/{y}-{m:02d}-{d:02d}/schedule.json?gender=women",
    "f1":     "https://api.sportradar.us/formula1/trial/v2/en/sport_events/{y}-{m:02d}-{d:02d}/summaries.json",
    "pga":    "https://api.sportradar.us/golf/trial/v3/en/schedules/{y}/pga/tournaments/schedule.json",
}
_SPORTRADAR_LEAGUE = {
    "nba": "NBA",
    "nhl": "NHL",
    "mlb": "MLB",
    "ncaab": "NCAAB",
    "nfl": "NFL",
    "ncaaf": "NCAAF",
    "epl": "EPL",
    "laliga": "LALIGA",
    "seriea": "SERIEA",
    "bundes": "BUNDES",
    "ligue1": "LIGUE1",
    "mls": "MLS",
    "ucl": "UCL",
    "ufc": "UFC",
    "atp": "ATP",
    "wta": "WTA",
    "f1": "F1",
    "pga": "PGA",
}
SPORTRADAR_SPORTS: tuple[str, ...] = tuple(_SPORTRADAR_SCHEDULE_PATHS.keys())


# Soccer-extended uses tournament names that don't always match our
# league code 1:1 (e.g. "Premier League" vs "EPL").  Map league code →
# substrings to match against `tournament.name` so we keep ONLY rows
# for the requested competition when ingesting per-league soccer feeds.
_SOCCER_TOURNAMENT_HINTS: dict[str, tuple[str, ...]] = {
    "EPL":    ("premier league", "english premier"),
    "LALIGA": ("laliga", "la liga", "primera division"),
    "SERIEA": ("serie a",),
    "BUNDES": ("bundesliga",),
    "LIGUE1": ("ligue 1",),
    "MLS":    ("major league soccer", "mls"),
    "UCL":    ("uefa champions league", "champions league"),
}


def _normalize_sportradar_payload(
    sport: str, league: str, payload: dict,
) -> list[dict]:
    """Map a Sportradar schedule payload to a list of canonical
    game-shaped dicts ({id, home/away, scheduled, status, scores}).

    Handles both the NA-league shapes
    (`{"games": [...]}` / `{"league": {"games": [...]}}` /
    `{"week": {"games": [...]}}`) and the soccer-extended /v4 shape
    (`{"schedules": [{"sport_event": {...},
    "sport_event_status": {...}}]}`), which we additionally filter by
    competition so a single global daily endpoint feeds 7 distinct
    per-league pollers without cross-attribution (task #196).
    """
    # NA shapes first — cheapest match.
    games = (
        payload.get("games")
        or (payload.get("league") or {}).get("games")
        or (payload.get("week") or {}).get("games")
        or []
    )
    if games:
        return list(games)

    # PGA Golf v3: `{"tournaments": [{id, name, start_date, end_date,
    # status, ...}]}`.  Each tournament is one tradable event (the
    # outright-winner market); competitor field arrives via a
    # separate leaderboard endpoint (follow-up #214) — for now we
    # emit the schedule rows so risk-gate per-event aggregation and
    # event_in_progress derivation work end-to-end.
    tournaments = payload.get("tournaments") or []
    if tournaments and league in ("PGA",):
        out: list[dict] = []
        for t in tournaments:
            if not isinstance(t, dict):
                continue
            tid = t.get("id")
            name = t.get("name") or t.get("event_name") or "PGA Tournament"
            start = (
                t.get("start_date")
                or t.get("scheduled")
                or t.get("start_time")
            )
            status = t.get("status") or "scheduled"
            out.append({
                "id": tid,
                "home": {"name": name},  # outright market; "home" = field
                "away": {"name": "FIELD"},
                "scheduled": start,
                "status": status,
            })
        return out

    # Combat & individual sports (task #197) and soccer-extended share
    # the v4-style envelope: schedules/sport_events/summaries lists of
    # `{sport_event, sport_event_status}` entries.  F1 uses
    # `summaries`, tennis/MMA use `sport_events`, soccer uses
    # `schedules`.  Soccer entries get filtered by tournament name;
    # the others pass through (the URL already scopes the
    # competition).
    schedules = (
        payload.get("schedules")
        or payload.get("sport_events")
        or payload.get("summaries")
        or []
    )
    if not schedules:
        return []
    hints = _SOCCER_TOURNAMENT_HINTS.get(league, ())
    out: list[dict] = []
    for entry in schedules:
        ev = entry.get("sport_event") or entry  # sport_events list omits the wrapper
        if not isinstance(ev, dict):
            continue
        tournament = (ev.get("tournament") or {})
        tname = str(tournament.get("name") or "").lower()
        if hints and not any(h in tname for h in hints):
            continue  # different competition on the shared global feed
        competitors = ev.get("competitors") or []
        status_obj = entry.get("sport_event_status") or {}
        # Soccer keeps home/away qualifiers; combat & individual
        # sports usually don't.  Fall back to ordinal position when no
        # qualifier is present so 2-way fights (UFC) and head-to-head
        # tennis matches still produce valid {home, away} rows.  For
        # >2-competitor events (F1 grids, golf fields) we additionally
        # emit `n_way_field` so the orchestrator's Plackett-Luce branch
        # can price each entrant.
        home_obj = next((c for c in competitors if c.get("qualifier") == "home"), None)
        away_obj = next((c for c in competitors if c.get("qualifier") == "away"), None)
        if home_obj is None and away_obj is None and len(competitors) >= 1:
            home_obj = competitors[0] if isinstance(competitors[0], dict) else {}
            away_obj = competitors[1] if len(competitors) >= 2 and isinstance(competitors[1], dict) else {}
        home_obj = home_obj or {}
        away_obj = away_obj or {}
        row: dict = {
            "id": ev.get("id"),
            "home": {"name": home_obj.get("name")},
            "away": {"name": away_obj.get("name")},
            "scheduled": ev.get("start_time") or ev.get("scheduled"),
            "status": status_obj.get("status") or "scheduled",
            "home_points": status_obj.get("home_score"),
            "away_points": status_obj.get("away_score"),
        }
        if len(competitors) > 2:
            # Field of N (F1 grid, tennis draw bracket, etc.).  Carry
            # competitor names + any seed/qualifier metadata through to
            # the candidate so `_predict_for_candidate` can build the
            # `n_way_field` rating mapping (task #197 follow-up #214).
            row["n_way_field"] = [
                {
                    "name": c.get("name"),
                    "id": c.get("id"),
                    "seed": c.get("seed"),
                    "qualifier": c.get("qualifier"),
                }
                for c in competitors if isinstance(c, dict)
            ]
        out.append(row)
    return out


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

    # NA-league shapes: {"games": [...]} | {"league": {"games": [...]}}
    # | {"week": {"games": [...]}} (NFL current_week).  Soccer-extended
    # /v4 returns {"schedules": [{"sport_event": {...},
    # "sport_event_status": {...}}]} keyed across competitions, so we
    # filter by tournament when ingesting per-league soccer feeds (task
    # #196).
    league = _SPORTRADAR_LEAGUE[sport]
    games = _normalize_sportradar_payload(sport, league, payload)
    if not games:
        return 0

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
