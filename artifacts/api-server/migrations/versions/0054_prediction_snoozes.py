"""Per-prediction snooze flags (task #832).

The Top-Edges operator tile lists every pending candidate from every
vertical that has cleared the 12-gate pipeline and is awaiting
execution.  Each row gets a one-click "snooze" action so the operator
can defer a candidate without halting the entire vertical.

This migration adds the minimal write-side table the snooze button
writes into.  Read-side queries (the tile, the executor's pre-fire
filter) LEFT JOIN this table and exclude rows whose ``snoozed_until``
is still in the future.

  * Primary key on ``prediction_id`` — one active snooze per
    prediction row, latest write wins via ``ON CONFLICT (prediction_id)
    DO UPDATE``.
  * ``snoozed_until`` is the wall-clock cutoff; once ``now() >=
    snoozed_until`` the row is treated as expired and is no longer
    suppressed by the executor or greyed in the tile.
  * ``by_user`` / ``by_email`` for audit; ``reason`` optional.

Schema-only.  No rows inserted.  Idempotent — re-running the migration
is safe.  Downgrade is a no-op so snooze history survives rollbacks.

Revision ID: 0054_prediction_snoozes
Revises: 0053_niche_sport_outcomes
"""
from __future__ import annotations

from alembic import op


revision = "0054_prediction_snoozes"
down_revision = "0053_niche_sport_outcomes"
branch_labels = None
depends_on = None


_SQL_UP = """
CREATE TABLE IF NOT EXISTS prediction_snoozes (
    prediction_id   bigint       PRIMARY KEY,
    snoozed_until   timestamptz  NOT NULL,
    by_user         text,
    by_email        text,
    reason          text,
    created_at      timestamptz  NOT NULL DEFAULT now(),
    updated_at      timestamptz  NOT NULL DEFAULT now()
);

-- Plain b-tree on `snoozed_until` — a partial index gated on
-- `now()` is not allowed in Postgres (predicates must be immutable),
-- and the executor's `NOT EXISTS (... s.snoozed_until > now())`
-- check uses an index range scan over this column either way.
CREATE INDEX IF NOT EXISTS prediction_snoozes_snoozed_until_idx
    ON prediction_snoozes (snoozed_until);
"""


def upgrade() -> None:
    op.execute(_SQL_UP)


def downgrade() -> None:
    # Snooze history survives rollbacks — no destructive down-migration.
    pass
