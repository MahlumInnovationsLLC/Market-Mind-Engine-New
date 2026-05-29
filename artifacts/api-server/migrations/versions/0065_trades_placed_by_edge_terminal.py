"""Allow ``edge_terminal`` + ``canary`` in trades.placed_by.

The Edge Terminal executor threads ``placed_by='edge_terminal'`` through
:func:`paper.place_order` (see ``app/edge_terminal/executor.py``), and the
live-trading canary proof-trade records ``placed_by='canary'``.  Neither
value was in the original ``trades_placed_by_check`` allow-list from
``0039_trades_placed_by``, so the FIRST real Edge Terminal order would
submit to Kalshi and then 400 the local ``trades`` insert on the CHECK
constraint — the order fills on the venue but is never recorded locally
(an auditability hole, and a hard error on the synchronous paper path).

This migration drops and re-creates the constraint with the two new
provenance values appended.  Existing rows are unaffected.

Allowed values after this migration:
  manual_ui, orchestrator, autotrader, swarm, operator_recovery,
  rh_import, unknown, edge_terminal, canary

Defensive ALTER pattern (mirrors 0039): skip cleanly if the ``trades``
table doesn't exist on a fresh dev DB.  Downgrade restores the prior
allow-list — but FAILS if any row already uses one of the new values
(re-tag those rows first), so rollback is dev-only.
"""
from __future__ import annotations

from alembic import op


revision = "0065_trades_placed_by_edge_terminal"
down_revision = "0064_edge_terminal_signal_decision"
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
    "edge_terminal",
    "canary",
)
_CHECK_LIST_SQL = ", ".join(f"'{v}'" for v in _ALLOWED_VALUES)


_PRIOR_VALUES = (
    "manual_ui",
    "orchestrator",
    "autotrader",
    "swarm",
    "operator_recovery",
    "rh_import",
    "unknown",
)
_PRIOR_LIST_SQL = ", ".join(f"'{v}'" for v in _PRIOR_VALUES)


_SQL_UP = f"""
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'trades'
    ) THEN
        RETURN;
    END IF;

    ALTER TABLE trades DROP CONSTRAINT IF EXISTS trades_placed_by_check;
    ALTER TABLE trades
        ADD CONSTRAINT trades_placed_by_check
        CHECK (placed_by IN ({_CHECK_LIST_SQL}));
END$$;
"""


_SQL_DOWN = f"""
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'trades'
    ) THEN
        RETURN;
    END IF;

    ALTER TABLE trades DROP CONSTRAINT IF EXISTS trades_placed_by_check;
    ALTER TABLE trades
        ADD CONSTRAINT trades_placed_by_check
        CHECK (placed_by IN ({_PRIOR_LIST_SQL}));
END$$;
"""


def upgrade() -> None:
    op.execute(_SQL_UP)


def downgrade() -> None:
    op.execute(_SQL_DOWN)
