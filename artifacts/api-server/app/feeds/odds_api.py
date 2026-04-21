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


log = logging.getLogger("axiom.feeds.odds")


THE_ODDS_API_BASE = "https://api.the-odds-api.com/v4"
# In-season default — operator can override via env.
DEFAULT_ODDS_SPORTS = os.environ.get(
    "AXIOM_ODDS_SPORTS",
    "basketball_nba,baseball_mlb,icehockey_nhl,basketball_ncaab",
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
                log.warning("the-odds-api rate-limited on %s", sport)
                continue
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


async def _poll_sportradar(sport: str) -> int:
    """Sportradar per-sport poller — minimal stub.

    Sportradar surfaces deep team/player feeds that the legacy code
    blends with The Odds API quotes.  Each sport endpoint differs and
    requires its own per-sport API key.  We surface the key check + a
    sentinel /sports/v8/en/league/seasons.json call so the operator can
    confirm the key works and see the latency without us speculating
    about which feed the orchestrator wants first.
    """
    key_var = f"SPORTRADAR_{sport.upper()}_KEY"
    api_key = os.environ.get(key_var) or os.environ.get("SPORTRADAR_API_KEY")
    if not api_key:
        raise RuntimeError(f"{key_var} not set")
    base = {
        "nba": "https://api.sportradar.us/nba/trial/v8/en/league/seasons.json",
        "nhl": "https://api.sportradar.us/nhl/trial/v7/en/league/seasons.json",
        "mlb": "https://api.sportradar.us/mlb/trial/v7/en/league/seasons.json",
        "ncaab": "https://api.sportradar.us/ncaamb/trial/v8/en/league/seasons.json",
    }.get(sport)
    if not base:
        raise RuntimeError(f"unknown sportradar sport: {sport}")

    timeout = httpx.Timeout(10.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(base, params={"api_key": api_key})
        if r.status_code == 401 or r.status_code == 403:
            raise RuntimeError(f"sportradar {sport} auth rejected")
        if r.status_code == 429:
            raise RuntimeError(f"sportradar {sport} rate-limited")
        if r.status_code >= 400:
            raise RuntimeError(f"sportradar {sport} HTTP {r.status_code}")
        # We don't write to the schema yet — orchestrator follow-up owns the
        # sport-specific persistence shape.  Counting the seasons in the
        # response gives a non-zero "I am alive" signal.
        try:
            payload = r.json()
            return len(payload.get("seasons", []) or [])
        except Exception:
            return 0


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

    for sport in ("nba", "nhl", "mlb", "ncaab"):
        key_var = f"SPORTRADAR_{sport.upper()}_KEY"
        sport_key = os.environ.get(key_var) or os.environ.get("SPORTRADAR_API_KEY")
        # Bind sport into the closure so each feed polls its own URL.
        async def _poll(sport: str = sport) -> int:
            return await _poll_sportradar(sport)
        registry.register(Feed(
            name=f"sportradar_{sport}",
            cadence_seconds=60.0,
            poll=_poll,
            enabled=bool(sport_key),
            skipped_reason=None if sport_key else f"{key_var} not set",
        ))
