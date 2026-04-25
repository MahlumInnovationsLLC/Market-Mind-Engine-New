"""Settlement columns for the Kalshi crypto producer (task #424).

The discovery poller (task #180, migration 0016) writes live BTC/ETH
binary markets into ``predictions`` and the orchestrator scores them,
but nothing closes the loop when the underlying Kalshi market settles
— so historical accuracy / PnL reporting can never join a prediction
back to the eventual outcome.

This migration adds three lightweight columns the new
``kalshi_crypto_settlements`` feed (in
``app.feeds.kalshi_crypto_markets``) stamps once Kalshi reports a final
result:

  * ``settled_at``    — UTC timestamp of when we observed the settlement.
  * ``outcome``       — Kalshi's canonical resolution (``'yes'``,
    ``'no'``, ``'void'``); kept as text so backtest / accuracy tooling
    can join on it without tri-state logic for void markets.
  * ``settle_price``  — 1.0 / 0.0 / NULL convenience for backtest math.

Two partial indexes support the producer/consumer split: one for the
settler's "unsettled candidate" scan, one for the reporting side that
slices settled rows by league.

All ``ADD COLUMN`` / ``CREATE INDEX`` clauses use ``IF NOT EXISTS`` so
re-running the migration on a fork that hand-rolled the columns is a
no-op. Per the standing convention, downgrade is a no-op so a rollback
never deletes settlement history.

Revision ID: 0022_predictions_settlement
Revises: 0021_crypto_backtest_runs
"""
from __future__ import annotations

from alembic import op


revision = "0022_predictions_settlement"
down_revision = "0021_crypto_backtest_runs"
branch_labels = None
depends_on = None


_SQL_UP = """
ALTER TABLE predictions
    ADD COLUMN IF NOT EXISTS settled_at   timestamptz,
    ADD COLUMN IF NOT EXISTS outcome      text,
    ADD COLUMN IF NOT EXISTS settle_price double precision;

-- Settler's hot scan: "unsettled markets whose game_time has passed".
-- Partial index keeps it tiny because the bulk of the table is either
-- already settled or still pre-close.
CREATE INDEX IF NOT EXISTS predictions_unsettled_kalshi_idx
    ON predictions (league, game_time)
    WHERE settled_at IS NULL AND kalshi_market_id IS NOT NULL;

-- Reporting side: backtest / accuracy joins want recent settled rows
-- per vertical without scanning the in-flight tail.
CREATE INDEX IF NOT EXISTS predictions_settled_at_idx
    ON predictions (league, settled_at)
    WHERE settled_at IS NOT NULL;
"""


def upgrade() -> None:
    op.execute(_SQL_UP)


def downgrade() -> None:
    # No-op: settlement history must survive rollbacks so backtest and
    # accuracy reporting do not lose their join key.
    pass
