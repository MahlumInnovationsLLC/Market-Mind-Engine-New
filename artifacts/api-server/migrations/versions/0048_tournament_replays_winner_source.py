"""Track where the tournament replay's actual-winner came from (task #708).

The replay harness (task #697) used to pick the actual winner by
reading the position-1 entrant of the most recent stored leaderboard
snapshot.  That works when the tournament finished cleanly, but it's
quietly wrong when polling stopped before the final whistle, when an
entrant was DQ'd after the snapshot was taken, or when the event was
cut short.  Task #708 layers a settlement-first lookup on top of that
inference so the harness prefers an authoritative source — the matched
Kalshi outright market settlement or a Sportradar finished-status
schedule row — before falling back to the snapshot heuristic.

So operators can spot inferred-vs-confirmed at a glance without
re-running the replay, this migration adds a ``winner_source`` text
column that records which source produced the winner: one of
``override`` (operator passed ``winner=`` on the replay endpoint),
``kalshi_settlement`` (the matched Kalshi outright market settled
YES on a single entrant), ``sportradar_summary`` (the Sportradar
schedule row reported the event finished) or ``snapshot_inference``
(last-resort fallback to the position-1 entrant of the latest
snapshot).  ``unknown`` is also valid for runs where no source could
be resolved at all.

Backfill: existing rows predate the settlement-first lookup, so we
stamp them with ``snapshot_inference`` to reflect how their winner
was actually chosen.  ``IF NOT EXISTS`` on the ADD COLUMN makes this
re-runnable on forks that hand-rolled the column.

Revision ID: 0048_tournament_replays_winner_source
Revises: 0047_soccer_settlement_review
"""
from __future__ import annotations

from alembic import op


revision = "0048_tournament_replays_winner_source"
down_revision = "0047_soccer_settlement_review"
branch_labels = None
depends_on = None


_SQL_UP = """
ALTER TABLE sportradar_tournament_replays
    ADD COLUMN IF NOT EXISTS winner_source text NOT NULL DEFAULT 'unknown';

-- Backfill any pre-existing rows: every replay before this migration
-- chose its winner via the position-1-of-latest-snapshot heuristic,
-- so stamp them with the matching source label.  Newly-inserted rows
-- carry the explicit source the harness resolved at run time.
UPDATE sportradar_tournament_replays
   SET winner_source = 'snapshot_inference'
 WHERE winner_source = 'unknown'
   AND winner IS NOT NULL;
"""


def upgrade() -> None:
    op.execute(_SQL_UP)


def downgrade() -> None:
    # Out of scope per repo convention — see ``0047_soccer_settlement_review``.
    # Dropping the column would erase the inferred-vs-confirmed audit
    # trail this migration exists to capture.
    raise NotImplementedError("destructive down-migration not provided")
