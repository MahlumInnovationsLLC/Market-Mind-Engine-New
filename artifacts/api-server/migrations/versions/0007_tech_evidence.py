"""Tech / AI evidence cache for the tech-ai vertical.

Adds:
  * `tech_evidence` — append-only-with-upsert table that the
    GitHub-releases / Hugging Face / ArXiv / MLPerf / earnings-calendar
    pollers write into. Unique on (source, signal_id) so the polling
    loop can blindly re-insert the same upstream payload without
    producing duplicates when the upstream cursor overlaps.

Per task #199.

Revision ID: 0007_tech_evidence
Revises: 0006_commodities_ohlcv
"""
from __future__ import annotations

from alembic import op


revision = "0007_tech_evidence"
down_revision = "0006_commodities_ohlcv"
branch_labels = None
depends_on = None


_SQL = """
CREATE TABLE IF NOT EXISTS tech_evidence (
    source       text             NOT NULL,
    signal_id    text             NOT NULL,
    symbol       text             NOT NULL,
    kind         text             NOT NULL,
    observed_at  timestamptz      NOT NULL,
    weight       double precision NOT NULL DEFAULT 0.0,
    payload      jsonb            NOT NULL,
    inserted_at  timestamptz      NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS tech_evidence_source_signal_uidx
    ON tech_evidence (source, signal_id);

CREATE INDEX IF NOT EXISTS tech_evidence_symbol_observed_idx
    ON tech_evidence (symbol, observed_at DESC);

CREATE INDEX IF NOT EXISTS tech_evidence_kind_observed_idx
    ON tech_evidence (kind, observed_at DESC);
"""


def upgrade() -> None:
    op.execute(_SQL)


def downgrade() -> None:
    raise NotImplementedError("destructive down-migration not provided")
