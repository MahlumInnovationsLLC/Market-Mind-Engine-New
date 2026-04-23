"""Predictions candidate table for the Kalshi crypto producer.

The orchestrator's ``_candidate_predictions`` SELECT (in
``app.orchestrator.cycle``) reads from a ``predictions`` table that was
historically created by lazy ``ensure_schema`` paths. On a fresh install
that table is missing, so every cycle silently swallows the
``relation "predictions" does not exist`` error and the Kalshi crypto
producer (task #180) has nowhere to write its candidate rows.

This migration:

  * Creates ``predictions`` with the full set of columns the cycle SELECT
    references, all ``IF NOT EXISTS`` so production installs that already
    have the table aren't disturbed.
  * Adds a unique index on ``kalshi_market_id`` so producers can ``ON
    CONFLICT`` upsert candidates keyed to the Kalshi ticker.

Per task spec, no destructive down-migration is provided.

Revision ID: 0016_predictions_kalshi_candidates
Revises: 0015_telegram_notify_push
"""
from __future__ import annotations

from alembic import op


revision = "0016_predictions_kalshi_candidates"
down_revision = "0015_telegram_notify_push"
branch_labels = None
depends_on = None


_SQL_UP = """
CREATE TABLE IF NOT EXISTS predictions (
    id                bigserial   PRIMARY KEY,
    game_id           text        NOT NULL,
    league            text,
    kalshi_market_id  text,
    kalshi_price      double precision,
    home_win_prob     double precision,
    confidence        double precision,
    edge              double precision,
    game_time         timestamptz,
    game_context      jsonb,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

-- Producers upsert candidate rows keyed to the Kalshi ticker so each
-- live market maps to a single ``predictions`` row regardless of how
-- many times the poller fires.
CREATE UNIQUE INDEX IF NOT EXISTS predictions_kalshi_market_id_uidx
    ON predictions (kalshi_market_id)
    WHERE kalshi_market_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS predictions_league_game_time_idx
    ON predictions (league, game_time);
"""


def upgrade() -> None:
    op.execute(_SQL_UP)


def downgrade() -> None:
    # No-op: the cycle depends on this table existing across rollbacks.
    pass
