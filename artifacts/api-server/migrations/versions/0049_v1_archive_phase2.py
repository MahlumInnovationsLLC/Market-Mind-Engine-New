"""V1-archive Phase 2: ``weather_observations`` / ``line_snapshots`` / ``llm_predictions``.

Task #731.  Phase 1 (``trade_analyses``) landed in migration 0037; this
migration carves out the next three V1 dumps under the same isolated
``v1_archive`` schema so the historical import cannot collide with
anything the live MME stack writes forward.

Each table's column shape is taken DIRECTLY from the V1 dump's ``COPY
public.<table> (...) FROM stdin;`` header (probed at task time from
``replit-port/archives/pg_dumps/``):

* ``weather_observations`` — 5 cols, 133,205 rows
    ``(id, city_code, temperature_f, observed_at, source)``
* ``llm_predictions`` — 19 cols, 57,390 rows
    ``(id, provider, model_id, category, league, game_id, matchup,
       prediction_type, predicted_value, confidence, actual_outcome,
       correct, latency_ms, tokens_used, cost_estimate, reasoning,
       predicted_at, resolved_at, metadata)``
* ``line_snapshots`` — 11 cols, 4,722,447 rows
    ``(id, game_id, sport, book, market_type, line_value, odds_value,
       home_team, away_team, is_opening, "timestamp")``

Schema notes — these are intentional V1-faithful choices, NOT a copy of
the live MME shapes:

* All ids land as ``bigint`` (V1 used integer auto-increment) — except
  V1's ``trade_analyses.id`` which was already ``text`` for historical
  reasons (see 0037).
* ``llm_predictions.actual_outcome`` is ``text`` in V1 (e.g. ``'win'``
  / ``'lose'``), not the binary 0/1 used in ``trade_analyses``.  Keep
  the V1 shape so a future audit can re-derive the binary outcome from
  the original string.
* ``line_snapshots.outcome`` (the column the live MME table added in
  migration 0030 with a NULL-distinct unique index) is intentionally
  NOT created here — V1's dump predates that column and trying to
  back-fill it would either duplicate-key-collide or coerce
  multi-outcome rows into the wrong bucket (see the gotcha note in
  task-731.md).  V1 rows live in ``v1_archive.line_snapshots`` and
  never participate in ``public.line_snapshots``'s unique index.
* ``imported_from`` + ``imported_at`` provenance columns are appended
  on top of every V1 shape so a future re-import or partial backfill
  can be audited and selectively rolled back.

Indexes are deliberately conservative — only the columns the
calibrator and verifier queries actually touch are indexed.  Adding
more is cheap later; over-indexing 4.7M rows up front is not.
"""
from __future__ import annotations

from alembic import op


revision = "0049_v1_archive_phase2"
down_revision = "0048_tournament_replays_winner_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Phase 1 already created the schema; IF NOT EXISTS keeps the
    # migration order-independent on dev DBs that re-baseline.
    op.execute("CREATE SCHEMA IF NOT EXISTS v1_archive")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS v1_archive.weather_observations (
            id              bigint       PRIMARY KEY,
            city_code       text,
            temperature_f   double precision,
            observed_at     timestamptz,
            source          text,
            imported_from   text         NOT NULL DEFAULT 'v1',
            imported_at     timestamptz  NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_v1_weather_obs_city_observed
            ON v1_archive.weather_observations (city_code, observed_at DESC)
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS v1_archive.llm_predictions (
            id              bigint       PRIMARY KEY,
            provider        text,
            model_id        text,
            category        text,
            league          text,
            game_id         text,
            matchup         text,
            prediction_type text,
            predicted_value double precision,
            confidence      double precision,
            actual_outcome  text,
            correct         boolean,
            latency_ms      integer,
            tokens_used     integer,
            cost_estimate   double precision,
            reasoning       text,
            predicted_at    timestamptz,
            resolved_at     timestamptz,
            metadata        jsonb,
            imported_from   text         NOT NULL DEFAULT 'v1',
            imported_at     timestamptz  NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_v1_llm_predictions_league_predicted
            ON v1_archive.llm_predictions (league, predicted_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_v1_llm_predictions_provider_model
            ON v1_archive.llm_predictions (provider, model_id)
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS v1_archive.line_snapshots (
            id              bigint       PRIMARY KEY,
            game_id         text,
            sport           text,
            book            text,
            market_type     text,
            line_value      double precision,
            odds_value      double precision,
            home_team       text,
            away_team       text,
            is_opening      boolean,
            "timestamp"     timestamptz,
            imported_from   text         NOT NULL DEFAULT 'v1',
            imported_at     timestamptz  NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_v1_line_snapshots_game_market
            ON v1_archive.line_snapshots (game_id, market_type)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_v1_line_snapshots_sport_ts
            ON v1_archive.line_snapshots (sport, "timestamp" DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS v1_archive.idx_v1_line_snapshots_sport_ts")
    op.execute("DROP INDEX IF EXISTS v1_archive.idx_v1_line_snapshots_game_market")
    op.execute("DROP TABLE IF EXISTS v1_archive.line_snapshots")

    op.execute("DROP INDEX IF EXISTS v1_archive.idx_v1_llm_predictions_provider_model")
    op.execute("DROP INDEX IF EXISTS v1_archive.idx_v1_llm_predictions_league_predicted")
    op.execute("DROP TABLE IF EXISTS v1_archive.llm_predictions")

    op.execute("DROP INDEX IF EXISTS v1_archive.idx_v1_weather_obs_city_observed")
    op.execute("DROP TABLE IF EXISTS v1_archive.weather_observations")
