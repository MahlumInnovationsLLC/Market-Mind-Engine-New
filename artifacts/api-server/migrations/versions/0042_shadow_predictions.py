"""Shadow-prediction snapshot table (task #753).

The orchestrator only persists a `predictions` row for markets one of
the discovery feeds materialises, and only ever assigns a
``model_yes_prob`` to those markets — so the per-league calibrator
trains exclusively on outcomes from markets we considered trading.
That biases calibration toward whatever sub-population our edge
gates surface and starves it of negative-edge / passed-on markets.

This migration creates the immutable snapshot table the shadow
predictor (``app/learning/shadow_predictor.py``) writes to on its
hourly cadence.  Each row captures the model's calibrated probability
for an active Kalshi market at a point in time, irrespective of
whether we traded the market or even produced an edge.  The matching
``shadow_learning_buffer`` (migration 0043) closes the loop on
settlement and feeds the rows into the calibrator's training set.

Schema notes:

  * ``predicted_at`` is the snapshot time; the unique index on
    ``(market_id, predicted_at)`` truncates to the hour at write time
    in app code so the hourly job is idempotent across re-runs of
    the same cycle.
  * ``settled_at`` and ``outcome`` mirror the live ``predictions``
    columns and are stamped by ``shadow_settler.py`` once the
    underlying market closes.
  * ``source`` defaults to ``'shadow'`` so a future expansion to
    other unbiased prediction sources (e.g. external benchmark
    models) can share the same table.
  * Two partial indexes split the hot/cold scans: one for the
    settler's "needs settlement" pass, one for the calibrator
    join's "already settled" pass.

Per the standing convention, the up migration uses
``CREATE TABLE IF NOT EXISTS`` so re-runs on a fork that hand-rolled
the table are no-ops, and the down migration drops the table.

Revision ID: 0042_shadow_predictions
Revises: 0041_paper_live_trading_mode
"""
from __future__ import annotations

from alembic import op


revision = "0042_shadow_predictions"
down_revision = "0041_paper_live_trading_mode"
branch_labels = None
depends_on = None


_SQL_UP = """
CREATE TABLE IF NOT EXISTS shadow_predictions (
    id                  bigserial   PRIMARY KEY,
    market_id           text        NOT NULL,
    league              text,
    vertical            text,
    kalshi_yes_price    double precision,
    predicted_prob      double precision,
    uncalibrated_prob   double precision,
    confidence          double precision,
    model_name          text,
    model_version       text,
    predicted_at        timestamptz NOT NULL DEFAULT now(),
    settled_at          timestamptz,
    outcome             integer,
    source              text        NOT NULL DEFAULT 'shadow',
    metadata            jsonb       NOT NULL DEFAULT '{}'::jsonb
);

-- Hourly idempotency: the predictor truncates `predicted_at` to the
-- hour before INSERT so two runs in the same hour collide on this
-- key and the second one falls through ON CONFLICT DO NOTHING.
CREATE UNIQUE INDEX IF NOT EXISTS shadow_predictions_market_hour_uidx
    ON shadow_predictions (market_id, predicted_at);

-- Settler hot scan: "rows that still need a settlement stamp".
CREATE INDEX IF NOT EXISTS shadow_predictions_unsettled_idx
    ON shadow_predictions (market_id, predicted_at)
    WHERE settled_at IS NULL;

-- Reporting / calibration join scan: "settled rows by league recently".
CREATE INDEX IF NOT EXISTS shadow_predictions_settled_at_idx
    ON shadow_predictions (league, settled_at)
    WHERE settled_at IS NOT NULL;
"""


_SQL_DOWN = """
DROP INDEX IF EXISTS shadow_predictions_settled_at_idx;
DROP INDEX IF EXISTS shadow_predictions_unsettled_idx;
DROP INDEX IF EXISTS shadow_predictions_market_hour_uidx;
DROP TABLE IF EXISTS shadow_predictions;
"""


def upgrade() -> None:
    op.execute(_SQL_UP)


def downgrade() -> None:
    op.execute(_SQL_DOWN)
