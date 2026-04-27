"""Persist the recipient's quiet-hours window on every notification log row.

Per task #630. Task #526 added a "Muted by quiet hours" annotation to
the mobile notifications feed by recomputing the window against the
user's *current* saved preferences. That works for fresh rows but
silently rewrites history the moment the user changes their saved
zone or hours: a row that was actually delivered while DND was
enabled stops carrying the annotation, and one that wasn't muted at
dispatch time can suddenly look like it was.

Storing the zone + start/end hours that were in effect for the
recipient at insert time lets the annotation stay accurate for the
lifetime of the row regardless of later preference edits.

Schema additions on ``mobile_notification_log``:

  * ``quiet_hours_tz`` — IANA zone string copied from
    ``user_notification_prefs.quiet_hours_tz`` at dispatch time, or
    ``NULL`` when the recipient hadn't configured one.
  * ``quiet_hours_start_hour`` / ``quiet_hours_end_hour`` —
    ``smallint`` snapshots of the corresponding pref columns.

All three are nullable: legacy rows that predate this revision have
no recorded zone, so the mobile UI just falls back to no annotation
for them rather than re-deriving (which would re-introduce the very
bug this fix is for).

Revision ID: 0027_notification_log_quiet_hours
Revises: 0026_predictions_model_yes_prob
"""
from __future__ import annotations

from alembic import op


revision = "0027_notification_log_quiet_hours"
down_revision = "0026_predictions_model_yes_prob"
branch_labels = None
depends_on = None


_SQL_UP = """
ALTER TABLE mobile_notification_log
    ADD COLUMN IF NOT EXISTS quiet_hours_tz         text,
    ADD COLUMN IF NOT EXISTS quiet_hours_start_hour smallint,
    ADD COLUMN IF NOT EXISTS quiet_hours_end_hour   smallint;
"""


_SQL_DOWN = """
ALTER TABLE mobile_notification_log
    DROP COLUMN IF EXISTS quiet_hours_tz,
    DROP COLUMN IF EXISTS quiet_hours_start_hour,
    DROP COLUMN IF EXISTS quiet_hours_end_hour;
"""


def upgrade() -> None:
    op.execute(_SQL_UP)


def downgrade() -> None:
    op.execute(_SQL_DOWN)
