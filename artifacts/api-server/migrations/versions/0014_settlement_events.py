"""Persist recent market_settle_complete events.

Per task #86: the operator dashboard "recent market settlements" panel
was sourced from an in-process ring buffer (`_RECENT_SETTLEMENTS_BUF`)
that was wiped on every API restart.  We now write each
``market_settle_complete`` event to ``settlement_events`` so the panel
survives restarts and is consistent across replicas.

Revision ID: 0014_settlement_events
Revises: 0013_rc_webhook_deliveries
"""
from __future__ import annotations

from alembic import op


revision = "0014_settlement_events"
down_revision = "0013_rc_webhook_deliveries"
branch_labels = None
depends_on = None


_SQL_UP = """
CREATE TABLE IF NOT EXISTS settlement_events (
    id              bigserial   PRIMARY KEY,
    recorded_at     timestamptz NOT NULL DEFAULT now(),
    event_type      text        NOT NULL,
    market_id       text        NULL,
    outcome_yes     integer     NULL,
    trades_settled  integer     NULL,
    alerts_raised   integer     NULL,
    verticals       text[]      NULL,
    payload         jsonb       NOT NULL
);

CREATE INDEX IF NOT EXISTS settlement_events_recorded_idx
    ON settlement_events (recorded_at DESC);
CREATE INDEX IF NOT EXISTS settlement_events_type_recorded_idx
    ON settlement_events (event_type, recorded_at DESC);
"""

_SQL_DOWN = """
DROP TABLE IF EXISTS settlement_events;
"""


def upgrade() -> None:
    op.execute(_SQL_UP)


def downgrade() -> None:
    op.execute(_SQL_DOWN)
