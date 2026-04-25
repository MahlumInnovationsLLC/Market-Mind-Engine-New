"""Persist scheduled crypto-binary backtest results (task #421).

The new ``crypto_backtest_runs`` table stores one row per scheduled
``run_backtest`` invocation so the dashboard can chart accuracy /
Brier / log-loss as a trend, and the drift watcher can compare each
fresh run against a rolling baseline of prior runs for the same
``(symbol, timeframe)`` pair.

Idempotent ``CREATE TABLE IF NOT EXISTS`` so re-running the migration
on a fork that was created with a hand-rolled DDL is a no-op.
"""
from __future__ import annotations

from alembic import op


revision = "0021_crypto_backtest_runs"
down_revision = "0020_llm_predictions_created_at"
branch_labels = None
depends_on = None


_SQL_UP = """
CREATE TABLE IF NOT EXISTS crypto_backtest_runs (
    id            BIGSERIAL PRIMARY KEY,
    symbol        TEXT NOT NULL,
    timeframe     TEXT NOT NULL,
    days          INTEGER NOT NULL,
    lookback      INTEGER NOT NULL,
    n_evaluated   INTEGER NOT NULL,
    accuracy      DOUBLE PRECISION NOT NULL,
    brier_score   DOUBLE PRECISION NOT NULL,
    log_loss      DOUBLE PRECISION NOT NULL,
    reliability   JSONB NOT NULL DEFAULT '[]'::jsonb,
    started_at    TIMESTAMPTZ NOT NULL,
    completed_at  TIMESTAMPTZ NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS crypto_backtest_runs_pair_idx
    ON crypto_backtest_runs (symbol, timeframe, completed_at DESC);

CREATE INDEX IF NOT EXISTS crypto_backtest_runs_completed_idx
    ON crypto_backtest_runs (completed_at DESC);
"""


_SQL_DOWN = """
DROP INDEX IF EXISTS crypto_backtest_runs_completed_idx;
DROP INDEX IF EXISTS crypto_backtest_runs_pair_idx;
DROP TABLE IF EXISTS crypto_backtest_runs;
"""


def upgrade() -> None:
    op.execute(_SQL_UP)


def downgrade() -> None:
    op.execute(_SQL_DOWN)
