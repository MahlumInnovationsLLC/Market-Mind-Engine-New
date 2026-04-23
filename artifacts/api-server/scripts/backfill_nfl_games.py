"""Operator CLI: one-time backfill of last completed NFL season's games.

Task #195 wired NFL into the live Sportradar feeds and per-league
calibrators / margin-regressors, but only the *current* week was ever
ingested.  That left both the per-league NFL calibrator and the NFL
margin regressor cold-starting from zero — they only sharpened as
freshly-settled trades arrived, which on a 17-game-per-team season
means it takes months for the artifacts to leave their textbook
fallbacks.

This script does a one-shot bulk fetch of an entire prior NFL season
(regular season + playoffs) and upserts every game into the canonical
``games`` table with final scores and ``scheduled_start`` populated.
That gives ``app.margin_regressor.refit_all_leagues()`` real history to
fit against and gives the per-league calibrator a meaningful prior on
day one (paired with the existing nightly per-league refit job).

Two input sources are supported (mix-and-match — they're loaded,
de-duplicated by ``game_id``, and upserted together):

    # Primary: pull the season from Sportradar NFL Official v7
    python scripts/backfill_nfl_games.py --from-sportradar --season 2024

    # Fallback / top-up: operator-supplied CSV export
    python scripts/backfill_nfl_games.py --csv nfl_2024.csv

The Sportradar season-schedule endpoint
(``/games/{year}/{season_type}/schedule.json``) returns every game
nested under ``weeks[].games[]``.  Finished games carry an inline
``scoring`` block with ``home_points`` / ``away_points``; for any game
where the inline scoring is missing or zero we fall back to the
per-game ``boxscore.json`` endpoint so we never write a "0-0" row for
a real completed game.

CSV format (header required, extra columns ignored):

    game_id,home_team,away_team,home_score,away_score,scheduled_start,status
    sr:nfl:2024:1:KC-BAL,Kansas City Chiefs,Baltimore Ravens,27,20,2024-09-05T20:20:00Z,closed

Idempotent: the upsert key is ``game_id``; re-running with the same
inputs only refreshes scores / status on the existing rows.
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db  # noqa: E402


log = logging.getLogger("axiom.scripts.backfill_nfl_games")


SPORTRADAR_NFL_BASE = (
    "https://api.sportradar.us/nfl/official/trial/v7/en"
)
SPORTRADAR_SCHEDULE_URL = (
    SPORTRADAR_NFL_BASE + "/games/{year}/{stype}/schedule.json"
)
SPORTRADAR_BOXSCORE_URL = (
    SPORTRADAR_NFL_BASE + "/games/{game_id}/boxscore.json"
)
DEFAULT_SEASON_TYPES: tuple[str, ...] = ("REG", "PST")
FINISHED_STATUSES: frozenset[str] = frozenset({
    "closed", "complete", "completed", "final", "finished",
})

REQUIRED_CSV_COLS = (
    "game_id", "home_team", "away_team",
    "home_score", "away_score", "scheduled_start",
)


def extract_games_from_schedule(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Walk a Sportradar NFL season-schedule payload to a flat list.

    The v7 envelope is ``{"id": ..., "year": Y, "type": "REG",
    "weeks": [{"id": ..., "sequence": 1, "games": [{...}, ...]}]}``.
    Per-game finished payloads include a ``scoring`` block that we
    project up so callers don't need to re-walk it.
    """
    out: list[dict[str, Any]] = []
    weeks = payload.get("weeks") or []
    for week in weeks:
        if not isinstance(week, dict):
            continue
        for g in week.get("games") or []:
            if not isinstance(g, dict):
                continue
            home = g.get("home") or {}
            away = g.get("away") or {}
            scoring = g.get("scoring") or {}
            home_points = scoring.get("home_points")
            away_points = scoring.get("away_points")
            # NFL Official v7 sometimes carries final scores at the top
            # level too (`g["home_points"]`); accept either shape.
            if home_points is None:
                home_points = g.get("home_points")
            if away_points is None:
                away_points = g.get("away_points")
            out.append({
                "id": g.get("id"),
                "scheduled": g.get("scheduled"),
                "status": g.get("status") or "scheduled",
                "home_team": home.get("name") or home.get("market") or "",
                "away_team": away.get("name") or away.get("market") or "",
                "home_score": home_points,
                "away_score": away_points,
                "week": week.get("sequence") or week.get("title"),
            })
    return out


def extract_score_from_boxscore(
    payload: dict[str, Any],
) -> tuple[int | None, int | None, str | None]:
    """Pull (home_score, away_score, status) from a boxscore payload.

    Boxscore wraps the live state under either the top level or a
    ``game`` key depending on the API surface; handle both.
    """
    g = payload.get("game") or payload
    if not isinstance(g, dict):
        return None, None, None
    home = g.get("home") or {}
    away = g.get("away") or {}
    scoring = g.get("scoring") or {}
    home_points = (
        scoring.get("home_points")
        if scoring.get("home_points") is not None
        else home.get("points")
    )
    away_points = (
        scoring.get("away_points")
        if scoring.get("away_points") is not None
        else away.get("points")
    )
    status = g.get("status")
    return (
        int(home_points) if home_points is not None else None,
        int(away_points) if away_points is not None else None,
        str(status) if status else None,
    )


async def fetch_season_schedule(
    client: httpx.AsyncClient,
    api_key: str,
    year: int,
    stype: str,
) -> list[dict[str, Any]]:
    """Hit Sportradar's NFL season-schedule endpoint for one season-type."""
    url = SPORTRADAR_SCHEDULE_URL.format(year=int(year), stype=stype.upper())
    r = await client.get(url, params={"api_key": api_key})
    if r.status_code in (401, 403):
        raise RuntimeError(
            f"sportradar NFL season schedule auth rejected ({r.status_code})"
        )
    if r.status_code >= 400:
        raise RuntimeError(
            f"sportradar NFL season schedule HTTP {r.status_code}: {r.text[:160]}"
        )
    payload = r.json() or {}
    games = extract_games_from_schedule(payload)
    log.info(
        "sportradar season=%s type=%s -> %d games", year, stype, len(games),
    )
    return games


async def fetch_boxscore_score(
    client: httpx.AsyncClient,
    api_key: str,
    game_id: str,
) -> tuple[int | None, int | None, str | None]:
    """Fall back to per-game boxscore when the schedule lacked scores."""
    url = SPORTRADAR_BOXSCORE_URL.format(game_id=game_id)
    try:
        r = await client.get(url, params={"api_key": api_key})
    except httpx.HTTPError as e:
        log.warning("boxscore fetch failed for %s: %s", game_id, e)
        return None, None, None
    if r.status_code >= 400:
        log.warning(
            "boxscore HTTP %s for %s: %s",
            r.status_code, game_id, r.text[:160],
        )
        return None, None, None
    try:
        payload = r.json() or {}
    except Exception:
        return None, None, None
    return extract_score_from_boxscore(payload)


def load_csv(path: Path) -> list[dict[str, Any]]:
    """Parse a CSV of finished NFL games.  Same shape as upsert_games()."""
    rows: list[dict[str, Any]] = []
    with path.open("r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing = [
            c for c in REQUIRED_CSV_COLS if c not in (reader.fieldnames or [])
        ]
        if missing:
            raise SystemExit(
                f"CSV {path} missing required columns: {missing}. "
                f"Expected: {list(REQUIRED_CSV_COLS)}"
            )
        for i, raw in enumerate(reader, start=2):
            try:
                rows.append({
                    "id": str(raw["game_id"]).strip(),
                    "scheduled": str(raw["scheduled_start"]).strip(),
                    "status": (
                        str(raw.get("status") or "closed").strip().lower()
                    ),
                    "home_team": str(raw["home_team"]).strip(),
                    "away_team": str(raw["away_team"]).strip(),
                    "home_score": int(float(raw["home_score"])),
                    "away_score": int(float(raw["away_score"])),
                })
            except Exception as e:
                log.warning("CSV row %d skipped: %s", i, e)
    log.info("loaded %d games from %s", len(rows), path)
    return rows


def merge_and_dedupe(
    *sources: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """De-duplicate by ``game_id``; later sources do NOT overwrite earlier."""
    seen: dict[str, dict[str, Any]] = {}
    for src in sources:
        for g in src:
            gid = str(g.get("id") or "").strip()
            if not gid:
                continue
            if gid not in seen:
                seen[gid] = g
    return list(seen.values())


def _build_upsert_rows(
    games: list[dict[str, Any]],
) -> list[tuple]:
    """Project parsed games into the tuple shape ``games`` upsert expects."""
    now_iso = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    rows: list[tuple] = []
    for g in games:
        gid = str(g.get("id") or "").strip()
        if not gid:
            continue
        home = str(g.get("home_team") or "").strip()
        away = str(g.get("away_team") or "").strip()
        scheduled = str(g.get("scheduled") or "").strip()
        if not (home and away and scheduled):
            continue
        status = str(g.get("status") or "scheduled").strip().lower()
        home_score = g.get("home_score")
        away_score = g.get("away_score")
        rows.append((
            gid, "nfl", "NFL", home, away, scheduled, status,
            int(home_score) if home_score is not None else None,
            int(away_score) if away_score is not None else None,
            now_iso, now_iso,
        ))
    return rows


async def upsert_games(games: list[dict[str, Any]]) -> int:
    """Upsert into the canonical ``games`` table.  Returns row count."""
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


async def fetch_from_sportradar(
    year: int,
    season_types: Iterable[str],
    *,
    backfill_missing_scores: bool = True,
) -> list[dict[str, Any]]:
    """Pull a full season's worth of NFL games from Sportradar.

    For finished games whose scores aren't already inline in the
    schedule envelope, fall back to ``boxscore.json`` per game so the
    rows we write actually have final scores (otherwise the margin
    regressor's ``home_score IS NOT NULL`` filter would skip them).
    """
    api_key = (
        os.environ.get("SPORTRADAR_NFL_KEY")
        or os.environ.get("SPORTRADAR_API_KEY")
    )
    if not api_key:
        raise SystemExit(
            "SPORTRADAR_NFL_KEY (or SPORTRADAR_API_KEY) not set; can't "
            "backfill from Sportradar.  Use --csv as a fallback."
        )
    out: list[dict[str, Any]] = []
    timeout = httpx.Timeout(20.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for stype in season_types:
            try:
                games = await fetch_season_schedule(
                    client, api_key, year, stype,
                )
            except Exception as e:
                log.warning("season schedule %s/%s failed: %s", year, stype, e)
                continue
            if backfill_missing_scores:
                for g in games:
                    if (
                        str(g.get("status") or "").lower() in FINISHED_STATUSES
                        and (g.get("home_score") is None
                             or g.get("away_score") is None)
                    ):
                        gid = str(g.get("id") or "")
                        if not gid:
                            continue
                        h, a, st = await fetch_boxscore_score(
                            client, api_key, gid,
                        )
                        if h is not None and a is not None:
                            g["home_score"] = h
                            g["away_score"] = a
                        if st:
                            g["status"] = st
            out.extend(games)
    return out


async def _main(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    t0 = time.perf_counter()
    sources: list[list[dict[str, Any]]] = []
    if args.csv:
        sources.append(load_csv(Path(args.csv)))
    if args.from_sportradar:
        season_types = [
            s.strip().upper()
            for s in (args.season_types or ",".join(DEFAULT_SEASON_TYPES)).split(",")
            if s.strip()
        ]
        sources.append(
            await fetch_from_sportradar(args.season, season_types)
        )
    if not sources:
        print(
            "No input source selected.  Pass --from-sportradar and/or --csv.",
            file=sys.stderr,
        )
        return 2
    games = merge_and_dedupe(*sources)
    if args.dry_run:
        print(json.dumps({
            "season": args.season,
            "input_games": len(games),
            "with_scores": sum(
                1 for g in games
                if g.get("home_score") is not None
                and g.get("away_score") is not None
            ),
            "dry_run": True,
        }, indent=2))
        return 0
    written = await upsert_games(games)
    print(json.dumps({
        "season": args.season,
        "input_games": len(games),
        "rows_upserted": written,
        "wall_clock_s": round(time.perf_counter() - t0, 2),
    }, indent=2))
    await db.close_pool()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=(
            "One-time backfill of last completed NFL season's games into "
            "the canonical `games` table."
        ),
    )
    p.add_argument(
        "--season", type=int,
        default=int(os.environ.get("AXIOM_NFL_BACKFILL_SEASON")
                    or (datetime.now(tz=timezone.utc).year - 1)),
        help="NFL season year to backfill (defaults to previous year).",
    )
    p.add_argument(
        "--season-types", default=",".join(DEFAULT_SEASON_TYPES),
        help="Comma-separated season types (REG,PST,PRE).  Default: REG,PST.",
    )
    p.add_argument(
        "--from-sportradar", action="store_true",
        help="Pull the season schedule from Sportradar NFL Official v7 "
             "(requires SPORTRADAR_NFL_KEY or SPORTRADAR_API_KEY).",
    )
    p.add_argument(
        "--csv", default=None,
        help="Path to a CSV of finished games "
             "(game_id,home_team,away_team,home_score,away_score,"
             "scheduled_start[,status]).",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Parse + dedupe but skip the DB upsert.",
    )
    args = p.parse_args()
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
