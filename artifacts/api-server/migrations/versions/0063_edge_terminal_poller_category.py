"""Edge Terminal poller outcome category (task #950).

Adds ``last_error_category`` to ``edge_terminal_poller_status`` so each
poll tick records *why* it produced no trades (one of ``ok`` /
``not_configured`` / ``browser_missing`` / ``login_failed`` /
``no_rows`` / ``error``).  The poller writes it on every tick and the
dashboard tile + health probe + admin self-check read it to surface a
clear, actionable reason instead of a silent empty result.

Revision ID: 0063_edge_terminal_poller_category
Revises: 0062_dashboard_vertical_indexes
"""
from __future__ import annotations

from alembic import op


revision = "0063_edge_terminal_poller_category"
down_revision = "0062_dashboard_vertical_indexes"
branch_labels = None
depends_on = None


_SQL_UP = """
ALTER TABLE edge_terminal_poller_status
    ADD COLUMN IF NOT EXISTS last_error_category text;
"""


def upgrade() -> None:
    op.execute(_SQL_UP)


def downgrade() -> None:
    # Schema-only artefact — leave history intact on rollback.
    pass
