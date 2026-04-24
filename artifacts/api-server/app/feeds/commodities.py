"""Commodities OHLCV poller (EIA + AlphaVantage, with cross-provider fallback).

The commodities-binary vertical (task #198) needs continuous 1h / 4h /
1d candles for the 10 supported symbols.  Provider routing reflects
what each public API actually publishes:

  * EIA (energy data agency) publishes daily settlement prices for
    crude oil (WTI) and natural gas (Henry Hub).  It does NOT publish
    spot prices for grains, metals, or softs — that data sits in
    USDA / CME and is not exposed via EIA's series-id endpoint.  So
    EIA is the *primary* source only for WTI and NG; for every other
    symbol it deliberately returns no bars and the AlphaVantage path
    takes over.

  * AlphaVantage exposes full OHLCV via TIME_SERIES_DAILY /
    TIME_SERIES_INTRADAY for the commodity-tracking ETFs that proxy
    each symbol — used as primary for grains, metals, and softs, and
    as the fallback for energy when EIA is unkeyed or rate-limited.

Both providers feed the same `commodities_ohlcv` table; the (symbol,
granularity, ts) unique index makes overlapping polls idempotent.

Both API keys are optional — without them this module short-circuits
its registration so a fresh dev install isn't blocked.  When the keys
ARE present the scheduler picks the feeds up automatically via
`registry.register()`.

Public surface:
    register_commodities_feeds() — wired into app.main on startup
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, time as _time, timezone
from typing import Any, Iterable

import httpx

from .. import db
from .registry import Feed, registry


log = logging.getLogger("axiom.feeds.commodities")


DEFAULT_SYMBOLS: tuple[str, ...] = tuple(
    s.strip().upper()
    for s in (
        os.environ.get("AXIOM_COMMODITIES_SYMBOLS")
        or "WTI,GOLD,NG,CORN,SOY,WHEAT,COPPER,SILVER,COFFEE,SUGAR"
    ).split(",")
    if s.strip()
)

# Provider routing: which provider has the cleanest series per symbol.
# Grains stay on AlphaVantage as primary (via the CORN/SOYB/WEAT ETFs)
# because EIA does not publish grain spot prices — its data domain is
# strictly energy.  See module docstring.
_PROVIDER_FOR_SYMBOL: dict[str, str] = {
    "WTI":    "eia",
    "NG":     "eia",
    "GOLD":   "alphavantage",
    "SILVER": "alphavantage",
    "COPPER": "alphavantage",
    "CORN":   "alphavantage",
    "SOY":    "alphavantage",
    "WHEAT":  "alphavantage",
    "COFFEE": "alphavantage",
    "SUGAR":  "alphavantage",
}

# Cadence per granularity (seconds between polls for the latest bar).
GRANULARITIES: dict[str, dict[str, Any]] = {
    "1h":  {"cadence": 300.0, "seconds": 60 * 60},
    "4h":  {"cadence": 900.0, "seconds": 4 * 60 * 60},
    "1d":  {"cadence": 3600.0, "seconds": 24 * 60 * 60},
}

# --- Provider details --------------------------------------------------------

EIA_BASE = "https://api.eia.gov/v2"

# EIA series identifiers (daily frequency, value-only).  EIA does not
# publish grain spot prices — those symbols deliberately fall through
# to AlphaVantage.
_EIA_SERIES: dict[str, str] = {
    "WTI": "PET.RWTC.D",        # Cushing OK WTI Spot Price FOB, daily $/bbl
    "NG":  "NG.RNGWHHD.D",      # Henry Hub Natural Gas Spot Price, daily $/MMBtu
}

ALPHAVANTAGE_BASE = "https://www.alphavantage.co/query"

# AlphaVantage doesn't expose intraday OHLC for raw commodities, but it
# does for the commodity-tracking ETFs that proxy each symbol.  Using
# TIME_SERIES_DAILY / TIME_SERIES_INTRADAY on these tickers gives us
# real OHLCV bars across all our granularities.
_ALPHAVANTAGE_PROXY: dict[str, str] = {
    "WTI":    "USO",
    "NG":     "UNG",
    "GOLD":   "GLD",
    "SILVER": "SLV",
    "COPPER": "CPER",
    "CORN":   "CORN",
    "SOY":    "SOYB",
    "WHEAT":  "WEAT",
    "COFFEE": "JO",
    "SUGAR":  "CANE",
}

# How many bars we ask each provider for on a single poll.  100 is the
# AlphaVantage "compact" outputsize and is more than enough to keep the
# 100-bar feature window warm.
_LATEST_LIMIT: int = 100

# Bound the per-request HTTP timeout so a flaky upstream can't stall
# the whole scheduler tick.
_HTTP_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


# --- Persistence -------------------------------------------------------------


async def upsert_bars(symbol: str, granularity: str, bars: Iterable[dict[str, Any]],
                      *, source: str) -> int:
    """Idempotent batched upsert into commodities_ohlcv.  Returns rows touched."""
    rows = list(bars)
    if not rows:
        return 0
    n = 0
    for b in rows:
        try:
            await db.execute(
                """
                INSERT INTO commodities_ohlcv
                  (symbol, granularity, ts, o, h, l, c, v, source)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (symbol, granularity, ts) DO UPDATE
                  SET o = EXCLUDED.o,
                      h = EXCLUDED.h,
                      l = EXCLUDED.l,
                      c = EXCLUDED.c,
                      v = EXCLUDED.v,
                      source = EXCLUDED.source
                """,
                symbol, granularity, b["ts"],
                float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"]),
                float(b.get("v") or 0.0),
                source,
            )
            n += 1
        except Exception as e:
            log.warning("commodities ohlcv upsert failed (%s/%s): %s",
                        symbol, granularity, e)
    return n


# --- Provider implementations ------------------------------------------------


def _parse_eia_period(period: str) -> datetime | None:
    """EIA returns 'YYYY-MM-DD' for daily series.  Snap to UTC midnight."""
    try:
        d = datetime.strptime(period, "%Y-%m-%d")
    except ValueError:
        return None
    return datetime.combine(d.date(), _time(0, 0), tzinfo=timezone.utc)


async def _eia_fetch_bars(symbol: str, granularity: str) -> list[dict[str, Any]]:
    """Pull a daily close series from EIA and synthesise OHLCV bars.

    EIA only publishes a single daily settlement price per series, so
    open/high/low/close all collapse onto that value and volume is 0.
    Returns oldest-first to match the upstream contract.
    """
    api_key = os.environ.get("EIA_API_KEY")
    if not api_key:
        return []
    # We only ingest daily bars from EIA — grains/intraday aren't
    # published.  Quietly skip 1h / 4h so the fallback can kick in.
    if granularity != "1d":
        return []
    series = _EIA_SERIES.get(symbol)
    if series is None:
        return []

    url = f"{EIA_BASE}/seriesid/{series}"
    params = {
        "api_key": api_key,
        "length": str(_LATEST_LIMIT),
        # Sort newest-first so EIA bounds the response window cleanly,
        # then we reverse to oldest-first below.
        "sort[0][column]": "period",
        "sort[0][direction]": "desc",
    }

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.get(url, params=params)
    if resp.status_code in (401, 403):
        log.warning("eia auth rejected for %s (%s): HTTP %d",
                    symbol, series, resp.status_code)
        return []
    if resp.status_code >= 400:
        log.warning("eia %s/%s HTTP %d: %s", symbol, granularity,
                    resp.status_code, resp.text[:200])
        return []

    payload = resp.json() or {}
    data = (payload.get("response") or {}).get("data") or []
    bars: list[dict[str, Any]] = []
    for row in data:
        period = row.get("period")
        value = row.get("value")
        if period is None or value is None:
            continue
        ts = _parse_eia_period(str(period))
        if ts is None:
            continue
        try:
            v = float(value)
        except (TypeError, ValueError):
            continue
        bars.append({
            "ts": ts,
            "o": v, "h": v, "l": v, "c": v, "v": 0.0,
        })
    # Oldest-first.
    bars.sort(key=lambda b: b["ts"])
    return bars


def _parse_av_daily_ts(key: str) -> datetime | None:
    try:
        d = datetime.strptime(key, "%Y-%m-%d")
    except ValueError:
        return None
    return datetime.combine(d.date(), _time(0, 0), tzinfo=timezone.utc)


def _parse_av_intraday_ts(key: str) -> datetime | None:
    """AlphaVantage intraday timestamps are tz-naive US/Eastern wall
    time.  We treat them as UTC since the relative spacing is what the
    feature builder cares about, and we snap to the bar boundary later.
    """
    try:
        return datetime.strptime(key, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _av_extract_series(payload: dict[str, Any]) -> dict[str, dict[str, Any]] | None:
    """Find the time-series dict regardless of which AlphaVantage
    endpoint we hit.  AV nests its bars under a key like 'Time Series
    (Daily)' or 'Time Series (60min)'.
    """
    for k, v in payload.items():
        if isinstance(v, dict) and k.lower().startswith("time series"):
            return v
    return None


def _coerce_bar(values: dict[str, Any]) -> dict[str, float] | None:
    try:
        return {
            "o": float(values["1. open"]),
            "h": float(values["2. high"]),
            "l": float(values["3. low"]),
            "c": float(values["4. close"]),
            "v": float(values.get("5. volume") or 0.0),
        }
    except (KeyError, ValueError, TypeError):
        return None


def _bucket_bars(bars: list[dict[str, Any]], bucket_seconds: int) -> list[dict[str, Any]]:
    """Resample oldest-first 1h bars into wider buckets (e.g. 4h).

    OHLCV aggregation: open=first, high=max, low=min, close=last,
    volume=sum.  Bucket boundaries snap to UTC midnight so multiple
    polls collapse onto the same row via the unique index.
    """
    if bucket_seconds <= 0 or not bars:
        return bars
    by_bucket: dict[int, dict[str, Any]] = {}
    for b in bars:
        ts: datetime = b["ts"]
        epoch = int(ts.timestamp())
        snap = epoch - (epoch % bucket_seconds)
        existing = by_bucket.get(snap)
        if existing is None:
            by_bucket[snap] = {
                "ts": datetime.fromtimestamp(snap, tz=timezone.utc),
                "o": float(b["o"]),
                "h": float(b["h"]),
                "l": float(b["l"]),
                "c": float(b["c"]),
                "v": float(b.get("v") or 0.0),
            }
        else:
            existing["h"] = max(existing["h"], float(b["h"]))
            existing["l"] = min(existing["l"], float(b["l"]))
            existing["c"] = float(b["c"])
            existing["v"] = float(existing["v"]) + float(b.get("v") or 0.0)
    return [by_bucket[k] for k in sorted(by_bucket)]


async def _alphavantage_fetch_bars(symbol: str, granularity: str) -> list[dict[str, Any]]:
    """Pull OHLCV bars from AlphaVantage for the proxy ETF.

    Granularity routing:
      * 1d → TIME_SERIES_DAILY (compact, ~100 daily bars)
      * 1h → TIME_SERIES_INTRADAY (interval=60min, ~100 hourly bars)
      * 4h → 1h intraday call resampled into 4-hour buckets
    """
    api_key = os.environ.get("ALPHAVANTAGE_API_KEY")
    if not api_key:
        return []
    proxy = _ALPHAVANTAGE_PROXY.get(symbol)
    if proxy is None:
        return []

    if granularity == "1d":
        params = {
            "function": "TIME_SERIES_DAILY",
            "symbol": proxy,
            "outputsize": "compact",
            "apikey": api_key,
        }
        ts_parser = _parse_av_daily_ts
        target_bucket: int | None = None
    elif granularity == "1h":
        params = {
            "function": "TIME_SERIES_INTRADAY",
            "symbol": proxy,
            "interval": "60min",
            "outputsize": "compact",
            "apikey": api_key,
        }
        ts_parser = _parse_av_intraday_ts
        target_bucket = None
    elif granularity == "4h":
        params = {
            "function": "TIME_SERIES_INTRADAY",
            "symbol": proxy,
            "interval": "60min",
            "outputsize": "compact",
            "apikey": api_key,
        }
        ts_parser = _parse_av_intraday_ts
        target_bucket = 4 * 60 * 60
    else:
        return []

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.get(ALPHAVANTAGE_BASE, params=params)
    if resp.status_code >= 400:
        log.warning("alphavantage %s/%s HTTP %d: %s", symbol, granularity,
                    resp.status_code, resp.text[:200])
        return []

    try:
        payload = resp.json()
    except ValueError:
        log.warning("alphavantage %s/%s returned non-JSON body", symbol, granularity)
        return []

    # AlphaVantage soft-errors look like {"Note": "...rate limit..."} or
    # {"Information": "..."} or {"Error Message": "..."}.  Treat any of
    # those as "no bars" so the caller can fall over.
    if not isinstance(payload, dict) or any(
        k in payload for k in ("Note", "Information", "Error Message")
    ):
        if isinstance(payload, dict):
            log.info("alphavantage %s/%s soft-error: %s",
                     symbol, granularity,
                     str(payload.get("Note") or payload.get("Information")
                         or payload.get("Error Message"))[:200])
        return []

    series = _av_extract_series(payload)
    if not series:
        return []

    bars: list[dict[str, Any]] = []
    for ts_key, values in series.items():
        ts = ts_parser(str(ts_key))
        if ts is None:
            continue
        bar = _coerce_bar(values)
        if bar is None:
            continue
        bars.append({"ts": ts, **bar})

    bars.sort(key=lambda b: b["ts"])
    if target_bucket is not None:
        bars = _bucket_bars(bars, target_bucket)
    # Cap to the most recent window so we don't write more than needed.
    if len(bars) > _LATEST_LIMIT:
        bars = bars[-_LATEST_LIMIT:]
    return bars


# --- Feed factory ------------------------------------------------------------


def _make_poller(symbol: str, granularity: str):
    """Return an async poller for `(symbol, granularity)`.

    Routes to the configured provider, falls back to the alternate when
    the primary returns no data (e.g. rate-limited / unkeyed).
    """
    primary = _PROVIDER_FOR_SYMBOL.get(symbol, "eia")

    async def poll() -> dict[str, Any]:
        bars: list[dict[str, Any]] = []
        used = primary
        try:
            if primary == "eia":
                bars = await _eia_fetch_bars(symbol, granularity)
                if not bars:
                    used = "alphavantage"
                    bars = await _alphavantage_fetch_bars(symbol, granularity)
            else:
                bars = await _alphavantage_fetch_bars(symbol, granularity)
                if not bars:
                    used = "eia"
                    bars = await _eia_fetch_bars(symbol, granularity)
        except Exception as e:  # pragma: no cover - defensive
            log.warning("commodities feed crash %s/%s: %s", symbol, granularity, e)
            return {"ok": False, "error": str(e)}
        wrote = await upsert_bars(symbol, granularity, bars, source=used)
        return {"ok": True, "rows": wrote, "source": used}

    return poll


def register_commodities_feeds() -> None:
    """Register one feed per (symbol, granularity).

    No-op when neither EIA nor AlphaVantage keys are configured AND the
    explicit force-register env knob is unset — a dev environment without
    keys would otherwise spam the scheduler with empty polls.
    """
    has_eia = bool(os.environ.get("EIA_API_KEY"))
    has_av = bool(os.environ.get("ALPHAVANTAGE_API_KEY"))
    forced = os.environ.get("AXIOM_COMMODITIES_FEEDS_FORCE", "").lower() in ("1", "true", "yes")
    if not (has_eia or has_av or forced):
        log.info("commodities feeds skipped (no EIA/AlphaVantage key, force flag off)")
        return
    for sym in DEFAULT_SYMBOLS:
        for gran, cfg in GRANULARITIES.items():
            registry.register(
                Feed(
                    name=f"commodities.{sym}.{gran}",
                    poll=_make_poller(sym, gran),
                    cadence_seconds=cfg["cadence"],
                )
            )
    log.info("commodities feeds registered: %d symbols × %d granularities",
             len(DEFAULT_SYMBOLS), len(GRANULARITIES))
