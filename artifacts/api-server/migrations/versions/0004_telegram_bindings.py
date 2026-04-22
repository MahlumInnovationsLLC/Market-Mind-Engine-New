"""Telegram concierge bindings.

Per task #111. Stores the per-operator Telegram binding so the
concierge bot can identify the requesting Telegram user as a known
Clerk operator before honouring read-only queries (/status, /positions,
/halts) or the confirm-protected /estop command.

Also serves as the merge migration for the four sibling 0003_* heads
that branched off ``0002_order_fills_position_rollup``. Without the
merge ``alembic upgrade head`` would have to be run with each head
explicitly; with it the script directory once again has a single tip
that the lifespan runner can target.

Revision ID: 0004_telegram_bindings
"""
from __future__ import annotations

from alembic import op


revision = "0004_telegram_bindings"
down_revision = (
    "0003_bankroll_events",
    "0003_crypto_ohlcv",
    "0003_free_tier_quota",
    "0003_uw_and_public_betting",
)
branch_labels = None
depends_on = None


_SQL = """
CREATE TABLE IF NOT EXISTS telegram_bindings (
    clerk_user_id        text PRIMARY KEY,
    telegram_user_id     bigint UNIQUE,
    telegram_chat_id     bigint,
    telegram_username    text,
    link_code            text,
    link_code_expires_at timestamptz,
    bound_at             timestamptz,
    last_seen_at         timestamptz,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS telegram_bindings_tg_user_idx
    ON telegram_bindings (telegram_user_id);

CREATE UNIQUE INDEX IF NOT EXISTS telegram_bindings_link_code_uidx
    ON telegram_bindings (link_code) WHERE link_code IS NOT NULL;
"""


def upgrade() -> None:
    op.execute(_SQL)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS telegram_bindings")
