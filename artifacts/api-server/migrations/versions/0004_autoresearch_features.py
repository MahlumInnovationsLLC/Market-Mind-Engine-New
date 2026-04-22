"""AutoResearch — discovered candidate features.

Per task #101. AutoResearch nightly mines Unusual Whales signals against
settled trade outcomes per vertical and discovers candidate features
(``signal_type`` × ``aggregator`` × ``vertical``) that show predictive
power. Discoveries land here behind a ``shadow`` status so the predictor
ignores them until an operator explicitly promotes one to production
from the admin console.

Columns:

* ``name`` — stable identifier the registry / admin UI key on.
* ``status`` — ``shadow`` (default), ``production``, or ``rejected``.
* ``ic`` / ``sharpe`` / ``monotonicity`` — last-mining-run metrics. IC is
  Pearson correlation of the candidate value with ``actual_outcome -
  model_prob`` (the residual the meta-model leaves on the table); Sharpe
  is the mean of ``value * residual`` divided by its std (per-trade
  signal-to-noise); monotonicity is the Spearman correlation between
  the value-quantile bin index and the bin-mean residual (≈1.0 means
  the bigger the signal, the bigger the residual it explains).
* ``promoted_at`` / ``promoted_by`` — set when an admin flips status to
  ``production``. ``rejected_at`` / ``rejected_by`` mirror the deny path.

Idempotent ``IF NOT EXISTS``; safe to re-run.

Revision ID: 0004_autoresearch_features
Revises: 0003_uw_and_public_betting
"""
from __future__ import annotations

from alembic import op


revision = "0004_autoresearch_features"
down_revision = "0003_uw_and_public_betting"
branch_labels = None
depends_on = None


_SQL = """
CREATE TABLE IF NOT EXISTS autoresearch_features (
    id              bigserial PRIMARY KEY,
    name            text        NOT NULL UNIQUE,
    vertical        text        NOT NULL,
    signal_type     text        NOT NULL,
    aggregator      text        NOT NULL,
    status          text        NOT NULL DEFAULT 'shadow',
    ic              double precision,
    sharpe          double precision,
    monotonicity    double precision,
    n_samples       integer,
    last_evaluated_at timestamptz,
    promoted_at     timestamptz,
    promoted_by     text,
    rejected_at     timestamptz,
    rejected_by     text,
    metadata        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS autoresearch_features_status_idx
    ON autoresearch_features (status);
CREATE INDEX IF NOT EXISTS autoresearch_features_vertical_idx
    ON autoresearch_features (vertical, status);
"""


def upgrade() -> None:
    op.execute(_SQL)


def downgrade() -> None:
    raise NotImplementedError("destructive down-migration not provided")
