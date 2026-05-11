"""TheOddsAPI WNBA player-props poller (task #823).

The Odds API exposes player props through a per-event endpoint:

    /sports/basketball_wnba/events/{eventId}/odds
        ?regions=us
        &oddsFormat=decimal
        &markets=player_points,player_rebounds,player_assists,
                 player_threes,player_blocks,player_steals,
                 player_turnovers,
                 player_points_rebounds_assists,
                 player_points_rebounds,player_points_assists,
                 player_rebounds_assists

We fan out across today's WNBA events (cached by ``odds_api`` from the
shared per-sport sweep) and persist quotes into ``player_props_quotes``.

The feed registers itself with ``enabled=False`` when neither
``THE_ODDS_API_KEY`` nor any alias is configured, so it shows up in
``/api/feeds/status`` with a clear ``skipped_reason``.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Iterable

import httpx

from .. import db
from .odds_api import THE_ODDS_API_BASE, get_recent_games_for_sport
from .registry import Feed, registry
from .scheduler import RateLimited


log = logging.getLogger("axiom.feeds.wnba_player_props")


_SPORT_KEY = "basketball_wnba"
_LEAGUE = "WNBA"
_DEFAULT_CADENCE_SECS = 5 * 60.0  # 5-min in-season

# TheOddsAPI markets → our canonical stat code.
_MARKET_MAP: dict[str, str] = {
    "player_points": "points",
    "player_rebounds": "rebounds",
    "player_assists": "assists",
    "player_threes": "threes",
    "player_blocks": "blocks",
    "player_steals": "steals",
    "player_turnovers": "turnovers",
    "player_points_rebounds_assists": "points_rebounds_assists",
    "player_points_rebounds": "points_rebounds",
    "player_points_assists": "points_assists",
    "player_rebounds_assists": "rebounds_assists",
    "player_double_double": "double_double",
}
_MARKETS_PARAM = ",".join(_MARKET_MAP.keys())

_HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


def _api_key() -> str | None:
    return (
        os.environ.get("THE_ODDS_API_KEY")
        or os.environ.get("THEODDSAPI_KEY")
        or os.environ.get("ODDS_API_KEY")
    )


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _decimal_to_american(decimal_odds: float | None) -> int | None:
    """Convert decimal odds to American.  Returns None when input is
    missing or non-positive."""
    if decimal_odds is None:
        return None
    try:
        d = float(decimal_odds)
    except (TypeError, ValueError):
        return None
    if d <= 1.0:
        return None
    if d >= 2.0:
        return int(round((d - 1.0) * 100.0))
    return int(round(-100.0 / (d - 1.0)))


async def _fetch_event_props(
    client: httpx.AsyncClient, event_id: str, api_key: str
) -> list[dict[str, Any]]:
    url = f"{THE_ODDS_API_BASE}/sports/{_SPORT_KEY}/events/{event_id}/odds"
    params = {
        "apiKey": api_key,
        "regions": os.environ.get("AXIOM_ODDS_REGIONS", "us"),
        "markets": _MARKETS_PARAM,
        "oddsFormat": "decimal",
    }
    try:
        r = await client.get(url, params=params)
    except httpx.HTTPError as e:
        log.warning("wnba props fetch failed for event %s: %s", event_id, e)
        return []
    if r.status_code == 401:
        raise RuntimeError("the-odds-api auth rejected — bad key")
    if r.status_code == 429:
        raise RateLimited(
            "the-odds-api-wnba-props",
            retry_after=_retry_after_seconds(r) or 60.0,
        )
    if r.status_code == 404:
        # No props on this event — common pre-tipoff for early games.
        return []
    if r.status_code >= 400:
        log.warning(
            "the-odds-api wnba props event=%s returned %s: %s",
            event_id, r.status_code, r.text[:200],
        )
        return []
    try:
        payload = r.json()
    except Exception:
        return []
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return payload
    return []


def _flatten_quotes(
    event_payload: dict[str, Any],
) -> list[tuple[str, str, str | None, str, str, str, float | None, float | None, dict]]:
    """Turn one event payload into rows for ``player_props_quotes``.

    Each tuple matches the column order in :func:`_persist_quotes`'s
    INSERT.
    """
    out: list[tuple] = []
    game_id = str(event_payload.get("id") or "")
    if not game_id:
        return out
    home_team = event_payload.get("home_team")
    away_team = event_payload.get("away_team")
    for bk in event_payload.get("bookmakers", []) or []:
        book = bk.get("key") or bk.get("title") or "unknown"
        for mk in bk.get("markets", []) or []:
            mkey = mk.get("key") or ""
            stat = _MARKET_MAP.get(mkey)
            if not stat:
                continue
            for outcome in mk.get("outcomes", []) or []:
                # The Odds API encodes player-props as
                #   { "name": "Over"|"Under", "description": "<player>",
                #     "price": <decimal>, "point": <line> }
                # `description` carries the player name.  Fall back to
                # `name` when description is missing (some books mirror
                # the player into `name` for double_double markets).
                player = (
                    outcome.get("description")
                    or outcome.get("participant")
                    or outcome.get("name")
                    or ""
                )
                player = str(player).strip()
                if not player:
                    continue
                side_raw = (outcome.get("name") or "").strip().lower()
                if side_raw in ("over", "under"):
                    side = side_raw
                elif side_raw in ("yes", "no"):
                    side = side_raw  # double_double
                else:
                    side = side_raw or "over"
                line = outcome.get("point")
                price = outcome.get("price")
                # Rough team attribution: the player lives on either
                # the home or away roster, but TheOddsAPI doesn't tag
                # the side here.  Leave as None — the dashboard joins
                # the team from `player_stat_history` instead.
                team = None
                out.append((
                    _LEAGUE,
                    _SPORT_KEY,
                    game_id,
                    player[:200],
                    team,
                    stat,
                    str(book)[:64],
                    side[:16],
                    float(line) if line is not None else None,
                    float(price) if price is not None else None,
                    {
                        "home_team": home_team,
                        "away_team": away_team,
                        "raw_outcome_name": outcome.get("name"),
                        "raw_market_key": mkey,
                    },
                ))
    return out


async def _persist_quotes(rows: list[tuple]) -> int:
    if not rows:
        return 0
    import json as _json

    sql_rows = []
    for r in rows:
        (league, sport, game_id, player, team, stat, book,
         side, line, price, raw) = r
        sql_rows.append((
            league, sport, game_id, player, team, stat, book, side,
            line, price, _decimal_to_american(price),
            _json.dumps(raw),
        ))

    written = 0
    try:
        async with db.acquire() as conn, conn.transaction():
            await conn.executemany(
                """
                INSERT INTO player_props_quotes
                  (league, sport, game_id, player, team, market, book,
                   outcome, line, price_decimal, price_american, raw,
                   "timestamp")
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                        $12::jsonb, NOW())
                ON CONFLICT (game_id, player, market, book, outcome,
                             "timestamp")
                  DO NOTHING
                """,
                sql_rows,
            )
            written = len(sql_rows)
    except Exception as e:
        log.warning("persist wnba player props failed: %s", e)
    return written


async def _poll_wnba_player_props() -> int:
    api_key = _api_key()
    if not api_key:
        raise RuntimeError("THE_ODDS_API_KEY not set")
    games = get_recent_games_for_sport(_SPORT_KEY, max_age_s=15 * 60.0)
    if not games:
        # Cache miss is normal during the offseason or before the
        # game-odds sweep ticks; we don't burn an extra HTTP call here.
        log.debug("no cached wnba games — skipping props poll")
        return 0
    written = 0
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        for g in games:
            event_id = str(g.get("id") or "")
            if not event_id:
                continue
            payloads = await _fetch_event_props(client, event_id, api_key)
            for payload in payloads:
                rows = _flatten_quotes(payload)
                written += await _persist_quotes(rows)
    return written


def register_wnba_player_props_feed() -> None:
    if registry.status_of("wnba_player_props"):
        return
    api_key = _api_key()
    cadence = float(os.environ.get(
        "AXIOM_WNBA_PROPS_CADENCE_SECS", _DEFAULT_CADENCE_SECS
    ))
    registry.register(Feed(
        name="wnba_player_props",
        cadence_seconds=cadence,
        poll=_poll_wnba_player_props,
        enabled=bool(api_key),
        skipped_reason=None if api_key else "THE_ODDS_API_KEY not set",
    ))
