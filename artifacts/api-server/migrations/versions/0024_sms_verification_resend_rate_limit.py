"""Per-user rate-limit counters for SMS verification resends.

Per task #430. The new ``POST /api/me/notification-prefs/sms/start-verification``
endpoint (task #191) issues a fresh paid Twilio SMS on every call, so a
buggy or hostile client can hammer it to either burn our SMS spend or
spam-text the phone owner. We cap resends at one per minute and ~five
per rolling hour per user; the new columns persist the per-user state
the limiter needs across pod restarts (in-memory counters wouldn't
survive a deploy and would silently let a redeploy reset the cap).

Schema additions on ``user_notification_prefs``:

  * ``sms_phone_verification_last_sent_at`` — timestamp of the most
    recent verification SMS we issued; powers the 60-second per-resend
    cooldown.
  * ``sms_phone_verification_attempts`` — integer counter for the
    *current* hourly window (``DEFAULT 0`` so existing rows behave as
    "no attempts yet"). Reset to 1 each time the window rolls over.
  * ``sms_phone_verification_window_started_at`` — timestamp marking
    the start of the rolling hourly window the counter belongs to. NULL
    means the next attempt opens a fresh window.

All three are also reset to NULL/0 in :func:`app.notifications.push.set_prefs`
whenever the user edits their phone number, so a legitimate "wrong
number, let me try a different one" doesn't get hit by the previous
number's cooldown.

Revision ID: 0024_sms_verification_resend_rate_limit
Revises: 0023_model_health_alerts_seq_repair

Chained onto ``0023_model_health_alerts_seq_repair`` — the merge node
that collapses the divergent 0022 branch — so ``alembic upgrade head``
remains single-headed. Picking ``0022_uw_signals_ticker_backfill`` as
the parent (one of the 0022 branches) would re-introduce a parallel
head and break ``alembic upgrade head`` on environments that have
already applied the merge.
"""
from __future__ import annotations

from alembic import op


revision = "0024_sms_verification_resend_rate_limit"
down_revision = "0023_model_health_alerts_seq_repair"
branch_labels = None
depends_on = None


_SQL_UP = """
ALTER TABLE user_notification_prefs
    ADD COLUMN IF NOT EXISTS sms_phone_verification_last_sent_at      timestamptz,
    ADD COLUMN IF NOT EXISTS sms_phone_verification_attempts          integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS sms_phone_verification_window_started_at timestamptz;
"""


_SQL_DOWN = """
ALTER TABLE user_notification_prefs
    DROP COLUMN IF EXISTS sms_phone_verification_last_sent_at,
    DROP COLUMN IF EXISTS sms_phone_verification_attempts,
    DROP COLUMN IF EXISTS sms_phone_verification_window_started_at;
"""


def upgrade() -> None:
    op.execute(_SQL_UP)


def downgrade() -> None:
    op.execute(_SQL_DOWN)
