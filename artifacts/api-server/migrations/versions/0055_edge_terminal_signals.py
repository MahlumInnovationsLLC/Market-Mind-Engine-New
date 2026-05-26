"""Edge Terminal scraper-input mode (task #844).

Adds the write-side table the Edge Terminal poller deduplicates into,
plus seeds the operator-visible default for the new ``app.input_mode``
system setting.

  * One row per (vertical, matchup, selection, UTC-date) — a UNIQUE
    constraint guarantees the poller cannot fire the same pick twice
    even if the scrape returns it on every 60-second poll.
  * ``status`` is the executor's state machine: ``pending`` rows are
    drained on each cycle, flipped to ``placed`` after a successful
    ``paper.place_order`` call (whose returned trades_id is stamped
    on the row), ``failed`` when the market resolver or executor
    rejects, or ``skipped`` when a pick is intentionally ignored
    (e.g. no matching Kalshi market).
  * ``raw`` preserves the scraper's full row so we can debug or
    re-resolve a pick offline.

Revision ID: 0055_edge_terminal_signals
Revises: 0054_prediction_snoozes
"""
from __future__ import annotations

from alembic import op


revision = "0055_edge_terminal_signals"
down_revision = "0054_prediction_snoozes"
branch_labels = None
depends_on = None


_SQL_UP = """
CREATE TABLE IF NOT EXISTS edge_terminal_signals (
    id              bigserial    PRIMARY KEY,
    seen_at         timestamptz  NOT NULL DEFAULT now(),
    vertical        text         NOT NULL,
    matchup         text         NOT NULL,
    selection       text         NOT NULL,
    signal_date     date         NOT NULL,
    edge_pct        double precision,
    model_prob      double precision,
    market_price    double precision,
    kalshi_ticker   text,
    market_title    text,
    raw             jsonb        NOT NULL DEFAULT '{}'::jsonb,
    status          text         NOT NULL DEFAULT 'pending',
    status_reason   text,
    trades_id       bigint,
    placed_at       timestamptz,
    updated_at      timestamptz  NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS edge_terminal_signals_dedupe_idx
    ON edge_terminal_signals (vertical, matchup, selection, signal_date);

CREATE INDEX IF NOT EXISTS edge_terminal_signals_status_idx
    ON edge_terminal_signals (status, seen_at);

-- Default the app to edge_terminal input mode on a fresh install; an
-- existing operator-flipped row wins via ON CONFLICT.  The dashboard
-- read-side, the orchestrator scheduler, and the boot lifespan all
-- read this row.
-- ``system_settings.value`` is declared ``text`` (see migration 0018), so
-- seed a plain string literal -- an earlier ``to_jsonb('edge_terminal')``
-- wrapper wrote the *jsonb-encoded* value ``"edge_terminal"`` (quoted)
-- into the text column, which then mis-compared against the bare string
-- ``edge_terminal`` every reader expects.
INSERT INTO system_settings (key, value, updated_at)
VALUES ('app.input_mode', 'edge_terminal', now())
ON CONFLICT (key) DO NOTHING;

CREATE TABLE IF NOT EXISTS edge_terminal_poller_status (
    id              integer      PRIMARY KEY DEFAULT 1,
    last_poll_at    timestamptz,
    last_ok_at      timestamptz,
    last_error      text,
    last_signal_count integer    NOT NULL DEFAULT 0,
    updated_at      timestamptz  NOT NULL DEFAULT now(),
    CHECK (id = 1)
);

INSERT INTO edge_terminal_poller_status (id) VALUES (1)
ON CONFLICT (id) DO NOTHING;
"""


def upgrade() -> None:
    op.execute(_SQL_UP)


def downgrade() -> None:
    # Schema-only artefacts — leave history intact on rollback.
    pass
