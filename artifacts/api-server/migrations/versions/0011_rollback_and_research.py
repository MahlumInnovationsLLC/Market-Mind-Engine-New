"""Drift-rollback log + AutoResearch directives.

Adds two narrowly-scoped tables that close named gaps from the
Axiom-Predict spec:

  * ``model_rollback_log`` — append-only record of every drift-driven
    rollback decision the retrain loop makes.  Holds the vertical,
    triggering snapshot id, brier_before/after, the previous
    `AXIOM_MODEL_VERSION` we want pinned, the rollback reason, and a
    JSON details blob.  The settlement worker writes one row whenever
    a freshly-fitted calibrator's post-fit Brier is *worse* than the
    raw model's pre-fit Brier — i.e. the retrain made things worse
    and we should not promote the new state.

  * ``research_directives`` — operator-facing queue of open research
    questions raised by the AutoResearch + drift loops (e.g. "weather
    EMOS is missing for KNYC", "calibration drift in NFL > 0.04 for
    3 consecutive cycles").  Status flow:
    `open -> investigating -> resolved | dismissed`.  Renders on the
    new ``/research`` page.

Per task #245.

Revision ID: 0011_rollback_and_research
Revises: 0010_entertainment_signals
"""
from __future__ import annotations

from alembic import op


revision = "0011_rollback_and_research"
down_revision = "0010_entertainment_signals"
branch_labels = None
depends_on = None


_SQL_UP = """
CREATE TABLE IF NOT EXISTS model_rollback_log (
    id                bigserial PRIMARY KEY,
    vertical          text             NOT NULL,
    snapshot_id       bigint,
    brier_before      double precision,
    brier_after       double precision,
    pinned_version    text,
    reason            text             NOT NULL,
    details           jsonb            NOT NULL DEFAULT '{}'::jsonb,
    created_at        timestamptz      NOT NULL DEFAULT now(),
    acknowledged      boolean          NOT NULL DEFAULT false,
    acknowledged_at   timestamptz,
    acknowledged_by   text
);

CREATE INDEX IF NOT EXISTS model_rollback_log_vertical_at_idx
    ON model_rollback_log (vertical, created_at DESC);
CREATE INDEX IF NOT EXISTS model_rollback_log_unack_idx
    ON model_rollback_log (created_at DESC)
    WHERE acknowledged = false;

CREATE TABLE IF NOT EXISTS research_directives (
    id                bigserial PRIMARY KEY,
    vertical          text,
    title             text             NOT NULL,
    hypothesis        text             NOT NULL,
    evidence          jsonb            NOT NULL DEFAULT '{}'::jsonb,
    status            text             NOT NULL DEFAULT 'open',
    priority          text             NOT NULL DEFAULT 'normal',
    source            text             NOT NULL DEFAULT 'autoresearch',
    created_at        timestamptz      NOT NULL DEFAULT now(),
    updated_at        timestamptz      NOT NULL DEFAULT now(),
    resolved_at       timestamptz,
    resolution_note   text
);

CREATE INDEX IF NOT EXISTS research_directives_status_idx
    ON research_directives (status, created_at DESC);
CREATE INDEX IF NOT EXISTS research_directives_vertical_idx
    ON research_directives (vertical, created_at DESC);
"""

_SQL_DOWN = """
DROP TABLE IF EXISTS research_directives;
DROP TABLE IF EXISTS model_rollback_log;
"""


def upgrade() -> None:
    op.execute(_SQL_UP)


def downgrade() -> None:
    op.execute(_SQL_DOWN)
