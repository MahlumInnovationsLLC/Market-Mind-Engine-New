"""Trades.metadata jsonb column (defensive add).

Per task #444.  The settlement pipeline already SELECTs ``metadata``
out of the ``trades`` table (see ``app.settlement.pipeline.settle_market``)
and the orchestrator now stamps a ``three_way_leg`` value into it for
soccer h/d/a candidates so the per-leg outcome derivation kicks in
end-to-end.  The production schema already carries the column; this
migration is the same defensive ALTER pattern used for ``market_title``
in the baseline so that:

  * fresh dev databases without the upstream trades schema (i.e. the
    ``trades`` table doesn't exist at all) skip the ALTER cleanly, and
  * forks that have ``trades`` but never received the column from the
    upstream production schema get it backfilled here.

The column defaults to an empty jsonb object so existing rows read back
as ``{}`` (matches the pattern in ``autoresearch_features.metadata``)
and the orchestrator's ``COALESCE($14::jsonb, '{}'::jsonb)`` insert
clause produces the same value for trades without any metadata.
"""
from __future__ import annotations

from alembic import op


revision = "0024_trades_metadata"
down_revision = "0023_model_health_alerts_seq_repair"
branch_labels = None
depends_on = None


_SQL_UP = """
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'trades'
    ) THEN
        ALTER TABLE trades
            ADD COLUMN IF NOT EXISTS metadata jsonb NOT NULL DEFAULT '{}'::jsonb;
    END IF;
END$$;
"""


def upgrade() -> None:
    op.execute(_SQL_UP)


def downgrade() -> None:
    # Out of scope per repo convention — the baseline migration also
    # raises NotImplementedError, and dropping the column would
    # destroy the per-trade `three_way_leg` annotations the
    # settlement pipeline depends on.
    raise NotImplementedError("destructive down-migration not provided")
