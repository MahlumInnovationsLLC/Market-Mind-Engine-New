"""Per-binding Telegram push opt-out + merge of 0014 heads.

Per task #186. Adds a `notify_push` boolean column to
``telegram_bindings`` so individual operators can mute the proactive
halt / emergency-stop pings without fully unbinding the chat.

Defaults to ``true`` so every existing binding continues to receive
broadcasts unless the operator explicitly opts out.

This revision also merges the two sibling 0014 heads
(``0014_pipeline_events`` and ``0014_settlement_events``) so the
script directory has a single tip again.

Revision ID: 0015_telegram_notify_push
"""
from __future__ import annotations

from alembic import op


revision = "0015_telegram_notify_push"
down_revision = (
    "0014_pipeline_events",
    "0014_settlement_events",
)
branch_labels = None
depends_on = None


_SQL = """
ALTER TABLE telegram_bindings
    ADD COLUMN IF NOT EXISTS notify_push boolean NOT NULL DEFAULT true;
"""


def upgrade() -> None:
    op.execute(_SQL)


def downgrade() -> None:
    op.execute("ALTER TABLE telegram_bindings DROP COLUMN IF EXISTS notify_push")
