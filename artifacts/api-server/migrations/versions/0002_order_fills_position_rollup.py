"""Order-fill idempotency table + position rollup columns.

Adds:
  * `order_fills` — append-only, idempotent on (order_id, fill_id). Each
    Kalshi WS partial-fill event lands here exactly once even if the
    socket re-delivers it on reconnect.
  * `positions.order_id`, `positions.filled_count`, `positions.over_filled`
    — let `handle_ws_fill` upsert a SINGLE position row per order_id with
    a weighted-average fill price. The PnL pipeline already reads from
    `positions`, so once partials roll up here they no longer need to be
    summed from raw `order_lifecycle` rows.

Per task #103. The `positions` table is owned by the upstream production
schema; only modify it if it already exists so a fresh DB without the
trade pipeline schema doesn't fail the migration.

Revision ID: 0002_order_fills_position_rollup
Revises: 0001_baseline
"""
from __future__ import annotations

from alembic import op


revision = "0002_order_fills_position_rollup"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


_SQL = """
CREATE TABLE IF NOT EXISTS order_fills (
    order_id     text        NOT NULL,
    fill_id      text        NOT NULL,
    count        integer     NOT NULL,
    price_cents  integer     NOT NULL,
    received_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (order_id, fill_id)
);

CREATE INDEX IF NOT EXISTS order_fills_order_id_idx
    ON order_fills (order_id);

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'positions'
    ) THEN
        ALTER TABLE positions
            ADD COLUMN IF NOT EXISTS order_id     text,
            ADD COLUMN IF NOT EXISTS filled_count integer,
            ADD COLUMN IF NOT EXISTS over_filled  boolean NOT NULL DEFAULT false;
        -- Full unique index (NOT a partial index) so it is a valid
        -- conflict-target for the `ON CONFLICT (order_id)` upsert in
        -- app.kalshi.lifecycle._upsert_position_rollup. Postgres allows
        -- multiple NULLs in a unique index by default, so existing
        -- upstream-managed rows without order_id are unaffected.
        CREATE UNIQUE INDEX IF NOT EXISTS positions_order_id_uidx
            ON positions (order_id);
    END IF;
END$$;
"""


def upgrade() -> None:
    op.execute(_SQL)


def downgrade() -> None:
    raise NotImplementedError("destructive down-migration not provided")
