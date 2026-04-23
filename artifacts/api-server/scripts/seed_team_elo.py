"""Operator CLI: seed `team_elo` / `team_elo_history` from 5 years of
finished game results.

Until this job runs, every team starts at the default Elo rating
(`INITIAL_RATING = 1500`) and `get_team_features()` returns identical
numbers for both sides — i.e. the Elo signal is pure noise.  This
script replays a chronologically-ordered stream of finished games
through `app.elo.seed_from_history()`, which idempotently fills both
`team_elo` (current snapshot) and `team_elo_history` (per-game
before/after rating).  Re-running with the same data is safe — the
unique `(league, team, game_id)` index in `team_elo_history` short-
circuits duplicates.

Three input sources are supported (mix-and-match — they're loaded,
de-duplicated by `game_id`, sorted by `played_at`, and replayed
together):

    # Primary: 5 years of operator-supplied results from CSV
    python scripts/seed_team_elo.py --csv historical_games.csv

    # Top-up: pull finished games already cached locally in `games`
    python scripts/seed_team_elo.py --from-db

    # Top-up: pull the last N days of finished scores from
    # The Odds API `/v4/sports/{sport}/scores` (free tier caps at
    # `daysFrom=3`, paid tiers go higher).
    python scripts/seed_team_elo.py --from-the-odds-api --days 3

CSV format (header required, extra columns ignored):

    league,home_team,away_team,home_score,away_score,game_id,played_at
    NBA,Lakers,Celtics,110,100,nba-2021-001,2021-10-19T23:00:00Z
    NFL,Chiefs,Bills,27,24,nfl-2021-014,2021-10-10T20:00:00Z

`league` should match the codes used by the predictor / Elo K-factor
table: NBA, NFL, NHL, MLB, NCAAB, MLS, EPL, LALIGA, SERIEA, BUNDES,
LIGUE1, UCL (or the generic `Soccer`).  Unknown leagues fall back to
the default K=20.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import httpx

# Allow running as `python scripts/seed_team_elo.py` from api-server.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db  # noqa: E402
from app import elo as _elo  # noqa: E402
from app.feeds.odds_api import (  # noqa: E402
    DEFAULT_ODDS_SPORTS,
    THE_ODDS_API_BASE,
)


log = logging.getLogger("axiom.scripts.seed_team_elo")


# Map The-Odds-API `sport_key` -> our internal Elo league code.
_SPORT_TO_LEAGUE: dict[str, str] = {
    "basketball_nba": "NBA",
    "basketball_ncaab": "NCAAB",
    "americanfootball_nfl": "NFL",
    "americanfootball_ncaaf": "NCAAF",
    "baseball_mlb": "MLB",
    "icehockey_nhl": "NHL",
    "soccer_epl": "EPL",
    "soccer_spain_la_liga": "LALIGA",
    "soccer_italy_serie_a": "SERIEA",
    "soccer_germany_bundesliga": "BUNDES",
    "soccer_france_ligue_one": "LIGUE1",
    "soccer_usa_mls": "MLS",
    "soccer_uefa_champs_league": "UCL",
}


REQUIRED_CSV_COLS = (
    "league", "home_team", "away_team",
    "home_score", "away_score", "game_id", "played_at",
)


def _parse_played_at(raw: Any) -> datetime:
    """Parse a timestamp from any of our input sources to a tz-aware UTC datetime.

    Bare ISO strings without a timezone are interpreted as UTC — every
    upstream source we ingest from (CSV exports, The Odds API,
    Sportradar) reports timestamps in UTC, and `merge_and_sort()`
    compares datetimes across sources, so naive vs aware mixing would
    raise `TypeError`.
    """
    if isinstance(raw, datetime):
        dt = raw
    else:
        s = str(raw).strip()
        if not s:
            raise ValueError("empty played_at")
        # Accept `Z` suffix and bare ISO.
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_csv(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in REQUIRED_CSV_COLS if c not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(
                f"CSV {path} missing required columns: {missing}. "
                f"Expected: {list(REQUIRED_CSV_COLS)}"
            )
        for i, raw in enumerate(reader, start=2):  # 1 = header
            try:
                rows.append({
                    "league": str(raw["league"]).strip(),
                    "home_team": str(raw["home_team"]).strip(),
                    "away_team": str(raw["away_team"]).strip(),
                    "home_score": float(raw["home_score"]),
                    "away_score": float(raw["away_score"]),
                    "game_id": str(raw["game_id"]).strip(),
                    "played_at": _parse_played_at(raw["played_at"]),
                })
            except Exception as e:
                log.warning("CSV row %d skipped: %s", i, e)
    log.info("loaded %d games from %s", len(rows), path)
    return rows


_FINISHED_STATUSES = (
    "closed", "complete", "completed", "final", "finished", "ended", "ft",
)


async def load_from_db() -> list[dict[str, Any]]:
    """Pull every finished row from the local `games` table.

    Skips rows missing teams, scores, or scheduled_start.  Respects the
    same idempotency contract as CSV input — `game_id` flows straight
    through to `team_elo_history.game_id`.
    """
    try:
        rows = await db.fetch(
            """
            SELECT game_id, league, home_team, away_team,
                   home_score, away_score, scheduled_start, status
              FROM games
             WHERE home_score IS NOT NULL
               AND away_score IS NOT NULL
               AND home_team IS NOT NULL AND home_team <> ''
               AND away_team IS NOT NULL AND away_team <> ''
               AND lower(coalesce(status, '')) = ANY($1::text[])
             ORDER BY scheduled_start ASC
            """,
            list(_FINISHED_STATUSES),
        )
    except Exception as e:
        log.warning("load_from_db failed (table may not exist): %s", e)
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            played_at = r["scheduled_start"]
            if played_at is None:
                continue
            if isinstance(played_at, str):
                played_at = _parse_played_at(played_at)
            out.append({
                "league": str(r["league"] or "").strip(),
                "home_team": str(r["home_team"]).strip(),
                "away_team": str(r["away_team"]).strip(),
                "home_score": float(r["home_score"]),
                "away_score": float(r["away_score"]),
                "game_id": str(r["game_id"]).strip(),
                "played_at": played_at,
            })
        except Exception as e:
            log.warning("db row skipped (%s): %s", r.get("game_id"), e)
    log.info("loaded %d finished games from local `games` table", len(out))
    return out


async def load_from_the_odds_api(
    days: int, sports: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Pull finished games from The Odds API `/v4/sports/{sport}/scores`.

    The free tier caps `daysFrom` at 3; paid tiers extend the window.
    For a true 5-year backfill, use `--csv` with an operator-supplied
    export — this path is intended as a top-up between full reloads.
    """
    api_key = os.environ.get("THE_ODDS_API_KEY") or os.environ.get("ODDS_API_KEY")
    if not api_key:
        log.warning("THE_ODDS_API_KEY not set; skipping --from-the-odds-api")
        return []

    days = max(1, int(days))
    sport_keys = sports or list(DEFAULT_ODDS_SPORTS)
    out: list[dict[str, Any]] = []
    timeout = httpx.Timeout(15.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for sport in [s.strip() for s in sport_keys if s.strip()]:
            league = _SPORT_TO_LEAGUE.get(sport)
            if not league:
                log.info("skip %s — no Elo league mapping", sport)
                continue
            url = f"{THE_ODDS_API_BASE}/sports/{sport}/scores"
            try:
                r = await client.get(url, params={
                    "apiKey": api_key, "daysFrom": days, "dateFormat": "iso",
                })
            except httpx.HTTPError as e:
                log.warning("the-odds-api scores fetch failed for %s: %s", sport, e)
                continue
            if r.status_code >= 400:
                log.warning("the-odds-api scores %s -> HTTP %s: %s",
                            sport, r.status_code, r.text[:160])
                continue
            try:
                games = r.json() or []
            except Exception:
                continue
            for g in games:
                if not g.get("completed"):
                    continue
                home = g.get("home_team")
                away = g.get("away_team")
                gid = g.get("id")
                played = g.get("commence_time")
                scores = g.get("scores") or []
                if not (home and away and gid and played and scores):
                    continue
                try:
                    by_team = {s.get("name"): float(s.get("score")) for s in scores}
                    home_score = by_team[home]
                    away_score = by_team[away]
                except Exception:
                    continue
                out.append({
                    "league": league,
                    "home_team": str(home).strip(),
                    "away_team": str(away).strip(),
                    "home_score": home_score,
                    "away_score": away_score,
                    "game_id": str(gid).strip(),
                    "played_at": _parse_played_at(played),
                })
    log.info("loaded %d finished games from The Odds API (last %dd)",
             len(out), days)
    return out


def merge_and_sort(*sources: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """De-duplicate by (league, game_id) and sort chronologically.

    `seed_from_history` MUST receive games in chronological order so
    each rating update reflects the strength a team had at that
    moment — feeding the latest game first would compound errors.
    """
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for src in sources:
        for g in src:
            key = (g["league"], g["game_id"])
            if key not in seen:
                seen[key] = g
    return sorted(seen.values(), key=lambda g: g["played_at"])


async def _post_run_summary(games: list[dict[str, Any]]) -> dict[str, Any]:
    """Sample a few teams per league so the operator can eyeball the
    spread of ratings after the replay."""
    by_league: dict[str, set[str]] = {}
    for g in games:
        by_league.setdefault(g["league"], set()).update((g["home_team"], g["away_team"]))
    summary: dict[str, Any] = {}
    for league, teams in by_league.items():
        sample = sorted(teams)[:5]
        ratings = []
        for t in sample:
            tr = await _elo.get_rating(league, t)
            ratings.append({
                "team": t,
                "rating": round(tr.rating, 2),
                "games_played": tr.games_played,
            })
        summary[league] = {
            "teams_seen": len(teams),
            "sample_ratings": ratings,
        }
    return summary


async def _main(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    t0 = time.perf_counter()
    sources: list[list[dict[str, Any]]] = []
    if args.csv:
        sources.append(load_csv(Path(args.csv)))
    if args.from_db:
        sources.append(await load_from_db())
    if args.from_the_odds_api:
        sports = [s.strip() for s in args.sports.split(",")] if args.sports else None
        sources.append(await load_from_the_odds_api(args.days, sports))

    if not sources:
        print(
            "No input source selected.  Pass --csv PATH and/or --from-db "
            "and/or --from-the-odds-api.",
            file=sys.stderr,
        )
        return 2

    games = merge_and_sort(*sources)
    if args.limit:
        games = games[: args.limit]
    if not games:
        print("No games to replay (all sources empty).", file=sys.stderr)
        return 1

    log.info(
        "replaying %d games (%s -> %s) through seed_from_history",
        len(games),
        games[0]["played_at"].isoformat(),
        games[-1]["played_at"].isoformat(),
    )

    res = await _elo.seed_from_history(games)
    summary = await _post_run_summary(games)

    final = {
        "games_input": len(games),
        "games_processed": res.games_processed,
        "games_skipped": res.games_skipped,
        "errors": res.errors[:20],
        "error_count": len(res.errors),
        "wall_clock_s": round(time.perf_counter() - t0, 2),
        "ratings_sample": summary,
    }
    print("\n--- seed_team_elo complete ---")
    print(json.dumps(final, indent=2, default=str))
    await db.close_pool()
    return 0 if not res.errors else 1


def main() -> int:
    p = argparse.ArgumentParser(
        description=(
            "Seed team_elo / team_elo_history from 5 years of finished "
            "game results.  Replays games chronologically through "
            "app.elo.seed_from_history()."
        ),
    )
    p.add_argument(
        "--csv", default=None,
        help="Path to a CSV of finished games "
             "(league,home_team,away_team,home_score,away_score,game_id,played_at).",
    )
    p.add_argument(
        "--from-db", action="store_true",
        help="Also pull finished rows from the local `games` table.",
    )
    p.add_argument(
        "--from-the-odds-api", action="store_true",
        help="Also pull finished games from The Odds API /scores "
             "(requires THE_ODDS_API_KEY; free tier caps at 3 days).",
    )
    p.add_argument(
        "--days", type=int, default=3,
        help="Days of history to request from The Odds API /scores "
             "(free tier caps the response at 3 days; paid plans extend the window).",
    )
    p.add_argument(
        "--sports", default=None,
        help="Comma-separated The-Odds-API sport keys to fetch "
             "(defaults to AXIOM_ODDS_SPORTS).",
    )
    p.add_argument(
        "--limit", type=int, default=None,
        help="Cap how many games to replay this run (after sort).",
    )
    args = p.parse_args()
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
