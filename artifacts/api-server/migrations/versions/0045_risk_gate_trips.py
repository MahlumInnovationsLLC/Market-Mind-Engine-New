"""risk_gate_trips: append-only structured log of risk-gate vetoes.

Backs the daily honesty report (Task #756) so operators can see an
"in the last 24 h, gate X tripped N times" tile without scraping
Postmark or Telegram history.  Currently written by the tail-risk
gate trip helper (`app/risk/gates._emit_tail_risk_trip_alert`); the
column shape is generic enough that any future gate that wants the
same paper trail can join the table without a fresh migration.

Designed to be cheap (append-only, no foreign keys, no triggers) and
self-pruning is left to operations — a 100/day baseline grows by ~3k
rows a month, well within Supabase free-tier headroom.

Revision ID: 0045_risk_gate_trips
Revises: 0044_calibration_snapshots_role_ab
"""
from __future__ import annotations

from alembic import op


revision = "0045_risk_gate_trips"
down_revision = "0044_calibration_snapshots_role_ab"
branch_labels = None
depends_on = None


_SQL_UP = """
CREATE TABLE IF NOT EXISTS risk_gate_trips (
    id                  bigserial PRIMARY KEY,
    gate_name           text        NOT NULL,
    market_id           text,
    vertical            text,
    side                text,
    intended_quantity   integer,
    current_price       double precision,
    reason              text,
    details             jsonb,
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS risk_gate_trips_created_at_idx
    ON risk_gate_trips (created_at DESC);

CREATE INDEX IF NOT EXISTS risk_gate_trips_gate_name_created_at_idx
    ON risk_gate_trips (gate_name, created_at DESC);
"""

_SQL_DOWN = """
DROP INDEX IF EXISTS risk_gate_trips_gate_name_created_at_idx;
DROP INDEX IF EXISTS risk_gate_trips_created_at_idx;
DROP TABLE IF EXISTS risk_gate_trips;
"""


def upgrade() -> None:
    op.execute(_SQL_UP)


def downgrade() -> None:
    op.execute(_SQL_DOWN)
