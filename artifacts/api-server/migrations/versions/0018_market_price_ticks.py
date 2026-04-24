"""Persist Kalshi market candlestick (tick) history for the trade-detail
chart so revisits don't have to round-trip the live exchange (task #359).

Every load of ``/trade/:ref`` used to hit Kalshi's
``/series/.../markets/.../candlesticks`` endpoint synchronously. For
markets that traders revisit (high-volume games, settled-but-popular
markets) that's wasted work; for live operator views it also makes us
hostage to any 5xx blip on the Kalshi candles endpoint.

This migration adds ``market_price_ticks`` — a small time-series
table keyed on ``(market_ticker, period_interval, bucket_ts)`` that
caches the close / mean cents Kalshi returns for each bucket.  The
trade-detail handler reads from it first, only calls Kalshi for
buckets that aren't already cached, and writes every fresh fetch
back; a separate background loop refreshes the recent window for
recently-traded markets so the next page load is served entirely
out of Postgres.

Schema notes:
  * ``period_interval`` is the Kalshi bar size in minutes (1 / 60 /
    1440).  The same market can carry rows at multiple bar sizes
    when different trade detail windows ask for different bar
    sizes.
  * ``bucket_ts`` mirrors Kalshi's ``end_period_ts`` (UTC, seconds-
    precision) so the on-the-wire normalisation is one-to-one with
    a row.
  * ``close_cents`` / ``mean_cents`` are nullable because Kalshi
    nulls them for empty windows; we still record the row so the
    cache covers the bucket and we don't refetch it.
  * ``fetched_at`` is bumped on conflict so the refresh job knows
    when each bucket was last revalidated.

Per repo convention there is no destructive down-migration — the
cache simply rebuilds itself from the next live Kalshi call when
the table is missing, but a rollback that drops the table mid-flight
would force every concurrent request back onto the synchronous live
path until the table came back.

Revision ID: 0018_market_price_ticks
Revises: 0017_precip_emos_observations,
         0017_weather_precip_today,
         0017_weather_temp_trained_models
"""
from __future__ import annotations

from alembic import op


revision = "0018_market_price_ticks"
down_revision = (
    "0017_precip_emos_observations",
    "0017_weather_precip_today",
    "0017_weather_temp_trained_models",
)
branch_labels = None
depends_on = None


_SQL_UP = """
CREATE TABLE IF NOT EXISTS market_price_ticks (
    market_ticker    text        NOT NULL,
    period_interval  int         NOT NULL,
    bucket_ts        timestamptz NOT NULL,
    close_cents      int,
    mean_cents       int,
    fetched_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (market_ticker, period_interval, bucket_ts)
);

CREATE INDEX IF NOT EXISTS market_price_ticks_lookup_idx
    ON market_price_ticks (market_ticker, period_interval, bucket_ts DESC);

CREATE INDEX IF NOT EXISTS market_price_ticks_fetched_at_idx
    ON market_price_ticks (fetched_at DESC);
"""


def upgrade() -> None:
    op.execute(_SQL_UP)


def downgrade() -> None:
    # No-op on purpose — the trade-detail handler falls back to live
    # Kalshi calls when the cache is empty, but dropping the table
    # mid-flight would convert every concurrent /trade/:ref load back
    # into a synchronous candlesticks round-trip and (worse) lose
    # the buckets we'd already cached for settled markets that Kalshi
    # may eventually expire from its own retention window.  Keep the
    # data; let migration history move forward only.
    pass
