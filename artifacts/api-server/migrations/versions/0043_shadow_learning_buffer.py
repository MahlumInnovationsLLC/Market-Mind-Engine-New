"""Shadow-learning buffer: settled snapshots fed into calibrator UNION.

Companion to migration 0042 (``shadow_predictions``).  The
``shadow_settler`` (``app/learning/shadow_settler.py``) consumes
freshly-settled shadow predictions and inserts one row per
(``market_id``, ``predicted_at``) pair into this buffer.  The
``calibrators._fetch_training_data`` helper UNIONs these rows on
top of the live ``trades`` x ``trade_postmortems`` join and the
``v1_archive.trade_analyses`` archive so the per-league nightly
refit learns from the *unbiased universe* of Kalshi markets, not
just the sub-population our edge gates surfaced for trading.

Schema notes:

  * ``shadow_prediction_id`` references the source row in
    ``shadow_predictions`` so an operator can audit the chain end
    to end.  We keep the FK loose (no ``REFERENCES``) so a stray
    delete on the source table can't take this buffer down.
  * ``processed`` mirrors the ``online_learning_buffer`` schema so
    a future shadow-aware retrain consumer can drain the buffer in
    the same shape.  The current calibrator UNION reads regardless
    of ``processed`` (it's a 90-day windowed scan, not a drain).
  * ``source`` defaults to ``'shadow'`` and is threaded through to
    ``calibration_snapshots.config`` so the operator can audit
    which fraction of a refit's training came from shadow vs live.

Per the standing convention, the up uses ``CREATE TABLE IF NOT
EXISTS`` so re-runs are no-ops; the down drops the table.

Revision ID: 0043_shadow_learning_buffer
Revises: 0042_shadow_predictions
"""
from __future__ import annotations

from alembic import op


revision = "0043_shadow_learning_buffer"
down_revision = "0042_shadow_predictions"
branch_labels = None
depends_on = None


_SQL_UP = """
CREATE TABLE IF NOT EXISTS shadow_learning_buffer (
    id                    bigserial   PRIMARY KEY,
    shadow_prediction_id  bigint,
    market_id             text,
    league                text,
    vertical              text,
    predicted_prob        double precision,
    uncalibrated_prob     double precision,
    actual_outcome        integer,
    source                text        NOT NULL DEFAULT 'shadow',
    has_full_features     boolean     NOT NULL DEFAULT false,
    processed             boolean     NOT NULL DEFAULT false,
    created_at            timestamptz NOT NULL DEFAULT now(),
    settled_at            timestamptz
);

-- Idempotency: the settler skips rows whose source shadow prediction
-- already produced a buffer entry.  Unique here keeps the safety
-- net even if the settler races.
CREATE UNIQUE INDEX IF NOT EXISTS shadow_learning_buffer_pred_uidx
    ON shadow_learning_buffer (shadow_prediction_id)
    WHERE shadow_prediction_id IS NOT NULL;

-- Drift / retrain scans ("how many settled shadow rows in the last
-- 24h for this vertical?").
CREATE INDEX IF NOT EXISTS shadow_learning_buffer_vertical_settled_idx
    ON shadow_learning_buffer (vertical, settled_at)
    WHERE settled_at IS NOT NULL;

-- Per-league calibrator UNION scan ("settled shadow rows in the
-- 90-day refit window for this league").
CREATE INDEX IF NOT EXISTS shadow_learning_buffer_league_settled_idx
    ON shadow_learning_buffer (league, settled_at)
    WHERE settled_at IS NOT NULL;
"""


_SQL_DOWN = """
DROP INDEX IF EXISTS shadow_learning_buffer_league_settled_idx;
DROP INDEX IF EXISTS shadow_learning_buffer_vertical_settled_idx;
DROP INDEX IF EXISTS shadow_learning_buffer_pred_uidx;
DROP TABLE IF EXISTS shadow_learning_buffer;
"""


def upgrade() -> None:
    op.execute(_SQL_UP)


def downgrade() -> None:
    op.execute(_SQL_DOWN)
