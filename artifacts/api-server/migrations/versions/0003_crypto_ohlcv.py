"""Crypto OHLCV bar storage for the crypto-binary vertical.

Adds:
  * `crypto_ohlcv` — append-only-with-upsert table of 1m/5m/15m/1h
    open/high/low/close/volume bars for BTC and ETH.  Unique on
    (symbol, granularity, ts) so the poller can blindly re-insert the
    most recent bar(s) without producing duplicates when two ticks
    overlap on the same bar boundary.
  * `crypto_ohlcv_symbol_granularity_ts_idx` — supporting index for
    the predictor's range scans (most-recent-N bars per market).

Per task #92.

Revision ID: 0003_crypto_ohlcv
Revises: 0002_order_fills_position_rollup
"""
from __future__ import annotations

from alembic import op


revision = "0003_crypto_ohlcv"
down_revision = "0002_order_fills_position_rollup"
branch_labels = None
depends_on = None


_SQL = """
CREATE TABLE IF NOT EXISTS crypto_ohlcv (
    symbol       text             NOT NULL,
    granularity  text             NOT NULL,
    ts           timestamptz      NOT NULL,
    o            double precision NOT NULL,
    h            double precision NOT NULL,
    l            double precision NOT NULL,
    c            double precision NOT NULL,
    v            double precision NOT NULL,
    source       text             NOT NULL,
    inserted_at  timestamptz      NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS crypto_ohlcv_symbol_granularity_ts_uidx
    ON crypto_ohlcv (symbol, granularity, ts);

CREATE INDEX IF NOT EXISTS crypto_ohlcv_symbol_granularity_ts_desc_idx
    ON crypto_ohlcv (symbol, granularity, ts DESC);
"""


def upgrade() -> None:
    op.execute(_SQL)


def downgrade() -> None:
    raise NotImplementedError("destructive down-migration not provided")
