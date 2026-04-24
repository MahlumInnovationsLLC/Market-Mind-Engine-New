"""Postgres-backed cache for Kalshi market candlestick (tick) history.

The trade-detail panel used to hit Kalshi's candlesticks endpoint
synchronously on every page load.  For revisits (and especially for
settled-but-popular markets) that's wasted work; for live views it
also tied us to whatever uptime the candles endpoint had at the
moment.  Per task #359 we persist each ``(market_ticker,
period_interval, bucket_ts)`` close / mean into ``market_price_ticks``
and serve the chart from cache, only calling Kalshi for buckets that
aren't already cached.

Public surface:

* :func:`fetch_window` — returns ``(points, missing_ranges)`` for the
  requested window, where ``points`` are the cached buckets in the
  same normalised shape the trade-detail series expects
  (``{t, model_prob: None, market_price}``) and ``missing_ranges`` is
  a list of ``(start_ts, end_ts)`` pairs the caller still needs to
  fetch from Kalshi.
* :func:`upsert_candles` — persists raw Kalshi candle dicts back into
  the cache.
* :func:`candles_to_points` — converts raw Kalshi candles into the
  normalised series shape (used both when serving newly-fetched
  candles and indirectly when reading from the cache).
* :func:`refresh_market_window` — convenience that runs a full
  cache-first read + tail fetch + upsert cycle for a single market;
  the periodic refresh loop in :mod:`app.kalshi.tick_cache_refresh`
  uses it to top up active markets.

Every helper is best-effort and swallows transport / DB errors so a
sick cache can never take down a trade-detail render — the worst case
is a zero-row cache, which simply forces the live Kalshi fetch the
caller already had to do before this module existed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable

from .. import db
from . import client as kalshi_client


log = logging.getLogger(__name__)


def _bucket_seconds(period_interval: int) -> int:
    """Width of a single Kalshi bar in seconds.

    Kalshi accepts ``1`` (minute), ``60`` (hour) and ``1440`` (day) as
    valid period intervals.  We multiply by 60 to translate the
    minute-denominated config into seconds for the boundary checks.
    """
    return max(1, int(period_interval)) * 60


def _to_iso_z(epoch_seconds: int | float) -> str:
    """Render a unix timestamp as an ``...Z`` ISO string.

    Matches the format the swarm_debates query and the existing
    candle-overlay code already emit so the merged series sorts
    deterministically by ``t``.
    """
    return (
        datetime.fromtimestamp(int(epoch_seconds), tz=timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _candle_price_cents(row: dict[str, Any]) -> int | None:
    """Pick the most representative cents value from a Kalshi candle.

    Mirrors the existing ``_fetch_market_price_history`` logic: prefer
    ``price.mean`` (the time-weighted mid Kalshi exposes) and fall
    back to ``price.close`` when the mean is null (Kalshi nulls it
    for empty buckets).  Returns ``None`` if neither is usable.
    """
    price_blk = row.get("price") or {}
    cents = price_blk.get("mean")
    if cents is None:
        cents = price_blk.get("close")
    return cents


def candles_to_points(candles: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalise raw Kalshi candle rows into trade-detail series points.

    Each output row has ``t`` (ISO Z), ``model_prob`` (always ``None``
    so the front-end keeps the model line driven exclusively by debate
    snapshots) and ``market_price`` as a 0..1 fraction.  Rows that
    can't be parsed into a price are silently skipped — same shape as
    the in-place loop ``_fetch_market_price_history`` used before.
    """
    out: list[dict[str, Any]] = []
    for row in candles or []:
        if not isinstance(row, dict):
            continue
        end_ts = row.get("end_period_ts")
        if not isinstance(end_ts, (int, float)):
            continue
        cents = _candle_price_cents(row)
        if cents is None:
            continue
        try:
            market_price = float(cents) / 100.0
        except (TypeError, ValueError):
            continue
        out.append(
            {
                "t": _to_iso_z(end_ts),
                "model_prob": None,
                "market_price": market_price,
            }
        )
    return out


async def fetch_window(
    market_ticker: str,
    *,
    period_interval: int,
    start_ts: int,
    end_ts: int,
) -> tuple[list[dict[str, Any]], list[tuple[int, int]]]:
    """Read the cached buckets for ``market_ticker`` in ``[start, end]``.

    Returns ``(points, missing_ranges)``:

    * ``points`` are normalised series points already in the shape the
      trade-detail caller will merge with the swarm-debate snapshots.
      Buckets whose price is null are still loaded so the caller
      knows the bucket is covered, but they're filtered out of the
      returned ``points`` list (a chart can't render ``None``).
    * ``missing_ranges`` is the list of ``(lo_ts, hi_ts)`` epoch-second
      pairs that aren't yet covered by the cache.  The caller fetches
      each from Kalshi and writes the results back via
      :func:`upsert_candles`; the boundary tolerance is one bar so a
      single missing bar at either edge doesn't trigger a full
      refetch.

    On any DB failure the cache is treated as empty and the entire
    window is reported as missing — the caller then falls back to the
    pre-cache live-fetch path automatically.
    """
    if not market_ticker or end_ts <= start_ts:
        return [], []
    try:
        rows = await db.fetch(
            """
            SELECT extract(epoch from bucket_ts)::bigint AS end_period_ts,
                   close_cents,
                   mean_cents
              FROM market_price_ticks
             WHERE market_ticker = $1
               AND period_interval = $2
               AND bucket_ts >= to_timestamp($3)
               AND bucket_ts <= to_timestamp($4)
             ORDER BY bucket_ts ASC
            """,
            market_ticker, int(period_interval), int(start_ts), int(end_ts),
        )
    except Exception as e:  # noqa: BLE001 — best-effort cache
        log.info("tick cache read failed for %s: %s", market_ticker, e)
        return [], [(int(start_ts), int(end_ts))]

    if not rows:
        return [], [(int(start_ts), int(end_ts))]

    bar_seconds = _bucket_seconds(period_interval)
    points: list[dict[str, Any]] = []
    for r in rows:
        cents = r["mean_cents"]
        if cents is None:
            cents = r["close_cents"]
        if cents is None:
            # Bucket is covered but Kalshi reported no trades — keep
            # the row out of the chart but DO leave it in the cache so
            # the next request doesn't refetch it.
            continue
        try:
            market_price = float(cents) / 100.0
        except (TypeError, ValueError):
            continue
        points.append(
            {
                "t": _to_iso_z(int(r["end_period_ts"])),
                "model_prob": None,
                "market_price": market_price,
            }
        )

    cached_lo = int(rows[0]["end_period_ts"])
    cached_hi = int(rows[-1]["end_period_ts"])
    missing: list[tuple[int, int]] = []
    if cached_lo - int(start_ts) > bar_seconds:
        missing.append((int(start_ts), cached_lo))
    if int(end_ts) - cached_hi > bar_seconds:
        missing.append((cached_hi, int(end_ts)))
    return points, missing


async def upsert_candles(
    market_ticker: str,
    *,
    period_interval: int,
    candles: Iterable[dict[str, Any]],
) -> int:
    """Persist a batch of Kalshi candle dicts.

    Rows are inserted with ``ON CONFLICT DO UPDATE`` so re-fetching a
    bucket simply bumps ``fetched_at`` (and overwrites prices when
    Kalshi corrects historical bars).  Buckets with no usable price
    block are still recorded with ``NULL`` cents so the cache covers
    the bucket — otherwise every empty Kalshi bar would force a
    refetch on the next request.

    Returns the number of buckets that were forwarded to the DB; on
    any DB failure the function logs and returns ``0`` so the calling
    request still serves the just-fetched data from memory.
    """
    if not market_ticker:
        return 0
    rows: list[tuple[Any, ...]] = []
    for candle in candles or []:
        if not isinstance(candle, dict):
            continue
        end_ts = candle.get("end_period_ts")
        if not isinstance(end_ts, (int, float)):
            continue
        price_blk = candle.get("price") or {}
        close_cents = price_blk.get("close")
        mean_cents = price_blk.get("mean")
        try:
            close_int = int(close_cents) if close_cents is not None else None
            mean_int = int(mean_cents) if mean_cents is not None else None
        except (TypeError, ValueError):
            close_int = None
            mean_int = None
        rows.append(
            (
                market_ticker,
                int(period_interval),
                int(end_ts),
                close_int,
                mean_int,
            )
        )
    if not rows:
        return 0
    try:
        await db.executemany(
            """
            INSERT INTO market_price_ticks
                (market_ticker, period_interval, bucket_ts, close_cents, mean_cents, fetched_at)
            VALUES ($1, $2, to_timestamp($3), $4, $5, now())
            ON CONFLICT (market_ticker, period_interval, bucket_ts)
            DO UPDATE SET close_cents = EXCLUDED.close_cents,
                          mean_cents  = EXCLUDED.mean_cents,
                          fetched_at  = now()
            """,
            rows,
        )
    except Exception as e:  # noqa: BLE001 — best-effort cache
        log.info(
            "tick cache upsert failed for %s (%d rows): %s",
            market_ticker, len(rows), e,
        )
        return 0
    return len(rows)


async def refresh_market_window(
    market_ticker: str,
    *,
    period_interval: int,
    start_ts: int,
    end_ts: int,
    client: Any | None = None,
) -> int:
    """Top up the cache for a single market window.

    Used by the periodic refresh loop and by the synchronous
    trade-detail path. Reads the cache, fetches any missing prefix /
    suffix from Kalshi, persists the freshly-fetched candles, and
    returns the total number of buckets newly persisted (0 means the
    cache already covered the window or Kalshi was unreachable).

    Pass an existing :class:`KalshiClient` via ``client`` to amortize
    auth / TLS setup across many markets in the refresh loop; when
    omitted we open a one-shot client and close it before returning.
    """
    if not market_ticker or end_ts <= start_ts:
        return 0
    _, missing = await fetch_window(
        market_ticker,
        period_interval=period_interval,
        start_ts=start_ts,
        end_ts=end_ts,
    )
    if not missing:
        return 0
    owns_client = False
    if client is None:
        try:
            client = kalshi_client.KalshiClient()
            owns_client = True
        except Exception as e:  # noqa: BLE001
            log.info("tick cache refresh: kalshi client unavailable: %s", e)
            return 0
    written = 0
    try:
        for lo, hi in missing:
            try:
                data = await client.get_market_candlesticks(
                    market_ticker,
                    start_ts=int(lo),
                    end_ts=int(hi),
                    period_interval=int(period_interval),
                )
            except kalshi_client.KalshiAPIError as e:
                log.info(
                    "tick cache refresh: kalshi candlesticks(%s) http %s",
                    market_ticker, e.status,
                )
                continue
            except Exception as e:  # noqa: BLE001
                log.info(
                    "tick cache refresh: kalshi candlesticks(%s) error: %s",
                    market_ticker, e,
                )
                continue
            candles = (data or {}).get("candlesticks") or []
            if not candles:
                continue
            written += await upsert_candles(
                market_ticker,
                period_interval=int(period_interval),
                candles=candles,
            )
    finally:
        if owns_client:
            try:
                await client.aclose()
            except Exception:
                pass
    return written
