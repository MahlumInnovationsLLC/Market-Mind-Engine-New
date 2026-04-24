"""Storage for per-market KXRAIN ensemble snapshots so the nightly refit
of the EMOS coefficients (task #280) has training data to fit on.

The shipped EMOS coefficients in ``app.weather.precip_emos`` are
hand-picked defaults sanity-checked against a synthetic
Gamma(1.2, 0.4) holdout. Once enough KXRAIN markets have settled we
want to maximize the binary log-likelihood of the gamma / logistic
EMOS link against real settled outcomes — but we only carry the
ensemble mean / variance through the cycle in-memory today. This
migration adds the lightweight observation table the predictor
upserts into per call and the settlement pipeline stamps with the
realized YES/NO outcome.

Schema:
  * ``market_id`` is the natural key — one observation per Kalshi
    KXRAIN market — so prediction-time upserts are idempotent across
    re-evaluations of the same candidate within a cycle window.
  * ``ensemble_mean`` / ``ensemble_var`` are the EMOS inputs the
    refit job consumes.
  * ``threshold_in`` + ``direction`` carry the contract spec the
    fitter needs to map ``actual_outcome_yes`` back onto the
    canonical "P(precip > K)" prediction the gamma SF returns.
  * ``settled_at`` + ``actual_outcome_yes`` are NULL until
    ``settle_market`` resolves the Kalshi market.

Per task spec the table has no destructive down-migration (the only
data here is observability for the refit loop).

Revision ID: 0017_precip_emos_observations
Revises: 0016_predictions_kalshi_candidates
"""
from __future__ import annotations

from alembic import op


revision = "0017_precip_emos_observations"
down_revision = "0016_predictions_kalshi_candidates"
branch_labels = None
depends_on = None


_SQL_UP = """
CREATE TABLE IF NOT EXISTS precip_emos_observations (
    id                  bigserial   PRIMARY KEY,
    market_id           text        NOT NULL UNIQUE,
    city                text,
    threshold_in        double precision NOT NULL,
    direction           text        NOT NULL CHECK (direction IN ('above','below')),
    ensemble_mean       double precision NOT NULL,
    ensemble_var        double precision NOT NULL,
    ensemble_n          integer,
    predicted_p_above   double precision,
    settled_at          timestamptz,
    actual_outcome_yes  smallint,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS precip_emos_observations_settled_at_idx
    ON precip_emos_observations (settled_at)
    WHERE settled_at IS NOT NULL;
"""


def upgrade() -> None:
    op.execute(_SQL_UP)


def downgrade() -> None:
    # No-op: this table is observability for the EMOS refit loop and
    # carries no downstream foreign-key obligations.
    pass
