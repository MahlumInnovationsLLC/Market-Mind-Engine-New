"""Backfill ``uw_signals.ticker`` from payload + normalise existing values.

Per task #425. The new per-ticker UW mining (task #189) joins settled
trades against ``upper(uw_signals.ticker) = $4``.  Historical rows that
landed with a NULL ticker (several UW endpoints don't surface a
top-level ``ticker`` field on the dark-pool feed for example) — or
with a non-canonical one (lowercase, whitespace-padded) — silently
drop out of the per-ticker aggregation, hurting the IC / Sharpe
estimates on crypto / equities / macro where per-ticker matters most.

This migration runs two idempotent passes:

  1. Fill NULL tickers from the stored ``payload`` JSON, walking the
     same precedence as :func:`app.feeds.unusual_whales._row_ticker`
     (``ticker`` → ``symbol`` → ``underlying`` → ``underlying_symbol``).
  2. Uppercase / trim existing ticker values so the
     ``upper(ticker) = $4`` join hits.

Both passes are guarded by ``WHERE`` predicates that exclude rows
which are already in canonical form, so re-applying the migration on
a fork that already has clean data is a no-op.  An equivalent admin
endpoint (``POST /api/admin/uw-signals/backfill-ticker``) calls the
same SQL via :func:`app.feeds.uw_ticker_backfill.backfill_uw_signals_ticker`
so operators can re-run the backfill after a degraded ingest window
without driving the whole Alembic stack.

Revision ID: 0022_uw_signals_ticker_backfill
Revises: 0021_crypto_backtest_runs
"""
from __future__ import annotations

from alembic import op


revision = "0022_uw_signals_ticker_backfill"
down_revision = "0021_crypto_backtest_runs"
branch_labels = None
depends_on = None


# ``DO`` block so the UPDATEs only run when the table actually exists
# (forks pinned to an older head where 0003 hasn't applied yet — or
# where the table was created by hand without ever joining the
# Alembic walk — must not abort the upgrade).
_SQL_UP = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
         WHERE table_name = 'uw_signals'
    ) THEN
        RETURN;
    END IF;

    -- Pass 1: derive ticker from the JSON payload for any row where
    -- the column is still NULL.  ``payload->>'k'`` returns NULL for
    -- absent / JSON-null entries; the trim+nullif chain also drops
    -- whitespace-only strings.
    UPDATE uw_signals
       SET ticker = upper(btrim(
               coalesce(
                   nullif(btrim(payload->>'ticker'), ''),
                   nullif(btrim(payload->>'symbol'), ''),
                   nullif(btrim(payload->>'underlying'), ''),
                   nullif(btrim(payload->>'underlying_symbol'), '')
               )
           ))
     WHERE ticker IS NULL
       AND coalesce(
               nullif(btrim(payload->>'ticker'), ''),
               nullif(btrim(payload->>'symbol'), ''),
               nullif(btrim(payload->>'underlying'),  ''),
               nullif(btrim(payload->>'underlying_symbol'), '')
           ) IS NOT NULL;

    -- Pass 2: normalise any existing ticker that isn't already
    -- ``upper(btrim(...))``.  Idempotent: the predicate excludes
    -- rows that are already canonical so re-running is free.
    UPDATE uw_signals
       SET ticker = upper(btrim(ticker))
     WHERE ticker IS NOT NULL
       AND ticker <> upper(btrim(ticker))
       AND btrim(ticker) <> '';
END $$;
"""


def upgrade() -> None:
    op.execute(_SQL_UP)


def downgrade() -> None:
    # The backfill is purely additive — it strictly improves the
    # column's coverage / canonicalisation.  There is no meaningful
    # inverse (we'd have to re-introduce NULLs / lowercase noise we
    # already lost the original form of), so refuse to roll back.
    raise NotImplementedError("destructive down-migration not provided")
