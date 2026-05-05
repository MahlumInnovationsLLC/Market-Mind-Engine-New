"""soccer_settlement_review: human-review queue for soccer settlements.

The settlement cycle now consults two independent sources for soccer
matches (Sportradar primary + ESPN fallback). When the two disagree —
or the secondary is unavailable while the primary is also stale — the
row is enqueued here instead of being auto-settled.  An operator
resolves it from the dashboard with one of:

  * ``sportradar``       — settle on the primary's verdict
  * ``fallback``         — settle on the fallback's verdict
  * ``manual_home``      — operator override
  * ``manual_draw``      — operator override
  * ``manual_away``      — operator override

Each resolution writes an `admin_audit_log` row and routes back through
``settle_market`` so the existing per-leg derivation, persona Brier
update, and online-learning buffer all fire normally.

Columns:
  * ``primary_payload`` / ``fallback_payload`` capture each source's
    full match summary (`{"result", "home_team", "away_team",
    "home_score", "away_score", "status"}`) so the operator UI can show
    the disagreement without re-querying upstream.
  * ``disagreement_kind`` ('disagree' | 'primary_stale_secondary_down')
    drives which "what red looks like" runbook entry the operator
    should consult.
  * ``resolved_result`` is the canonical 3-way outcome the resolution
    settled on — denormalised so the dashboard tile and audit reports
    don't have to re-derive it from `resolution` + payloads.
  * Partial unique index on ``market_id WHERE resolved_at IS NULL``
    prevents the settlement loop from inserting two open rows for the
    same market in back-to-back cycles.

Revision ID: 0047_soccer_settlement_review
Revises: 0046_v1_backtest_gate_runs
"""
from __future__ import annotations

from alembic import op


revision = "0047_soccer_settlement_review"
down_revision = "0046_v1_backtest_gate_runs"
branch_labels = None
depends_on = None


_SQL_UP = """
CREATE TABLE IF NOT EXISTS soccer_settlement_review (
    id                  bigserial PRIMARY KEY,
    market_id           text        NOT NULL,
    game_id             text,
    league              text,
    primary_source      text        NOT NULL DEFAULT 'sportradar',
    primary_payload     jsonb,
    fallback_source     text        NOT NULL DEFAULT 'espn',
    fallback_payload    jsonb,
    disagreement_kind   text        NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    resolved_at         timestamptz,
    resolved_by         text,
    resolved_by_email   text,
    resolution          text,
    resolved_result     text,
    notes               text,
    -- Stamped only after the downstream ``settle_market`` call returns
    -- successfully.  When ``resolved_at`` is set but this column is
    -- NULL the resolve attempt completed audit + flipping the row but
    -- crashed before settling — a retry endpoint may safely re-run.
    settle_completed_at timestamptz
);

CREATE INDEX IF NOT EXISTS soccer_settlement_review_unresolved_idx
    ON soccer_settlement_review (created_at DESC)
    WHERE resolved_at IS NULL;

CREATE INDEX IF NOT EXISTS soccer_settlement_review_market_idx
    ON soccer_settlement_review (market_id);

CREATE UNIQUE INDEX IF NOT EXISTS soccer_settlement_review_open_market_uniq
    ON soccer_settlement_review (market_id)
    WHERE resolved_at IS NULL;
"""

_SQL_DOWN = """
DROP INDEX IF EXISTS soccer_settlement_review_open_market_uniq;
DROP INDEX IF EXISTS soccer_settlement_review_market_idx;
DROP INDEX IF EXISTS soccer_settlement_review_unresolved_idx;
DROP TABLE IF EXISTS soccer_settlement_review;
"""


def upgrade() -> None:
    op.execute(_SQL_UP)


def downgrade() -> None:
    op.execute(_SQL_DOWN)
