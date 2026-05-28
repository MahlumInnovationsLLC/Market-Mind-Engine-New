"""Cache LLM-resolved Kalshi tickers per (vertical, matchup, selection, pick_type).

When the deterministic token-score resolver fails to clear ``_MIN_SCORE``
the executor falls back to an LLM ranker. Calls are slow (~1-2s each)
and cost money, so we memoize by a stable signal key. A row keyed
``"none"`` means the LLM explicitly declined to pick — we still skip
the API on the next poll so a chronically un-resolvable signal doesn't
keep paying tokens.

Revision ID: 0060_edge_terminal_llm_resolver_cache
Revises: 0059_soccer_baseline_lambdas
"""
from __future__ import annotations

from alembic import op


revision = "0060_edge_terminal_llm_resolver_cache"
down_revision = "0059_soccer_baseline_lambdas"
branch_labels = None
depends_on = None


_SQL_UP = """
CREATE TABLE IF NOT EXISTS edge_terminal_llm_resolver_cache (
    signal_key  text PRIMARY KEY,
    ticker      text,
    model       text,
    score       integer,
    reason      text,
    resolved_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS edge_terminal_llm_resolver_cache_resolved_at_idx
    ON edge_terminal_llm_resolver_cache (resolved_at DESC);
"""


_SQL_DOWN = """
DROP TABLE IF EXISTS edge_terminal_llm_resolver_cache;
"""


def upgrade() -> None:
    op.execute(_SQL_UP)


def downgrade() -> None:
    op.execute(_SQL_DOWN)
