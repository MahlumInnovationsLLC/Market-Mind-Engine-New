"""Persist ``side`` (YES/NO) on edge_terminal_signals (task #844 audit fix).

The scraper already extracts ``side`` from the pick tile (data-side attr
or ``Under``/``No``/``Below`` text heuristic) and the executor honours
it via :class:`RiskContext.side`.  Before this migration the column did
not exist, so the poller dropped the field on INSERT and ``executor.
drain_pending`` reloaded the row without it — making every fired pick
default to YES regardless of what the scraper saw.  That's a
wrong-side-trade bug for any ``Under``/``No``/``Below`` selection.

Revision ID: 0056_edge_terminal_signal_side
Revises: 0055_edge_terminal_signals
"""
from __future__ import annotations

from alembic import op


revision = "0056_edge_terminal_signal_side"
down_revision = "0055_edge_terminal_signals"
branch_labels = None
depends_on = None


_SQL_UP = """
ALTER TABLE edge_terminal_signals
    ADD COLUMN IF NOT EXISTS side text NOT NULL DEFAULT 'YES'
    CHECK (side IN ('YES', 'NO'));
"""


def upgrade() -> None:
    op.execute(_SQL_UP)


def downgrade() -> None:
    pass
