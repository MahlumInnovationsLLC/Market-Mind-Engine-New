"""Inline model YES probability on predictions (task #646).

The crypto accuracy report introduced in task #580 joins each settled
``predictions`` row to the most recent
``swarm_debates.consensus_probability`` for the same Kalshi market so
it can score realised Brier / log-loss.  The lateral join means rows
without a matching debate (legacy crypto rows from before the swarm
was wired up, or markets that settled before any debate landed) are
counted under ``n_unscored`` and excluded from the metrics.

This migration adds ``predictions.model_yes_prob`` so the orchestrator
can stamp the model's YES probability inline at scoring time, removing
the join.  It pairs with:

  * ``app.orchestrator.cycle`` — writes the value as soon as the gates
    have a real probability for the candidate.
  * ``app.crypto.probability_backfill`` — one-shot operator-triggered
    task that copies historical ``swarm_debates.consensus_probability``
    onto matching prediction rows for the legacy tail.
  * ``app.crypto.accuracy_report`` — simplified SELECT that reads the
    column directly instead of laterally joining ``swarm_debates``.

A partial index lets the backfill scan locate "settled rows still
missing a probability" cheaply without dragging the live tail along
for the ride.

``ADD COLUMN IF NOT EXISTS`` / ``CREATE INDEX IF NOT EXISTS`` keep the
upgrade safe to re-run on a fork that already added the column by
hand.  Per the standing convention the downgrade is a no-op so a
rollback never drops persisted scoring history.

Revision ID: 0026_predictions_model_yes_prob
Revises: 0025_sportradar_tournament_leaderboards
"""
from __future__ import annotations

from alembic import op


revision = "0026_predictions_model_yes_prob"
down_revision = "0025_sportradar_tournament_leaderboards"
branch_labels = None
depends_on = None


_SQL_UP = """
ALTER TABLE predictions
    ADD COLUMN IF NOT EXISTS model_yes_prob double precision;

-- Backfill helper index: scoped to settled rows that are still missing
-- a model probability so the one-shot ``probability_backfill`` task
-- can locate its targets without a sequential scan over the live tail.
CREATE INDEX IF NOT EXISTS predictions_unscored_settled_idx
    ON predictions (league, settled_at)
    WHERE settled_at IS NOT NULL AND model_yes_prob IS NULL;
"""


def upgrade() -> None:
    op.execute(_SQL_UP)


def downgrade() -> None:
    # No-op: dropping the column would erase scored history that the
    # accuracy report depends on across rollbacks.
    pass
