"""Persist per-(city, metric) trained GRU+EMOS coefficients for the
temperature vertical (task #254).

The temperature predictor used to ship deterministic baseline GRU
weights and read EMOS coefficients (a, b, b_g, c, d, d_g) from
``AXIOM_TEMP_EMOS_*`` environment variables.  That meant a forecast
for NYC and one for MIA reused identical bias / spread parameters,
which is wrong: the residual structure of an ensemble of weather
forecasts is highly city- and metric-dependent (a clear-day desert
city has a tighter HRRR-vs-observed residual than a marine-layer
coastal city).

This migration adds a small table ``weather_temp_trained_models``
keyed on (city_code, metric).  The retrain job
(``app.weather.training``) writes one row per (city, metric) pair
when it has enough joined HRRR / observation / climatology samples
to fit, and the live predictor (``app.weather.predictor``) reads
back the trained ``GRUWeights`` + ``EMOSCoefficients`` at request
time.  Held-out Brier (raw model + per-(city, metric) calibrator)
is logged on the snapshot row so the operator dashboard can show
that the trained model beats the deterministic-baseline floor.

Per repo convention no destructive down-migration is provided.

Revision ID: 0017_weather_temp_trained_models
Revises: 0016_predictions_kalshi_candidates
"""
from __future__ import annotations

from alembic import op


revision = "0017_weather_temp_trained_models"
down_revision = "0016_predictions_kalshi_candidates"
branch_labels = None
depends_on = None


_SQL_UP = """
CREATE TABLE IF NOT EXISTS weather_temp_trained_models (
    city_code        text        NOT NULL,
    metric           text        NOT NULL,
    gru_weights      jsonb       NOT NULL,
    emos_coef        jsonb       NOT NULL,
    fit_metrics      jsonb       NOT NULL DEFAULT '{}'::jsonb,
    n_samples        int         NOT NULL DEFAULT 0,
    n_holdout        int         NOT NULL DEFAULT 0,
    baseline_brier   double precision,
    holdout_brier    double precision,
    fit_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (city_code, metric)
);

CREATE INDEX IF NOT EXISTS weather_temp_trained_models_fit_at_idx
    ON weather_temp_trained_models (fit_at DESC);
"""


def upgrade() -> None:
    op.execute(_SQL_UP)


def downgrade() -> None:
    # No-op: the predictor falls back to env-var coefficients when the
    # table is missing, but a rollback that drops the table mid-flight
    # would force us to refit everything before the next prediction
    # could match the trained accuracy floor.  Keep the data.
    pass
