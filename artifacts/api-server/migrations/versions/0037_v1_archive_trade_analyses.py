"""V1-archive schema + ``trade_analyses`` table for historical-data import.

The MME app already retains its own forward-going ``trades`` /
``trade_history`` tables; this migration carves out a SEPARATE
``v1_archive`` schema so the V1 bulk import (Supabase storage bucket
``axiom-predict-artifacts/replit-port/archives/pg_dumps/``) can land
without colliding with anything the live system writes.

Only the V1 ``trade_analyses`` table is created here — it is the
calibrator-unblock target (per-league predicted_prob + actual_outcome
pairs).  Other V1 dumps (``weather_observations``, ``line_snapshots``,
``llm_predictions``, ``uw_*`` etc.) will land in their own follow-up
migrations once the per-table column shapes have been confirmed
against the live MME equivalents.

Schema inferred directly from the V1 ``COPY public.trade_analyses
(...) FROM stdin;`` header in
``replit-port/archives/pg_dumps/trade_analyses.sql.gz`` plus a sample
of the dump body:

* ``id`` is mixed integer-stringified-as-text AND UUID — must be
  ``text``, not ``bigint``.
* ``predicted_prob`` is occasionally >1.0 in the V1 data (model
  miscalibration noise that the calibrator-fit step is meant to
  correct).  Stored as ``double precision`` with no CHECK constraint
  so the raw V1 distribution survives the round-trip.
* ``actual_outcome`` is the binary 0/1 settlement outcome, stored as
  ``integer`` to match V1.
* ``analysis_json`` is the per-trade structured analysis blob.
* ``imported_from`` + ``imported_at`` columns are added on top of the
  V1 shape so any future re-import (or partial backfill) can be
  audited and selectively rolled back without touching forward-going
  MME data.
"""
from __future__ import annotations

from alembic import op


revision = "0037_v1_archive_trade_analyses"
down_revision = "0036_gatekeeper_setting_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS v1_archive")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS v1_archive.trade_analyses (
            id                      text         PRIMARY KEY,
            trade_id                text,
            market_id               text,
            league                  text,
            market_type             text,
            predicted_prob          double precision,
            actual_outcome          integer,
            edge_at_entry           double precision,
            pnl                     double precision,
            analysis_json           jsonb,
            improvement_suggestion  text,
            grade                   text,
            created_at              timestamptz,
            vertical                text,
            imported_from           text         NOT NULL DEFAULT 'v1',
            imported_at             timestamptz  NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_v1_trade_analyses_league_market
            ON v1_archive.trade_analyses (league, market_type)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_v1_trade_analyses_created
            ON v1_archive.trade_analyses (created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_v1_trade_analyses_vertical
            ON v1_archive.trade_analyses (vertical)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS v1_archive.idx_v1_trade_analyses_vertical")
    op.execute("DROP INDEX IF EXISTS v1_archive.idx_v1_trade_analyses_created")
    op.execute("DROP INDEX IF EXISTS v1_archive.idx_v1_trade_analyses_league_market")
    op.execute("DROP TABLE IF EXISTS v1_archive.trade_analyses")
    op.execute("DROP SCHEMA IF EXISTS v1_archive")
