"""Entertainment-signals cache for the entertainment vertical.

Adds:
  * `entertainment_signals` — append-only-with-upsert table that the
    box-office / awards / charts / reviews pollers write into.  Unique
    on (source, signal_id) so a polling cursor can blindly re-insert
    the same upstream payload without duplicates.

`signal_type` is the canonical class (BOX/OSCAR/EMMY/GRAMMY/SPOTIFY/
STREAM/RT).  `event_at` is the scheduled UTC time of the resolving
event (release weekend, ceremony, chart-week end).  `title` is the
subject (movie / show / artist / category).  `value` carries the
headline numeric reading (opening-weekend gross, RT score, chart rank,
votes).  `payload` carries the raw upstream blob for forensic replay.

Per task #203.

Revision ID: 0010_entertainment_signals
Revises: 0009_econ_releases
"""
from __future__ import annotations

from alembic import op


revision = "0010_entertainment_signals"
down_revision = "0009_econ_releases"
branch_labels = None
depends_on = None


_SQL = """
CREATE TABLE IF NOT EXISTS entertainment_signals (
    source       text             NOT NULL,
    signal_id    text             NOT NULL,
    signal_type  text             NOT NULL,
    title        text             NOT NULL,
    event_at     timestamptz      NOT NULL,
    value        double precision,
    settled      boolean          NOT NULL DEFAULT false,
    outcome      text,
    payload      jsonb            NOT NULL,
    observed_at  timestamptz      NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS entertainment_signals_source_signal_uidx
    ON entertainment_signals (source, signal_id);

CREATE INDEX IF NOT EXISTS entertainment_signals_type_at_idx
    ON entertainment_signals (signal_type, event_at DESC);

CREATE INDEX IF NOT EXISTS entertainment_signals_at_idx
    ON entertainment_signals (event_at DESC);

CREATE INDEX IF NOT EXISTS entertainment_signals_title_idx
    ON entertainment_signals (lower(title));
"""


def upgrade() -> None:
    op.execute(_SQL)


def downgrade() -> None:
    raise NotImplementedError("destructive down-migration not provided")
