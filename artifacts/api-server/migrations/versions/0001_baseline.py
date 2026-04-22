"""Baseline schema.

Captures every table and column that the app previously created via
lazy ``ensure_schema``/``ensure_table`` helpers at request-time. All
statements use ``IF NOT EXISTS`` / ``ADD COLUMN IF NOT EXISTS`` so the
migration is safe to apply against the existing production database
where these objects were already created by the lazy paths.

Per task spec, no destructive down-migration is provided.

Revision ID: 0001_baseline
Revises:
"""
from __future__ import annotations

from alembic import op


revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


_BASELINE_SQL = """
-- ---------------------------------------------------------------------
-- Admin: app_users + admin_audit_log (was app/admin.py::ensure_schema)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_users (
    clerk_user_id   text PRIMARY KEY,
    email           text,
    display_name    text,
    approved        boolean NOT NULL DEFAULT false,
    is_admin        boolean NOT NULL DEFAULT false,
    first_seen_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at    timestamptz NOT NULL DEFAULT now(),
    activity_count  integer NOT NULL DEFAULT 0,
    clerk_created_at      timestamptz,
    clerk_last_sign_in_at timestamptz,
    clerk_synced_at       timestamptz,
    denied_at             timestamptz
);

ALTER TABLE app_users ADD COLUMN IF NOT EXISTS clerk_created_at      timestamptz;
ALTER TABLE app_users ADD COLUMN IF NOT EXISTS clerk_last_sign_in_at timestamptz;
ALTER TABLE app_users ADD COLUMN IF NOT EXISTS clerk_synced_at       timestamptz;
ALTER TABLE app_users ADD COLUMN IF NOT EXISTS denied_at             timestamptz;

CREATE INDEX IF NOT EXISTS app_users_email_idx ON app_users (lower(email));

CREATE TABLE IF NOT EXISTS admin_audit_log (
    id              bigserial PRIMARY KEY,
    actor_user_id   text NOT NULL,
    actor_email     text,
    target_user_id  text NOT NULL,
    target_email    text,
    action          text NOT NULL,
    before_value    jsonb,
    after_value     jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS admin_audit_log_target_idx
    ON admin_audit_log (target_user_id, created_at DESC);

-- ---------------------------------------------------------------------
-- Stripe billing (was app/billing.py::ensure_schema)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS subscriptions (
    user_id TEXT PRIMARY KEY,
    email TEXT,
    tier TEXT NOT NULL DEFAULT 'free',
    status TEXT NOT NULL DEFAULT 'inactive',
    stripe_customer_id TEXT,
    stripe_subscription_id TEXT,
    stripe_price_id TEXT,
    interval TEXT,
    current_period_end TIMESTAMPTZ,
    cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS subscriptions_customer_idx
    ON subscriptions (stripe_customer_id);
CREATE INDEX IF NOT EXISTS subscriptions_subscription_idx
    ON subscriptions (stripe_subscription_id);

CREATE TABLE IF NOT EXISTS stripe_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    payload JSONB
);

CREATE TABLE IF NOT EXISTS stripe_subscriptions (
    stripe_subscription_id TEXT PRIMARY KEY,
    stripe_customer_id TEXT,
    user_id TEXT,
    status TEXT,
    stripe_price_id TEXT,
    interval TEXT,
    current_period_end TIMESTAMPTZ,
    cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE,
    raw JSONB,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS stripe_subscriptions_customer_idx
    ON stripe_subscriptions (stripe_customer_id);
CREATE INDEX IF NOT EXISTS stripe_subscriptions_user_idx
    ON stripe_subscriptions (user_id);

-- ---------------------------------------------------------------------
-- RevenueCat billing mirror (was app/rc_billing.py::ensure_schema)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rc_subscriptions (
    key TEXT PRIMARY KEY,
    app_user_id TEXT,
    user_id TEXT,
    product_id TEXT,
    entitlement TEXT,
    tier TEXT,
    event_type TEXT,
    event_id TEXT,
    store TEXT,
    expires_at TIMESTAMPTZ,
    raw JSONB,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS rc_subscriptions_user_idx
    ON rc_subscriptions (user_id);

CREATE TABLE IF NOT EXISTS rc_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    payload JSONB
);

-- ---------------------------------------------------------------------
-- Notifications (alerts dedupe + suppression + churn + daily summary)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS notification_dedupe (
    key  TEXT PRIMARY KEY,
    date_utc DATE,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ
);

ALTER TABLE notification_dedupe ALTER COLUMN date_utc DROP NOT NULL;
ALTER TABLE notification_dedupe ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS email_suppressions (
    email      TEXT PRIMARY KEY,
    reason     TEXT NOT NULL,
    detail     TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS billing_churn_events (
    id            BIGSERIAL PRIMARY KEY,
    event_type    TEXT NOT NULL,
    user_id       TEXT,
    email         TEXT,
    email_sent    BOOLEAN NOT NULL DEFAULT FALSE,
    skipped_reason TEXT,
    details       JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS billing_churn_events_created_idx
    ON billing_churn_events (created_at DESC);

-- ---------------------------------------------------------------------
-- Kalshi market title cache + trades.market_title column
-- (was app/kalshi/market_titles.py::ensure_schema)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS market_title_cache (
    ticker      text PRIMARY KEY,
    title       text NOT NULL,
    fetched_at  timestamptz NOT NULL DEFAULT now()
);

-- The `trades` table is owned by the upstream production schema; only
-- add the column if the table exists so a fresh DB without the trade
-- pipeline schema doesn't fail the migration.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'trades'
    ) THEN
        ALTER TABLE trades ADD COLUMN IF NOT EXISTS market_title text;
    END IF;
END$$;

-- ---------------------------------------------------------------------
-- Drift / model-health alert ack + snooze columns
-- (was main.py::_ensure_alert_schema)
-- ---------------------------------------------------------------------
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'model_health_alerts'
    ) THEN
        ALTER TABLE model_health_alerts
            ADD COLUMN IF NOT EXISTS acknowledged_by  text,
            ADD COLUMN IF NOT EXISTS acknowledged_at  timestamptz,
            ADD COLUMN IF NOT EXISTS snooze_until     timestamptz;
    END IF;
END$$;
"""


def upgrade() -> None:
    op.execute(_BASELINE_SQL)


def downgrade() -> None:
    # Out of scope per task spec — destructive down-migrations are not
    # provided for this baseline.
    raise NotImplementedError("baseline migration is not reversible")
