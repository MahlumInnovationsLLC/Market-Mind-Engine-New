"""Persist completed crypto-binary backtest runs for trend visualisation.

Adds:
  * ``crypto_backtest_runs`` — append-only table of completed
    expanding-window backtests.  One row per ``run_backtest`` call so
    the operator console can plot Brier / log-loss / accuracy trend
    lines per (symbol, timeframe) pair without re-running on demand.
  * ``crypto_backtest_runs_pair_completed_idx`` — supporting index on
    (symbol, timeframe, completed_at DESC) so the history endpoint's
    "most-recent-N rows for a pair" query stays cheap.

Per task #422 (Show the crypto backtest history as a trend chart).
"""
from __future__ import annotations

from alembic import op


revision = "0021_crypto_backtest_runs"
down_revision = "0020_llm_predictions_created_at"
branch_labels = None
depends_on = None


_SQL_UP = """
CREATE TABLE IF NOT EXISTS crypto_backtest_runs (
    id            bigserial         PRIMARY KEY,
    symbol        text              NOT NULL,
    timeframe     text              NOT NULL,
    days          integer           NOT NULL,
    lookback      integer           NOT NULL,
    n_evaluated   integer           NOT NULL,
    accuracy      double precision  NOT NULL,
    brier_score   double precision  NOT NULL,
    log_loss      double precision  NOT NULL,
    started_at    timestamptz,
    completed_at  timestamptz       NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS crypto_backtest_runs_pair_completed_idx
    ON crypto_backtest_runs (symbol, timeframe, completed_at DESC);
"""

_SQL_DOWN = """
DROP INDEX IF EXISTS crypto_backtest_runs_pair_completed_idx;
DROP TABLE IF EXISTS crypto_backtest_runs;
"""


def upgrade() -> None:
    op.execute(_SQL_UP)


def downgrade() -> None:
    op.execute(_SQL_DOWN)
