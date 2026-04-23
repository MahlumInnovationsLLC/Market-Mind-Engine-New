"""Persist orchestrator pipeline events so /pipeline survives API restarts.

Per task #121: the in-process ring buffer that backs
``/api/orchestrator/recent-events`` empties on every API restart (deploy,
crash, lifespan reload). Operators then stare at an empty pipeline page
until the next cycle event lands. We mirror every published event into
``pipeline_events`` so the snapshot endpoint can hydrate from Postgres
when the in-memory ring is cold.

Schema:
  * ``cycle_id`` references ``trading_cycles(id)`` so cascading deletes
    of a cycle row drop the matching event log too.
  * ``seq`` is the same monotonic stamp the WS already attaches to each
    frame — kept here so a snapshot+stream client can dedupe across the
    DB + live WS join.
  * ``payload`` stores the full event dict (already JSON-serialisable)
    so we can replay any future shape without schema churn.

Revision ID: 0014_pipeline_events
Revises: 0013_rc_webhook_deliveries
"""
from __future__ import annotations

from alembic import op


revision = "0014_pipeline_events"
down_revision = "0013_rc_webhook_deliveries"
branch_labels = None
depends_on = None


_SQL_UP = """
CREATE TABLE IF NOT EXISTS pipeline_events (
    id          bigserial   PRIMARY KEY,
    cycle_id    bigint      NULL REFERENCES trading_cycles(id) ON DELETE CASCADE,
    type        text        NOT NULL,
    seq         bigint      NOT NULL,
    payload     jsonb       NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS pipeline_events_cycle_id_idx
    ON pipeline_events (cycle_id);
CREATE INDEX IF NOT EXISTS pipeline_events_created_at_idx
    ON pipeline_events (created_at DESC);
"""

_SQL_DOWN = """
DROP TABLE IF EXISTS pipeline_events;
"""


def upgrade() -> None:
    op.execute(_SQL_UP)


def downgrade() -> None:
    op.execute(_SQL_DOWN)
