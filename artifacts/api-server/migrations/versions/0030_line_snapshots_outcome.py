"""Add ``outcome`` column to line_snapshots so multi-outcome markets persist.

The unique index ``idx_line_snapshots_uq`` was originally
``(game_id, book, market_type, "timestamp")``.  That tuple is only
unique enough for ONE outcome per (game, book, market, ts), but every
real market has multiple outcomes:

* ``h2h``     -> "<home team>", "<away team>", and sometimes "Draw"
* ``spreads`` -> "<home team>", "<away team>" (with mirrored ``point``)
* ``totals``  -> "Over", "Under" (sharing one ``point``)

Migration 0029 widened ``line_snapshots.sport`` so the cross-sport
``executemany`` would stop rolling back; that fix unblocked writes
and the table jumped from 0 rows to ~290k in a single day.  But
the ``ON CONFLICT (game_id, book, market_type, "timestamp") DO NOTHING``
clause was silently keeping only the FIRST outcome each cycle and
dropping every other one — so the positive-EV scanner (which
de-vigs across both sides of every market) was working with half
the data and any consumer that compared "Over" vs "Under" prices
saw only one of them.

Fix:
1. Add a nullable ``outcome varchar(255)`` column (matches the width
   of ``home_team`` / ``away_team`` for soccer team-name length).
2. Replace the unique index with one that includes ``outcome``.
   Existing rows have ``outcome = NULL``; Postgres treats NULL as
   distinct in unique indexes by default so historical rows never
   collide with each other or with new (non-NULL) rows.
3. The producer in ``app.feeds.odds_api._persist_odds_payload`` is
   updated in the same commit to populate ``outcome`` from
   ``outcome.name`` and to extend the ``ON CONFLICT`` clause.

Backward compatibility:
* Readers (``app.margin_regressor`` queries, downstream backtests)
  ``SELECT line_value`` (and sometimes ``odds_value``) without
  filtering on outcome.  For ``spreads`` the home/away rows share
  the same magnitude (only the sign differs); for ``totals`` Over
  and Under share the same line.  ``DISTINCT ON (game_id)`` will
  pick one row arbitrarily — same observed behavior as today.
* The in-memory ``positive_ev`` scanner reads from the cache in
  ``odds_api._LAST_GAMES_BY_SPORT`` (raw API JSON), not the DB,
  so it's unaffected by this schema change but DOES already
  consume the per-outcome data correctly.

Downgrade restores the original unique index and drops the column.
"""
from __future__ import annotations

from alembic import op


revision = "0030_line_snapshots_outcome"
down_revision = "0029_widen_alert_and_sport_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE line_snapshots "
        "ADD COLUMN IF NOT EXISTS outcome varchar(255)"
    )
    op.execute("DROP INDEX IF EXISTS idx_line_snapshots_uq")
    op.execute(
        "CREATE UNIQUE INDEX idx_line_snapshots_uq "
        'ON line_snapshots (game_id, book, market_type, outcome, "timestamp")'
    )


def downgrade() -> None:
    # Rollback playbook (read this BEFORE running `alembic downgrade`):
    #
    # Because the upgrade INTENTIONALLY enables multi-outcome rows for
    # the same (game_id, book, market_type, "timestamp") tuple, any
    # period of normal production traffic after the upgrade will create
    # rows that the downgrade's narrower unique index CANNOT
    # accommodate.  ``CREATE UNIQUE INDEX`` will then fail with a
    # ``duplicate key value violates unique constraint`` error —
    # surfaced rather than silently truncated, which is intentional.
    #
    # If you genuinely need to roll back, dedupe FIRST inside a
    # single transaction, e.g.:
    #
    #   BEGIN;
    #   DELETE FROM line_snapshots ls1
    #     USING line_snapshots ls2
    #    WHERE ls1.id < ls2.id
    #      AND ls1.game_id     = ls2.game_id
    #      AND ls1.book        = ls2.book
    #      AND ls1.market_type = ls2.market_type
    #      AND ls1."timestamp" = ls2."timestamp";
    #   -- then run `alembic downgrade -1`
    #   COMMIT;
    #
    # That collapses each (game, book, market, ts) family back to a
    # single row, which the old index can hold.  You will lose every
    # non-first outcome in the rolled-back window — accept that
    # consciously, don't paper over it.
    op.execute("DROP INDEX IF EXISTS idx_line_snapshots_uq")
    op.execute(
        "CREATE UNIQUE INDEX idx_line_snapshots_uq "
        'ON line_snapshots (game_id, book, market_type, "timestamp")'
    )
    op.execute("ALTER TABLE line_snapshots DROP COLUMN IF EXISTS outcome")
