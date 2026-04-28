"""Add ``stale_cancel_observations`` — per-cycle stale-cancel sweep
counters used by the rolling 24h baseline + anomaly alerting.

The cycle-start sweep (``app.orchestrator.cycle._cancel_stale_resting_orders``)
already returns ``{scanned, cancelled, failed, max_age}`` per cycle but
the only persistence is the in-process counter on ``kalshi.lifecycle``.
That hides two real production failure modes:

  1. **Volume anomaly** — a wave of stale-cancel events per cycle in a
     vertical (e.g. 50/cycle when the rolling 24h mean is 3) usually
     means upstream (model / odds / strategy) is producing thrashing
     candidates, not that anything is "broken" with the sweep itself.
     Without a baseline an operator has no way to spot this.

  2. **Failure spike** — two consecutive cycles where ``failed > 0``
     means the venue-side cancel API is unhappy in a way the
     swallow-and-continue path silences. Shows up as a quiet `degraded`
     badge on health, never pages.

This migration adds the time-series store the baseline computer reads
and the anomaly evaluator writes:

  * ``stale_cancel_observations`` — one row per (cycle, vertical),
    written in the cycle's tail. Retention is 7 days (anomaly evaluator
    only needs 24h, but operators need a week of history for the
    sparkline + after-the-fact triage).

Purely additive: no existing tables touched, no existing columns
altered, no ID column types changed. Old code paths that don't write
observations continue to work — the anomaly evaluator just sees an
empty baseline window and stays quiet.

Per task #711.
"""
from __future__ import annotations

from alembic import op


revision = "0033_stale_cancel_observations"
down_revision = "0032_order_recovery_attempts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS stale_cancel_observations (
            id          bigserial PRIMARY KEY,
            observed_at timestamptz NOT NULL DEFAULT now(),
            vertical    varchar(64) NOT NULL,
            cycle_id    bigint,
            scanned     integer NOT NULL DEFAULT 0,
            cancelled   integer NOT NULL DEFAULT 0,
            failed      integer NOT NULL DEFAULT 0,
            max_age     integer NOT NULL DEFAULT 0
        )
        """
    )
    # Baseline computer query: scan rows for one vertical inside the
    # rolling window. Composite index on (vertical, observed_at DESC)
    # keeps the lookup O(log n + k) regardless of total table size.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_stale_cancel_obs_vertical_time
            ON stale_cancel_observations (vertical, observed_at DESC)
        """
    )
    # Retention sweep input: time-ordered scan of rows older than the
    # GC threshold (7 days). Partial index keeps the scan tiny —
    # fresh rows are skipped because the WHERE clause filters them out.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_stale_cancel_obs_observed_at
            ON stale_cancel_observations (observed_at)
        """
    )


def downgrade() -> None:
    # Purely additive — safe to drop on rollback.
    op.execute("DROP INDEX IF EXISTS idx_stale_cancel_obs_observed_at")
    op.execute("DROP INDEX IF EXISTS idx_stale_cancel_obs_vertical_time")
    op.execute("DROP TABLE IF EXISTS stale_cancel_observations")
