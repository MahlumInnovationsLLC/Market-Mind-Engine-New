"""RevenueCat webhook delivery audit table.

Per task #252: surface per-status delivery counts on the operator
dashboard and fire an alert when the 5xx rate spikes or when no
events are received during business hours.

Revision ID: 0013_rc_webhook_deliveries
Revises: 0012_intangibles_reports
"""
from __future__ import annotations

from alembic import op


revision = "0013_rc_webhook_deliveries"
down_revision = "0012_intangibles_reports"
branch_labels = None
depends_on = None


_SQL_UP = """
CREATE TABLE IF NOT EXISTS rc_webhook_deliveries (
    id            bigserial PRIMARY KEY,
    received_at   timestamptz NOT NULL DEFAULT now(),
    status        text        NOT NULL,
    http_status   integer     NOT NULL,
    event_id      text        NULL,
    event_type    text        NULL,
    error         text        NULL
);

CREATE INDEX IF NOT EXISTS rc_webhook_deliveries_received_idx
    ON rc_webhook_deliveries (received_at DESC);
CREATE INDEX IF NOT EXISTS rc_webhook_deliveries_status_idx
    ON rc_webhook_deliveries (status, received_at DESC);
CREATE INDEX IF NOT EXISTS rc_webhook_deliveries_event_idx
    ON rc_webhook_deliveries (event_id, received_at DESC);
"""

_SQL_DOWN = """
DROP TABLE IF EXISTS rc_webhook_deliveries;
"""


def upgrade() -> None:
    op.execute(_SQL_UP)


def downgrade() -> None:
    op.execute(_SQL_DOWN)
