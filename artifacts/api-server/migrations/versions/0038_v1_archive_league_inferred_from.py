"""V1-archive UNKNOWN-league backfill: ``league_inferred_from`` audit column.

Task #740.  V1 wrote literal ``'unknown'`` (or NULL) into
``v1_archive.trade_analyses.league`` for 598,218 of 603,128 rows, so the
calibrator-fit harness in ``scripts/backtest_v1_archive.py`` bucketed
~99% of usable rows as a synthetic ``UNKNOWN`` league with no calibrator
loaded.  The backfill script
``scripts/backfill_v1_archive_leagues.py`` re-derives the real league
from ``market_id`` (Kalshi series prefix), ``vertical``, and the
per-trade ``analysis_json`` blob.

This migration only adds the audit column + index.  The actual UPDATE
runs in the backfill script so it can be re-run idempotently with
``--dry-run`` semantics matching the V1 import path.

Column semantics:

* ``NULL`` — row was untouched (already had a real league when imported,
  or the backfill has not run yet).
* non-NULL — row's ``league`` was REWRITTEN by the backfill; the value
  is the rule key that fired (e.g. ``'kalshi_prefix:KXNBAGAME'``,
  ``'vertical:soccer'``, ``'analysis_json.league'``).  The original V1
  ``league`` value is captured in the same string under the
  ``orig=...`` suffix so a future audit can reverse the decision
  without re-querying the dump:
  e.g. ``'kalshi_prefix:KXNBAGAME orig=unknown'``.

Index ``idx_v1_trade_analyses_inferred_from`` is a partial index on
non-NULL values so dashboards / verifier queries can quickly count how
many rows were repaired by which rule without a full table scan.
"""
from __future__ import annotations

from alembic import op


revision = "0038_v1_archive_league_inferred_from"
down_revision = "0037_v1_archive_trade_analyses"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE v1_archive.trade_analyses
            ADD COLUMN IF NOT EXISTS league_inferred_from text
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_v1_trade_analyses_inferred_from
            ON v1_archive.trade_analyses (league_inferred_from)
            WHERE league_inferred_from IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP INDEX IF EXISTS v1_archive.idx_v1_trade_analyses_inferred_from"
    )
    op.execute(
        "ALTER TABLE v1_archive.trade_analyses "
        "DROP COLUMN IF EXISTS league_inferred_from"
    )
