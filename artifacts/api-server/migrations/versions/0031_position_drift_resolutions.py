"""Add ``position_drift_resolutions`` table — operator workflow for the
unresolved drifts surfaced by the Kalshi reconciler.

The reconciler in ``app/kalshi/reconciler.py`` already detects drift
between local ``positions`` and Kalshi's ``/portfolio/positions`` and
fires deduped operator alerts (``model_health_alerts.alert_type =
'position_drift'``).  What it could NOT do until now was give an
operator a path to RESOLVE that drift — accept exchange truth and
overwrite the local row, or snooze a known-benign drift so the alert
stops re-firing each cycle.

This migration adds the dedicated resolution queue.  It is purely
additive: no existing tables touched, no existing columns altered.
The reconciler reads / writes this table behind the
``AXIOM_RECONCILER_RESOLUTION_ENABLED`` env flag (default off) so the
schema can land safely without changing runtime behaviour until the
flag is flipped.

Per cutover task #709.
"""
from __future__ import annotations

from alembic import op


revision = "0031_position_drift_resolutions"
down_revision = "0030_line_snapshots_outcome"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS position_drift_resolutions (
            id              bigserial PRIMARY KEY,
            ticker          varchar(255) NOT NULL,
            vertical        varchar(64),
            local_qty       integer NOT NULL,
            exchange_qty    integer NOT NULL,
            drift_qty       integer NOT NULL,
            state           varchar(40) NOT NULL DEFAULT 'open',
            resolver_email  varchar(255),
            resolution_reason text,
            snoozed_until   timestamptz,
            first_seen_at   timestamptz NOT NULL DEFAULT now(),
            last_seen_at    timestamptz NOT NULL DEFAULT now(),
            resolved_at     timestamptz,
            created_at      timestamptz NOT NULL DEFAULT now(),
            updated_at      timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # One open / snoozed row per ticker at a time. Resolved rows are
    # historical and may accumulate, so we partial-index the unique
    # constraint to ``state IN ('open', 'snoozed')`` only.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_pos_drift_res_active_ticker
            ON position_drift_resolutions (ticker)
         WHERE state IN ('open', 'snoozed')
        """
    )
    # Time-ordered scans for the operator queue and audit views.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_pos_drift_res_state_seen
            ON position_drift_resolutions (state, last_seen_at DESC)
        """
    )


def downgrade() -> None:
    # Purely additive table — safe to drop on rollback.
    op.execute("DROP INDEX IF EXISTS idx_pos_drift_res_state_seen")
    op.execute("DROP INDEX IF EXISTS idx_pos_drift_res_active_ticker")
    op.execute("DROP TABLE IF EXISTS position_drift_resolutions")
