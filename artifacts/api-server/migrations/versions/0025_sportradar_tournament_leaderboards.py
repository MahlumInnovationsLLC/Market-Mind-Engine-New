"""Append-only history of Sportradar tournament leaderboard snapshots.

Per task #623.  Task #449 wired ``_poll_tournament_leaderboards`` to
keep the per-event ratings cache in :mod:`app.sports.ratings` fresh
from the live PGA / F1 leaderboard, but that cache is process-local
and gets blown away on every restart — there's no audit trail of what
the field looked like at any past minute, so we can't replay the
prices the orchestrator quoted mid-event.

This migration adds ``sportradar_tournament_leaderboards``, an
append-only table that captures one row per (event_id, position,
name) on every poll.  All rows from a single fetch share the same
``fetched_at`` timestamp so a "snapshot" is just
``WHERE event_id = $1 AND fetched_at = MAX(fetched_at)`` — the helper
:func:`app.feeds.sportradar.latest_tournament_leaderboard` does
exactly that.

This revision also doubles as the merge node for the two parallel
0024 heads (``0024_sms_verification_resend_rate_limit`` from task
#430 and ``0024_trades_metadata`` from task #444) so
``alembic upgrade head`` stays single-headed.
"""
from __future__ import annotations

from alembic import op


revision = "0025_sportradar_tournament_leaderboards"
down_revision = (
    "0024_sms_verification_resend_rate_limit",
    "0024_trades_metadata",
)
branch_labels = None
depends_on = None


_SQL_UP = """
CREATE TABLE IF NOT EXISTS sportradar_tournament_leaderboards (
    id          bigserial   PRIMARY KEY,
    event_id    text        NOT NULL,
    position    integer,
    name        text        NOT NULL,
    fetched_at  timestamptz NOT NULL DEFAULT now()
);

-- Lookup pattern: "give me the most recent snapshot for an event" —
-- driven by (event_id, fetched_at DESC).  The composite index also
-- accelerates the MAX(fetched_at) sub-query the helper uses.
CREATE INDEX IF NOT EXISTS sportradar_tournament_leaderboards_event_idx
    ON sportradar_tournament_leaderboards (event_id, fetched_at DESC);
"""


def upgrade() -> None:
    op.execute(_SQL_UP)


def downgrade() -> None:
    # Out of scope per repo convention — see ``0024_trades_metadata``.
    # Dropping the table would destroy the historical audit trail this
    # migration exists to capture.
    raise NotImplementedError("destructive down-migration not provided")
