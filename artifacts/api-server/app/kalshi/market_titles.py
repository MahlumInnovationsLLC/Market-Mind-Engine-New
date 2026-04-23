"""Resolve Kalshi market tickers to human-readable titles.

Tickers like ``KXBTC-24DEC12-50000`` are opaque codes. Kalshi's REST
``/markets/{ticker}`` endpoint returns a ``title`` field (e.g. "Will
Bitcoin close above $50,000 on Dec 12?") that we surface across the
operator console and mobile app.

This module provides :func:`resolve_titles` which:

  * Returns immediately from a process-local hot dict for known tickers.
  * Then checks the persistent ``market_title_cache`` table.
  * Finally calls Kalshi's API for any still-missing tickers, persists
    the results, and primes the hot cache.

Failures are swallowed — callers always get a ``{ticker: title|None}``
dict so the UI can fall back to the raw ticker.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Iterable

from .. import db
from ..config import get_settings
from . import auth as kalshi_auth
from .client import KalshiAPIError, KalshiClient


log = logging.getLogger(__name__)


_HOT_CACHE: dict[str, str] = {}
_HOT_LOCK = asyncio.Lock()


def _clean_tickers(tickers: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for t in tickers:
        if not t:
            continue
        s = str(t).strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


async def _load_from_db(tickers: list[str]) -> dict[str, str]:
    if not tickers:
        return {}
    try:
        rows = await db.fetch(
            "SELECT ticker, title FROM market_title_cache WHERE ticker = ANY($1::text[])",
            tickers,
        )
        return {r["ticker"]: r["title"] for r in rows if r["title"]}
    except Exception as e:
        log.warning("market_title_cache read failed: %s", e)
        return {}


async def _persist(ticker: str, title: str) -> None:
    try:
        await db.execute(
            """
            INSERT INTO market_title_cache (ticker, title, fetched_at)
            VALUES ($1, $2, now())
            ON CONFLICT (ticker) DO UPDATE
              SET title = EXCLUDED.title,
                  fetched_at = EXCLUDED.fetched_at
            """,
            ticker,
            title,
        )
    except Exception as e:
        log.warning("market_title_cache write failed for %s: %s", ticker, e)


async def _backfill_trades(ticker: str, title: str) -> None:
    """Populate trades.market_title for any rows that already had this ticker."""
    try:
        await db.execute(
            """
            UPDATE trades
               SET market_title = $2
             WHERE market_id = $1
               AND (market_title IS NULL OR market_title = '')
            """,
            ticker,
            title,
        )
    except Exception as e:
        log.warning("trades.market_title backfill failed for %s: %s", ticker, e)


async def _fetch_from_kalshi(ticker: str) -> str | None:
    settings = get_settings()
    if not settings.kalshi_api_key_id or not settings.kalshi_private_key:
        return None
    try:
        async with KalshiClient(settings=settings, timeout=8.0) as c:
            data = await c.get_market(ticker)
    except KalshiAPIError as e:
        log.info("kalshi get_market(%s) failed: http %s", ticker, e.status)
        return None
    except kalshi_auth.KalshiAuthError as e:
        log.info("kalshi get_market(%s) auth error: %s", ticker, e)
        return None
    except Exception as e:
        log.info("kalshi get_market(%s) error: %s", ticker, e)
        return None
    market = data.get("market") if isinstance(data, dict) else None
    if isinstance(market, dict):
        title = market.get("title") or market.get("subtitle")
    else:
        title = data.get("title") if isinstance(data, dict) else None
    if isinstance(title, str) and title.strip():
        return title.strip()
    return None


async def resolve_titles(tickers: Iterable[str]) -> dict[str, str]:
    """Return ``{ticker: title}`` for every resolvable ticker.

    Tickers that can't be resolved are simply omitted from the result;
    callers should fall back to the raw ticker for display.
    """
    cleaned = _clean_tickers(tickers)
    if not cleaned:
        return {}

    out: dict[str, str] = {}
    missing: list[str] = []
    for t in cleaned:
        if t in _HOT_CACHE:
            out[t] = _HOT_CACHE[t]
        else:
            missing.append(t)

    if not missing:
        return out

    db_hits = await _load_from_db(missing)
    for t, title in db_hits.items():
        out[t] = title
        _HOT_CACHE[t] = title
    still_missing = [t for t in missing if t not in db_hits]
    if not still_missing:
        return out

    # Fetch from Kalshi sequentially — small batches, and we want to
    # avoid hammering the rate limit when a cycle drops 25 tickers at once.
    for ticker in still_missing:
        title = await _fetch_from_kalshi(ticker)
        if not title:
            continue
        out[ticker] = title
        _HOT_CACHE[ticker] = title
        await _persist(ticker, title)
        await _backfill_trades(ticker, title)

    return out


async def get_title(ticker: str) -> str | None:
    """Single-ticker convenience wrapper."""
    if not ticker:
        return None
    res = await resolve_titles([ticker])
    return res.get(ticker)
