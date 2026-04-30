"""Eliminate ``placed_by='unknown'`` rows + drop the column DEFAULT.

Operator directive (2026-04-30): "anything above the $400 threshold
is probably still me, so please make sure there are NO unknowns".

Migration 0039 introduced the ``placed_by`` column with a 4-rule
priority retro-classification (Rule A: notional > $400 -> manual_ui;
Rule B: orchestrator-stamped metadata key -> orchestrator; Rule C:
robinhood_import source -> rh_import; Rule D fallback -> unknown).
After 0039 ran, the only rows still tagged ``unknown`` are those that
fell through to Rule D — i.e. trades with notional <= $400 AND no
orchestrator metadata key AND not a robinhood import.

Per the operator's bimodal heuristic, any sub-$400 trade is by
definition NOT a manual UI bet (those are always >$400) — so the
remaining unknowns are system-placed trades whose orchestrator
metadata happens to be missing (older rows from before the metadata-
stamping pipeline was wired, or rows where the metadata stamp was
lost in a refactor).  Reclassify them as ``orchestrator``.

The retag runs in two passes: (1a) sub-$400 unknowns with non-null
price+quantity, then (1b) the residual (null-notional malformed
legacy rows).  A hard ``RAISE EXCEPTION`` after both passes makes the
migration fail the deploy if any unknowns remain — the operator's
"NO unknowns" directive is enforced at the migration layer rather
than just relying on the data shape we expect.

Also drop the column DEFAULT so future INSERTs can no longer silently
land at ``unknown`` — every insert site must now pass an explicit
``placed_by``.  Live audit confirmed only two production paths write
to ``trades``: ``app/orchestrator/paper.py::record_paper_trade``
(passes ``'orchestrator'``) and ``scripts/backfill_kalshi_fills.py``
(now passes the >$400 heuristic per-row).  The ``CHECK`` constraint
from 0039 stays in place — ``'unknown'`` is still allowed in the
enum for genuinely-unclassifiable future imports, just no longer the
default.

Defensive ALTER pattern (mirrors 0039 / 0024): skip cleanly if the
upstream ``trades`` table doesn't exist on a fresh dev DB.

Downgrade restores the ``'unknown'`` default but does NOT re-tag the
re-classified rows back to ``unknown`` (data-destructive and
ambiguous — there's no way to know which rows were originally
unknown).
"""
from __future__ import annotations

from alembic import op


revision = "0040_trades_no_unknowns"
down_revision = "0039_trades_placed_by"
branch_labels = None
depends_on = None


_SQL_UP = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'trades'
    ) THEN
        RETURN;
    END IF;

    -- 1. Re-tag remaining 'unknown' rows in two passes.  Per the
    --    operator's strict "NO unknowns" directive, every row must
    --    carry a meaningful tag — not just the ones with computable
    --    notional.

    -- 1a. Sub-$400 unknowns (the bulk of the residual): orchestrator
    --     by the operator's bimodal heuristic.  0039 already promoted
    --     >$400 unknowns to 'manual_ui', so this hits the < $400
    --     residual cleanly.
    UPDATE trades
       SET placed_by = 'orchestrator'
     WHERE placed_by = 'unknown'
       AND price IS NOT NULL
       AND quantity IS NOT NULL
       AND (price * quantity) <= 400;

    -- 1b. Null-notional unknowns: also orchestrator.  These are
    --     malformed legacy rows where price or quantity is NULL —
    --     they cannot be classified by the bimodal heuristic, so we
    --     default them to the system bucket per the operator's rule
    --     that anything not flagged as a $400+ manual bet is system.
    --     This guarantees the post-migration assertion below holds.
    UPDATE trades
       SET placed_by = 'orchestrator'
     WHERE placed_by = 'unknown';

    -- 2. Hard assertion: zero unknowns must remain.  Raise loudly if
    --    anything slipped through (e.g. a row inserted between the
    --    two UPDATEs above) so the migration fails the deploy rather
    --    than silently leaving holes.
    IF EXISTS (SELECT 1 FROM trades WHERE placed_by = 'unknown') THEN
        RAISE EXCEPTION 'migration 0040: % rows still tagged unknown after retag passes',
            (SELECT count(*) FROM trades WHERE placed_by = 'unknown');
    END IF;

    -- 3. Drop the column DEFAULT so future inserts must pass an
    --    explicit value.  The CHECK constraint from 0039 still
    --    permits 'unknown' as a value — we only remove it as the
    --    silent fallback.
    ALTER TABLE trades
        ALTER COLUMN placed_by DROP DEFAULT;
END$$;
"""


_SQL_DOWN = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'trades'
    ) THEN
        RETURN;
    END IF;

    -- Restore the 'unknown' default (the data re-tag from upgrade()
    -- is intentionally NOT reversed — re-flipping rows back to
    -- 'unknown' would be data-destructive and ambiguous).
    ALTER TABLE trades
        ALTER COLUMN placed_by SET DEFAULT 'unknown';
END$$;
"""


def upgrade() -> None:
    op.execute(_SQL_UP)


def downgrade() -> None:
    op.execute(_SQL_DOWN)
