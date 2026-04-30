"""Trades.placed_by provenance column + retro-classification.

Adds a ``placed_by`` text column to ``trades`` that records WHO
initiated the trade — the user via the UI vs the orchestrator /
autotrader / swarm cycle.  The column powers a "Manual / System /
Both" filter on every P&L analytics surface (web + mobile + API).

Allowed values (CHECK constraint enforced):

  * ``manual_ui``         — placed by the user via the web/mobile UI
  * ``orchestrator``      — placed by the cycle / orchestrator path
  * ``autotrader``        — placed by the autotrader (sub-class of orch)
  * ``swarm``             — placed by the swarm consensus path
  * ``operator_recovery`` — created by an operator-claimed reconciliation
                            (see ``order_fills.source = 'operator_manual'``)
  * ``rh_import``         — imported from a Robinhood account snapshot
  * ``unknown``           — provenance could not be determined

Retro-classification (Option B, refined from a 2026-04-30 live probe
of 5,725 production trades).  Priority order — first match wins:

  Rule A.  ``price * quantity > 400`` → ``manual_ui``.
           Operator-confirmed heuristic: bet sizes over $400 are
           always the user's manual orders (the autotrader's Kelly-
           sizing / risk-gate stack caps system-placed bets well below
           that threshold).  Live probe confirmed: 135 / 5,725 trades
           are over $400 and ZERO of them carry the ``swarm_action``
           orchestrator-stamp metadata key — the bimodal split is
           clean.

  Rule B.  Any of the orchestrator-stamped metadata keys present →
           ``orchestrator``.  Keys: ``swarm_action``, ``swarm_data``,
           ``trade_signal``, ``recommended_action``, ``pipeline_stages``.
           These are stamped at insert time by ``app/orchestrator/``
           paths and never appear on user-initiated trades.

  Rule C.  ``metadata->>'source' = 'robinhood_import'`` → ``rh_import``.
           266 imported rows distinct from native trades.

  Rule D.  Everything else → ``unknown``.

Index ``idx_trades_placed_by_executed_at`` supports the analytics
filter (``WHERE placed_by = ... ORDER BY executed_at DESC``).

Defensive ALTER pattern (mirrors 0024 / 0033 / 0038): skip cleanly if
the upstream ``trades`` table doesn't exist on a fresh dev DB.

Downgrade is provided (drop index, drop CHECK, drop column) but is
data-destructive — rollback only in dev, never in prod.
"""
from __future__ import annotations

from alembic import op


revision = "0039_trades_placed_by"
down_revision = "0038_v1_archive_league_inferred_from"
branch_labels = None
depends_on = None


_ALLOWED_VALUES = (
    "manual_ui",
    "orchestrator",
    "autotrader",
    "swarm",
    "operator_recovery",
    "rh_import",
    "unknown",
)
_CHECK_LIST_SQL = ", ".join(f"'{v}'" for v in _ALLOWED_VALUES)


# Orchestrator-stamped metadata keys.  Presence of ANY of these is a
# strong signal that the trade was placed by the orchestrator/swarm
# path.  Live probe counts (5,725 trades total): pipeline_stages 2444,
# swarm_data 304, trade_signal 302, recommended_action 140,
# swarm_action 22.
_ORCH_KEYS = (
    "swarm_action", "swarm_data", "trade_signal",
    "recommended_action", "pipeline_stages",
)
_ORCH_PREDICATE = " OR ".join(f"metadata ? '{k}'" for k in _ORCH_KEYS)


_SQL_UP = f"""
DO $$
BEGIN
    -- Skip cleanly on dev DBs without the upstream trades schema.
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'trades'
    ) THEN
        RETURN;
    END IF;

    -- 1. Add the column with a safe default so existing INSERT paths
    --    keep working until they're updated to pass placed_by.
    ALTER TABLE trades
        ADD COLUMN IF NOT EXISTS placed_by text NOT NULL DEFAULT 'unknown';

    -- 2. Retro-classify.  Order matters — the manual-UI rule (A) wins
    --    over the orchestrator-key rule (B) because a $400+ bet is a
    --    stronger signal than the presence of a metadata key (which
    --    could leak in via a future code path that touches metadata
    --    on a user-initiated trade).  Each UPDATE re-asserts the
    --    "still at default" predicate so re-running the migration
    --    after a crash doesn't clobber rows already classified.

    -- Rule A: notional > $400 → manual_ui (user heuristic).
    UPDATE trades
       SET placed_by = 'manual_ui'
     WHERE placed_by = 'unknown'
       AND price IS NOT NULL
       AND quantity IS NOT NULL
       AND (price * quantity) > 400;

    -- Rule B: orchestrator-stamped metadata key present → orchestrator.
    UPDATE trades
       SET placed_by = 'orchestrator'
     WHERE placed_by = 'unknown'
       AND metadata IS NOT NULL
       AND ({_ORCH_PREDICATE});

    -- Rule C: Robinhood-import provenance → rh_import.
    UPDATE trades
       SET placed_by = 'rh_import'
     WHERE placed_by = 'unknown'
       AND metadata->>'source' = 'robinhood_import';

    -- 3. Lock the column to the allowed value set.  Constraint added
    --    AFTER the retro-classify so partially-classified state during
    --    the data migration can't trip the check.
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'trades_placed_by_check'
    ) THEN
        ALTER TABLE trades
            ADD CONSTRAINT trades_placed_by_check
            CHECK (placed_by IN ({_CHECK_LIST_SQL}));
    END IF;

    -- 4. Filter-friendly composite index.  ``executed_at DESC`` is the
    --    natural P&L pagination order.
    CREATE INDEX IF NOT EXISTS idx_trades_placed_by_executed_at
        ON trades (placed_by, executed_at DESC);
END$$;
"""


_SQL_DOWN = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'trades'
    ) THEN
        RETURN;
    END IF;
    DROP INDEX IF EXISTS idx_trades_placed_by_executed_at;
    ALTER TABLE trades DROP CONSTRAINT IF EXISTS trades_placed_by_check;
    ALTER TABLE trades DROP COLUMN IF EXISTS placed_by;
END$$;
"""


def upgrade() -> None:
    op.execute(_SQL_UP)


def downgrade() -> None:
    op.execute(_SQL_DOWN)
