"""One-shot backfill that re-derives ``uw_signals.ticker`` (task #425).

The new per-ticker UW mining (task #189) groups settled trades by the
underlying symbol it parses out of ``trades.market_id`` and joins
against ``upper(uw_signals.ticker) = $4``.  Historical UW rows that
landed with a NULL ticker (the column is nullable, and several UW
endpoints don't surface a top-level ``ticker`` field on the dark-pool
recent feed for example) — or with a non-canonical one (lowercase, or
with leading/trailing whitespace) — silently drop out of that join,
hurting the IC / Sharpe estimates on the very verticals (crypto /
equities / macro) where per-ticker matters most.

This module exposes:

  * :func:`backfill_uw_signals_ticker` — idempotent SQL pass that
    1. fills NULL tickers from the stored ``payload`` JSON using the
       same ``ticker`` / ``symbol`` / ``underlying`` /
       ``underlying_symbol`` precedence as
       :func:`app.feeds.unusual_whales._row_ticker`, and
    2. uppercases / trims existing ticker values so the
       ``upper(ticker) = $4`` join hits.
  * :func:`derive_payload_ticker` — pure Python mirror of the SQL
    derivation, used by the unit tests so the precedence stays in
    lockstep with the live ingest path.

Both passes are wrapped in ``WITH updated AS ( ... RETURNING 1)
SELECT count(*) FROM updated`` so we can return the per-pass row
counts without depending on parsing asyncpg's ``UPDATE n`` status
string.  Re-running is a no-op once every row already has a canonical
ticker (the ``WHERE`` clauses filter out rows that wouldn't change).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .. import db


log = logging.getLogger("axiom.feeds.uw.ticker_backfill")


# Precedence mirrors :func:`app.feeds.unusual_whales._row_ticker`.
# Kept here as a constant so the SQL and the Python helper can share it.
_PAYLOAD_TICKER_KEYS: tuple[str, ...] = (
    "ticker",
    "symbol",
    "underlying",
    "underlying_symbol",
)


def derive_payload_ticker(payload: dict[str, Any] | None) -> str | None:
    """Return the canonical ticker for a UW payload, or ``None``.

    Mirrors ``_row_ticker`` in :mod:`app.feeds.unusual_whales` so the
    backfill picks the same value the live ingest path would have
    written, but additionally ``strip()``s whitespace (the ingest path
    relied on the upstream not padding values; the backfill has to
    handle whatever historical rows actually contain).
    """
    if not isinstance(payload, dict):
        return None
    for key in _PAYLOAD_TICKER_KEYS:
        v = payload.get(key)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s.upper()
    return None


@dataclass(frozen=True)
class BackfillResult:
    """Counts returned by :func:`backfill_uw_signals_ticker`."""

    filled_from_payload: int
    normalised_existing: int

    def as_dict(self) -> dict[str, int]:
        return {
            "filled_from_payload": self.filled_from_payload,
            "normalised_existing": self.normalised_existing,
        }


# SQL note: ``payload->>'key'`` returns NULL for missing keys *and* for
# JSON ``null`` literals, and the empty-string check below filters out
# rows where every candidate is just whitespace.  ``btrim`` + ``upper``
# normalise to the same form the join expects.
_FILL_FROM_PAYLOAD_SQL = """
WITH updated AS (
    UPDATE uw_signals
       SET ticker = upper(btrim(
               coalesce(
                   nullif(btrim(payload->>'ticker'), ''),
                   nullif(btrim(payload->>'symbol'), ''),
                   nullif(btrim(payload->>'underlying'), ''),
                   nullif(btrim(payload->>'underlying_symbol'), '')
               )
           ))
     WHERE ticker IS NULL
       AND coalesce(
               nullif(btrim(payload->>'ticker'), ''),
               nullif(btrim(payload->>'symbol'), ''),
               nullif(btrim(payload->>'underlying'),  ''),
               nullif(btrim(payload->>'underlying_symbol'), '')
           ) IS NOT NULL
    RETURNING 1
)
SELECT count(*)::bigint FROM updated
"""


# Idempotent: rows whose ticker is already in canonical form are
# excluded by the ``ticker <> upper(btrim(ticker))`` predicate, so a
# second invocation is a no-op.
_NORMALISE_EXISTING_SQL = """
WITH updated AS (
    UPDATE uw_signals
       SET ticker = upper(btrim(ticker))
     WHERE ticker IS NOT NULL
       AND ticker <> upper(btrim(ticker))
       AND btrim(ticker) <> ''
    RETURNING 1
)
SELECT count(*)::bigint FROM updated
"""


async def backfill_uw_signals_ticker() -> BackfillResult:
    """Run both backfill passes and return per-pass row counts.

    The two passes commit in a single transaction so an admin
    triggering this concurrently with feed pollers sees a consistent
    snapshot (the ingest path's own writes go through their own short
    transactions, so the lock window here is tiny — both updates only
    touch rows that aren't yet canonical).
    """
    async with db.acquire() as conn, conn.transaction():
        filled = await conn.fetchval(_FILL_FROM_PAYLOAD_SQL)
        normalised = await conn.fetchval(_NORMALISE_EXISTING_SQL)
    result = BackfillResult(
        filled_from_payload=int(filled or 0),
        normalised_existing=int(normalised or 0),
    )
    log.info(
        "uw_signals ticker backfill: filled_from_payload=%d normalised_existing=%d",
        result.filled_from_payload, result.normalised_existing,
    )
    return result
