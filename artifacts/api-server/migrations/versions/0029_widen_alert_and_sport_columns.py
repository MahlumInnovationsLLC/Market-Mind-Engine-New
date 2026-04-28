"""Widen two over-tight varchar columns blocking go-live.

Two production paths were failing every cycle because the column types
were narrower than the data we actually feed them:

* ``model_health_alerts.metric_name`` was ``varchar(50)`` but the
  Kalshi position-drift reconciler stores the full market ticker in
  this column (e.g.
  ``KXMVESPORTSMULTIGAMEEXTENDED-S20263320C8C2EF4-E4BE8428C73`` — 58
  chars).  Every cycle we logged
  ``WARNI [axiom.kalshi.reconciler] position_drift alert insert
  failed for ...: value too long for type character varying(50)`` and
  the alert was silently dropped, so operators were never paged on a
  real position drift.

* ``line_snapshots.sport`` was ``varchar(20)`` but The Odds API sport
  keys for international soccer are longer than 20 chars (e.g.
  ``soccer_germany_bundesliga`` = 25, ``soccer_uefa_champs_league`` =
  24).  Every odds-poll cycle the executemany batch for those
  leagues raised ``value too long for type character varying(20)``,
  and because the batch was a single transaction, the failure
  rolled back the entire sweep — so ``line_snapshots`` had ZERO
  rows in production despite the feed running every 30 s.  This
  in turn starved every downstream consumer (positive-EV
  derivation, backtest replay) of input.

Both changes are non-destructive ``ALTER COLUMN ... TYPE`` widenings.
No data is rewritten or relocated; existing rows fit the new bound
trivially.  Downgrade restores the original tighter bounds.
"""
from __future__ import annotations

from alembic import op


revision = "0029_widen_alert_and_sport_columns"
down_revision = "0028_sportradar_tournament_replays"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE model_health_alerts "
        "ALTER COLUMN metric_name TYPE varchar(255)"
    )
    op.execute(
        "ALTER TABLE line_snapshots "
        "ALTER COLUMN sport TYPE varchar(50)"
    )


def downgrade() -> None:
    # Note: downgrade will fail if any current value exceeds the old bound.
    # That is intentional — refusing to silently truncate operator data.
    op.execute(
        "ALTER TABLE model_health_alerts "
        "ALTER COLUMN metric_name TYPE varchar(50)"
    )
    op.execute(
        "ALTER TABLE line_snapshots "
        "ALTER COLUMN sport TYPE varchar(20)"
    )
