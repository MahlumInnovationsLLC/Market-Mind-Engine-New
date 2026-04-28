"""Add ``positions.source`` column to tag the origin of synthetic rows.

The position-drift operator-resolution workflow (task #709, migration
0031) inserts synthetic ``positions`` rows for two paths:

* ``reconciler_orphan`` — auto-skeleton for an exchange position the
  local DB has never heard of.
* ``reconciler_accept_truth`` — operator clicked "accept exchange
  truth" and we need a single row matching the exchange.

Without an explicit source tag the only way to distinguish these from
real fills is to grep the synthetic ``order_id`` prefix
(``recon-orphan-…`` / ``recon-accept-truth-…``), which is brittle. A
nullable ``source`` column gives us a clean filter for audit views
and prevents future code from accidentally treating these as
operator-placed orders.

Purely additive: NULL for every existing row, no ID type changes, no
ALTER on the primary key. Backfill is intentionally NULL — only new
synthetic inserts populate it.
"""
from __future__ import annotations

from alembic import op


revision = "0032_positions_source_column"
down_revision = "0031_position_drift_resolutions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE positions
            ADD COLUMN IF NOT EXISTS source varchar(64)
        """
    )
    # Cheap partial index for the operator audit views — almost every
    # row has source IS NULL (real fills) so the index stays tiny.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_positions_source
            ON positions (source)
         WHERE source IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_positions_source")
    op.execute("ALTER TABLE positions DROP COLUMN IF EXISTS source")
