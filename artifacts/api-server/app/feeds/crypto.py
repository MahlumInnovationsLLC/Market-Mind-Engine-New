"""Crypto OHLCV pollers (CoinAPI primary, CoinGecko fallback).

The crypto-binary vertical (BTC/ETH hourly markets) needs continuous
1m / 5m / 15m / 1h candles.  Per the spec we hit CoinAPI first and fall
back to CoinGecko whenever CoinAPI is unkeyed, rate-limited, or
returns an error.  Both providers feed the same `crypto_ohlcv` table;
the (symbol, granularity, ts) unique index makes overlapping polls
idempotent.

Public surface:
    register_crypto_feeds()   — wired into app.main on startup
    backfill(days=90)         — invoked by scripts/backfill_crypto.py
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import httpx

from .. import db
from .registry import Feed, registry
from .scheduler import RateLimited


log = logging.getLogger("axiom.feeds.crypto")


# --- Configuration -----------------------------------------------------------

DEFAULT_SYMBOLS: tuple[str, ...] = tuple(
    s.strip().upper()
    for s in (os.environ.get("AXIOM_CRYPTO_SYMBOLS") or "BTC,ETH").split(",")
    if s.strip()
)

# Map our internal granularity to provider-specific identifiers + cadence.
# Cadence is how often we poll the latest bar(s); a 1m bar refreshes every
# ~30s, a 1h bar every 5min.  The table's unique index dedupes the
# overlapping rewrites of the in-progress bar.
GRANULARITIES: dict[str, dict[str, Any]] = {
    "1m":  {"coinapi": "1MIN",  "coingecko_days": 1,   "cadence": 30.0,  "seconds": 60},
    "5m":  {"coinapi": "5MIN",  "coingecko_days": 1,   "cadence": 60.0,  "seconds": 5 * 60},
    "15m": {"coinapi": "15MIN", "coingecko_days": 7,   "cadence": 120.0, "seconds": 15 * 60},
    "1h":  {"coinapi": "1HRS",  "coingecko_days": 30,  "cadence": 300.0, "seconds": 60 * 60},
}

COINAPI_BASE = "https://rest.coinapi.io/v1"
COINGECKO_BASE = "https://api.coingecko.com/api/v3"

# CoinGecko OHLC endpoint reports 4-hour candles for `days >= 31` and
# 30-min candles for `2 <= days <= 30`.  When falling back at finer
# granularities we pick the smallest `days` window that still covers a
# few of our bars; the unique index handles overlapping coverage.
_COINGECKO_COIN_ID = {"BTC": "bitcoin", "ETH": "ethereum"}


# --- HTTP helpers ------------------------------------------------------------


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("retry-after")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        from email.utils import parsedate_to_datetime
        try:
            dt = parsedate_to_datetime(raw)
            return max(1.0, (dt - datetime.now(tz=timezone.utc)).total_seconds())
        except Exception:
            return None


# --- CoinAPI -----------------------------------------------------------------


def _coinapi_symbol_id(symbol: str) -> str:
    """CoinAPI's "spot exchange rate" endpoint takes the bare asset id."""
    return symbol.upper()


async def _fetch_coinapi(
    client: httpx.AsyncClient,
    api_key: str,
    symbol: str,
    granularity: str,
    *,
    limit: int = 100,
    time_start: datetime | None = None,
    time_end: datetime | None = None,
) -> list[dict[str, Any]]:
    """Pull OHLCV history for the BTC|ETH/USD spot rate from CoinAPI."""
    period = GRANULARITIES[granularity]["coinapi"]
    asset = _coinapi_symbol_id(symbol)
    # The spot-rate history endpoint synthesises an OHLCV view across
    # exchanges so we don't have to pick a single venue.
    url = f"{COINAPI_BASE}/exchangerate/{asset}/USD/history"
    params: dict[str, Any] = {"period_id": period, "limit": str(limit)}
    if time_start is not None:
        params["time_start"] = time_start.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if time_end is not None:
        params["time_end"] = time_end.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    headers = {"X-CoinAPI-Key": api_key, "Accept": "application/json"}
    r = await client.get(url, params=params, headers=headers)
    if r.status_code in (401, 403):
        raise RuntimeError(f"coinapi auth rejected for {symbol}/{granularity}")
    if r.status_code == 429:
        raise RateLimited("coinapi", retry_after=_retry_after_seconds(r) or 60.0)
    if r.status_code >= 400:
        raise RuntimeError(f"coinapi {symbol}/{granularity} HTTP {r.status_code}: {r.text[:200]}")
    payload = r.json() or []
    rows: list[dict[str, Any]] = []
    for bar in payload:
        ts_raw = bar.get("time_period_start") or bar.get("time_open")
        if not ts_raw:
            continue
        try:
            ts = _parse_iso(ts_raw)
        except Exception:
            continue
        rows.append({
            "ts": ts,
            "o": float(bar.get("rate_open") or bar.get("price_open") or 0.0),
            "h": float(bar.get("rate_high") or bar.get("price_high") or 0.0),
            "l": float(bar.get("rate_low")  or bar.get("price_low")  or 0.0),
            "c": float(bar.get("rate_close") or bar.get("price_close") or 0.0),
            # Spot-rate aggregates have no notional volume; default to 0.
            "v": float(bar.get("volume_traded") or 0.0),
        })
    return rows


# --- CoinGecko fallback ------------------------------------------------------


async def _fetch_coingecko(
    client: httpx.AsyncClient,
    api_key: str | None,
    symbol: str,
    granularity: str,
    *,
    days: int | None = None,
) -> list[dict[str, Any]]:
    """Pull OHLC bars from CoinGecko (volume queried separately)."""
    coin_id = _COINGECKO_COIN_ID.get(symbol.upper())
    if coin_id is None:
        raise RuntimeError(f"coingecko: no coin_id mapping for {symbol}")
    window_days = days or GRANULARITIES[granularity]["coingecko_days"]
    headers: dict[str, str] = {"Accept": "application/json"}
    if api_key:
        headers["x-cg-pro-api-key"] = api_key

    ohlc_url = f"{COINGECKO_BASE}/coins/{coin_id}/ohlc"
    r = await client.get(
        ohlc_url, params={"vs_currency": "usd", "days": str(window_days)}, headers=headers
    )
    if r.status_code == 429:
        raise RateLimited("coingecko", retry_after=_retry_after_seconds(r) or 60.0)
    if r.status_code >= 400:
        raise RuntimeError(f"coingecko {symbol}/{granularity} HTTP {r.status_code}: {r.text[:200]}")
    raw = r.json() or []

    bucket_seconds = GRANULARITIES[granularity]["seconds"]
    rows: list[dict[str, Any]] = []
    for bar in raw:
        if not isinstance(bar, list) or len(bar) < 5:
            continue
        ts_ms, o, h, l, c = bar[0], bar[1], bar[2], bar[3], bar[4]
        ts = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
        # Snap to the requested granularity boundary so multiple polls
        # collapse onto the same row via the unique index.
        epoch = int(ts.timestamp())
        snapped = epoch - (epoch % bucket_seconds)
        rows.append({
            "ts": datetime.fromtimestamp(snapped, tz=timezone.utc),
            "o": float(o), "h": float(h), "l": float(l), "c": float(c),
            "v": 0.0,
        })
    return rows


# --- Persistence -------------------------------------------------------------


async def _persist(
    symbol: str, granularity: str, source: str, rows: Iterable[dict[str, Any]]
) -> int:
    bars = list(rows)
    if not bars:
        return 0
    payload = [
        (symbol, granularity, b["ts"], b["o"], b["h"], b["l"], b["c"], b["v"], source)
        for b in bars
    ]
    async with db.acquire() as conn, conn.transaction():
        # ON CONFLICT updates so the in-progress bar's high/low/close get
        # refreshed each tick, and a fallback poll can correct a stale
        # primary-source row.
        await conn.executemany(
            """
            INSERT INTO crypto_ohlcv
              (symbol, granularity, ts, o, h, l, c, v, source)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (symbol, granularity, ts) DO UPDATE SET
              o = EXCLUDED.o,
              h = EXCLUDED.h,
              l = EXCLUDED.l,
              c = EXCLUDED.c,
              v = EXCLUDED.v,
              source = EXCLUDED.source
            """,
            payload,
        )
    return len(payload)


def _parse_iso(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).astimezone(timezone.utc)


# --- Poll function -----------------------------------------------------------


async def _poll(symbol: str, granularity: str) -> int:
    """Poll one (symbol, granularity) pair, primary then fallback."""
    coinapi_key = os.environ.get("COINAPI_API_KEY") or os.environ.get("COINAPI_KEY")
    coingecko_key = os.environ.get("COINGECKO_API_KEY")

    timeout = httpx.Timeout(15.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        # --- Primary: CoinAPI -------------------------------------------
        if coinapi_key:
            try:
                rows = await _fetch_coinapi(
                    client, coinapi_key, symbol, granularity, limit=10
                )
                return await _persist(symbol, granularity, "coinapi", rows)
            except RateLimited:
                # Rate-limited primary still falls through to fallback so
                # the table stays warm; the scheduler will record the
                # backoff against the per-feed status.
                log.warning("coinapi rate-limited %s/%s, falling back", symbol, granularity)
            except Exception as e:
                log.warning("coinapi fetch failed %s/%s: %s — trying fallback", symbol, granularity, e)

        # --- Fallback: CoinGecko ---------------------------------------
        try:
            rows = await _fetch_coingecko(client, coingecko_key, symbol, granularity)
            return await _persist(symbol, granularity, "coingecko", rows)
        except RateLimited:
            raise
        except Exception as e:
            # Both primary and fallback have failed.  Surface to the
            # scheduler so the operator sees a non-stale error.
            raise RuntimeError(f"crypto {symbol}/{granularity}: all providers failed: {e}")


# --- Backfill ----------------------------------------------------------------


async def backfill(days: int = 90, symbols: Iterable[str] | None = None) -> dict[str, int]:
    """One-shot loader for the historical 90-day prime.

    Walks each (symbol, granularity) pair in fixed-size pages so we stay
    under provider page limits.  Returns a per-feed row-count summary
    suitable for printing from the CLI.
    """
    coinapi_key = os.environ.get("COINAPI_API_KEY") or os.environ.get("COINAPI_KEY")
    coingecko_key = os.environ.get("COINGECKO_API_KEY")
    if not coinapi_key and not coingecko_key:
        raise RuntimeError(
            "backfill requires COINAPI_API_KEY or COINGECKO_API_KEY in the environment"
        )

    targets = [s.upper() for s in (symbols or DEFAULT_SYMBOLS)]
    summary: dict[str, int] = {}
    end = datetime.now(tz=timezone.utc).replace(microsecond=0)
    start = end - timedelta(days=days)

    timeout = httpx.Timeout(30.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for symbol in targets:
            for granularity, meta in GRANULARITIES.items():
                key = f"{symbol}/{granularity}"
                written = 0
                # CoinAPI's spot-rate history caps at 100 rows per call
                # by default; raise to its hard cap to keep the loop
                # cheap.  We still page by time window so long horizons
                # (90d × 1m = 130k bars) come through completely.
                page_seconds = meta["seconds"] * 1000
                cursor = start
                if coinapi_key:
                    try:
                        while cursor < end:
                            window_end = min(end, cursor + timedelta(seconds=page_seconds))
                            rows = await _fetch_coinapi(
                                client, coinapi_key, symbol, granularity,
                                limit=1000, time_start=cursor, time_end=window_end,
                            )
                            if not rows:
                                cursor = window_end
                                continue
                            written += await _persist(symbol, granularity, "coinapi", rows)
                            # Advance past the last bar we received to
                            # avoid an infinite loop when the provider
                            # returns fewer rows than requested.
                            last_ts = max(r["ts"] for r in rows)
                            cursor = max(window_end, last_ts + timedelta(seconds=meta["seconds"]))
                    except Exception as e:
                        log.warning("coinapi backfill %s failed: %s — trying coingecko", key, e)

                if written == 0:
                    # CoinGecko OHLC only returns recent windows (max
                    # 365d for free tier; finer granularity past 30d is
                    # 4h candles).  This is best-effort: it primes
                    # everything CoinGecko exposes.
                    try:
                        rows = await _fetch_coingecko(
                            client, coingecko_key, symbol, granularity,
                            days=min(days, 365),
                        )
                        written = await _persist(symbol, granularity, "coingecko", rows)
                    except Exception as e:
                        log.warning("coingecko backfill %s failed: %s", key, e)

                summary[key] = written
                log.info("backfill %s: %d bars", key, written)
                # Be polite between (symbol, granularity) pages.
                await asyncio.sleep(0.5)
    return summary


# --- Registration ------------------------------------------------------------


def register_crypto_feeds() -> None:
    """Register one feed per (symbol, granularity).  Skips with a clear
    `skipped_reason` when neither provider has a key configured."""
    coinapi_key = os.environ.get("COINAPI_API_KEY") or os.environ.get("COINAPI_KEY")
    coingecko_key = os.environ.get("COINGECKO_API_KEY")
    has_any_key = bool(coinapi_key or coingecko_key)
    skipped_reason = None if has_any_key else "COINAPI_API_KEY / COINGECKO_API_KEY not set"

    for symbol in DEFAULT_SYMBOLS:
        for granularity, meta in GRANULARITIES.items():
            name = f"crypto_{symbol.lower()}_{granularity}"
            if registry.status_of(name):
                continue
            # Bind loop vars into defaults so each closure polls its own pair.
            async def _bound_poll(symbol: str = symbol, granularity: str = granularity) -> int:
                return await _poll(symbol, granularity)
            registry.register(Feed(
                name=name,
                cadence_seconds=float(meta["cadence"]),
                poll=_bound_poll,
                enabled=has_any_key,
                skipped_reason=skipped_reason,
            ))
