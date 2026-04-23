"""DB read/write for ``autoresearch_features``.

All callers funnel through here so the ``status`` invariant
(``shadow`` → ``production`` / ``rejected``) and the audit-log entry
stay in one place.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .. import db


log = logging.getLogger("axiom.autoresearch.store")


VALID_STATUSES = ("shadow", "production", "rejected")


def feature_name(
    vertical: str,
    signal_type: str,
    aggregator: str,
    ticker: str | None = None,
) -> str:
    """Stable identifier for a discovered candidate.

    Format: ``uw__{signal_type}__{aggregator}__{vertical}`` for global
    (vertical-only) candidates, or
    ``uw__{signal_type}__{aggregator}__{vertical}__{ticker}`` for the
    per-(vertical, ticker) candidates emitted by ticker-linked verticals
    (crypto, equities, macro etc — see
    :data:`app.autoresearch.mining.TICKER_LINKED_VERTICALS`).

    Kept underscored / lowercase so it can also serve as a column name
    in downstream training tables without quoting.
    """
    parts = [str(p).strip().lower() for p in (signal_type, aggregator, vertical)]
    ticker_norm = (ticker or "").strip().lower()
    if ticker_norm:
        parts.append(ticker_norm)
    return "uw__" + "__".join(parts)


async def upsert_metrics(
    *,
    name: str,
    vertical: str,
    signal_type: str,
    aggregator: str,
    ic: float,
    sharpe: float,
    monotonicity: float,
    n_samples: int,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Insert or refresh a candidate row with the latest mining metrics.

    Existing ``status`` is preserved so a previously-promoted (or
    rejected) feature does not get demoted by the next nightly run.
    """
    payload = json.dumps(metadata or {}, default=str)
    try:
        async with db.acquire() as c:
            row = await c.fetchrow(
                """
                INSERT INTO autoresearch_features
                  (name, vertical, signal_type, aggregator,
                   ic, sharpe, monotonicity, n_samples,
                   last_evaluated_at, metadata)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now(), $9::jsonb)
                ON CONFLICT (name) DO UPDATE SET
                  vertical          = EXCLUDED.vertical,
                  signal_type       = EXCLUDED.signal_type,
                  aggregator        = EXCLUDED.aggregator,
                  ic                = EXCLUDED.ic,
                  sharpe            = EXCLUDED.sharpe,
                  monotonicity      = EXCLUDED.monotonicity,
                  n_samples         = EXCLUDED.n_samples,
                  last_evaluated_at = now(),
                  metadata          = EXCLUDED.metadata
                RETURNING *
                """,
                name, vertical, signal_type, aggregator,
                ic, sharpe, monotonicity, n_samples, payload,
            )
            return dict(row) if row else None
    except Exception as e:
        log.warning("autoresearch upsert failed for %s: %s", name, e)
        return None


async def list_candidates(
    *, status: str | None = None, vertical: str | None = None
) -> list[dict[str, Any]]:
    where: list[str] = []
    args: list[Any] = []
    if status:
        args.append(status)
        where.append(f"status = ${len(args)}")
    if vertical:
        args.append(vertical)
        where.append(f"vertical = ${len(args)}")
    sql = """
        SELECT
          id, name, vertical, signal_type, aggregator, status,
          ic, sharpe, monotonicity, n_samples,
          to_char(last_evaluated_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS last_evaluated_at,
          to_char(promoted_at,       'YYYY-MM-DD"T"HH24:MI:SSZ') AS promoted_at,
          promoted_by,
          to_char(rejected_at,       'YYYY-MM-DD"T"HH24:MI:SSZ') AS rejected_at,
          rejected_by,
          metadata,
          to_char(created_at,        'YYYY-MM-DD"T"HH24:MI:SSZ') AS created_at
        FROM autoresearch_features
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    # Highest |IC| first so the most promising candidates float to the top.
    sql += " ORDER BY abs(coalesce(ic, 0)) DESC NULLS LAST, name"
    try:
        async with db.acquire() as c:
            rows = await c.fetch(sql, *args)
            return [_serialise(r) for r in rows]
    except Exception as e:
        log.warning("autoresearch list failed: %s", e)
        return []


async def get_candidate(name: str) -> dict[str, Any] | None:
    try:
        async with db.acquire() as c:
            row = await c.fetchrow(
                """
                SELECT
                  id, name, vertical, signal_type, aggregator, status,
                  ic, sharpe, monotonicity, n_samples,
                  to_char(last_evaluated_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS last_evaluated_at,
                  to_char(promoted_at,       'YYYY-MM-DD"T"HH24:MI:SSZ') AS promoted_at,
                  promoted_by,
                  to_char(rejected_at,       'YYYY-MM-DD"T"HH24:MI:SSZ') AS rejected_at,
                  rejected_by,
                  metadata,
                  to_char(created_at,        'YYYY-MM-DD"T"HH24:MI:SSZ') AS created_at
                FROM autoresearch_features
                WHERE name = $1
                """,
                name,
            )
            return _serialise(dict(row)) if row else None
    except Exception as e:
        log.warning("autoresearch get failed for %s: %s", name, e)
        return None


async def set_status(
    *, name: str, status: str, actor_user_id: str
) -> dict[str, Any] | None:
    """Flip a candidate's status. Caller is responsible for the audit log."""
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status {status!r}; must be one of {VALID_STATUSES}")
    if status == "production":
        sql = """
            UPDATE autoresearch_features
               SET status = 'production',
                   promoted_at = now(),
                   promoted_by = $2,
                   rejected_at = NULL,
                   rejected_by = NULL
             WHERE name = $1
             RETURNING *
        """
    elif status == "rejected":
        sql = """
            UPDATE autoresearch_features
               SET status = 'rejected',
                   rejected_at = now(),
                   rejected_by = $2,
                   promoted_at = NULL,
                   promoted_by = NULL
             WHERE name = $1
             RETURNING *
        """
    else:  # shadow — clear any prior decision
        sql = """
            UPDATE autoresearch_features
               SET status = 'shadow',
                   promoted_at = NULL,
                   promoted_by = NULL,
                   rejected_at = NULL,
                   rejected_by = NULL
             WHERE name = $1
             RETURNING *
        """
    try:
        async with db.acquire() as c:
            row = await c.fetchrow(sql, name, actor_user_id)
            return _serialise(dict(row)) if row else None
    except Exception as e:
        log.warning("autoresearch set_status failed for %s: %s", name, e)
        return None


def _serialise(row: dict[str, Any]) -> dict[str, Any]:
    """Normalise asyncpg row → JSON-friendly dict (jsonb columns come back
    as already-decoded ``dict``s; ``Decimal`` floats need a cast)."""
    out = dict(row)
    for k in ("ic", "sharpe", "monotonicity"):
        v = out.get(k)
        if v is not None:
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                out[k] = None
    md = out.get("metadata")
    if isinstance(md, str):
        try:
            out["metadata"] = json.loads(md)
        except Exception:
            out["metadata"] = {}
    elif md is None:
        out["metadata"] = {}
    return out
