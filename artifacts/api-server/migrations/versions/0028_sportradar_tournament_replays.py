"""Persisted summary of every tournament replay run (task #697).

Task #651 added an `iter_tournament_leaderboard_history` helper that
yields one ratings dict per stored leaderboard snapshot.  Task #697
wires that helper into a backtest harness that walks every saved
snapshot of a finished tournament, prices each one through the
existing Plackett-Luce predictor, and grades the resulting
probabilities against the actual winner.

The harness is on-demand (operators trigger it from an admin endpoint),
but we want the resulting per-event accuracy report to survive the
HTTP round-trip so the operator dashboard can list the most recent
replays without re-walking the snapshot history every render.  This
table captures one row per completed run so the dashboard can plot a
trend and operators can spot which events the orchestrator priced
well mid-event vs which it whiffed on.

Schema is intentionally narrow — the per-snapshot detail lives in
``sportradar_tournament_leaderboards`` already and the harness can
re-derive it on demand if anyone needs to drill in.

Revision ID: 0028_sportradar_tournament_replays
Revises: 0027_notification_log_quiet_hours
"""
from __future__ import annotations

from alembic import op


revision = "0028_sportradar_tournament_replays"
down_revision = "0027_notification_log_quiet_hours"
branch_labels = None
depends_on = None


_SQL_UP = """
CREATE TABLE IF NOT EXISTS sportradar_tournament_replays (
    id            bigserial          PRIMARY KEY,
    event_id      text               NOT NULL,
    league        text               NOT NULL DEFAULT '',
    event_name    text               NOT NULL DEFAULT '',
    winner        text,
    n_snapshots   integer            NOT NULL,
    accuracy      double precision   NOT NULL,
    brier_score   double precision   NOT NULL,
    log_loss      double precision   NOT NULL,
    started_at    timestamptz        NOT NULL DEFAULT now(),
    completed_at  timestamptz        NOT NULL DEFAULT now()
);

-- Lookup pattern: "give me the most recent replays" — the operator
-- dashboard's recent-runs panel is keyed off completed_at DESC.
CREATE INDEX IF NOT EXISTS sportradar_tournament_replays_completed_idx
    ON sportradar_tournament_replays (completed_at DESC);

-- Lookup pattern: "give me every run for a single event" — operators
-- can drill into one tournament's history without scanning the whole
-- table.
CREATE INDEX IF NOT EXISTS sportradar_tournament_replays_event_idx
    ON sportradar_tournament_replays (event_id, completed_at DESC);
"""


def upgrade() -> None:
    op.execute(_SQL_UP)


def downgrade() -> None:
    # Out of scope per repo convention — see ``0027_notification_log_quiet_hours``.
    # Dropping the table would destroy the replay audit trail this
    # migration exists to capture.
    raise NotImplementedError("destructive down-migration not provided")
