"""Add ``order_recovery_attempts`` sidecar + ``order_fills.source`` —
per-order submit-recovery tracking and dead-letter queue.

The existing submit-recovery path in ``app/kalshi/lifecycle.py`` keeps
*aggregate* counters (`_submit_recovery`) on the process and never
identifies which `client_order_id` is stuck. That hides two real
production failure modes:

  1. A single `client_order_id` permanently in `submit_failed` because
     the venue silently dropped it — invisible against the aggregate
     `degraded` health badge.
  2. A wave of `not_found` results across many orders — usually a sign
     the exchange itself is misbehaving, not us.

This migration adds the per-order tracking required by the operator
dead-letter / resolve workflow:

  * ``order_recovery_attempts`` — one row per ``client_order_id`` that
    has ever entered the recovery path. Tracks attempt count, latest
    outcome, dead-letter timestamp, and the eventual operator
    resolution. **Sidecar** (not extra columns on `order_lifecycle`)
    because `order_lifecycle` has many rows per order; this is one
    row per logical submission.

  * ``order_fills.source`` — distinguishes WS-derived fills (`'ws'`,
    the existing path) from operator-claimed fills (`'operator_manual'`,
    written by the `claim_filled` resolve action). The reconciler reads
    this so it can audit which positions were back-filled by a human.

Purely additive: no existing tables touched, no existing columns
altered, no ID column types changed. Old recovery code paths continue
to work uninstrumented until the runtime updates land.

Per task #710.
"""
from __future__ import annotations

from alembic import op


revision = "0032_order_recovery_attempts"
down_revision = "0031_position_drift_resolutions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS order_recovery_attempts (
            client_order_id       varchar(255) PRIMARY KEY,
            market_ticker         varchar(255) NOT NULL,
            side                  varchar(10) NOT NULL,
            action                varchar(10) NOT NULL,
            contracts             integer NOT NULL,
            limit_price_cents     integer,
            vertical              varchar(64),
            recovery_attempts     integer NOT NULL DEFAULT 0,
            last_recovery_at      timestamptz,
            last_recovery_outcome varchar(40),
            last_error            text,
            first_failed_at       timestamptz NOT NULL DEFAULT now(),
            dead_lettered_at      timestamptz,
            resolved_at           timestamptz,
            resolution_action     varchar(40),
            resolver_email        varchar(255),
            resolution_reason     text,
            fill_price_cents      integer,
            created_at            timestamptz NOT NULL DEFAULT now(),
            updated_at            timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # Operator queue (open dead-letters): time-ordered scans of rows the
    # sweeper marked but no operator has resolved yet.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_order_recovery_dead_letter_open
            ON order_recovery_attempts (dead_lettered_at DESC)
         WHERE dead_lettered_at IS NOT NULL
           AND resolved_at IS NULL
        """
    )
    # Sweeper input: rows that are still failing (no dead-letter yet,
    # not resolved). The sweep query filters on `first_failed_at`
    # against now()-threshold; this index keeps the scan O(log n).
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_order_recovery_failing_open
            ON order_recovery_attempts (first_failed_at)
         WHERE dead_lettered_at IS NULL
           AND resolved_at IS NULL
        """
    )

    # Distinguish WS-derived fills from operator-claimed manual fills.
    # Default 'ws' preserves existing behaviour for every pre-existing
    # row and every code path that doesn't know about the column.
    op.execute(
        """
        ALTER TABLE order_fills
            ADD COLUMN IF NOT EXISTS source varchar(40) NOT NULL DEFAULT 'ws'
        """
    )


def downgrade() -> None:
    # Purely additive — safe to drop on rollback.
    op.execute("DROP INDEX IF EXISTS idx_order_recovery_failing_open")
    op.execute("DROP INDEX IF EXISTS idx_order_recovery_dead_letter_open")
    op.execute("DROP TABLE IF EXISTS order_recovery_attempts")
    op.execute("ALTER TABLE order_fills DROP COLUMN IF EXISTS source")
