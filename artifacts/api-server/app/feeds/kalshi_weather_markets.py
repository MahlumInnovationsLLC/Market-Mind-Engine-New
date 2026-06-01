"""Discover live Kalshi weather binary markets and feed them into the
weather trading cycle.

The weather vertical (registered in :mod:`app.verticals.builtins`) routes
temperature contracts through the GRU + EMOS pipeline
(:mod:`app.weather.predictor`) and precipitation through the gamma/logistic
EMOS stack (:mod:`app.weather.precip_predictor`), but
:func:`app.orchestrator.cycle.run_cycle` pulls candidate rows from the
``predictions`` table — and nothing populates it for weather markets out of
the box.  Crypto has :mod:`app.feeds.kalshi_crypto_markets` and sports has
:mod:`app.feeds.kalshi_sports_markets`; weather had no equivalent, so
``_candidate_predictions('weather')`` returned zero rows and the vertical
never traded a single market despite the forecast feeds and predictor
models being fully wired.

This poller closes that gap:

  * Calls Kalshi's market index (``GET /markets``) once per supported
    weather series prefix — temperature (``KXHIGH`` / ``KXLOW`` / ``KXTEMP``)
    and precipitation (``KXRAIN``), fanned across the supported city codes
    (see :mod:`app.weather.tickers`).
  * Filters to markets the weather ticker parsers accept — temperature via
    :func:`app.weather.tickers.parse_temperature_ticker` and precip via
    :func:`app.weather.precip_tickers.parse_precipitation_ticker` — so the
    predictor only ever sees markets it can price.  Anything else (a city /
    metric we don't model, a malformed shape) is skipped silently.
  * Upserts a row per market into ``predictions`` keyed to the Kalshi
    ticker, with ``league = 'WEATHER'`` so the existing
    ``_candidate_predictions`` SELECT for the ``weather`` vertical picks
    them up.

The feed registers itself as ``enabled=False`` (with a clear
``skipped_reason``) when Kalshi credentials are missing, matching how the
other API-keyed feeds advertise themselves on the operator console.

Public surface:
    register_kalshi_weather_markets_feed() — wired into app.main on startup
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Iterable

from .. import db
from ..config import get_settings
from ..kalshi import client as kalshi_client_mod
from ..weather.precip_tickers import (
    InvalidPrecipitationTicker,
    parse_precipitation_ticker,
)
from ..weather.tickers import (
    SUPPORTED_CITIES,
    InvalidTemperatureTicker,
    parse_temperature_ticker,
)
from .registry import Feed, registry


log = logging.getLogger("axiom.feeds.kalshi_weather_markets")


# Kalshi's per-page cap is 200; we walk a handful of pages so a large
# event burst (e.g. a daily contract refresh across every city) can't
# silently drop tickers off the end.
_PAGE_LIMIT: int = 200
_MAX_PAGES: int = 5
# Weather contracts refresh daily, not by the second — a slower cadence
# than crypto keeps the per-series fan-out (one ``list_markets`` call per
# city × metric) off the rate limiter while still catching the daily roll.
_DEFAULT_CADENCE_SECS: float = 120.0
_LEAGUE: str = "WEATHER"

# Temperature series prefixes recognised by the temperature parser.
_TEMP_PREFIXES: tuple[str, ...] = ("KXHIGH", "KXLOW", "KXTEMP")
# Precipitation series prefix recognised by the precip parser.
_PRECIP_PREFIX: str = "KXRAIN"

# City code fragments Kalshi appends to each weather series prefix
# (``KXHIGH`` + ``NY`` → series ``KXHIGHNY``).  Derived from the parser's
# canonical supported cities plus the ``NY`` short form for New York —
# the one city whose Kalshi series code (``KXHIGHNY``) differs from the
# canonical 3-letter code (``NYC``).  Querying a series Kalshi doesn't
# list just yields an empty page (skipped), so an extra alias is harmless;
# every returned market is independently validated by the parsers below.
_CITY_CODES: tuple[str, ...] = tuple(
    dict.fromkeys((*SUPPORTED_CITIES, "NY"))  # ordered de-dupe
)


def _series_prefixes() -> tuple[str, ...]:
    """Every weather series_ticker to poll: temp metrics × cities + rain."""
    series: list[str] = []
    for metric in _TEMP_PREFIXES:
        for city in _CITY_CODES:
            series.append(f"{metric}{city}")
    for city in _CITY_CODES:
        series.append(f"{_PRECIP_PREFIX}{city}")
    return tuple(series)


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

    Mirrors :func:`app.feeds.kalshi_crypto_markets._mid_price_dollars`:
    prefer the live ``*_dollars`` fields (already 0..1), fall back to the
    legacy cents fields (divide by 100), and across sides prefer
    bid+ask midpoint → last → ask → bid so a freshly-listed market with
    one-sided liquidity still seeds the cycle with a price hint.
    """
    def _to_num(v: Any) -> float | None:
        if v is None:
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        if f < 0:
            return None
        return f

    def _resolve(dollars_key: str, cents_key: str) -> float | None:
        d = _to_num(market.get(dollars_key))
        if d is not None:
            return d
        c = _to_num(market.get(cents_key))
        if c is not None:
            return c / 100.0
        return None

    bid_d = _resolve("yes_bid_dollars", "yes_bid")
    ask_d = _resolve("yes_ask_dollars", "yes_ask")
    last_d = _resolve("last_price_dollars", "last_price")

    if bid_d is not None and ask_d is not None and ask_d >= bid_d:
        return (bid_d + ask_d) / 2.0
    if last_d is not None:
        return last_d
    if ask_d is not None:
        return ask_d
    if bid_d is not None:
        return bid_d
    return None


def _candidate_from_market(market: dict[str, Any]) -> dict[str, Any] | None:
    """Build the ``predictions`` row payload for a single Kalshi weather
    market, or ``None`` when the ticker isn't a parseable temperature or
    precipitation contract (unsupported city/metric, malformed shape).

    The cycle's weather predictor re-parses the ticker from
    ``kalshi_market_id`` itself, so the row only needs the ticker, a price
    hint, and the close time — everything else lives in ``metadata`` for
    forensics.
    """
    ticker = (market.get("ticker") or market.get("market_ticker") or "").strip()
    if not ticker:
        return None

    metric: str
    detail: dict[str, Any]
    try:
        temp = parse_temperature_ticker(ticker)
        metric = temp.metric
        detail = {
            "city": temp.city,
            "metric": temp.metric,
            "threshold": temp.threshold,
            "direction": temp.direction,
        }
    except InvalidTemperatureTicker:
        try:
            precip = parse_precipitation_ticker(ticker)
            metric = "RAIN"
            detail = {
                "city": precip.city,
                "metric": "RAIN",
                "threshold": precip.threshold_in,
                "direction": precip.direction,
            }
        except InvalidPrecipitationTicker:
            return None

    close_time = _parse_close_time(
        market.get("close_time") or market.get("expected_expiration_time")
    )
    price = _mid_price_dollars(market)

    # ``game_id`` doubles as the canonical event identifier downstream
    # (settlement, swarm debate dedupe, etc).  Re-using the Kalshi ticker
    # keeps the producer trivially idempotent — every cycle collapses onto
    # the same row via the unique index on ``kalshi_market_id``.
    return {
        "game_id": ticker,
        "league": _LEAGUE,
        "kalshi_market_id": ticker,
        "kalshi_price": price,
        "game_time": close_time,
        "metadata": {
            "kalshi_market_id": ticker,
            "kalshi_price": price,
            "title": market.get("title"),
            "status": market.get("status"),
            "close_time": close_time.isoformat() if close_time else None,
            "yes_bid": market.get("yes_bid"),
            "yes_ask": market.get("yes_ask"),
            "last_price": market.get("last_price"),
            "yes_bid_dollars": market.get("yes_bid_dollars"),
            "yes_ask_dollars": market.get("yes_ask_dollars"),
            "last_price_dollars": market.get("last_price_dollars"),
            "source": "kalshi_weather_markets",
            **detail,
        },
    }


# --- Persistence -------------------------------------------------------------


# NB: predictions has ``metadata`` (jsonb default '{}'), NO ``game_context``
# and NO ``updated_at`` column — mirror the crypto feed's UPSERT exactly so
# a stray column reference can't silently sink every row in the per-row
# ``except`` (which manifests as zero weather candidates ever reaching the
# orchestrator).
_UPSERT_SQL = """
INSERT INTO predictions
       (game_id, league, kalshi_market_id, kalshi_price, game_time, metadata)
VALUES ($1, $2, $3, $4, $5, $6::jsonb)
ON CONFLICT (kalshi_market_id) WHERE kalshi_market_id IS NOT NULL
DO UPDATE SET
    league       = EXCLUDED.league,
    kalshi_price = EXCLUDED.kalshi_price,
    game_time    = EXCLUDED.game_time,
    metadata     = EXCLUDED.metadata;
"""


async def _upsert_candidates(rows: Iterable[dict[str, Any]]) -> int:
    """Idempotent per-row upsert into ``predictions``.  Returns rows touched.

    Each row is its own statement so a single malformed ticker can't take
    the rest of the page down with it — the cycle is far better off with
    N-1 valid candidates than zero.
    """
    import json as _json
    from datetime import timezone as _tz

    n = 0
    for row in rows:
        # ``predictions.game_time`` is ``timestamp WITHOUT time zone``;
        # Kalshi gives tz-aware close times and asyncpg refuses to bind a
        # tz-aware datetime to a naive column.  Normalize to naive UTC at
        # the bind boundary (see the crypto feed for the original bug).
        gt = row.get("game_time")
        if gt is not None and getattr(gt, "tzinfo", None) is not None:
            gt = gt.astimezone(_tz.utc).replace(tzinfo=None)
        try:
            await db.execute(
                _UPSERT_SQL,
                row["game_id"],
                row["league"],
                row["kalshi_market_id"],
                row.get("kalshi_price"),
                gt,
                _json.dumps(row.get("metadata") or {}),
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
    """Walk every page of open markets under ``series`` (``KXHIGHNY`` etc.).

    A series Kalshi doesn't list (off-season / wrong city code) simply
    yields an empty page; failures are logged at debug so the daily
    fan-out across cities can't flood the operator console with noise.
    """
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
            log.debug("kalshi list_markets failed for %s: %s", series, e)
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


async def poll_kalshi_weather_markets() -> int:
    """One poll: fetch every live weather binary market and upsert.

    Returns the number of ``predictions`` rows touched so the feed
    registry can surface the count on the operator console.  Soft-fails
    when Kalshi credentials aren't configured (returns 0) — the
    registration code already advertises the feed as skipped in that case.
    """
    settings = get_settings()
    if not (settings.kalshi_api_key_id and settings.kalshi_private_key):
        return 0

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        async with kalshi_client_mod.KalshiClient(settings=settings) as client:
            for series in _series_prefixes():
                markets = await _list_series_markets(client, series)
                for m in markets:
                    cand = _candidate_from_market(m)
                    if cand is None:
                        continue
                    # Different city series can never collide, but de-dupe
                    # defensively so a quirk in Kalshi's index can't double
                    # a ticker within a single poll.
                    if cand["kalshi_market_id"] in seen:
                        continue
                    seen.add(cand["kalshi_market_id"])
                    rows.append(cand)
    except Exception as e:  # noqa: BLE001
        log.warning("kalshi_weather_markets poll crashed: %s", e)
        return 0

    if not rows:
        return 0
    return await _upsert_candidates(rows)


# --- Registration ------------------------------------------------------------


def _cadence_seconds() -> float:
    raw = os.environ.get("AXIOM_KALSHI_WEATHER_MARKETS_CADENCE_SECS")
    if not raw:
        return _DEFAULT_CADENCE_SECS
    try:
        v = float(raw)
    except ValueError:
        return _DEFAULT_CADENCE_SECS
    # Floor at 30s so an over-eager override can't melt the per-series
    # fan-out; weather contracts roll daily so anything faster is waste.
    return max(30.0, v)


def register_kalshi_weather_markets_feed() -> None:
    """Register the weather market discovery poller with the global feed
    registry.

    Idempotent so a hot reload doesn't crash the lifespan; advertises a
    clear ``skipped_reason`` when Kalshi credentials are missing so the
    operator console renders the feed as ``enabled=false`` rather than
    silently swallowing the registration.
    """
    name = "kalshi_weather_markets"
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
        poll=poll_kalshi_weather_markets,
        enabled=has_creds,
        skipped_reason=skipped_reason,
    ))
    log.info(
        "kalshi weather markets feed registered (enabled=%s, cadence=%.1fs)",
        has_creds,
        _cadence_seconds(),
    )
