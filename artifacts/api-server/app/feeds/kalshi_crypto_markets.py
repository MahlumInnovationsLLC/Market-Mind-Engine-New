"""Discover live Kalshi BTC/ETH binary markets and feed them into the
crypto-binary trading cycle.

The crypto-binary vertical (registered in :mod:`app.verticals.builtins`)
is wired to the specialist OHLCV ensemble in
:mod:`app.crypto.predictor`, but :func:`app.orchestrator.cycle.run_cycle`
pulls candidate rows from the ``predictions`` table — and nothing
populates it for crypto markets out of the box.  Without a producer the
cycle finds nothing to evaluate, so the new predictor never gets a
chance to fire.

This poller closes that gap:

  * Calls Kalshi's market index (``GET /markets``) once per supported
    series prefix (``KXBTC``, ``KXETH``).
  * Filters to live binary close-above / close-below markets that the
    crypto ticker parser (:mod:`app.crypto.tickers`) accepts — anything
    with a different shape (range bins, hourly compounds, etc.) is
    skipped so the predictor only sees markets it can price.
  * Upserts a row per market into ``predictions`` keyed to the Kalshi
    ticker, with ``league = 'CRYPTO'`` so the existing
    ``_candidate_predictions`` SELECT for the ``crypto-binary`` vertical
    picks them up.

The feed registers itself as ``enabled=False`` (with a clear
``skipped_reason``) when Kalshi credentials are missing, matching how
the other API-keyed feeds advertise themselves on the operator console.

Public surface:
    register_kalshi_crypto_markets_feed() — wired into app.main on startup
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Iterable

from .. import db
from ..config import get_settings
from ..crypto.tickers import (
    InvalidCryptoTicker,
    SUPPORTED_SYMBOLS,
    parse_crypto_ticker,
)
from ..kalshi import client as kalshi_client_mod
from .registry import Feed, registry


log = logging.getLogger("axiom.feeds.kalshi_crypto_markets")


# Kalshi's per-page cap is 200; we walk a handful of pages so a large
# event burst (e.g. a daily contract refresh) can't silently drop
# tickers off the end.
_PAGE_LIMIT: int = 200
_MAX_PAGES: int = 5
_DEFAULT_CADENCE_SECS: float = 60.0
_LEAGUE: str = "CRYPTO"

# Series prefixes mapped from the supported base symbols. Kalshi groups
# every BTC binary under ``KXBTC*`` (5m / 15m / 1h / daily variants
# share the same series_ticker for the index call), and likewise for
# ETH.  We hit one series_ticker per symbol so the venue-side filter
# does the heavy lifting.
def _series_prefixes() -> tuple[str, ...]:
    return tuple(f"KX{sym}" for sym in SUPPORTED_SYMBOLS)


def _parse_close_time(raw: Any) -> datetime | None:
    """Coerce Kalshi's ISO-8601 close timestamp to an aware datetime.

    Kalshi historically emits ``...Z`` and ``...+00:00`` interchangeably;
    a missing or unparseable value comes back as ``None`` so the
    candidate row still upserts (the cycle treats a NULL ``game_time``
    as "no kickoff filter").
    """
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


def _mid_price_dollars(market: dict[str, Any]) -> float | None:
    """Best-effort YES mid-price in dollars (0..1).

    Kalshi exposes ``yes_bid`` / ``yes_ask`` in cents (0..100). When both
    sides are present we take the midpoint; otherwise we fall back to
    ``last_price`` / ``yes_ask`` / ``yes_bid`` so a freshly-listed market
    with one-sided liquidity still gets a price hint to seed the cycle.
    """
    bid = market.get("yes_bid")
    ask = market.get("yes_ask")
    last = market.get("last_price")

    def _to_cents(v: Any) -> float | None:
        if v is None:
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        if f < 0:
            return None
        return f

    bid_c = _to_cents(bid)
    ask_c = _to_cents(ask)
    last_c = _to_cents(last)
    if bid_c is not None and ask_c is not None and ask_c >= bid_c:
        return ((bid_c + ask_c) / 2.0) / 100.0
    if last_c is not None:
        return last_c / 100.0
    if ask_c is not None:
        return ask_c / 100.0
    if bid_c is not None:
        return bid_c / 100.0
    return None


def _candidate_from_market(market: dict[str, Any]) -> dict[str, Any] | None:
    """Build the row payload for a single Kalshi crypto market.

    Returns ``None`` when the ticker isn't a parseable crypto-binary
    contract — e.g. a range-binned market or a malformed shape — so the
    upsert loop can skip it without surfacing an error to the operator.
    """
    ticker = (market.get("ticker") or market.get("market_ticker") or "").strip()
    if not ticker:
        return None
    try:
        parsed = parse_crypto_ticker(ticker)
    except InvalidCryptoTicker:
        return None

    close_time = _parse_close_time(
        market.get("close_time") or market.get("expected_expiration_time")
    )
    price = _mid_price_dollars(market)

    # ``game_id`` doubles as the canonical event identifier downstream
    # (settlement, swarm debate dedupe, etc).  Re-using the Kalshi
    # ticker keeps the producer trivially idempotent — every cycle
    # collapses onto the same row via the unique index on
    # ``kalshi_market_id``.
    return {
        "game_id": ticker,
        "league": _LEAGUE,
        "kalshi_market_id": ticker,
        "kalshi_price": price,
        "game_time": close_time,
        "game_context": {
            "kalshi_market_id": ticker,
            "kalshi_price": price,
            "symbol": parsed.symbol,
            "timeframe": parsed.timeframe,
            "threshold": parsed.threshold,
            "direction": parsed.direction,
            "title": market.get("title"),
            "status": market.get("status"),
            "close_time": close_time.isoformat() if close_time else None,
            "yes_bid": market.get("yes_bid"),
            "yes_ask": market.get("yes_ask"),
            "last_price": market.get("last_price"),
            "source": "kalshi_crypto_markets",
        },
    }


# --- Persistence -------------------------------------------------------------


_UPSERT_SQL = """
INSERT INTO predictions
       (game_id, league, kalshi_market_id, kalshi_price, game_time, game_context)
VALUES ($1, $2, $3, $4, $5, $6::jsonb)
ON CONFLICT (kalshi_market_id) WHERE kalshi_market_id IS NOT NULL
DO UPDATE SET
    league       = EXCLUDED.league,
    kalshi_price = EXCLUDED.kalshi_price,
    game_time    = EXCLUDED.game_time,
    game_context = EXCLUDED.game_context,
    updated_at   = now();
"""


async def _upsert_candidates(rows: Iterable[dict[str, Any]]) -> int:
    """Idempotent batched upsert into ``predictions``.  Returns rows touched.

    Each row is its own statement so a single malformed ticker can't
    take the rest of the page down with it — the cycle is far better
    off with N-1 valid candidates than zero.
    """
    import json as _json

    n = 0
    for row in rows:
        try:
            await db.execute(
                _UPSERT_SQL,
                row["game_id"],
                row["league"],
                row["kalshi_market_id"],
                row.get("kalshi_price"),
                row.get("game_time"),
                _json.dumps(row.get("game_context") or {}),
            )
            n += 1
        except Exception as e:  # noqa: BLE001
            log.warning(
                "predictions upsert failed for %s: %s",
                row.get("kalshi_market_id"),
                e,
            )
    return n


# --- Polling -----------------------------------------------------------------


async def _list_series_markets(
    client: kalshi_client_mod.KalshiClient, series: str
) -> list[dict[str, Any]]:
    """Walk every page of open markets under ``series`` (``KXBTC`` etc.)."""
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(_MAX_PAGES):
        try:
            page = await client.list_markets(
                limit=_PAGE_LIMIT,
                status="open",
                series_ticker=series,
                cursor=cursor,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("kalshi list_markets failed for %s: %s", series, e)
            break
        body = page or {}
        markets = body.get("markets") or []
        if not isinstance(markets, list):
            markets = []
        out.extend(m for m in markets if isinstance(m, dict))
        cursor = body.get("cursor") or body.get("next_cursor")
        if not cursor:
            break
    return out


async def poll_kalshi_crypto_markets() -> int:
    """One poll: fetch every live BTC/ETH binary market and upsert.

    Returns the number of ``predictions`` rows touched so the feed
    registry can surface the count on the operator console.  Soft-fails
    when Kalshi credentials aren't configured (returns 0) — the
    registration code already advertises the feed as skipped in that
    case so the operator sees a clear ``skipped_reason``.
    """
    settings = get_settings()
    if not (settings.kalshi_api_key_id and settings.kalshi_private_key):
        return 0

    rows: list[dict[str, Any]] = []
    try:
        async with kalshi_client_mod.KalshiClient(settings=settings) as client:
            for series in _series_prefixes():
                markets = await _list_series_markets(client, series)
                for m in markets:
                    cand = _candidate_from_market(m)
                    if cand is not None:
                        rows.append(cand)
    except Exception as e:  # noqa: BLE001
        log.warning("kalshi_crypto_markets poll crashed: %s", e)
        return 0

    if not rows:
        return 0
    return await _upsert_candidates(rows)


# --- Registration ------------------------------------------------------------


def _cadence_seconds() -> float:
    raw = os.environ.get("AXIOM_KALSHI_CRYPTO_MARKETS_CADENCE_SECS")
    if not raw:
        return _DEFAULT_CADENCE_SECS
    try:
        v = float(raw)
    except ValueError:
        return _DEFAULT_CADENCE_SECS
    return max(15.0, v)


def register_kalshi_crypto_markets_feed() -> None:
    """Register the BTC/ETH market discovery poller with the global
    feed registry.

    Idempotent so a hot reload doesn't crash the lifespan; advertises a
    clear ``skipped_reason`` when Kalshi credentials are missing so the
    operator console can render the feed as ``enabled=false`` rather
    than silently swallowing the registration.
    """
    name = "kalshi_crypto_markets"
    if registry.status_of(name):
        return
    settings = get_settings()
    has_creds = bool(
        settings.kalshi_api_key_id and settings.kalshi_private_key
    )
    skipped_reason = (
        None if has_creds
        else "KALSHI_API_KEY_ID / KALSHI_API_PRIVATE_KEY not set"
    )
    registry.register(Feed(
        name=name,
        cadence_seconds=_cadence_seconds(),
        poll=poll_kalshi_crypto_markets,
        enabled=has_creds,
        skipped_reason=skipped_reason,
    ))
    log.info(
        "kalshi crypto markets feed registered (enabled=%s, cadence=%.1fs)",
        has_creds,
        _cadence_seconds(),
    )
