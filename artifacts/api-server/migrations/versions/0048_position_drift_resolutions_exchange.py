"""Add ``exchange`` column to ``position_drift_resolutions`` so the
operator-resolution queue (originally Kalshi-only, task #709) can serve
the Polymarket reconciler too without a second table (task #715).

Strategy:
  * Add the column with default ``'kalshi'`` so existing rows backfill
    silently to the only exchange that wrote to the table before this
    migration ran.
  * Drop and re-create the partial unique index on ``(exchange,
    ticker)`` so ``(kalshi, MKT-A)`` and ``(polymarket, MKT-A)`` can
    coexist as independent active queue rows.
  * Drop / re-create the queue scan index to lead with ``exchange``
    so the per-venue UI tab fans out via index lookup.

Purely additive on the data side — existing kalshi rows keep the same
behaviour because every callsite defaults ``exchange='kalshi'`` when
the parameter is omitted.
"""
from __future__ import annotations

from alembic import op


revision = "0048_position_drift_resolutions_exchange"
down_revision = "0047_soccer_settlement_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE position_drift_resolutions
            ADD COLUMN IF NOT EXISTS exchange varchar(32)
                NOT NULL DEFAULT 'kalshi'
        """
    )
    # Belt-and-braces backfill in case the column already existed
    # without the default (e.g. partially-applied historical schema).
    op.execute(
        """
        UPDATE position_drift_resolutions
           SET exchange = 'kalshi'
         WHERE exchange IS NULL OR exchange = ''
        """
    )
    # Replace the ticker-only partial unique index with a per-exchange
    # one so each venue gets its own active row per ticker.
    op.execute(
        "DROP INDEX IF EXISTS idx_pos_drift_res_active_ticker"
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS
            idx_pos_drift_res_active_exchange_ticker
            ON position_drift_resolutions (exchange, ticker)
         WHERE state IN ('open', 'snoozed')
        """
    )
    # Re-create the queue scan index leading with exchange so the
    # per-venue UI filter is index-served.
    op.execute(
        "DROP INDEX IF EXISTS idx_pos_drift_res_state_seen"
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_pos_drift_res_exch_state_seen
            ON position_drift_resolutions
               (exchange, state, last_seen_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_pos_drift_res_exch_state_seen")
    op.execute(
        "DROP INDEX IF EXISTS idx_pos_drift_res_active_exchange_ticker"
    )
    # Restore the original kalshi-only indexes from migration 0031.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_pos_drift_res_active_ticker
            ON position_drift_resolutions (ticker)
         WHERE state IN ('open', 'snoozed')
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_pos_drift_res_state_seen
            ON position_drift_resolutions (state, last_seen_at DESC)
        """
    )
    op.execute(
        """
        ALTER TABLE position_drift_resolutions
            DROP COLUMN IF EXISTS exchange
        """
    )
