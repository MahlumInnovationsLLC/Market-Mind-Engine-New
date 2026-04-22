"""Append-only bankroll events feed.

Adds:
  * `bankroll_events` — every change to live bankroll (deposits, withdrawals,
    fills, fees, and the periodic exchange-sync delta from the Kalshi
    `/portfolio/balance` poller) lands here as a single row. The bankroll-
    floor risk gate and the equity curve both read the cumulative sum so
    they always reflect reality rather than a stale daily snapshot.

The table is intentionally minimal: balance is stored in cents (integer) so
there's no floating-point drift over thousands of rows. `reason` is a free
short string — the emergency-stop / sync loop / fill ingestion paths each
use a stable token (`exchange_sync`, `fill`, `deposit`, `withdrawal`,
`manual_adjustment`).

Per task #104.

Revision ID: 0003_bankroll_events
Revises: 0002_order_fills_position_rollup
"""
from __future__ import annotations

from alembic import op


revision = "0003_bankroll_events"
down_revision = "0002_order_fills_position_rollup"
branch_labels = None
depends_on = None


_SQL = """
CREATE TABLE IF NOT EXISTS bankroll_events (
    id              bigserial   PRIMARY KEY,
    occurred_at     timestamptz NOT NULL DEFAULT now(),
    exchange        text        NOT NULL DEFAULT 'kalshi',
    reason          text        NOT NULL,
    delta_cents     bigint      NOT NULL,
    balance_cents   bigint      NOT NULL,
    details         jsonb       NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS bankroll_events_occurred_at_idx
    ON bankroll_events (occurred_at DESC);

CREATE INDEX IF NOT EXISTS bankroll_events_exchange_occurred_at_idx
    ON bankroll_events (exchange, occurred_at DESC);
"""


def upgrade() -> None:
    op.execute(_SQL)


def downgrade() -> None:
    raise NotImplementedError("destructive down-migration not provided")
