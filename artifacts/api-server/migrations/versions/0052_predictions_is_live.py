"""Predictions.is_live tag for the in-game live prediction loop (task #830).

Adds a single boolean column to the ``predictions`` table so the
in-game live prediction loop (``app.learning.in_game_loop``) can tag
any row whose underlying game is currently in-progress at the time we
re-evaluated it.  The orchestrator's pre-game cycle leaves the column
at its default ``false``; the live loop sets it to ``true`` the first
time it touches a row (the value is monotonic — a row that ever went
live stays live for the honesty report's pre-vs-in-game brier split).

A small partial index keeps the operator dashboard's "live game now"
tile fast — the read is "count predictions where is_live=true and
league=X", which is exactly what the partial index covers.

Ships per-league disabled (see ``runtime_settings`` keys
``risk.live_loop_enabled_<sport>`` — default ``false`` until shadow
Brier is within 0.02 of the pre-game model for that sport).
"""
from __future__ import annotations

from alembic import op


revision = "0052_predictions_is_live"
down_revision = "0051_best_line_alert_actions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE predictions
            ADD COLUMN IF NOT EXISTS is_live boolean NOT NULL DEFAULT false
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS predictions_is_live_league_idx
            ON predictions (league)
            WHERE is_live = true
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS predictions_is_live_league_idx")
    op.execute("ALTER TABLE predictions DROP COLUMN IF EXISTS is_live")
