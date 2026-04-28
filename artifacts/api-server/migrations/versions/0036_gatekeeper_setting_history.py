"""Per-vertical gatekeeper-knob override audit history (task #714).

The four swarm-gatekeeper risk knobs (``min_edge_pp``,
``min_agreement_rate``, ``max_confidence_band`` and
``kelly_fraction_cap``) already accept a single global override stored
as ``system_settings`` rows ``risk.<knob>``.  Task #714 layers a
per-vertical override on top — ``risk.<knob>.<vertical>`` — so a
single noisy book (e.g. ``crypto``) can be tightened without
sand-bagging the rest of the portfolio.

We keep the override values themselves in the existing
``system_settings`` table (no new column / no schema change for the
hot read path) and only add an append-only audit feed here:

* ``gatekeeper_setting_history`` — one row per change (set / clear).
  ``vertical`` is NULL for global-scope changes and the lower-cased
  vertical name otherwise.  ``old_value`` / ``new_value`` are stored
  as the raw stringified numeric (preserving exact slider input)
  rather than ``numeric`` so a future int-only knob can reuse the
  same table without a column-type migration.  No-op writes (where
  ``old_value == new_value``) are NOT recorded — the API layer
  short-circuits them so the audit feed only reflects real changes.
"""
from __future__ import annotations

from alembic import op


revision = "0036_gatekeeper_setting_history"
down_revision = "0035_vertical_halt_schedule_and_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS gatekeeper_setting_history (
            id              bigserial    PRIMARY KEY,
            knob            varchar(64)  NOT NULL,
            vertical        varchar(64),
            old_value       text,
            new_value       text,
            changed_by_user_id varchar(128),
            changed_by_email   varchar(320),
            reason          text,
            changed_at      timestamptz  NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_gatekeeper_setting_history_at
            ON gatekeeper_setting_history (changed_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_gatekeeper_setting_history_knob
            ON gatekeeper_setting_history (knob, changed_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_gatekeeper_setting_history_vertical
            ON gatekeeper_setting_history (vertical, changed_at DESC)
            WHERE vertical IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_gatekeeper_setting_history_vertical")
    op.execute("DROP INDEX IF EXISTS idx_gatekeeper_setting_history_knob")
    op.execute("DROP INDEX IF EXISTS idx_gatekeeper_setting_history_at")
    op.execute("DROP TABLE IF EXISTS gatekeeper_setting_history")
