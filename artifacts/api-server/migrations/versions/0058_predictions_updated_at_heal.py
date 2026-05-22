"""Heal predictions.updated_at on lazy-DDL databases (task #821).

Migration 0016 originally created the ``predictions`` table with both
``created_at`` and ``updated_at`` columns.  However, some environments
provisioned the table via a lazy ``CREATE TABLE IF NOT EXISTS`` path
*before* migration 0016 ran (or with ``AXIOM_SKIP_ALEMBIC_AT_STARTUP=1``
set), and those installs are missing the ``updated_at`` column entirely.

When code referenced ``predictions.updated_at`` on those databases it
raised ``column "updated_at" does not exist``.  Commit 429a6ed worked
around the breakage in four call sites (most notably the shadow
predictor's active scan) by switching to ``created_at``, but the
schema drift itself was never repaired.  This migration repairs it so
future code that *does* need a real "last touched" timestamp can rely
on the column being present everywhere.

``ADD COLUMN IF NOT EXISTS`` makes this a no-op on healthy databases
that already have the column from migration 0016.  The default of
``now()`` backfills existing rows with the migration timestamp, which
is a strictly better signal than NULL for freshness comparisons.
"""
from __future__ import annotations

from alembic import op


revision = "0058_predictions_updated_at_heal"
down_revision = "0057_edge_terminal_signal_enrichment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE predictions
            ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now()
        """
    )


def downgrade() -> None:
    # Intentionally a no-op: migration 0016 already declares this column
    # on fresh installs, so dropping it on downgrade would corrupt those
    # databases.  This migration only heals drifted environments.
    pass
