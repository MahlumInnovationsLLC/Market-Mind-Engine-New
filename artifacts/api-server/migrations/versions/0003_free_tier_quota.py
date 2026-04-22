"""Free-tier signal-quota table.

Tracks the distinct ``(user_id, market_id)`` pairs a free-tier caller
has evaluated each UTC day so the signal endpoints can enforce the
"5 markets/day" cap from the pricing page.  ``view_date`` is a UTC
``DATE`` so the counter resets at UTC midnight by construction.

Revision ID: 0003_free_tier_quota
Revises: 0002_order_fills_position_rollup
"""
from __future__ import annotations

from alembic import op


revision = "0003_free_tier_quota"
down_revision = "0002_order_fills_position_rollup"
branch_labels = None
depends_on = None


_SQL = """
CREATE TABLE IF NOT EXISTS free_tier_market_views (
    user_id     TEXT        NOT NULL,
    view_date   DATE        NOT NULL,
    market_id   TEXT        NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, view_date, market_id)
);

CREATE INDEX IF NOT EXISTS free_tier_market_views_user_date_idx
    ON free_tier_market_views (user_id, view_date);
"""


def upgrade() -> None:
    op.execute(_SQL)


def downgrade() -> None:
    raise NotImplementedError("free-tier quota table is not reversible")
