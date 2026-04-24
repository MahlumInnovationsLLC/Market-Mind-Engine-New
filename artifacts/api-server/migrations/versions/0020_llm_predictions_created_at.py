"""Add ``llm_predictions.created_at`` for the fallback-spike monitor.

The swarm fallback monitor (``app.swarm.fallback_monitor``) groups recent
persona-call rows in a trailing time window keyed on
``llm_predictions.created_at``.  The baseline schema only carried
``predicted_at`` (the moment the persona was asked) and ``resolved_at``
(when the outcome settled), so the monitor fails at boot with
``column "created_at" does not exist`` on every prod / fork that was
provisioned before this column was added.

Adding the column as ``timestamptz NOT NULL DEFAULT now()`` keeps every
existing row valid (the default backfills NULLs to "now"), and matches
the ``timestamptz`` arithmetic the monitor's ``now() - make_interval(...)``
clause expects.  An index on ``created_at DESC`` keeps the fallback
window query cheap as the table grows.
"""
from __future__ import annotations

from alembic import op


revision = "0020_llm_predictions_created_at"
down_revision = "0019_merge_heads"
branch_labels = None
depends_on = None


_SQL_UP = """
-- Step 1: add the column NULL-able so we can deterministically tell
-- which rows still need a backfill on re-runs (idempotent).
ALTER TABLE llm_predictions
    ADD COLUMN IF NOT EXISTS created_at timestamptz;

-- Step 2: backfill any still-NULL rows from predicted_at where useful so
-- historical windowed queries don't bunch up at "now".  predicted_at is
-- ``timestamp without time zone`` so we cast it to UTC.  Falls back to
-- now() for rows that have neither.  This predicate is fully idempotent:
-- on re-run there are no NULLs left so it's a no-op.
UPDATE llm_predictions
   SET created_at = COALESCE(predicted_at AT TIME ZONE 'UTC', now())
 WHERE created_at IS NULL;

-- Step 3: now that no NULLs remain, lock the column down with NOT NULL
-- + a default for future inserts.  Uses DO blocks so it's idempotent
-- across re-runs (information_schema check avoids duplicate ALTERs).
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'llm_predictions'
           AND column_name = 'created_at'
           AND is_nullable = 'YES'
    ) THEN
        ALTER TABLE llm_predictions ALTER COLUMN created_at SET NOT NULL;
    END IF;
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'llm_predictions'
           AND column_name = 'created_at'
           AND column_default IS NULL
    ) THEN
        ALTER TABLE llm_predictions ALTER COLUMN created_at SET DEFAULT now();
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS llm_predictions_created_at_idx
    ON llm_predictions (created_at DESC);
"""

_SQL_DOWN = """
DROP INDEX IF EXISTS llm_predictions_created_at_idx;
ALTER TABLE llm_predictions DROP COLUMN IF EXISTS created_at;
"""


def upgrade() -> None:
    op.execute(_SQL_UP)


def downgrade() -> None:
    op.execute(_SQL_DOWN)
