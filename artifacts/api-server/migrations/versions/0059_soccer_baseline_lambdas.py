"""Per-league fitted soccer baseline scoring intensities (task #903).

Companion table to the runtime-settings-backed Dixon-Coles rho overrides
shipped by task #900.  The weekly fitter in
:mod:`app.learning.soccer_lambda_fitter` pulls terminal scorelines from
``sportradar_live_state`` and writes per-league
``(lambda_home, lambda_away)`` MLEs here.  The live predictor in
:mod:`app.learning.soccer_live` reads the table on startup (and after
each successful refit) into an in-memory override map; the hand-set
``_BASELINE_LAMBDAS_BY_LEAGUE`` table remains the fallback whenever the
override row is missing or the fit was skipped for insufficient samples.

Schema notes
------------

  * ``league`` is the canonical soccer league label (EPL, LALIGA, etc.)
    and is the natural primary key — at most one fitted row per league.
  * ``lambda_home`` / ``lambda_away`` are the fitted goals-per-match
    intensities (Poisson MLE on the home / away marginals).
  * ``matches`` records the sample size that backed the fit so the
    operator console can surface "fit was on N matches" alongside the
    coefficient.
  * ``log_likelihood`` is the independent-Poisson sum at the MLE; nullable
    because some fits (e.g. exhaustive smoke tests) may skip it.
  * ``fitted_at`` records when this row was written; the predictor uses
    it for the "stale fit" badge on the operator panel.

Per the standing convention, ``CREATE TABLE IF NOT EXISTS`` makes the
upgrade idempotent and the downgrade drops the table.
"""
from __future__ import annotations

from alembic import op


revision = "0059_soccer_baseline_lambdas"
down_revision = "0058_predictions_updated_at_heal"
branch_labels = None
depends_on = None


_SQL_UP = """
CREATE TABLE IF NOT EXISTS soccer_baseline_lambdas (
    league          text        PRIMARY KEY,
    lambda_home     double precision NOT NULL,
    lambda_away     double precision NOT NULL,
    matches         integer     NOT NULL,
    log_likelihood  double precision,
    fitted_at       timestamptz NOT NULL DEFAULT now()
);
"""


_SQL_DOWN = """
DROP TABLE IF EXISTS soccer_baseline_lambdas;
"""


def upgrade() -> None:
    op.execute(_SQL_UP)


def downgrade() -> None:
    op.execute(_SQL_DOWN)
