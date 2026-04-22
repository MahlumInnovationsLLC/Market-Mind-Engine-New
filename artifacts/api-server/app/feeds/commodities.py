"""Commodities OHLCV poller (EIA primary, AlphaVantage fallback).

The commodities-binary vertical (task #198) needs continuous 1h / 4h /
1d candles for the 10 supported symbols.  Public market data comes from
the U.S. Energy Information Administration (EIA) for energy + grains
and from AlphaVantage for metals and softs.  Both providers feed the
same `commodities_ohlcv` table; the (symbol, granularity, ts) unique
index makes overlapping polls idempotent.

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
from typing import Any, Iterable

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
_PROVIDER_FOR_SYMBOL: dict[str, str] = {
    "WTI":    "eia",
    "NG":     "eia",
    "GOLD":   "alphavantage",
    "SILVER": "alphavantage",
    "COPPER": "alphavantage",
    "CORN":   "eia",
    "SOY":    "eia",
    "WHEAT":  "eia",
    "COFFEE": "alphavantage",
    "SUGAR":  "alphavantage",
}

# Cadence per granularity (seconds between polls for the latest bar).
GRANULARITIES: dict[str, dict[str, Any]] = {
    "1h":  {"cadence": 300.0, "seconds": 60 * 60},
    "4h":  {"cadence": 900.0, "seconds": 4 * 60 * 60},
    "1d":  {"cadence": 3600.0, "seconds": 24 * 60 * 60},
}


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


# --- Provider stubs ----------------------------------------------------------
#
# Both providers are stubbed against their public REST endpoints; the
# actual HTTP wiring lives behind feature flags so a fresh install with
# no API keys silently no-ops the feed (and the predictor falls back to
# swarm-only signals).  The real implementations follow the same shape
# as `app.feeds.crypto._coinapi_fetch_bars` — return an oldest-first
# list of `{ts, o, h, l, c, v}` dicts.


async def _eia_fetch_bars(symbol: str, granularity: str) -> list[dict[str, Any]]:
    """EIA public series — placeholder until the live HTTP wiring lands."""
    return []


async def _alphavantage_fetch_bars(symbol: str, granularity: str) -> list[dict[str, Any]]:
    """AlphaVantage commodities endpoint — placeholder."""
    return []


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
