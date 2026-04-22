"""Commodities OHLCV bar storage for the commodities-binary vertical.

Adds:
  * `commodities_ohlcv` — append-only-with-upsert table of 1h/4h/1d
    open/high/low/close/volume bars for the 10 supported commodity
    symbols (WTI/GOLD/NG/CORN/SOY/WHEAT/COPPER/SILVER/COFFEE/SUGAR).
    Unique on (symbol, granularity, ts) so the EIA / FRED /
    AlphaVantage poller can blindly re-insert the most recent bar
    without producing duplicates when ticks straddle a bar boundary.

Per task #198.

Revision ID: 0006_commodities_ohlcv
Revises: 0005_swarm_personas_per_vertical
"""
from __future__ import annotations

from alembic import op


revision = "0006_commodities_ohlcv"
down_revision = "0005_swarm_personas_per_vertical"
branch_labels = None
depends_on = None


_SQL = """
CREATE TABLE IF NOT EXISTS commodities_ohlcv (
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

CREATE UNIQUE INDEX IF NOT EXISTS commodities_ohlcv_symbol_granularity_ts_uidx
    ON commodities_ohlcv (symbol, granularity, ts);

CREATE INDEX IF NOT EXISTS commodities_ohlcv_symbol_granularity_ts_desc_idx
    ON commodities_ohlcv (symbol, granularity, ts DESC);
"""


def upgrade() -> None:
    op.execute(_SQL)


def downgrade() -> None:
    raise NotImplementedError("destructive down-migration not provided")
