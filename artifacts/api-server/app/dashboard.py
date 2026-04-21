"""Dashboard read-models against the real production schema.

Each individual stat is wrapped in try/except so a single failing query
doesn't take down the whole response.
"""
from __future__ import annotations

import logging
from typing import Any

from . import db


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
              to_char(executed_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS executed_at
            FROM trades
            ORDER BY executed_at DESC NULLS LAST
            LIMIT $1
            """,
            limit,
        )
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning("recent_trades failed: %s", e)
        return []


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


async def get_recent_swarm_debates(limit: int = 10) -> list[dict[str, Any]]:
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
