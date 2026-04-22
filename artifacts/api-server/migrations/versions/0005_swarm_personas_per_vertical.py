"""Per-vertical scoping for swarm_analyst_metrics + persona toggle table.

Per task #194. The 6-persona swarm grows to 9 per-vertical profiles
(sports / weather / crypto / politics / commodities / tech-ai /
geopolitics / elections / entertainment / events-econ).  Bayesian
weights must isolate by vertical so a persona's Brier in one domain
doesn't leak into another.

Adds:

* ``swarm_analyst_metrics.vertical text NOT NULL DEFAULT 'sports'`` —
  every existing row is backfilled to ``sports`` (the only profile in
  production prior to #194).
* Composite UNIQUE INDEX on
  ``(analyst_id, vertical, league, market_type, COALESCE(home_away,'all'),
    COALESCE(spread_bucket,'all'), COALESCE(injury_context,'all'))``
  so per-vertical rows can coexist for the same (analyst, league,
  market_type) tuple. Created with IF NOT EXISTS so the migration is
  idempotent against a partially-applied environment.
* ``swarm_persona_toggles`` — admin-toggle persistence used by the new
  ``/api/admin/persona-toggles`` endpoint and the "Personas" tab in
  the admin console.

Idempotent throughout — every statement uses ``IF (NOT) EXISTS`` so the
migration is safe to re-run against a database where the upstream
production schema has already added the table by some other path.

Revision ID: 0005_swarm_personas_per_vertical
Revises: 0004_telegram_bindings, 0004_autoresearch_features
"""
from __future__ import annotations

from alembic import op


revision = "0005_swarm_personas_per_vertical"
down_revision = ("0004_telegram_bindings", "0004_autoresearch_features")
branch_labels = None
depends_on = None


_SQL = """
-- Ensure the table exists (production seeded it out-of-band; tests boot
-- against a fresh DB).  The column list mirrors what the live writer in
-- ``app/settlement/pipeline.py`` already INSERTs.
CREATE TABLE IF NOT EXISTS swarm_analyst_metrics (
    id                    bigserial PRIMARY KEY,
    analyst_id            text NOT NULL,
    league                text,
    market_type           text,
    home_away             text,
    spread_bucket         text,
    injury_context        text,
    total_predictions     integer NOT NULL DEFAULT 0,
    correct_predictions   integer NOT NULL DEFAULT 0,
    brier_score           double precision,
    directional_accuracy  double precision,
    avg_confidence        double precision,
    last_updated          timestamptz
);

ALTER TABLE swarm_analyst_metrics
    ADD COLUMN IF NOT EXISTS vertical text NOT NULL DEFAULT 'sports';

UPDATE swarm_analyst_metrics SET vertical = 'sports'
 WHERE vertical IS NULL OR vertical = '';

CREATE UNIQUE INDEX IF NOT EXISTS swarm_analyst_metrics_per_vertical_uidx
    ON swarm_analyst_metrics (
        analyst_id,
        vertical,
        COALESCE(league, ''),
        COALESCE(market_type, ''),
        COALESCE(home_away, 'all'),
        COALESCE(spread_bucket, 'all'),
        COALESCE(injury_context, 'all')
    );

CREATE INDEX IF NOT EXISTS swarm_analyst_metrics_vertical_idx
    ON swarm_analyst_metrics (vertical);

-- Per-vertical persona allow-/deny-list. One row per *disabled*
-- (vertical, persona_id) pair; an absent row means the persona is
-- active inside its profile (the runner falls back to the full profile
-- when every persona has been disabled, see ``swarm.profiles``).
CREATE TABLE IF NOT EXISTS swarm_persona_toggles (
    vertical    text NOT NULL,
    persona     text NOT NULL,
    enabled     boolean NOT NULL DEFAULT true,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text,
    PRIMARY KEY (vertical, persona)
);

CREATE INDEX IF NOT EXISTS swarm_persona_toggles_vertical_idx
    ON swarm_persona_toggles (vertical);
"""


def upgrade() -> None:
    op.execute(_SQL)


def downgrade() -> None:
    raise NotImplementedError("destructive down-migration not provided")
