"""v1_backtest_gate_runs: persisted history of the weekly V1 backtest.

Each row is one weekly cron run of the V1 backtest harness against the
current calibrators (Task #756, T-22 on the win-rate roadmap).  The
dashboard tile reads the latest row plus a short history strip so the
operator can see the gate trip the moment a per-league Brier regresses
against last week's snapshot.

Columns:

  * ``parity_gate_status`` mirrors the JSON the script already writes
    to ``artifacts/api-server/reports/`` and the audit narrative in
    ``replit.md``.
  * ``per_league_brier`` is a flat ``{league: brier}`` dict so the
    regression check is a cheap dict-diff, no nested digging.
  * ``regressed_leagues`` is the array of league codes whose current
    Brier is worse than the prior row's by more than the harness's
    standing tolerance (``_BRIER_TOLERANCE``); empty array == green.
  * ``report_path`` is the on-disk JSON path the cron wrote to so the
    operator can grab the full report without re-running the audit.

Revision ID: 0046_v1_backtest_gate_runs
Revises: 0045_risk_gate_trips
"""
from __future__ import annotations

from alembic import op


revision = "0046_v1_backtest_gate_runs"
down_revision = "0045_risk_gate_trips"
branch_labels = None
depends_on = None


_SQL_UP = """
CREATE TABLE IF NOT EXISTS v1_backtest_gate_runs (
    id                  bigserial PRIMARY KEY,
    run_at              timestamptz NOT NULL DEFAULT now(),
    overall_verdict     text        NOT NULL,
    parity_gate_status  jsonb       NOT NULL,
    per_league_brier    jsonb,
    regressed_leagues   text[]      NOT NULL DEFAULT '{}',
    report_path         text,
    duration_seconds    double precision,
    notes               text
);

CREATE INDEX IF NOT EXISTS v1_backtest_gate_runs_run_at_idx
    ON v1_backtest_gate_runs (run_at DESC);
"""

_SQL_DOWN = """
DROP INDEX IF EXISTS v1_backtest_gate_runs_run_at_idx;
DROP TABLE IF EXISTS v1_backtest_gate_runs;
"""


def upgrade() -> None:
    op.execute(_SQL_UP)


def downgrade() -> None:
    op.execute(_SQL_DOWN)
