"""Persist richer Edge Terminal signal fields (task #845).

Task #844's original spec called for the signal row to include
``pick_type``, ``avg_odds``, ``ev_pct``, an optional
``source_screenshot_url`` and ``kalshi_market_id``.  Migration 0055
only landed ``edge_pct`` / ``model_prob`` / ``market_price`` /
``kalshi_ticker``, and 0056 added ``side``.  This migration finishes
the contract so the scraper can persist what it sees on the tile and
the dashboard can show pick type and screenshot evidence.

All new columns are nullable so existing rows survive the upgrade
untouched; the scraper fills them when the source tile exposes the
value and leaves them ``NULL`` otherwise.

Revision ID: 0057_edge_terminal_signal_enrichment
Revises: 0056_edge_terminal_signal_side
"""
from __future__ import annotations

from alembic import op


revision = "0057_edge_terminal_signal_enrichment"
down_revision = "0056_edge_terminal_signal_side"
branch_labels = None
depends_on = None


_SQL_UP = """
ALTER TABLE edge_terminal_signals
    ADD COLUMN IF NOT EXISTS pick_type            text,
    ADD COLUMN IF NOT EXISTS avg_odds             numeric,
    ADD COLUMN IF NOT EXISTS ev_pct               numeric,
    ADD COLUMN IF NOT EXISTS source_screenshot_url text,
    ADD COLUMN IF NOT EXISTS kalshi_market_id     text;
"""


def upgrade() -> None:
    op.execute(_SQL_UP)


def downgrade() -> None:
    pass
