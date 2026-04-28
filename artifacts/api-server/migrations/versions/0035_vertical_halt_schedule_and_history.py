"""Per-vertical halt scheduling + audit history (task #713).

Adds two purely additive tables:

* ``vertical_halt_schedules`` — recurring halt rules keyed by 5-field
  cron expression. The background scheduler ticks each rule on the
  next minute boundary that matches and applies a halt with the
  rule's ``duration_secs`` TTL. ``last_fired_at`` is the dedupe key
  that prevents a sub-minute scheduler tick from re-firing a rule
  that already fired in the current window.

* ``vertical_halt_history`` — append-only audit feed. Every halt
  event (manual / scheduled / TTL-expiry / resume) writes one row
  so the operator-console history view doesn't have to reconstruct
  the timeline from ``admin_audit_log`` JSON blobs.

Existing ``system_settings`` rows are untouched — TTL is stored
inside the existing JSON value blob (``expires_at`` field) so the
risk-gate chain keeps reading the same key with no schema change.
"""
from __future__ import annotations

from alembic import op


revision = "0035_vertical_halt_schedule_and_history"
down_revision = "0034_operator_alert_preferences"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS vertical_halt_schedules (
            id              bigserial    PRIMARY KEY,
            vertical        varchar(64)  NOT NULL,
            cron_expr       varchar(128) NOT NULL,
            duration_secs   integer      NOT NULL CHECK (duration_secs > 0),
            reason          text         NOT NULL,
            enabled         boolean      NOT NULL DEFAULT true,
            created_by_user_id varchar(128),
            created_by_email   varchar(320),
            created_at      timestamptz  NOT NULL DEFAULT now(),
            updated_at      timestamptz  NOT NULL DEFAULT now(),
            last_fired_at   timestamptz
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_vertical_halt_schedules_vertical
            ON vertical_halt_schedules (vertical)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_vertical_halt_schedules_enabled
            ON vertical_halt_schedules (enabled)
            WHERE enabled = true
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS vertical_halt_history (
            id              bigserial    PRIMARY KEY,
            vertical        varchar(64)  NOT NULL,
            event           varchar(32)  NOT NULL
                CHECK (event IN ('halted', 'resumed', 'ttl_expired')),
            source          varchar(32)  NOT NULL
                CHECK (source IN ('manual_admin', 'scheduled_rule', 'ttl_expiry')),
            actor_user_id   varchar(128),
            actor_email     varchar(320),
            reason          text,
            effective_at    timestamptz  NOT NULL DEFAULT now(),
            expires_at      timestamptz,
            schedule_id     bigint
                REFERENCES vertical_halt_schedules(id) ON DELETE SET NULL
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_vertical_halt_history_vertical_at
            ON vertical_halt_history (vertical, effective_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_vertical_halt_history_at
            ON vertical_halt_history (effective_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_vertical_halt_history_at")
    op.execute("DROP INDEX IF EXISTS idx_vertical_halt_history_vertical_at")
    op.execute("DROP TABLE IF EXISTS vertical_halt_history")
    op.execute("DROP INDEX IF EXISTS idx_vertical_halt_schedules_enabled")
    op.execute("DROP INDEX IF EXISTS idx_vertical_halt_schedules_vertical")
    op.execute("DROP TABLE IF EXISTS vertical_halt_schedules")
