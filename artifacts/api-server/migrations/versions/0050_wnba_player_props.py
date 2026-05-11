"""WNBA player props vertical (task #823).

Introduces the first **player-level** market primitive in the system:

* ``player_props_quotes`` — per-player per-stat per-book per-outcome
  quote ticks (sport-agnostic; WNBA is the seed sport).
* ``player_stat_history`` — per-player per-game box-score history used
  by the per-player baseline distribution.
* ``player_injury_status`` — per-player availability snapshot read by
  the player-level risk gate.

All three tables are sport-agnostic so future verticals
(``nba_player_props``, ``mlb_player_props``, etc.) reuse them by
filtering on the ``league`` column rather than duplicating the schema.
"""
from __future__ import annotations

from alembic import op


revision = "0050_wnba_player_props"
down_revision = "0049_v1_archive_phase2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS player_props_quotes (
            id              bigserial PRIMARY KEY,
            league          text NOT NULL,
            sport           text NOT NULL,
            game_id         text NOT NULL,
            player          text NOT NULL,
            team            text,
            market          text NOT NULL,
            book            text NOT NULL,
            outcome         text NOT NULL,
            line            double precision,
            price_decimal   double precision,
            price_american  integer,
            "timestamp"     timestamptz NOT NULL DEFAULT now(),
            raw             jsonb
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_player_props_quotes_uq
          ON player_props_quotes (game_id, player, market, book, outcome, "timestamp")
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_player_props_quotes_lookup
          ON player_props_quotes (league, game_id, player, market, "timestamp" DESC)
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS player_stat_history (
            id              bigserial PRIMARY KEY,
            league          text NOT NULL,
            player          text NOT NULL,
            team            text,
            opponent        text,
            game_id         text,
            played_at       timestamptz NOT NULL,
            minutes         double precision,
            points          double precision,
            rebounds        double precision,
            assists         double precision,
            threes          double precision,
            blocks          double precision,
            steals          double precision,
            turnovers       double precision,
            is_starter      boolean,
            home_or_away    text,
            pace_estimate   double precision,
            source          text
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_player_stat_history_uq
          ON player_stat_history (league, player, game_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_player_stat_history_player_recent
          ON player_stat_history (league, player, played_at DESC)
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS player_injury_status (
            id           bigserial PRIMARY KEY,
            league       text NOT NULL,
            player       text NOT NULL,
            team         text,
            status       text NOT NULL,
            description  text,
            fetched_at   timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_player_injury_status_lookup
          ON player_injury_status (league, player, fetched_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_player_injury_status_lookup")
    op.execute("DROP TABLE IF EXISTS player_injury_status")
    op.execute("DROP INDEX IF EXISTS idx_player_stat_history_player_recent")
    op.execute("DROP INDEX IF EXISTS idx_player_stat_history_uq")
    op.execute("DROP TABLE IF EXISTS player_stat_history")
    op.execute("DROP INDEX IF EXISTS idx_player_props_quotes_lookup")
    op.execute("DROP INDEX IF EXISTS idx_player_props_quotes_uq")
    op.execute("DROP TABLE IF EXISTS player_props_quotes")
