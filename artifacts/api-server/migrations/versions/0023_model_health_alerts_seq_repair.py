"""Repair the ``model_health_alerts_id_seq`` sequence after drift.

Per task #576.  The Kalshi reconciler started flooding the log with::

    position_drift alert insert failed for KXBTCD-26APR2511-T100749.99:
        duplicate key value violates unique constraint
        "model_health_alerts_pkey" — DETAIL: Key (id)=(828) already exists.

Root cause is a runtime *sequence drift*, not a schema bug.  The
``model_health_alerts.id`` column is a regular ``serial`` whose default
is ``nextval('model_health_alerts_id_seq')``.  At some point in the
table's history rows were bulk-inserted with explicit ``id`` values
(a backfill, a restore from a logical dump, or a manual operator
INSERT) — the bookkeeping for those inserts did *not* call
``setval()`` afterwards, so the sequence's ``last_value`` (827) lags
the table's ``MAX(id)`` (1577) by ~750.  Every subsequent default
insert reuses an id that already exists and aborts the transaction.

This migration is the canonical Postgres recipe for repairing that:

    SELECT setval(
        'model_health_alerts_id_seq',
        GREATEST((SELECT COALESCE(MAX(id), 0) FROM model_health_alerts), 1),
        true
    );

After this runs, the next ``nextval()`` returns ``MAX(id)+1`` and
collisions stop.  The third arg ``true`` means ``is_called=true`` so
the *next* call advances by one (matching the semantics that
``serial`` columns always assume).

The fix is wrapped in a ``DO`` block guarded by both table-existence
and sequence-existence checks so a fresh DB (where Alembic has just
created the table and the sequence is already in sync) and a fork
that has the table without the sequence (custom DDL) both no-op
cleanly.

Note:  call-site hardening (``ON CONFLICT (id) DO NOTHING`` on every
``INSERT INTO model_health_alerts``) is applied alongside this
migration as a belt-and-suspenders guard for any *future* drift event.
This migration alone is sufficient to clear the current backlog.

Revision ID: 0023_model_health_alerts_seq_repair
Revises: 0022_uw_signals_ticker_backfill, 0022_predictions_settlement

This migration also serves as the merge point between the two
parallel ``0022_*`` heads that landed simultaneously:

  * ``0022_uw_signals_ticker_backfill`` (task #425, UW per-ticker mining)
  * ``0022_predictions_settlement``      (task #424, Kalshi crypto settler)

Both extend ``0021_crypto_backtest_runs`` independently, so a tuple
``down_revision`` here collapses the divergent tree back into a
single head before adding the sequence repair.  Listing both parents
is *required* — without the merge, ``alembic upgrade head`` would
abort with ``Multiple head revisions are present`` on any environment
where both 0022 migrations have been applied (which is already true
on the production Supabase DB this api-server points at).
"""
from __future__ import annotations

from alembic import op


revision = "0023_model_health_alerts_seq_repair"
down_revision = (
    "0022_uw_signals_ticker_backfill",
    "0022_predictions_settlement",
)
branch_labels = None
depends_on = None


_SQL_UP = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
         WHERE table_schema = 'public'
           AND table_name = 'model_health_alerts'
    ) THEN
        RETURN;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.sequences
         WHERE sequence_schema = 'public'
           AND sequence_name = 'model_health_alerts_id_seq'
    ) THEN
        RETURN;
    END IF;

    -- Use ``MAX(id) + 1`` with ``is_called=false`` so that:
    --   1) ``last_value`` is *strictly greater* than ``MAX(id)``
    --      immediately after this migration runs (the post-condition
    --      asked for in the task plan — operators can verify the
    --      repair without waiting for the first nextval()), and
    --   2) the very next ``nextval()`` returns exactly ``MAX(id)+1``
    --      (with ``is_called=false`` Postgres returns ``last_value``
    --      itself on the next call, then advances).
    PERFORM setval(
        'model_health_alerts_id_seq',
        GREATEST(
            (SELECT COALESCE(MAX(id), 0) FROM model_health_alerts) + 1,
            1
        ),
        false
    );
END $$;
"""


def upgrade() -> None:
    op.execute(_SQL_UP)


def downgrade() -> None:
    # No-op per the task plan: rolling the sequence value backwards
    # would re-introduce the very collision this migration cured, and
    # there is no meaningful inverse for a one-shot data repair.
    pass
