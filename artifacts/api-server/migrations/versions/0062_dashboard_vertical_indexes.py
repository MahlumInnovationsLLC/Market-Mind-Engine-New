"""Add vertical-scoped indexes for the dashboard N+1 collapse (task #927).

``pg_stat_user_tables`` showed ``positions`` running 83,801 sequential
scans vs. 2,673 index scans (on 2,895 rows) and ``trades`` running
40,798 sequential scans. The dashboard's per-vertical loops in
:func:`app.dashboard.get_vertical_performance` and
:func:`app.dashboard.get_verticals_overview` issue dozens of queries
per refresh of the form::

    SELECT ... FROM positions WHERE vertical = $1 AND status = 'open' ...
    SELECT ... FROM trades    WHERE vertical = $1 AND executed_at >= ...

with NO indexes covering the ``vertical`` filter. Migration 0041 added
``positions_mode_status_idx (trading_mode, status)`` and 0039 added
``idx_trades_placed_by_executed_at (placed_by, executed_at DESC)`` —
neither leads with ``vertical``, so each per-vertical probe falls back
to a seq scan.

This migration adds the two missing leading-``vertical`` indexes the
collapsed dashboard queries (and any external scoped probe) will use.
Both are created ``CONCURRENTLY`` so the deploy doesn't hold an
AccessExclusive lock on the live writer paths
(``app.kalshi.lifecycle`` / ``app.orchestrator.cycle``).

Revision ID: 0062_dashboard_vertical_indexes
Revises: 0061_partition_line_snapshots_odds_changes
"""
from __future__ import annotations

from alembic import op


revision = "0062_dashboard_vertical_indexes"
down_revision = "0061_partition_line_snapshots_odds_changes"
branch_labels = None
depends_on = None


# CREATE INDEX CONCURRENTLY cannot run inside a transaction block, so
# each statement is executed via ``op.execute`` with an autocommit
# bridge below. The ``IF NOT EXISTS`` guard makes a partial run safe
# to retry — alembic will re-invoke ``upgrade`` if it crashes between
# the two index creations.
_INDEXES = (
    # positions: drives `WHERE vertical = $1 AND status = 'open' [AND trading_mode = ?]`
    # in dashboard.get_vertical_performance (~line 2802) and
    # dashboard.get_verticals_overview (~line 3529), plus the count(*) /
    # sum(notional) over open-by-vertical from
    # `app.risk.daily_loss` and the orchestrator's per-vertical
    # exposure roll-up.
    (
        "idx_positions_vertical_status",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_positions_vertical_status "
        "ON positions (vertical, status)",
    ),
    # trades: drives `WHERE vertical = $1 AND executed_at >= ...` in
    # both per-vertical dashboard helpers AND in the verticals-overview
    # Sharpe/PnL-today probes.  Leading ``vertical`` is what 0039's
    # ``(placed_by, executed_at DESC)`` index does NOT cover, which is
    # why the planner falls back to seq-scan even though both columns
    # are indexed separately.
    (
        "idx_trades_vertical_executed_at",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_trades_vertical_executed_at "
        "ON trades (vertical, executed_at DESC)",
    ),
)


def upgrade() -> None:
    # CONCURRENTLY requires running outside a transaction; alembic's
    # ``autocommit_block`` is the supported escape hatch (it suspends
    # the surrounding transaction, runs the body in autocommit, then
    # re-opens a fresh transaction for any later operations). Using
    # ``op.execute`` here also works correctly under SQLAlchemy 2.x,
    # which would reject ``bind.execute(<raw str>)``.
    with op.get_context().autocommit_block():
        for _name, sql in _INDEXES:
            op.execute(sql)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, _ in _INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
