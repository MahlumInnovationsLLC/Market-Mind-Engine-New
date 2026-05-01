"""Add ``trading_mode`` to ``bankroll_events`` and ``positions``.

Round-11 audit, Bug #6/7: dashboard, daily-loss circuit breaker, and the
bankroll ledger silently mix paper and live data because two of the three
trade-related tables had no ``trading_mode`` discriminator.  The ``trades``
table already carries the column (every orchestrator INSERT stamps
``'paper'`` or ``'live'``); this migration brings the other two ledgers
into line so every read query in the dashboard and the daily-loss snapshot
can scope cleanly to one mode.

Defensive ALTER pattern (mirrors 0024 / 0039 / 0040): both tables are owned
by the upstream production schema, so we only alter when the table actually
exists on the target DB.  Existing rows are backfilled to ``'live'`` —
the only path that historically wrote into ``bankroll_events`` is the
Kalshi exchange sync (``app/bankroll_sync.py``) which by definition
captures live exchange balance, and the only path that writes into
``positions`` is ``app/kalshi/lifecycle.py`` / ``app/kalshi/reconciler.py``
on confirmed live fills.  Tagging legacy rows ``'live'`` matches their
provenance and means flipping the dashboard toggle to ``paper`` shows the
empty-state UX (no historical paper bankroll, no historical paper
positions) the operator actually expects rather than the data being
silently mis-attributed.

A CHECK constraint pins the enum to ``('paper','live')`` on both columns
so a typo in a future code path can't slip through and become
unaccountable data.

Downgrade is data-safe (drops the column + constraint) but does not
restore any state — the column never existed before.
"""
from __future__ import annotations

from alembic import op


revision = "0041_paper_live_trading_mode"
down_revision = "0040_trades_no_unknowns"
branch_labels = None
depends_on = None


_SQL_UP = """
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'bankroll_events'
    ) THEN
        ALTER TABLE bankroll_events
            ADD COLUMN IF NOT EXISTS trading_mode text;
        UPDATE bankroll_events
           SET trading_mode = 'live'
         WHERE trading_mode IS NULL;
        ALTER TABLE bankroll_events
            ALTER COLUMN trading_mode SET DEFAULT 'live';
        ALTER TABLE bankroll_events
            ALTER COLUMN trading_mode SET NOT NULL;
        ALTER TABLE bankroll_events
            DROP CONSTRAINT IF EXISTS bankroll_events_trading_mode_check;
        ALTER TABLE bankroll_events
            ADD CONSTRAINT bankroll_events_trading_mode_check
            CHECK (trading_mode IN ('paper', 'live'));
        CREATE INDEX IF NOT EXISTS bankroll_events_mode_id_idx
            ON bankroll_events (trading_mode, id DESC);
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'positions'
    ) THEN
        ALTER TABLE positions
            ADD COLUMN IF NOT EXISTS trading_mode text;
        UPDATE positions
           SET trading_mode = 'live'
         WHERE trading_mode IS NULL;
        ALTER TABLE positions
            ALTER COLUMN trading_mode SET DEFAULT 'live';
        ALTER TABLE positions
            ALTER COLUMN trading_mode SET NOT NULL;
        ALTER TABLE positions
            DROP CONSTRAINT IF EXISTS positions_trading_mode_check;
        ALTER TABLE positions
            ADD CONSTRAINT positions_trading_mode_check
            CHECK (trading_mode IN ('paper', 'live'));
        CREATE INDEX IF NOT EXISTS positions_mode_status_idx
            ON positions (trading_mode, status);
    END IF;
END$$;
"""


_SQL_DOWN = """
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'bankroll_events'
    ) THEN
        DROP INDEX IF EXISTS bankroll_events_mode_id_idx;
        ALTER TABLE bankroll_events
            DROP CONSTRAINT IF EXISTS bankroll_events_trading_mode_check;
        ALTER TABLE bankroll_events
            DROP COLUMN IF EXISTS trading_mode;
    END IF;
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'positions'
    ) THEN
        DROP INDEX IF EXISTS positions_mode_status_idx;
        ALTER TABLE positions
            DROP CONSTRAINT IF EXISTS positions_trading_mode_check;
        ALTER TABLE positions
            DROP COLUMN IF EXISTS trading_mode;
    END IF;
END$$;
"""


def upgrade() -> None:
    op.execute(_SQL_UP)


def downgrade() -> None:
    op.execute(_SQL_DOWN)
