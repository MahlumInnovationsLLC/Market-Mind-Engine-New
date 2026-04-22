"""Unusual Whales signals + public-betting consensus tables.

Per task #93. Adds the two ingestion targets the new feeds need:

  * `uw_signals` — append-only-ish landing zone for Unusual Whales rows
    (options flow alerts, dark-pool prints, ticker snapshots).
    `(signal_type, dedupe_key)` is unique so re-polling the same window
    is a no-op rather than a duplicate insert. AutoResearch (separate
    task) reads from here to mine model features.
  * `public_betting_consensus` — one row per
    (source, sport, game_id, market_type, side) holding public-ticket %,
    public-money %, and a sharp-disagreement flag. BettingPros and
    SportsLine each write into the same shape.

Both tables use `IF NOT EXISTS` so the migration is idempotent against
any pre-existing copy.

Revision ID: 0003_uw_and_public_betting
Revises: 0002_order_fills_position_rollup
"""
from __future__ import annotations

from alembic import op


revision = "0003_uw_and_public_betting"
down_revision = "0002_order_fills_position_rollup"
branch_labels = None
depends_on = None


_SQL = """
CREATE TABLE IF NOT EXISTS uw_signals (
    id           bigserial PRIMARY KEY,
    signal_type  text        NOT NULL,
    ticker       text,
    occurred_at  timestamptz,
    payload      jsonb       NOT NULL,
    fetched_at   timestamptz NOT NULL DEFAULT now(),
    dedupe_key   text        NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uw_signals_dedupe_uidx
    ON uw_signals (signal_type, dedupe_key);
CREATE INDEX IF NOT EXISTS uw_signals_ticker_when_idx
    ON uw_signals (ticker, occurred_at DESC);
CREATE INDEX IF NOT EXISTS uw_signals_when_idx
    ON uw_signals (occurred_at DESC);

CREATE TABLE IF NOT EXISTS public_betting_consensus (
    source              text        NOT NULL,
    sport               text        NOT NULL,
    game_id             text        NOT NULL,
    market_type         text        NOT NULL,
    side                text        NOT NULL,
    public_tickets_pct  numeric,
    public_money_pct    numeric,
    sharp_disagreement  boolean     NOT NULL DEFAULT false,
    home_team           text,
    away_team           text,
    payload             jsonb,
    fetched_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source, sport, game_id, market_type, side)
);
CREATE INDEX IF NOT EXISTS public_betting_consensus_game_idx
    ON public_betting_consensus (sport, game_id);
CREATE INDEX IF NOT EXISTS public_betting_consensus_when_idx
    ON public_betting_consensus (fetched_at DESC);
"""


def upgrade() -> None:
    op.execute(_SQL)


def downgrade() -> None:
    raise NotImplementedError("destructive down-migration not provided")
