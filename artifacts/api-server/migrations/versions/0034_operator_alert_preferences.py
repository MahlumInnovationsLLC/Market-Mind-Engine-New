"""Add ``operator_alert_preferences`` — per-operator opt-out for the
new branded HTML per-trade operator alert (task #712).

The existing operator alert plumbing fans out to every email in
``OPERATOR_ALERT_EMAILS`` (or ``OPERATOR_ALLOWED_EMAILS`` as a
fallback) with no per-recipient knob. The new
``operator_trade_alert`` is going to drop a richer HTML email on
every live fill — operators who only want the firehose plain-text
``live_trade()`` need a way to opt themselves out of the branded
companion without un-subscribing from every other operator alert.

Schema:

  * Composite primary key (email, alert_type) — same operator can
    independently mute different alert types if we add more later.
  * ``enabled`` defaults to ``true``: a fresh install sends to every
    configured operator without any setup.
  * ``updated_at`` lets the admin UI show "last changed".

Purely additive: no existing tables touched, no columns altered,
no IDs changed. Safe to roll back at any time.
"""
from __future__ import annotations

from alembic import op


revision = "0034_operator_alert_preferences"
down_revision = "0033_stale_cancel_observations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS operator_alert_preferences (
            email       varchar(320) NOT NULL,
            alert_type  varchar(64)  NOT NULL,
            enabled     boolean      NOT NULL DEFAULT true,
            updated_at  timestamptz  NOT NULL DEFAULT now(),
            PRIMARY KEY (email, alert_type)
        )
        """
    )
    # Lookups during the per-fill fan-out are by alert_type + email,
    # which the PK index already covers. An additional index by
    # alert_type alone lets the admin UI list all operators who have
    # touched their pref for a given type without scanning the table.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_operator_alert_prefs_type
            ON operator_alert_preferences (alert_type)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_operator_alert_prefs_type")
    op.execute("DROP TABLE IF EXISTS operator_alert_preferences")
