"""Election polls + Polymarket cross-reference cache for the elections vertical.

Adds:
  * `election_polls` — append-only-with-upsert table that the
    538 / Cook / RCP / Polymarket-xref pollers write into. Unique on
    (source, signal_id) so the polling loop can blindly re-insert the
    same upstream payload without producing duplicates when the upstream
    cursor overlaps.

`scope` is a free-form string the resolver matches against (e.g.
``US_PRES_2028``, ``GOV_CA_2026``, ``SEN_OH_2026``, ``HOUSE_NY_03_2026``,
``FR_PRES_2027``).  The market→scope resolver in
``app.elections.polls`` pivots the parsed Kalshi ticker onto this
column.

Per task #201.

Revision ID: 0008_election_polls
Revises: 0007_tech_evidence
"""
from __future__ import annotations

from alembic import op


revision = "0008_election_polls"
down_revision = "0007_tech_evidence"
branch_labels = None
depends_on = None


_SQL = """
CREATE TABLE IF NOT EXISTS election_polls (
    source       text             NOT NULL,
    signal_id    text             NOT NULL,
    scope        text             NOT NULL,
    candidate    text             NOT NULL,
    kind         text             NOT NULL,
    observed_at  timestamptz      NOT NULL,
    sample_size  integer,
    support_pct  double precision,
    weight       double precision NOT NULL DEFAULT 0.0,
    payload      jsonb            NOT NULL,
    inserted_at  timestamptz      NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS election_polls_source_signal_uidx
    ON election_polls (source, signal_id);

CREATE INDEX IF NOT EXISTS election_polls_scope_observed_idx
    ON election_polls (scope, observed_at DESC);

CREATE INDEX IF NOT EXISTS election_polls_kind_observed_idx
    ON election_polls (kind, observed_at DESC);
"""


def upgrade() -> None:
    op.execute(_SQL)


def downgrade() -> None:
    raise NotImplementedError("destructive down-migration not provided")
