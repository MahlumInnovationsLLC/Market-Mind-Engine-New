"""Economic-release calendar cache for the events-econ vertical.

Adds:
  * `econ_releases` — append-only-with-upsert table that the
    economic-calendar pollers write into. Unique on
    (source, signal_id) so a polling cursor can blindly re-insert the
    same upstream payload without duplicates.

`release_type` is the canonical class (CPI/PPI/GDP/FOMC/NFP/JOBLESS/
RETAIL/...).  `release_at` is the scheduled UTC time of the print.
`consensus`, `previous`, `actual`, `revised_previous` are the headline
numeric reads the predictor folds into a prior + variance estimate.
`payload` carries the raw upstream blob for forensic replay.

Per task #202.

Revision ID: 0009_econ_releases
Revises: 0008_election_polls
"""
from __future__ import annotations

from alembic import op


revision = "0009_econ_releases"
down_revision = "0008_election_polls"
branch_labels = None
depends_on = None


_SQL = """
CREATE TABLE IF NOT EXISTS econ_releases (
    source            text             NOT NULL,
    signal_id         text             NOT NULL,
    release_type      text             NOT NULL,
    release_at        timestamptz      NOT NULL,
    consensus         double precision,
    previous          double precision,
    actual            double precision,
    revised_previous  double precision,
    payload           jsonb            NOT NULL,
    observed_at       timestamptz      NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS econ_releases_source_signal_uidx
    ON econ_releases (source, signal_id);

CREATE INDEX IF NOT EXISTS econ_releases_type_at_idx
    ON econ_releases (release_type, release_at DESC);

CREATE INDEX IF NOT EXISTS econ_releases_at_idx
    ON econ_releases (release_at DESC);
"""


def upgrade() -> None:
    op.execute(_SQL)


def downgrade() -> None:
    raise NotImplementedError("destructive down-migration not provided")
