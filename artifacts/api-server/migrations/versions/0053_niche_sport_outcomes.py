"""Niche-sport settled outcomes backfill table (task #831).

Holds historical settled outcomes for the four niche individual-sport
verticals (Tennis ATP/WTA, F1, PGA, UFC) so the per-sport calibrators
have something to train against the first time the backfill job runs.

The table is intentionally sport-agnostic — a single `(sport, league,
event_id, entry_id)` primary key lets the backfill insert UFC fight
results, ATP/WTA matches, F1 race finishes and PGA tournament
positions side-by-side without one schema per sport.

Migration is **schema-only** — no rows are inserted.  The actual
backfill job (Sportradar archives + public sources) lands in the
follow-up task; this migration just makes the destination exist so
the backfill is idempotent (``ON CONFLICT (...) DO NOTHING`` keeps
re-runs free of double-counts, per the architectural constraint in
the task brief).
"""
from __future__ import annotations

from alembic import op


revision = "0053_niche_sport_outcomes"
down_revision = "0052_predictions_is_live"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS niche_sport_outcomes (
            id              bigserial PRIMARY KEY,
            sport           text NOT NULL,
            league          text NOT NULL,
            event_id        text NOT NULL,
            event_date      date NOT NULL,
            entry_id        text NOT NULL,
            entry_name      text,
            opponent_id     text,
            opponent_name   text,
            surface         text,
            venue           text,
            outcome         text NOT NULL,
            -- 1 = win (or made-cut / top-10 etc. depending on outcome label),
            -- 0 = loss / missed cut.
            outcome_int     smallint NOT NULL,
            raw             jsonb,
            source          text NOT NULL DEFAULT 'unknown',
            ingested_at     timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # Idempotent backfill key: re-running the same source twice must
    # not double-insert.  The `(sport, league, event_id, entry_id,
    # outcome)` tuple is the natural primary key — a single fighter
    # can show up with both a win and a loss outcome label on the same
    # event when the data source tags both the W and the L.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS niche_sport_outcomes_uq_idx
            ON niche_sport_outcomes (sport, league, event_id, entry_id, outcome)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS niche_sport_outcomes_event_date_idx
            ON niche_sport_outcomes (league, event_date DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS niche_sport_outcomes")
