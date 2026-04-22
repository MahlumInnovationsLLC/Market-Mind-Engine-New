"""Dashboard read-models against the real production schema.

Each individual stat is wrapped in try/except so a single failing query
doesn't take down the whole response.
"""
from __future__ import annotations

import logging
from typing import Any

from . import db
from .kalshi.market_titles import resolve_titles as resolve_market_titles


log = logging.getLogger(__name__)


async def _safe_count(table: str) -> int | None:
    try:
        return int(await db.fetchval(f"SELECT count(*) FROM {table}"))
    except Exception as e:
        log.warning("count(%s) failed: %s", table, e)
        return None


async def _safe_fetchval(query: str) -> Any:
    try:
        return await db.fetchval(query)
    except Exception as e:
        log.warning("fetchval failed: %s :: %s", query, e)
        return None


async def get_overview() -> dict[str, Any]:
    counts = {
        "trading_cycles": await _safe_count("trading_cycles"),
        "trades": await _safe_count("trades"),
        "swarm_debates": await _safe_count("swarm_debates"),
        "ml_training_logs": await _safe_count("ml_training_logs"),
        "predictions": await _safe_count("predictions"),
        "positions": await _safe_count("positions"),
    }
    latest_cycle_at = await _safe_fetchval(
        "SELECT to_char(max(started_at), 'YYYY-MM-DD\"T\"HH24:MI:SSZ') FROM trading_cycles"
    )
    latest_trade_at = await _safe_fetchval(
        "SELECT to_char(max(executed_at), 'YYYY-MM-DD\"T\"HH24:MI:SSZ') FROM trades"
    )
    realized_pnl = await _safe_fetchval("SELECT coalesce(sum(pnl),0)::float FROM trades")
    win_rate = await _safe_fetchval(
        "SELECT (sum((pnl > 0)::int)::float / nullif(count(*) filter (where pnl is not null), 0)) "
        "FROM trades WHERE pnl IS NOT NULL"
    )
    open_positions = await _safe_fetchval(
        "SELECT count(*) FROM positions WHERE coalesce(status,'open') = 'open'"
    )
    return {
        "counts": counts,
        "latest_cycle_at": latest_cycle_at,
        "latest_trade_at": latest_trade_at,
        "realized_pnl": realized_pnl,
        "win_rate": win_rate,
        "open_positions": int(open_positions) if open_positions is not None else None,
    }


async def get_recent_trades(limit: int = 25) -> list[dict[str, Any]]:
    try:
        rows = await db.fetch(
            """
            SELECT
              id,
              trade_id,
              market_id,
              vertical,
              league,
              side,
              action,
              quantity::float        AS quantity,
              price::float           AS price,
              fees::float            AS fees,
              pnl::float             AS pnl,
              trading_mode,
              exchange,
              market_title,
              to_char(executed_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS executed_at
            FROM trades
            ORDER BY executed_at DESC NULLS LAST
            LIMIT $1
            """,
            limit,
        )
        out = [dict(r) for r in rows]
    except Exception as e:
        log.warning("recent_trades failed: %s", e)
        return []

    # Enrich any rows whose market_title hasn't been backfilled yet.
    missing = {
        row["market_id"]
        for row in out
        if row.get("market_id") and not row.get("market_title")
    }
    if missing:
        try:
            titles = await resolve_market_titles(missing)
        except Exception as e:
            log.warning("resolve_market_titles failed: %s", e)
            titles = {}
        for row in out:
            mid = row.get("market_id")
            if mid and not row.get("market_title") and mid in titles:
                row["market_title"] = titles[mid]
    return out


async def get_swarm_personas() -> list[dict[str, Any]]:
    try:
        rows = await db.fetch(
            """
            SELECT
              analyst_id              AS persona,
              league,
              market_type,
              total_predictions       AS n,
              correct_predictions     AS correct,
              brier_score             AS brier,
              calibration_error       AS calibration_error,
              directional_accuracy    AS accuracy,
              avg_confidence,
              to_char(last_updated, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS updated_at
            FROM swarm_analyst_metrics
            ORDER BY total_predictions DESC NULLS LAST
            LIMIT 24
            """
        )
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning("swarm_personas failed: %s", e)
        return []


async def get_recent_cycles(limit: int = 10) -> list[dict[str, Any]]:
    try:
        rows = await db.fetch(
            """
            SELECT
              id,
              vertical,
              picks_evaluated,
              trades_placed,
              errors,
              to_char(started_at,   'YYYY-MM-DD"T"HH24:MI:SSZ') AS started_at,
              to_char(completed_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS completed_at,
              extract(epoch from (completed_at - started_at))::float AS duration_seconds
            FROM trading_cycles
            ORDER BY started_at DESC NULLS LAST
            LIMIT $1
            """,
            limit,
        )
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning("recent_cycles failed: %s", e)
        return []


async def get_public_equity_curve(days: int = 30) -> list[dict[str, Any]]:
    """Daily cumulative realized PnL across all trades, in chronological order.

    Public — used by the marketing landing page.  Bounded so a hostile
    caller can't trigger a multi-year scan; the dashboard never asks for
    more than ~90 days of history.

    Returns a list of `{date, daily_pnl, cumulative_pnl, trades}` rows,
    one per calendar day in the window (chronological).  Days with no
    trades carry the previous `cumulative_pnl` forward with `daily_pnl=0`
    and `trades=0`, so the curve is continuous.  Returns `[]` on any
    failure.
    """
    days = max(1, min(int(days), 365))
    try:
        rows = await db.fetch(
            """
            WITH bounds AS (
              SELECT
                ((now() at time zone 'utc')::date - ($1::int - 1)) AS start_d,
                (now() at time zone 'utc')::date                   AS end_d
            ),
            calendar AS (
              SELECT generate_series(b.start_d, b.end_d, interval '1 day')::date AS d
              FROM bounds b
            ),
            window_trades AS (
              SELECT
                date_trunc('day', executed_at)::date AS d,
                coalesce(pnl, 0)::float              AS pnl
              FROM trades, bounds b
              WHERE executed_at >= b.start_d
            ),
            daily AS (
              SELECT
                d,
                sum(pnl)::float AS daily_pnl,
                count(*)::int   AS trades
              FROM window_trades
              GROUP BY d
            ),
            filled AS (
              SELECT
                c.d,
                coalesce(daily.daily_pnl, 0)::float AS daily_pnl,
                coalesce(daily.trades, 0)::int      AS trades
              FROM calendar c
              LEFT JOIN daily ON daily.d = c.d
            )
            SELECT
              to_char(d, 'YYYY-MM-DD') AS date,
              daily_pnl,
              sum(daily_pnl) OVER (ORDER BY d ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)::float
                                       AS cumulative_pnl,
              trades
            FROM filled
            ORDER BY d ASC
            """,
            days,
        )
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning("public_equity_curve failed: %s", e)
        return []


async def get_public_stats() -> dict[str, Any]:
    """High-level numbers safe to expose to anonymous visitors.

    Used by the marketing hero — total realized PnL, win rate, trade
    count, and the most recent trade timestamp.  Every individual stat
    is wrapped so a single failure doesn't blank the whole hero.
    """
    realized_pnl = await _safe_fetchval("SELECT coalesce(sum(pnl), 0)::float FROM trades")
    total_trades = await _safe_count("trades")
    win_rate = await _safe_fetchval(
        "SELECT (sum((pnl > 0)::int)::float / nullif(count(*) filter (where pnl is not null), 0)) "
        "FROM trades WHERE pnl IS NOT NULL"
    )
    last_trade_at = await _safe_fetchval(
        "SELECT to_char(max(executed_at), 'YYYY-MM-DD\"T\"HH24:MI:SSZ') FROM trades"
    )
    cycles = await _safe_count("trading_cycles")
    debates = await _safe_count("swarm_debates")
    return {
        "realized_pnl": realized_pnl,
        "total_trades": total_trades,
        "win_rate": win_rate,
        "last_trade_at": last_trade_at,
        "trading_cycles": cycles,
        "swarm_debates": debates,
    }


async def get_recent_settled_signals(limit: int = 4) -> list[dict[str, Any]]:
    """Most-recent settled trades with model_prob, market settle price, and
    actual outcome — anonymized for the public landing page.

    Anonymized = no operator-identifying fields (account, position size,
    PnL).  We expose the *vertical* and *league* (e.g. "sports / NFL") so
    the visitor knows the swarm is real, plus the model's predicted
    probability, the market's settle price, and the binary YES/NO
    outcome.  Returns `[]` on any failure or when no settled trades exist.
    """
    limit = max(1, min(int(limit), 20))
    try:
        rows = await db.fetch(
            """
            SELECT
              t.vertical                                       AS vertical,
              t.league                                         AS league,
              lower(coalesce(t.side, ''))                      AS side,
              t.model_prob::float                              AS model_prob,
              t.price::float                                   AS entry_price,
              pm.actual_outcome::int                           AS actual_outcome,
              (pm.details->>'settle_price')::float             AS settle_price,
              to_char(t.settled_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS settled_at
              FROM trades t
              JOIN trade_postmortems pm
                ON pm.trade_id = coalesce(t.trade_id, t.id::text)
             WHERE t.settled_at IS NOT NULL
               AND t.model_prob IS NOT NULL
               AND pm.actual_outcome IS NOT NULL
             ORDER BY t.settled_at DESC NULLS LAST
             LIMIT $1
            """,
            limit,
        )
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning("recent_settled_signals failed: %s", e)
        return []


_LEADS_TABLE_READY = False


async def _ensure_leads_table() -> None:
    global _LEADS_TABLE_READY
    if _LEADS_TABLE_READY:
        return
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS landing_leads (
            id           bigserial PRIMARY KEY,
            email        text NOT NULL,
            interest     text,
            source       text,
            user_agent   text,
            ip_hash      text,
            created_at   timestamptz NOT NULL DEFAULT now(),
            confirmation_sent_at timestamptz
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS landing_leads_email_idx ON landing_leads (lower(email))"
    )
    _LEADS_TABLE_READY = True


async def insert_landing_lead(
    *,
    email: str,
    interest: str | None,
    source: str | None = None,
    user_agent: str | None = None,
    ip_hash: str | None = None,
) -> int:
    """Insert a row into `landing_leads` and return its id.  Creates the
    table on first call so a fresh database doesn't need a migration."""
    await _ensure_leads_table()
    row = await db.fetchrow(
        """
        INSERT INTO landing_leads (email, interest, source, user_agent, ip_hash)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING id
        """,
        email,
        interest,
        source,
        user_agent,
        ip_hash,
    )
    return int(row["id"])


async def mark_lead_confirmation_sent(lead_id: int) -> None:
    try:
        await db.execute(
            "UPDATE landing_leads SET confirmation_sent_at = now() WHERE id = $1",
            lead_id,
        )
    except Exception as e:
        log.warning("mark_lead_confirmation_sent failed for %s: %s", lead_id, e)


async def get_recent_swarm_debates(limit: int = 10) -> list[dict[str, Any]]:
    """Recent swarm-debate rows including the per-persona ``analyst_verdicts``
    and ``moderator_synthesis`` jsonb columns.

    The transcript columns are returned raw — the calling endpoint is
    responsible for redacting them for sub-Pro callers (see
    :func:`redact_swarm_debate_transcripts`).
    """
    try:
        rows = await db.fetch(
            """
            SELECT
              id,
              league,
              matchup,
              market_type,
              consensus_probability,
              confidence_band_low,
              confidence_band_high,
              agreement_rate,
              approved,
              recommended_action,
              outcome,
              cost_estimate,
              latency_ms,
              analyst_verdicts,
              moderator_synthesis,
              to_char(created_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS created_at
            FROM swarm_debates
            ORDER BY created_at DESC NULLS LAST
            LIMIT $1
            """,
            limit,
        )
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning("recent_swarm_debates failed: %s", e)
        return []


def redact_swarm_debate_transcripts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop the per-persona reasoning + moderator narrative from each item
    and stamp ``redacted: True`` so the frontend can show an upsell.

    Sub-Pro callers see the consensus + verdict (already public-safe on
    the marketing site) but never the raw persona reasoning.
    """
    out: list[dict[str, Any]] = []
    for it in items:
        clone = dict(it)
        clone.pop("analyst_verdicts", None)
        clone.pop("moderator_synthesis", None)
        clone["redacted"] = True
        out.append(clone)
    return out
