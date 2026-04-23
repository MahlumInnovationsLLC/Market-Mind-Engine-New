"""Outlier.bet intangibles reports.

Adds the ``intangibles_reports`` table referenced by the spec
(§"Domain: Sports" → "intangibles_reports — Intangibles per game").
The Outlier.bet feed (``app/feeds/outlier_bet.py``) writes one row
per scanned positive-EV market it returns; the orchestrator can
later join these against ``odds_snapshots`` to fade or follow.

The ``UNIQUE (provider, external_id, scanned_at)`` constraint makes
ingest idempotent — re-pushing the same scan is a no-op.

Per task #245 (closing the Outlier.bet ingest gap raised in code
review).

Revision ID: 0012_intangibles_reports
Revises: 0011_rollback_and_research
"""
from __future__ import annotations

from alembic import op


revision = "0012_intangibles_reports"
down_revision = "0011_rollback_and_research"
branch_labels = None
depends_on = None


_SQL_UP = """
CREATE TABLE IF NOT EXISTS intangibles_reports (
    id              bigserial PRIMARY KEY,
    provider        text        NOT NULL,
    sport           text        NOT NULL,
    external_id     text        NOT NULL,
    market_type     text        NULL,
    side            text        NULL,
    edge_pct        double precision NULL,
    fair_prob       double precision NULL,
    book_prob       double precision NULL,
    sportsbook      text        NULL,
    teams           text        NULL,
    payload         jsonb       NOT NULL DEFAULT '{}'::jsonb,
    scanned_at      timestamptz NOT NULL DEFAULT now(),
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (provider, external_id, scanned_at)
);

CREATE INDEX IF NOT EXISTS intangibles_reports_provider_scanned_idx
    ON intangibles_reports (provider, scanned_at DESC);
CREATE INDEX IF NOT EXISTS intangibles_reports_sport_scanned_idx
    ON intangibles_reports (sport, scanned_at DESC);
"""

_SQL_DOWN = """
DROP TABLE IF EXISTS intangibles_reports;
"""


def upgrade() -> None:
    op.execute(_SQL_UP)


def downgrade() -> None:
    op.execute(_SQL_DOWN)
