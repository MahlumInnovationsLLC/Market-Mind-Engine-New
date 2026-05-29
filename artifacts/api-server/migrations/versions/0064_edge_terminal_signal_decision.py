"""Persist Edge Terminal deciding inputs + badge flag (task #951).

The executor derives several decision inputs at fire time — the model
probability it actually sized against (which may be inferred from
``edge_pct`` / ``ev_pct`` when the scraper omitted it), the signed edge,
and which source produced the probability — but until now none of those
were persisted, so the operator queue could not explain *why* a signal
was placed, stood down, or sized the way it was.  This migration lands
the columns the executor writes via ``_persist_decision``:

  * ``signal_badged``      — the "must be signal-badged" gate flag.  The
    scraper only emits signal-tagged tiles, so existing + new rows
    default to ``true``; the column makes the condition auditable and
    lets a future non-badged source be stood down explicitly.
  * ``confidence_source``  — ``scraped`` / ``edge_inferred`` /
    ``ev_inferred`` / ``none``: where the deciding probability came from.
  * ``decided_model_prob`` — the probability used at decision time.
  * ``decided_edge``       — the signed edge from the chosen side.

All new columns are nullable (except ``signal_badged`` which has a safe
default) so existing rows survive untouched.

Revision ID: 0064_edge_terminal_signal_decision
Revises: 0063_edge_terminal_poller_category
"""
from __future__ import annotations

from alembic import op


revision = "0064_edge_terminal_signal_decision"
down_revision = "0063_edge_terminal_poller_category"
branch_labels = None
depends_on = None


_SQL_UP = """
ALTER TABLE edge_terminal_signals
    ADD COLUMN IF NOT EXISTS signal_badged      boolean NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS confidence_source  text,
    ADD COLUMN IF NOT EXISTS decided_model_prob double precision,
    ADD COLUMN IF NOT EXISTS decided_edge       double precision;
"""


def upgrade() -> None:
    op.execute(_SQL_UP)


def downgrade() -> None:
    # Additive, nullable/defaulted columns — leave intact on rollback.
    pass
