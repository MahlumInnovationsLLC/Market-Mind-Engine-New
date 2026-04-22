"""Public-betting consensus feed (BettingPros + SportsLine).

We pull the percentage of public *tickets* and public *money* on each
side of a market so the orchestrator can fade overwhelming public
sentiment when the model disagrees.  Two providers are wired:

  * **BettingPros** — JSON endpoints (lower TOS risk than HTML scraping).
    The public-bets endpoint returns per-market consensus across
    sportsbooks; we record one row per (sport, game_id, market_type, side).
  * **SportsLine** — HTML page scraping is rate-limited and brittle;
    instead we hit their JSON sidebar endpoint when configured via
    ``SPORTSLINE_API_KEY``.  Used as a *fallback* — it only polls if
    BettingPros has no data for a given sport in the last hour.

Both providers write into the same `public_betting_consensus` table.

Sports targeted: NFL, NBA, MLB, NHL, NCAAB, NCAAF (the same set the
Sportradar + Odds API feeds cover).  Operators can override via
``AXIOM_PUBLIC_BETTING_SPORTS``.
"""
from __future__ import annotations

import json as _json
import logging
import os
import time
from typing import Any

import httpx

from .. import db
from .odds_api import _retry_after_seconds
from .registry import Feed, registry
from .scheduler import RateLimited


log = logging.getLogger("axiom.feeds.public_betting")


BETTINGPROS_BASE = os.environ.get("AXIOM_BETTINGPROS_BASE_URL") or "https://api.bettingpros.com"
SPORTSLINE_BASE = os.environ.get("AXIOM_SPORTSLINE_BASE_URL") or "https://www.sportsline.com"

DEFAULT_SPORTS = [
    s.strip().lower()
    for s in (os.environ.get("AXIOM_PUBLIC_BETTING_SPORTS") or "nfl,nba,mlb,nhl,ncaab,ncaaf").split(",")
    if s.strip()
]
DEFAULT_CADENCE = max(60, int(os.environ.get("AXIOM_PUBLIC_BETTING_CADENCE") or "300"))
SHARP_DISAGREEMENT_THRESHOLD = float(
    os.environ.get("AXIOM_PUBLIC_BETTING_SHARP_THRESHOLD") or "15"
)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


async def _persist_consensus(rows: list[tuple]) -> int:
    if not rows:
        return 0
    try:
        async with db.acquire() as conn, conn.transaction():
            await conn.executemany(
                """
                INSERT INTO public_betting_consensus
                  (source, sport, game_id, market_type, side,
                   public_tickets_pct, public_money_pct, sharp_disagreement,
                   home_team, away_team, payload, fetched_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, NOW())
                ON CONFLICT (source, sport, game_id, market_type, side) DO UPDATE SET
                  public_tickets_pct = EXCLUDED.public_tickets_pct,
                  public_money_pct   = EXCLUDED.public_money_pct,
                  sharp_disagreement = EXCLUDED.sharp_disagreement,
                  home_team          = EXCLUDED.home_team,
                  away_team          = EXCLUDED.away_team,
                  payload            = EXCLUDED.payload,
                  fetched_at         = NOW()
                """,
                rows,
            )
    except Exception as e:
        log.warning("public_betting_consensus upsert failed: %s", e)
        raise
    return len(rows)


def _to_pct(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # Normalise 0..1 floats up to 0..100 so every row is comparable.
    if 0.0 <= f <= 1.0:
        f *= 100.0
    return round(f, 2)


def _is_sharp_disagreement(tickets: float | None, money: float | None) -> bool:
    """Classic sharp-vs-public split: tickets favour one side strongly while
    money favours the other.  Threshold is configurable."""
    if tickets is None or money is None:
        return False
    return abs(tickets - money) >= SHARP_DISAGREEMENT_THRESHOLD


# ---------------------------------------------------------------------------
# BettingPros
# ---------------------------------------------------------------------------


_BETTINGPROS_MARKET_IDS = {
    # BettingPros uses numeric market_id codes.  Map our canonical names
    # to the codes for the markets we care about.  These match the public
    # docs at the time of writing; operators can override via env if BP
    # rotates them.
    "h2h":     int(os.environ.get("AXIOM_BETTINGPROS_MARKET_H2H") or "1"),
    "spread":  int(os.environ.get("AXIOM_BETTINGPROS_MARKET_SPREAD") or "3"),
    "total":   int(os.environ.get("AXIOM_BETTINGPROS_MARKET_TOTAL") or "2"),
}


async def _bp_get(path: str, params: dict, api_key: str | None) -> Any:
    headers = {
        "Accept": "application/json",
        "User-Agent": "axiom-predict/1.0 (+feeds.public_betting)",
    }
    if api_key:
        headers["x-api-key"] = api_key
    timeout = httpx.Timeout(15.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(f"{BETTINGPROS_BASE}{path}", headers=headers, params=params)
    if r.status_code in (401, 403):
        raise RuntimeError(f"bettingpros auth rejected ({r.status_code})")
    if r.status_code == 429:
        raise RateLimited("bettingpros", retry_after=_retry_after_seconds(r) or 60.0)
    if r.status_code == 404:
        return None
    if r.status_code >= 400:
        raise RuntimeError(f"bettingpros HTTP {r.status_code}: {r.text[:200]}")
    try:
        return r.json()
    except Exception:
        return None


def _normalise_bp_offer(
    sport: str, market_label: str, offer: dict[str, Any]
) -> list[tuple]:
    """Flatten a BettingPros offer into one consensus row per side."""
    rows: list[tuple] = []
    event = offer.get("event") or {}
    game_id = str(
        offer.get("event_id")
        or event.get("id")
        or event.get("event_id")
        or offer.get("id")
        or ""
    )
    if not game_id:
        return rows
    home = str(event.get("home") or event.get("home_team") or "")
    away = str(event.get("away") or event.get("away_team") or "")
    selections = (
        offer.get("selections")
        or offer.get("public_bets")
        or offer.get("bets")
        or offer.get("sides")
        or []
    )
    for sel in selections:
        if not isinstance(sel, dict):
            continue
        side = str(
            sel.get("label")
            or sel.get("name")
            or sel.get("side")
            or sel.get("selection")
            or ""
        ).lower()
        if not side:
            continue
        # Normalise the labels we care about.
        if side in ("home", "away", "over", "under"):
            normalised = side
        elif home and home.lower() in side:
            normalised = "home"
        elif away and away.lower() in side:
            normalised = "away"
        elif "over" in side:
            normalised = "over"
        elif "under" in side:
            normalised = "under"
        else:
            normalised = side[:32]
        tickets = _to_pct(sel.get("public_tickets") or sel.get("tickets") or sel.get("bets_pct"))
        money = _to_pct(sel.get("public_money") or sel.get("money") or sel.get("handle_pct"))
        rows.append((
            "bettingpros",
            sport,
            game_id,
            market_label,
            normalised,
            tickets,
            money,
            _is_sharp_disagreement(tickets, money),
            home or None,
            away or None,
            _json.dumps({"offer": offer, "selection": sel}, default=str),
        ))
    return rows


async def _poll_bettingpros() -> int:
    api_key = os.environ.get("BETTINGPROS_API_KEY") or os.environ.get("AXIOM_BETTINGPROS_API_KEY")
    written = 0
    for sport in DEFAULT_SPORTS:
        for market_label, market_id in _BETTINGPROS_MARKET_IDS.items():
            try:
                payload = await _bp_get(
                    "/v3/public-bets",
                    {"sport": sport.upper(), "market_id": market_id},
                    api_key,
                )
            except RateLimited:
                raise
            except Exception as e:
                log.warning(
                    "bettingpros fetch failed sport=%s market=%s: %s", sport, market_label, e
                )
                continue
            if not payload:
                continue
            offers = (
                payload.get("offers")
                or payload.get("public_bets")
                or payload.get("data")
                or []
            )
            if isinstance(offers, dict):
                offers = [offers]
            rows: list[tuple] = []
            for offer in offers:
                if isinstance(offer, dict):
                    rows.extend(_normalise_bp_offer(sport, market_label, offer))
            written += await _persist_consensus(rows)
            _PROVIDER_LAST_OK["bettingpros"][sport] = time.time()
    return written


# ---------------------------------------------------------------------------
# SportsLine fallback
# ---------------------------------------------------------------------------


# Per-sport last-success cache so the fallback can decide whether
# BettingPros is fresh enough.  Wall-clock seconds.
_PROVIDER_LAST_OK: dict[str, dict[str, float]] = {
    "bettingpros": {},
    "sportsline": {},
}
SPORTSLINE_FALLBACK_AFTER_SECONDS = max(
    60, int(os.environ.get("AXIOM_SPORTSLINE_FALLBACK_AFTER_SECONDS") or "3600")
)


def _bettingpros_is_stale_for(sport: str) -> bool:
    last = _PROVIDER_LAST_OK["bettingpros"].get(sport)
    if last is None:
        return True
    return (time.time() - last) > SPORTSLINE_FALLBACK_AFTER_SECONDS


async def _sl_get(path: str, params: dict, api_key: str) -> Any:
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "axiom-predict/1.0 (+feeds.public_betting)",
    }
    timeout = httpx.Timeout(15.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(f"{SPORTSLINE_BASE}{path}", headers=headers, params=params)
    if r.status_code in (401, 403):
        raise RuntimeError(f"sportsline auth rejected ({r.status_code})")
    if r.status_code == 429:
        raise RateLimited("sportsline", retry_after=_retry_after_seconds(r) or 60.0)
    if r.status_code == 404:
        return None
    if r.status_code >= 400:
        raise RuntimeError(f"sportsline HTTP {r.status_code}: {r.text[:200]}")
    try:
        return r.json()
    except Exception:
        return None


def _normalise_sl_game(sport: str, game: dict[str, Any]) -> list[tuple]:
    rows: list[tuple] = []
    game_id = str(game.get("game_id") or game.get("id") or "")
    if not game_id:
        return rows
    home = str(game.get("home_team") or game.get("home") or "")
    away = str(game.get("away_team") or game.get("away") or "")
    consensus = game.get("consensus") or game.get("public") or {}
    if not isinstance(consensus, dict):
        return rows
    # SportsLine groups by market type at the leaf.
    for market_label in ("h2h", "spread", "total"):
        block = consensus.get(market_label) or consensus.get(market_label.upper()) or {}
        if not isinstance(block, dict):
            continue
        for side in ("home", "away", "over", "under"):
            sel = block.get(side)
            if not isinstance(sel, dict):
                continue
            tickets = _to_pct(sel.get("tickets_pct") or sel.get("public_pct"))
            money = _to_pct(sel.get("money_pct") or sel.get("handle_pct"))
            rows.append((
                "sportsline",
                sport,
                game_id,
                market_label,
                side,
                tickets,
                money,
                _is_sharp_disagreement(tickets, money),
                home or None,
                away or None,
                _json.dumps({"game": game, "selection": sel}, default=str),
            ))
    return rows


async def _poll_sportsline() -> int:
    api_key = os.environ.get("SPORTSLINE_API_KEY") or os.environ.get("AXIOM_SPORTSLINE_API_KEY")
    if not api_key:
        raise RuntimeError("SPORTSLINE_API_KEY not set")
    written = 0
    for sport in DEFAULT_SPORTS:
        # Only spend a SportsLine quota call when BettingPros is stale for
        # this sport.  Operators can shorten the freshness window via env.
        if not _bettingpros_is_stale_for(sport):
            continue
        try:
            payload = await _sl_get(
                "/sports-api/v1/public-consensus", {"sport": sport.upper()}, api_key,
            )
        except RateLimited:
            raise
        except Exception as e:
            log.warning("sportsline fetch failed sport=%s: %s", sport, e)
            continue
        if not payload:
            continue
        games = payload.get("games") or payload.get("data") or []
        if isinstance(games, dict):
            games = [games]
        rows: list[tuple] = []
        for game in games:
            if isinstance(game, dict):
                rows.extend(_normalise_sl_game(sport, game))
        written += await _persist_consensus(rows)
        if rows:
            _PROVIDER_LAST_OK["sportsline"][sport] = time.time()
    return written


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_public_betting_feeds() -> None:
    """Register BettingPros (primary) + SportsLine (fallback) pollers.

    BettingPros runs with or without an API key — its public-bets endpoint
    is reachable anonymously; an `x-api-key` is forwarded only if set.
    SportsLine requires `SPORTSLINE_API_KEY` and is otherwise registered
    as disabled with a clear skipped_reason.
    """
    if registry.status_of("public_betting_bettingpros") is None:
        registry.register(Feed(
            name="public_betting_bettingpros",
            cadence_seconds=float(DEFAULT_CADENCE),
            poll=_poll_bettingpros,
            enabled=True,
            skipped_reason=None,
        ))

    sl_key = os.environ.get("SPORTSLINE_API_KEY") or os.environ.get("AXIOM_SPORTSLINE_API_KEY")
    if registry.status_of("public_betting_sportsline") is None:
        registry.register(Feed(
            name="public_betting_sportsline",
            cadence_seconds=float(DEFAULT_CADENCE * 2),
            poll=_poll_sportsline,
            enabled=bool(sl_key),
            skipped_reason=None if sl_key else "SPORTSLINE_API_KEY not set",
        ))
