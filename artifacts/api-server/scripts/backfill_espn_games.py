"""Operator CLI: backfill historical games from ESPN's free public API.

Loads a completed season's worth of finished games for any supported
league into the canonical ``games`` table, providing real history for
``app.margin_regressor.refit_all_leagues()`` and a meaningful prior for
the per-league calibrators on cold start.

ESPN's site API is public, free, and requires no key.  Idempotent —
upsert key is ``game_id``; re-running the same args refreshes scores
and status on existing rows.

Usage (from artifacts/api-server)::

    # Default: previous calendar year, ALL supported leagues
    python scripts/backfill_espn_games.py

    # Specific subset
    python scripts/backfill_espn_games.py --leagues NFL,NBA --season 2024

    # Smoke / dry-run — fetch + parse, no DB writes
    python scripts/backfill_espn_games.py --leagues NFL --dry-run

Supported leagues:
    NFL, NCAAF, NBA, NCAAB, MLB, NHL,
    EPL, LALIGA, BUNDES, SERIEA, LIGUE1, MLS, UCL
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from calendar import monthrange
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db  # noqa: E402


log = logging.getLogger("axiom.scripts.backfill_espn_games")

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

FINISHED_STATUSES: frozenset[str] = frozenset({
    "STATUS_FINAL",
    "STATUS_FULL_TIME",
    "STATUS_FULL_PEN",
    "STATUS_END_PERIOD",
})


LEAGUE_CONFIGS: dict[str, dict[str, str]] = {
    "NFL":    {"path": "football/nfl",                       "sport": "nfl",      "chunk": "year"},
    "NCAAF":  {"path": "football/college-football",          "sport": "ncaaf",    "chunk": "year"},
    "NBA":    {"path": "basketball/nba",                     "sport": "nba",      "chunk": "month"},
    # NCAAB rejects ESPN's ``YYYYMMDD-YYYYMMDD`` hyphen-range syntax with
    # HTTP 404 ("Failed to get events endpoint").  ``YYYYMM`` works.
    "NCAAB":  {"path": "basketball/mens-college-basketball", "sport": "ncaab",    "chunk": "yearmonth"},
    "MLB":    {"path": "baseball/mlb",                       "sport": "mlb",      "chunk": "month"},
    "NHL":    {"path": "hockey/nhl",                         "sport": "nhl",      "chunk": "month"},
    "EPL":    {"path": "soccer/eng.1",                       "sport": "soccer",   "chunk": "year"},
    "LALIGA": {"path": "soccer/esp.1",                       "sport": "soccer",   "chunk": "year"},
    "BUNDES": {"path": "soccer/ger.1",                       "sport": "soccer",   "chunk": "year"},
    "SERIEA": {"path": "soccer/ita.1",                       "sport": "soccer",   "chunk": "year"},
    "LIGUE1": {"path": "soccer/fra.1",                       "sport": "soccer",   "chunk": "year"},
    "MLS":    {"path": "soccer/usa.1",                       "sport": "soccer",   "chunk": "year"},
    "UCL":    {"path": "soccer/uefa.champions",              "sport": "soccer",   "chunk": "year"},
}


def _date_chunks(season: int, chunk: str) -> list[str]:
    """Build ESPN ``dates=`` parameter values covering a season.

    Walks Jan-Dec of the season year plus Jan-Mar of the following year
    (covers EPL/UCL spring tail and NFL playoffs that bleed into Jan/Feb,
    plus NCAAB which is Nov-Mar).

    ``year``: one ``YYYYMMDD-YYYYMMDD`` range per calendar year.
    ``month``: one ``YYYYMMDD-YYYYMMDD`` range per calendar month
    (keeps high-volume leagues under ESPN's ~1000-event response cap).
    ``yearmonth``: one ``YYYYMM`` token per month — required by the NCAAB
    path which 404s on hyphen-ranges.
    """
    months = [(season, m) for m in range(1, 13)] + [(season + 1, m) for m in range(1, 4)]
    if chunk == "year":
        out: list[str] = []
        for yr in (season, season + 1):
            yr_months = [m for (y, m) in months if y == yr]
            if not yr_months:
                continue
            first_m, last_m = min(yr_months), max(yr_months)
            start = f"{yr:04d}{first_m:02d}01"
            last_day = monthrange(yr, last_m)[1]
            end = f"{yr:04d}{last_m:02d}{last_day:02d}"
            out.append(f"{start}-{end}")
        return out
    if chunk == "yearmonth":
        return [f"{y:04d}{m:02d}" for (y, m) in months]
    # default: month with hyphen range
    return [
        f"{y:04d}{m:02d}01-{y:04d}{m:02d}{monthrange(y, m)[1]:02d}"
        for (y, m) in months
    ]


async def _fetch_scoreboard(
    client: httpx.AsyncClient, path: str, dates_param: str,
) -> list[dict[str, Any]]:
    url = f"{ESPN_BASE}/{path}/scoreboard"
    params = {"dates": dates_param, "limit": 1000}
    try:
        r = await client.get(url, params=params)
    except Exception as e:
        log.warning("ESPN fetch failed for %s dates=%s: %s", path, dates_param, e)
        return []
    if r.status_code != 200:
        log.warning(
            "ESPN HTTP %d for %s dates=%s: %s",
            r.status_code, path, dates_param, r.text[:160],
        )
        return []
    try:
        payload = r.json() or {}
    except Exception as e:
        log.warning("ESPN JSON parse failed for %s dates=%s: %s", path, dates_param, e)
        return []
    return payload.get("events") or []


def _parse_event(
    event: dict[str, Any], league: str, sport: str,
) -> dict[str, Any] | None:
    """Map an ESPN scoreboard event to our ``games`` upsert tuple shape."""
    eid = str(event.get("id") or "").strip()
    if not eid:
        return None
    status_obj = (event.get("status") or {}).get("type") or {}
    status_name = str(status_obj.get("name") or "").upper()
    if status_name not in FINISHED_STATUSES:
        # Skip in-progress / scheduled / postponed games — only ingest finals.
        return None
    competitions = event.get("competitions") or []
    if not competitions:
        return None
    competitors = competitions[0].get("competitors") or []
    if len(competitors) != 2:
        return None
    home = away = None
    home_score = away_score = None
    for c in competitors:
        side = (c.get("homeAway") or "").lower()
        team_name = ((c.get("team") or {}).get("displayName") or "").strip()
        try:
            score = int(c.get("score")) if c.get("score") not in (None, "") else None
        except (TypeError, ValueError):
            score = None
        if side == "home":
            home = team_name
            home_score = score
        elif side == "away":
            away = team_name
            away_score = score
    if not (home and away):
        return None
    scheduled = str(event.get("date") or "").strip()
    if not scheduled:
        return None
    return {
        "id": f"espn:{league.lower()}:{eid}",
        "sport": sport,
        "league": league,
        "home_team": home,
        "away_team": away,
        "scheduled": scheduled,
        "status": "closed",
        "home_score": home_score,
        "away_score": away_score,
    }


async def fetch_league_season(
    client: httpx.AsyncClient,
    league: str,
    season: int,
    *,
    rate_limit_sleep: float = 0.15,
) -> list[dict[str, Any]]:
    cfg = LEAGUE_CONFIGS[league]
    chunks = _date_chunks(season, cfg["chunk"])
    seen: dict[str, dict[str, Any]] = {}
    for dates_param in chunks:
        events = await _fetch_scoreboard(client, cfg["path"], dates_param)
        for ev in events:
            parsed = _parse_event(ev, league, cfg["sport"])
            if parsed and parsed["id"] not in seen:
                seen[parsed["id"]] = parsed
        await asyncio.sleep(rate_limit_sleep)
    return list(seen.values())


def _build_upsert_rows(games: list[dict[str, Any]]) -> list[tuple]:
    now_iso = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    rows: list[tuple] = []
    for g in games:
        rows.append((
            g["id"], g["sport"], g["league"],
            g["home_team"], g["away_team"], g["scheduled"], g["status"],
            int(g["home_score"]) if g.get("home_score") is not None else None,
            int(g["away_score"]) if g.get("away_score") is not None else None,
            now_iso, now_iso,
        ))
    return rows


async def upsert_games(games: list[dict[str, Any]]) -> int:
    rows = _build_upsert_rows(games)
    if not rows:
        return 0
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
              scheduled_start = EXCLUDED.scheduled_start,
              home_team = EXCLUDED.home_team,
              away_team = EXCLUDED.away_team,
              last_updated = EXCLUDED.last_updated
            """,
            rows,
        )
    return len(rows)


async def _main(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    leagues = (
        [s.strip().upper() for s in args.leagues.split(",") if s.strip()]
        if args.leagues else list(LEAGUE_CONFIGS.keys())
    )
    bad = [lg for lg in leagues if lg not in LEAGUE_CONFIGS]
    if bad:
        print(f"Unknown leagues: {bad}.  Supported: {list(LEAGUE_CONFIGS.keys())}", file=sys.stderr)
        return 2

    summary: dict[str, Any] = {"season": args.season, "leagues": {}}
    t0 = time.perf_counter()
    timeout = httpx.Timeout(20.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for league in leagues:
            t_league = time.perf_counter()
            try:
                games = await fetch_league_season(client, league, args.season)
            except Exception as e:
                log.exception("[%s] fetch failed", league)
                summary["leagues"][league] = {"error": f"{type(e).__name__}: {e}"}
                continue
            with_scores = sum(
                1 for g in games
                if g.get("home_score") is not None and g.get("away_score") is not None
            )
            if args.dry_run:
                written = 0
            else:
                try:
                    written = await upsert_games(games)
                except Exception as e:
                    log.exception("[%s] upsert failed", league)
                    summary["leagues"][league] = {
                        "fetched": len(games), "with_scores": with_scores,
                        "error": f"{type(e).__name__}: {e}",
                    }
                    continue
            summary["leagues"][league] = {
                "fetched": len(games),
                "with_scores": with_scores,
                "rows_upserted": written,
                "wall_clock_s": round(time.perf_counter() - t_league, 2),
            }
            log.info(
                "[%s] %d games fetched, %d with scores, %d upserted (%.1fs)",
                league, len(games), with_scores, written,
                time.perf_counter() - t_league,
            )

    summary["wall_clock_s"] = round(time.perf_counter() - t0, 2)
    summary["dry_run"] = bool(args.dry_run)
    print(json.dumps(summary, indent=2))
    if not args.dry_run:
        await db.close_pool()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Backfill historical games from ESPN's public API.",
    )
    p.add_argument(
        "--season", type=int,
        default=datetime.now(tz=timezone.utc).year - 1,
        help="Season anchor year (defaults to previous year).",
    )
    p.add_argument(
        "--leagues", default=None,
        help=f"Comma-separated league codes.  Default: all "
             f"({','.join(LEAGUE_CONFIGS.keys())}).",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Fetch + parse but skip the DB upsert.",
    )
    args = p.parse_args()
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
