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

# Settler tunables.  The settler is intentionally rate-limited: each
# tick it picks the oldest N unsettled markets and asks Kalshi for the
# resolved row.  ``_SETTLE_DEFAULT_CADENCE_SECS`` is much slower than
# the discovery poll because settlements are sticky — once Kalshi
# stamps a result the row stays settled forever, so re-checking every
# minute would be pure burn.
_SETTLE_DEFAULT_BATCH: int = 50
_SETTLE_DEFAULT_CADENCE_SECS: float = 300.0
# Kalshi's market lifecycle goes ``open`` → ``closed`` → (briefly)
# ``determined`` → ``settled`` → ``finalized``.  Any of the latter
# three carry a final ``result`` we can stamp.
_SETTLED_STATUSES: frozenset[str] = frozenset({
    "settled", "finalized", "determined", "resolved",
})
# The Kalshi API ships the resolution as ``result`` (modern) or
# ``settlement`` (older payloads).  Both can be ``yes``/``no``/``void``.
_VALID_OUTCOMES: frozenset[str] = frozenset({"yes", "no", "void"})

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
        # Persisted into ``predictions.metadata`` (jsonb).  Earlier
        # revisions of this feed wrote into a ``game_context`` column
        # that doesn't exist on the predictions table — every upsert
        # silently raised ``column "game_context" ... does not exist``
        # in the per-row defensive ``except``, which manifested as
        # zero crypto candidates ever reaching the orchestrator.
        # Renaming to the actual column unblocks the BTC/ETH cycle.
        "metadata": {
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


# NB: predictions has ``metadata`` (jsonb default '{}'), NO
# ``game_context`` and NO ``updated_at`` column.  The previous version
# of this UPSERT referenced both — every poll silently raised inside
# the per-row ``except`` and the crypto-binary cycle saw zero
# candidates.  See ``_candidate_from_market`` for the matching dict
# rename.
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
    """Idempotent batched upsert into ``predictions``.  Returns rows touched.

    Each row is its own statement so a single malformed ticker can't
    take the rest of the page down with it — the cycle is far better
    off with N-1 valid candidates than zero.
    """
    import json as _json
    from datetime import timezone as _tz

    n = 0
    for row in rows:
        # ``predictions.game_time`` is ``timestamp WITHOUT time zone``
        # (per the migrations), but Kalshi gives us tz-aware close
        # times.  asyncpg refuses to bind a tz-aware datetime to a
        # naive column ("can't subtract offset-naive and offset-aware
        # datetimes") and the row is silently dropped in the
        # per-row ``except`` below.  Normalize to naive UTC at the
        # bind boundary so the producer's row dict can stay tz-aware.
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


# --- Settlement --------------------------------------------------------------
#
# The discovery poller above writes live BTC/ETH markets into
# ``predictions``; the cycle scores them; and once Kalshi resolves a
# market we want the row stamped with the canonical YES/NO/void
# outcome so historical accuracy + backtest reporting can join settled
# predictions to their original prediction without a manual sweep.
#
# We deliberately keep this on the same module as discovery: both
# halves share the Kalshi client, the league filter, and the
# ``parse_close_time`` helper.  Splitting them across files would
# fork the credential check and double the surface for the operator
# console.


def _settled_outcome(market: dict[str, Any]) -> str | None:
    """Extract Kalshi's canonical YES/NO/void result from a market row.

    Kalshi exposes the resolution under either ``result`` (modern) or
    ``settlement`` (older payloads); both share the same enum.  We
    require BOTH a settled-looking ``status`` and a valid outcome
    string so a half-populated payload during the brief
    ``closed → determined`` window can't trick us into stamping
    settlement on a market that hasn't actually resolved yet.

    Returns one of ``"yes"`` / ``"no"`` / ``"void"`` when the market is
    settled, else ``None``.
    """
    status = (market.get("status") or "").strip().lower()
    raw = market.get("result")
    if raw is None or raw == "":
        raw = market.get("settlement")
    outcome = (str(raw) if raw is not None else "").strip().lower()
    if outcome not in _VALID_OUTCOMES:
        return None
    # ``determined`` markets always carry a final result; some Kalshi
    # responses show ``status='closed'`` alongside a populated result
    # for a few seconds during the resolution sweep — accept either.
    if status in _SETTLED_STATUSES or status == "closed":
        return outcome
    return None


def _settle_price_dollars(outcome: str) -> float | None:
    """Convenience YES-price for backtest math: 1.0 for YES, 0.0 for NO,
    NULL for voided markets (those refund every contract)."""
    if outcome == "yes":
        return 1.0
    if outcome == "no":
        return 0.0
    return None


def _settled_at_for(market: dict[str, Any]) -> datetime:
    """Pick the best-available settlement timestamp for a market.

    Preference order: explicit ``settled_time`` / ``settlement_time``
    fields → ``expiration_time`` / ``close_time`` → ``now()``.  We
    always return a UTC-aware datetime so the UPDATE binds cleanly
    against the ``timestamptz`` column.
    """
    for k in (
        "settled_time", "settlement_time",
        "determination_time", "result_time",
        "expiration_time", "close_time",
    ):
        ts = _parse_close_time(market.get(k))
        if ts is not None:
            return ts
    return datetime.now(timezone.utc)


_UNSETTLED_SQL = """
SELECT id, kalshi_market_id, game_time
  FROM predictions
 WHERE league = $1
   AND kalshi_market_id IS NOT NULL
   AND settled_at IS NULL
   AND (game_time IS NULL OR game_time < now())
 ORDER BY coalesce(game_time, created_at) ASC
 LIMIT $2
"""

_SETTLE_UPDATE_SQL = """
UPDATE predictions
   SET settled_at   = $2,
       outcome      = $3,
       settle_price = $4,
       updated_at   = now()
 WHERE kalshi_market_id = $1
   AND settled_at IS NULL
"""


async def _list_unsettled_candidates(limit: int) -> list[dict[str, Any]]:
    """Read up to ``limit`` unsettled crypto rows whose game_time has
    passed.  Soft-fails to an empty list so a transient DB hiccup
    leaves the next tick to retry."""
    try:
        rows = await db.fetch(_UNSETTLED_SQL, _LEAGUE, int(limit))
    except Exception as e:  # noqa: BLE001
        log.debug("kalshi_crypto_settlements candidate query failed: %s", e)
        return []
    return [dict(r) for r in rows]


async def _record_settlement(
    ticker: str, outcome: str, settled_at: datetime, settle_price: float | None,
) -> bool:
    """Stamp the settlement onto the matching ``predictions`` row.

    The ``settled_at IS NULL`` guard in the WHERE clause makes the
    update idempotent: once the row has been stamped, subsequent
    polls become no-ops (rowcount == 0) so the settler can safely
    re-poll a sticky settlement window without overwriting the
    timestamp we already recorded.

    Returns ``True`` when a row was actually updated.
    """
    try:
        result = await db.execute(
            _SETTLE_UPDATE_SQL, ticker, settled_at, outcome, settle_price,
        )
    except Exception as e:  # noqa: BLE001
        log.warning(
            "predictions settlement update failed for %s: %s", ticker, e,
        )
        return False
    # asyncpg returns the command tag (e.g. ``"UPDATE 1"``) — a missing
    # tag still means the statement ran, so be permissive.
    if isinstance(result, str) and result.startswith("UPDATE "):
        try:
            return int(result.split(" ", 1)[1]) > 0
        except (IndexError, ValueError):
            return True
    return True


async def poll_kalshi_crypto_settlements() -> int:
    """One settler tick.

    Pulls a small batch of unsettled crypto predictions, asks Kalshi
    for the resolved row, and stamps ``settled_at`` / ``outcome`` /
    ``settle_price`` whenever the market reports a final result.
    Returns the number of rows freshly settled this tick (so the feed
    registry's ``records_written_total`` reflects realised closures).

    Soft-fails when Kalshi credentials aren't configured (returns 0)
    — registration already advertises a clear ``skipped_reason`` in
    that case.  All per-market errors are swallowed so one bad ticker
    can't take the rest of the batch down.
    """
    settings = get_settings()
    if not (settings.kalshi_api_key_id and settings.kalshi_private_key):
        return 0

    candidates = await _list_unsettled_candidates(_settle_batch_size())
    if not candidates:
        return 0

    n = 0
    try:
        async with kalshi_client_mod.KalshiClient(settings=settings) as client:
            for row in candidates:
                ticker = row.get("kalshi_market_id")
                if not ticker:
                    continue
                try:
                    body = await client.get_market(ticker)
                except Exception as e:  # noqa: BLE001
                    # Per-ticker fetch failures are common (network blip,
                    # 5xx) and the next tick re-picks the same rows, so
                    # we just log and skip.
                    log.debug("kalshi get_market failed for %s: %s", ticker, e)
                    continue
                market = body.get("market") if isinstance(body, dict) else None
                if not isinstance(market, dict):
                    continue
                outcome = _settled_outcome(market)
                if outcome is None:
                    continue
                settled_at = _settled_at_for(market)
                settle_price = _settle_price_dollars(outcome)
                if await _record_settlement(
                    ticker, outcome, settled_at, settle_price,
                ):
                    n += 1
    except Exception as e:  # noqa: BLE001
        log.warning("kalshi_crypto_settlements poll crashed: %s", e)
        return n
    if n:
        log.info("kalshi_crypto_settlements stamped %d row(s)", n)
    return n


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


def _settle_cadence_seconds() -> float:
    raw = os.environ.get("AXIOM_KALSHI_CRYPTO_SETTLEMENTS_CADENCE_SECS")
    if not raw:
        return _SETTLE_DEFAULT_CADENCE_SECS
    try:
        v = float(raw)
    except ValueError:
        return _SETTLE_DEFAULT_CADENCE_SECS
    # Floor at 30s so an over-eager override can't melt the per-market
    # endpoint; settlements are sticky so anything below this is waste.
    return max(30.0, v)


def _settle_batch_size() -> int:
    raw = os.environ.get("AXIOM_KALSHI_CRYPTO_SETTLEMENTS_BATCH")
    if not raw:
        return _SETTLE_DEFAULT_BATCH
    try:
        v = int(raw)
    except ValueError:
        return _SETTLE_DEFAULT_BATCH
    # Clamp to [1, 200] — Kalshi's per-market endpoint is cheap but
    # not free, and we'd rather walk the backlog over a few ticks than
    # block the scheduler on a giant burst.
    return max(1, min(200, v))


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


def register_kalshi_crypto_settlements_feed() -> None:
    """Register the BTC/ETH settlement poller with the global feed
    registry.

    Mirrors :func:`register_kalshi_crypto_markets_feed`: idempotent on
    re-import, advertises a clear ``skipped_reason`` when Kalshi
    credentials are missing, and runs on a much slower cadence
    (settlements are sticky — once stamped the row never un-settles).
    """
    name = "kalshi_crypto_settlements"
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
        cadence_seconds=_settle_cadence_seconds(),
        poll=poll_kalshi_crypto_settlements,
        enabled=has_creds,
        skipped_reason=skipped_reason,
    ))
    log.info(
        "kalshi crypto settlements feed registered (enabled=%s, cadence=%.1fs)",
        has_creds,
        _settle_cadence_seconds(),
    )
