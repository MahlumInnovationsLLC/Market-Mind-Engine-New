"""calibration_snapshots: champion-challenger A/B columns.

Adds three columns to ``calibration_snapshots`` so the per-league
refit pipeline (``app/calibrators.py``) can run a champion-challenger
A/B framework on top of the existing snapshot history:

  * ``role`` — text, NOT NULL, DEFAULT ``'champion'``.  Existing rows
    are backfilled to ``champion`` by the column DEFAULT.  Subsequent
    refits write a fresh ``challenger`` row alongside a re-evaluated
    ``champion`` row so the auto-promotion job can compare them on
    out-of-sample Brier.  When a challenger is promoted, the prior
    champion row is rewritten to ``retired_champion`` in the same
    transaction so DISTINCT ON (sub_category, role) loads the right
    artifact at next start-up.
  * ``brier_in_sample`` — float8, nullable.  In-sample Brier on the
    leading 90% of the windowed training set (sanity-check that the
    fit converged; also used by the dashboard tile).
  * ``brier_out_of_sample`` — float8, nullable.  Held-out Brier on
    the trailing 10% temporal slice.  This is the metric the
    promotion logic uses — in-sample Brier is too easy to game with
    a non-parametric isotonic fit.

Per the standing convention, the ``up`` is wrapped in
``ADD COLUMN IF NOT EXISTS`` so re-runs are no-ops; the ``down``
drops only the columns this revision introduced.

Revision ID: 0044_calibration_snapshots_role_ab
Revises: 0043_shadow_learning_buffer
"""
from __future__ import annotations

from alembic import op


revision = "0044_calibration_snapshots_role_ab"
down_revision = "0043_shadow_learning_buffer"
branch_labels = None
depends_on = None


_SQL_UP = """
ALTER TABLE calibration_snapshots
    ADD COLUMN IF NOT EXISTS role text NOT NULL DEFAULT 'champion',
    ADD COLUMN IF NOT EXISTS brier_in_sample double precision,
    ADD COLUMN IF NOT EXISTS brier_out_of_sample double precision;

CREATE INDEX IF NOT EXISTS calibration_snapshots_subcat_role_created_idx
    ON calibration_snapshots (sub_category, role, created_at DESC);
"""

_SQL_DOWN = """
DROP INDEX IF EXISTS calibration_snapshots_subcat_role_created_idx;

ALTER TABLE calibration_snapshots
    DROP COLUMN IF EXISTS brier_out_of_sample,
    DROP COLUMN IF EXISTS brier_in_sample,
    DROP COLUMN IF EXISTS role;
"""


def upgrade() -> None:
    op.execute(_SQL_UP)


def downgrade() -> None:
    op.execute(_SQL_DOWN)
