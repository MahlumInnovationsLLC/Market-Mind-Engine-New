"""Best-line cross-book router operator actions (task #828).

When ``gate_best_line`` blocks a Kalshi candidate because a traditional
sportsbook is offering a meaningfully better implied price, the
operator gets a ``best_line_alert`` and (optionally) places the bet
manually on the named book.  This table records that manual
placement so the daily honesty report and the operator dashboard tile
can reconcile P&L afterwards.

Schema is intentionally minimal — every richer attribute (the book
name, our implied, the better implied, the delta) is already stored
verbatim in the ``notification_dedupe`` alert envelope.  This table
only owns the operator-action side of the loop.
"""
from __future__ import annotations

from alembic import op


revision = "0051_best_line_alert_actions"
down_revision = "0050_wnba_player_props"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS best_line_alert_actions (
            id                bigserial PRIMARY KEY,
            dedupe_key        text NOT NULL UNIQUE,
            action            text NOT NULL,
            operator_email    text,
            placed_book       text,
            placed_amount_usd double precision,
            note              text,
            created_at        timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_best_line_alert_actions_created
            ON best_line_alert_actions (created_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_best_line_alert_actions_created")
    op.execute("DROP TABLE IF EXISTS best_line_alert_actions")
