"""Dashboard read-models against the real production schema.

Each individual stat is wrapped in try/except so a single failing query
doesn't take down the whole response.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from datetime import datetime, timedelta, timezone

from . import db
from .kalshi import client as kalshi_client
from .kalshi import tick_cache as kalshi_tick_cache
from .kalshi.market_titles import resolve_titles as resolve_market_titles
from .polymarket import client as polymarket_client


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


def _registered_verticals_snapshot() -> list[dict[str, Any]]:
    """Snapshot of the pluggable vertical registry for `/api/health`
    and `/api/dashboard/overview` consumers."""
    from . import verticals as _vertical_registry

    return [s.to_dict() for s in _vertical_registry.all_specs()]


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
    from .config import get_settings as _get_settings
    return {
        "counts": counts,
        "latest_cycle_at": latest_cycle_at,
        "latest_trade_at": latest_trade_at,
        "realized_pnl": realized_pnl,
        "win_rate": win_rate,
        "open_positions": int(open_positions) if open_positions is not None else None,
        "verticals": _registered_verticals_snapshot(),
        "active_verticals": list(_get_settings().orchestrator_verticals),
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


async def get_swarm_personas_today(date_utc: str | None = None) -> dict[str, Any]:
    """Today's per-persona Brier / accuracy / lean breakdown.

    Defaults `date_utc` to the current UTC date and delegates to the
    shared aggregator in :mod:`app.notifications.daily_summary` so the
    dashboard table can never disagree with the morning email.
    """
    from datetime import datetime, timezone
    from .notifications.daily_summary import aggregate_persona_day

    if not date_utc:
        date_utc = datetime.now(tz=timezone.utc).date().isoformat()
    try:
        items = await aggregate_persona_day(date_utc)
    except Exception as e:
        log.warning("swarm_personas_today failed: %s", e)
        items = []
    return {"date_utc": date_utc, "items": items}


async def get_swarm_fallbacks_today(date_utc: str | None = None) -> dict[str, Any]:
    """Per-persona count of cross-provider fallbacks for ``date_utc``.

    Walks today's ``swarm_debates.analyst_verdicts`` JSON and counts the
    rows where ``fallback_used`` is true.  Each entry in the returned
    ``items`` list also carries the most-recent ``primary_provider`` /
    ``primary_model`` and an example ``last_reason`` string so operators
    can see *why* the swap happened without opening a debate.

    Returns ``{"date_utc": ..., "items": [...]}`` ordered by count desc.
    Errors are swallowed and surface as an empty list — the rest of the
    operator dashboard must not break when this query fails.
    """
    from datetime import datetime, timezone

    if not date_utc:
        date_utc = datetime.now(tz=timezone.utc).date().isoformat()
    agg: dict[str, dict[str, Any]] = {}
    try:
        rows = await db.fetch(
            """
            SELECT analyst_verdicts
              FROM swarm_debates
             WHERE date_trunc('day', created_at AT TIME ZONE 'UTC') = $1::date
            """,
            date_utc,
        )
        for r in rows:
            verdicts = r["analyst_verdicts"]
            if isinstance(verdicts, str):
                try:
                    verdicts = json.loads(verdicts)
                except Exception:
                    continue
            if not isinstance(verdicts, list):
                continue
            for v in verdicts:
                if not isinstance(v, dict):
                    continue
                if not v.get("fallback_used"):
                    continue
                pid = v.get("persona") or v.get("analyst_id") or "?"
                slot = agg.setdefault(
                    pid,
                    {
                        "persona_id": pid,
                        "fallbacks": 0,
                        "primary_provider": v.get("primary_provider"),
                        "primary_model": v.get("primary_model"),
                        "fallback_provider": v.get("provider"),
                        "fallback_model": v.get("model"),
                        "last_reason": v.get("fallback_reason"),
                    },
                )
                slot["fallbacks"] += 1
                # Keep latest non-null fields.
                for k_in, k_out in (
                    ("primary_provider", "primary_provider"),
                    ("primary_model", "primary_model"),
                    ("provider", "fallback_provider"),
                    ("model", "fallback_model"),
                    ("fallback_reason", "last_reason"),
                ):
                    val = v.get(k_in)
                    if val:
                        slot[k_out] = val
    except Exception as e:
        log.warning("swarm_fallbacks_today failed: %s", e)
        return {"date_utc": date_utc, "items": []}
    items = sorted(
        agg.values(), key=lambda r: (-int(r["fallbacks"]), r["persona_id"])
    )
    return {"date_utc": date_utc, "items": items}


async def get_provider_fallback_rates() -> dict[str, Any]:
    """Per-provider LLM fallback rate over the alert's trailing window.

    Operators use this panel to spot a primary provider whose failure
    rate is creeping up *before* the alert in
    :mod:`app.swarm.fallback_monitor` actually fires.  We surface the
    same window / threshold / min-sample knobs so the panel and the
    alert never disagree.

    Returns ``{window_minutes, threshold_pct, min_samples, alert_enabled,
    items: [{provider, total, fallback, rate, reasons, would_alert}]}``.
    The ``rate`` is a fraction in ``[0, 1]``.  ``would_alert`` is true
    iff the row has enough samples *and* its rate is over the threshold,
    so the UI can colour it red even when the alert was deduped.
    """
    from .config import get_settings as _get_settings
    from .swarm.fallback_monitor import fetch_provider_stats

    s = _get_settings()
    window = s.llm_fallback_alert_window_minutes
    threshold_pct = float(s.llm_fallback_alert_threshold_pct)
    min_samples = int(s.llm_fallback_alert_min_samples)
    threshold = threshold_pct / 100.0

    try:
        stats = await fetch_provider_stats(window)
    except Exception as e:
        log.warning("provider_fallback_rates failed: %s", e)
        stats = []

    items: list[dict[str, Any]] = []
    for r in stats:
        total = int(r["total"])
        rate = float(r["rate"])
        items.append(
            {
                "provider": r["provider"],
                "total": total,
                "fallback": int(r["fallback"]),
                "rate": rate,
                "reasons": list(r["reasons"]),
                "would_alert": total >= min_samples and rate > threshold,
            }
        )
    return {
        "window_minutes": int(window),
        "threshold_pct": threshold_pct,
        "min_samples": min_samples,
        "alert_enabled": bool(s.llm_fallback_alert_enabled),
        "items": items,
    }


async def get_recent_cycles(limit: int = 10) -> list[dict[str, Any]]:
    try:
        rows = await db.fetch(
            """
            SELECT
              c.id,
              c.vertical,
              c.picks_evaluated,
              c.trades_placed,
              c.errors,
              to_char(c.started_at,   'YYYY-MM-DD"T"HH24:MI:SSZ') AS started_at,
              to_char(c.completed_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS completed_at,
              extract(epoch from (c.completed_at - c.started_at))::float AS duration_seconds,
              coalesce((
                SELECT count(*)::int FROM order_lifecycle ol
                 WHERE ol.event_type = 'rejected'
                   AND ol.metadata->>'reason' = 'slippage_guard'
                   AND (ol.vertical IS NULL OR c.vertical IS NULL OR ol.vertical = c.vertical)
                   AND ol.created_at >= c.started_at
                   AND ol.created_at <= coalesce(c.completed_at, now())
              ), 0)                                                  AS slippage_guard_rejects
            FROM trading_cycles c
            ORDER BY c.started_at DESC NULLS LAST
            LIMIT $1
            """,
            limit,
        )
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning("recent_cycles failed: %s", e)
        return []


async def get_slippage_guard_rejects(
    *,
    cycle_id: int | None = None,
    since_hours: int = 24,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return recent slippage-guard rejection rows from `order_lifecycle`.

    Each row carries the metadata the lifecycle attached at reject time
    (market ticker, requested vs. fillable depth, worst projected fill
    price, slippage in cents, model edge in cents, and how much of that
    edge would have been erased).

    When ``cycle_id`` is provided the result is scoped to rejects whose
    ``created_at`` falls inside that cycle's [started_at, completed_at]
    window (matching the per-cycle counter exposed by
    :func:`get_recent_cycles`). Otherwise the query falls back to the
    last ``since_hours`` of activity.
    """
    limit = max(1, min(500, int(limit)))
    try:
        if cycle_id is not None:
            rows = await db.fetch(
                """
                WITH c AS (
                  SELECT id, vertical, started_at, completed_at
                    FROM trading_cycles WHERE id = $1
                )
                SELECT
                  ol.id,
                  ol.market_id                                              AS market_ticker,
                  ol.side,
                  ol.vertical,
                  to_char(ol.created_at, 'YYYY-MM-DD"T"HH24:MI:SSZ')        AS created_at,
                  ol.metadata->>'detail'                                    AS detail,
                  (ol.metadata->>'requested_count')::int                    AS requested_count,
                  (ol.metadata->>'filled_available')::int                   AS filled_available,
                  nullif(ol.metadata->>'worst_fill_price_cents','')::int    AS worst_fill_price_cents,
                  nullif(ol.metadata->>'limit_price_cents','')::int         AS limit_price_cents,
                  nullif(ol.metadata->>'slippage_cents','')::int            AS slippage_cents,
                  nullif(ol.metadata->>'model_edge_cents','')::float        AS model_edge_cents,
                  nullif(ol.metadata->>'edge_erased_pct','')::float         AS edge_erased_pct,
                  nullif(ol.metadata->>'threshold_pct','')::float           AS threshold_pct,
                  c.id                                                      AS cycle_id
                  FROM order_lifecycle ol JOIN c ON true
                 WHERE ol.event_type = 'rejected'
                   AND ol.metadata->>'reason' = 'slippage_guard'
                   AND (ol.vertical IS NULL OR c.vertical IS NULL OR ol.vertical = c.vertical)
                   AND ol.created_at >= c.started_at
                   AND ol.created_at <= coalesce(c.completed_at, now())
                 ORDER BY ol.created_at DESC
                 LIMIT $2
                """,
                int(cycle_id), limit,
            )
        else:
            since = max(1, min(168, int(since_hours)))
            rows = await db.fetch(
                f"""
                SELECT
                  ol.id,
                  ol.market_id                                              AS market_ticker,
                  ol.side,
                  ol.vertical,
                  to_char(ol.created_at, 'YYYY-MM-DD"T"HH24:MI:SSZ')        AS created_at,
                  ol.metadata->>'detail'                                    AS detail,
                  (ol.metadata->>'requested_count')::int                    AS requested_count,
                  (ol.metadata->>'filled_available')::int                   AS filled_available,
                  nullif(ol.metadata->>'worst_fill_price_cents','')::int    AS worst_fill_price_cents,
                  nullif(ol.metadata->>'limit_price_cents','')::int         AS limit_price_cents,
                  nullif(ol.metadata->>'slippage_cents','')::int            AS slippage_cents,
                  nullif(ol.metadata->>'model_edge_cents','')::float        AS model_edge_cents,
                  nullif(ol.metadata->>'edge_erased_pct','')::float         AS edge_erased_pct,
                  nullif(ol.metadata->>'threshold_pct','')::float           AS threshold_pct,
                  NULL::bigint                                              AS cycle_id
                  FROM order_lifecycle ol
                 WHERE ol.event_type = 'rejected'
                   AND ol.metadata->>'reason' = 'slippage_guard'
                   AND ol.created_at >= now() - interval '{since} hours'
                 ORDER BY ol.created_at DESC
                 LIMIT $1
                """,
                limit,
            )
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning("slippage_guard_rejects failed: %s", e)
        return []


async def get_slippage_guard_top_offenders(
    *, date_utc: str, limit: int = 10
) -> list[dict[str, Any]]:
    """Per-market repeat-offender list for the daily summary email.

    Returns up to ``limit`` markets ordered by the number of slippage-guard
    rejections recorded on ``date_utc`` (UTC), heaviest first.
    """
    limit = max(1, min(50, int(limit)))
    try:
        rows = await db.fetch(
            """
            SELECT
              market_id                                            AS market_ticker,
              count(*)::int                                        AS rejects,
              max(nullif(metadata->>'edge_erased_pct','')::float)  AS max_edge_erased_pct,
              max(nullif(metadata->>'slippage_cents','')::int)     AS max_slippage_cents
              FROM order_lifecycle
             WHERE event_type = 'rejected'
               AND metadata->>'reason' = 'slippage_guard'
               AND date_trunc('day', created_at AT TIME ZONE 'UTC') = $1::date
             GROUP BY market_id
             ORDER BY rejects DESC, market_id
             LIMIT $2
            """,
            date_utc, limit,
        )
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning("slippage_guard_top_offenders failed: %s", e)
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


async def find_recent_lead_by_email(email: str, hours: int = 24) -> int | None:
    """Return the id of a recent landing-lead row matching lower(email).

    Used to suppress duplicate confirmation emails when the same address
    submits the form repeatedly within `hours`.  Returns None if no row
    matches or if the leads table doesn't exist yet.
    """
    await _ensure_leads_table()
    try:
        row = await db.fetchrow(
            """
            SELECT id
              FROM landing_leads
             WHERE lower(email) = lower($1)
               AND created_at > now() - ($2 || ' hours')::interval
             ORDER BY created_at DESC
             LIMIT 1
            """,
            email,
            str(int(hours)),
        )
    except Exception as e:
        log.warning("find_recent_lead_by_email failed: %s", e)
        return None
    return int(row["id"]) if row else None


async def mark_lead_confirmation_sent(lead_id: int) -> None:
    try:
        await db.execute(
            "UPDATE landing_leads SET confirmation_sent_at = now() WHERE id = $1",
            lead_id,
        )
    except Exception as e:
        log.warning("mark_lead_confirmation_sent failed for %s: %s", lead_id, e)


def _build_landing_leads_filter(
    *,
    interest: str | None,
    since: str | None,
    until: str | None,
    search: str | None,
    start_index: int = 1,
) -> tuple[str, list[Any]]:
    """Compose the WHERE clause shared by the listing / count / CSV
    endpoints.  Returns ``(sql, params)`` where ``sql`` either starts
    with ``WHERE`` or is empty.

    The interest filter accepts ``"__none__"`` as a sentinel for "no
    interest area selected" — useful so an operator can find rows that
    came in without picking a vertical.  Date bounds are inclusive on
    the start and exclusive on the end (caller passes the next-day date
    to capture the full UTC day).
    """
    clauses: list[str] = []
    params: list[Any] = []
    idx = start_index
    if interest is not None:
        if interest == "__none__":
            clauses.append("interest IS NULL")
        else:
            clauses.append(f"lower(coalesce(interest, '')) = lower(${idx})")
            params.append(interest)
            idx += 1
    if since:
        clauses.append(f"created_at >= ${idx}::timestamptz")
        params.append(since)
        idx += 1
    if until:
        clauses.append(f"created_at < ${idx}::timestamptz")
        params.append(until)
        idx += 1
    if search:
        clauses.append(f"lower(email) LIKE ${idx}")
        params.append(f"%{search.lower()}%")
        idx += 1
    if not clauses:
        return "", params
    return "WHERE " + " AND ".join(clauses), params


async def list_landing_lead_interests() -> list[str]:
    """Distinct, non-empty interest areas — drives the filter dropdown."""
    await _ensure_leads_table()
    try:
        rows = await db.fetch(
            """
            SELECT DISTINCT interest
              FROM landing_leads
             WHERE interest IS NOT NULL AND length(trim(interest)) > 0
             ORDER BY interest
            """
        )
        return [r["interest"] for r in rows]
    except Exception as e:
        log.warning("list_landing_lead_interests failed: %s", e)
        return []


async def list_landing_leads(
    *,
    interest: str | None = None,
    since: str | None = None,
    until: str | None = None,
    search: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """Paginated landing-lead rows for the admin console.

    Returns ``{items, total, limit, offset}``.  ``items`` is sorted
    most-recent-first.  All filter args are optional and combine with
    AND.  Always returns a structured payload — on any database error
    we surface an empty list with ``total=0`` so the UI can render
    "no leads yet" instead of a hard failure.
    """
    await _ensure_leads_table()
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    where, params = _build_landing_leads_filter(
        interest=interest, since=since, until=until, search=search
    )
    next_idx = len(params) + 1
    list_sql = f"""
        SELECT id,
               email,
               interest,
               source,
               to_char(created_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS created_at,
               to_char(confirmation_sent_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS confirmation_sent_at
          FROM landing_leads
        {where}
         ORDER BY created_at DESC NULLS LAST
         LIMIT ${next_idx} OFFSET ${next_idx + 1}
    """
    count_sql = f"SELECT count(*) FROM landing_leads {where}"
    try:
        rows = await db.fetch(list_sql, *params, limit, offset)
        total = int(await db.fetchval(count_sql, *params) or 0)
        return {
            "items": [dict(r) for r in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    except Exception as e:
        log.warning("list_landing_leads failed: %s", e)
        return {"items": [], "total": 0, "limit": limit, "offset": offset}


async def export_landing_leads_csv(
    *,
    interest: str | None = None,
    since: str | None = None,
    until: str | None = None,
    search: str | None = None,
    limit: int = 10000,
) -> str:
    """Render the (filtered) landing-lead list as CSV text.

    Capped at ``limit`` rows so a hostile / accidental click can't pull
    the whole table into memory.  Quoting is handled by the stdlib
    :mod:`csv` module so embedded commas / quotes / newlines in user
    input are escaped correctly.
    """
    await _ensure_leads_table()
    import csv
    import io

    limit = max(1, min(int(limit), 50000))
    where, params = _build_landing_leads_filter(
        interest=interest, since=since, until=until, search=search
    )
    next_idx = len(params) + 1
    sql = f"""
        SELECT id,
               email,
               coalesce(interest, '')                                         AS interest,
               coalesce(source, '')                                           AS source,
               to_char(created_at, 'YYYY-MM-DD"T"HH24:MI:SSZ')                AS created_at,
               coalesce(to_char(confirmation_sent_at, 'YYYY-MM-DD"T"HH24:MI:SSZ'), '') AS confirmation_sent_at
          FROM landing_leads
        {where}
         ORDER BY created_at DESC NULLS LAST
         LIMIT ${next_idx}
    """
    try:
        rows = await db.fetch(sql, *params, limit)
    except Exception as e:
        log.warning("export_landing_leads_csv failed: %s", e)
        rows = []

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "email", "interest", "source", "created_at", "confirmation_sent_at"])
    for r in rows:
        writer.writerow(
            [
                r["id"],
                r["email"],
                r["interest"],
                r["source"],
                r["created_at"],
                r["confirmation_sent_at"],
            ]
        )
    return buf.getvalue()


_ISO_DATE_RE = None  # lazily built


def _looks_like_iso_date(q: str) -> bool:
    """Return True if `q` looks like an ISO calendar date (YYYY-MM-DD).

    We don't try to parse arbitrary timestamp shapes here — the search
    box advertises "ticker, market id, ISO date" so accepting just the
    date prefix keeps the SQL deterministic.
    """
    import re
    global _ISO_DATE_RE
    if _ISO_DATE_RE is None:
        _ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    return bool(_ISO_DATE_RE.match(q))


async def search_global(
    q: str,
    limit: int = 10,
    *,
    include_admin: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """Find trades / debates / cycles that match `q`.

    Spec: ticker (e.g. ``KXNFL-25SEP04SEAARI-SEA``), market id, or an
    ISO date (``YYYY-MM-DD``).  No fuzzy / NL search — that's
    explicitly out of scope.

    Tickers and market ids are matched as case-insensitive substring on
    ``trades.market_id`` (Kalshi tickers are stored here) and
    ``trades.market_title``.  Debates are matched on ``matchup`` /
    ``league`` and on ``game_context->>'kalshi_market_id'``.  Cycles are
    matched by id when the query is numeric, or by ``vertical``.

    An ISO-date query restricts each table to that calendar day in UTC
    (``executed_at`` / ``created_at`` / ``started_at`` respectively).

    When ``include_admin`` is True (caller is an admin) the response
    also includes ``users`` (matches on ``app_users`` by email / display
    name / clerk user id), ``audit`` (matches on ``admin_audit_log``
    by action / actor / target email, plus numeric id), and ``billing``
    — stuck RC webhook deliveries (matched on ``rc_webhook_deliveries``
    by ``event_id`` / last error substring, latest 5xx wins) plus
    Stripe / RevenueCat customer rows (matched on ``subscriptions`` /
    ``rc_subscriptions`` by email / clerk user id / stripe customer
    id). Non-admin callers always receive empty lists for those keys
    so the operator palette can render them unconditionally.
    """
    q = (q or "").strip()
    if not q:
        return {
            "trades": [],
            "debates": [],
            "cycles": [],
            "users": [],
            "audit": [],
            "billing": [],
        }
    limit = max(1, min(50, int(limit)))

    is_date = _looks_like_iso_date(q)
    like = f"%{q}%"

    trades: list[dict[str, Any]] = []
    debates: list[dict[str, Any]] = []
    cycles: list[dict[str, Any]] = []

    try:
        if is_date:
            rows = await db.fetch(
                """
                SELECT id, trade_id, market_id, market_title, side, action,
                       price::float AS price, pnl::float AS pnl,
                       to_char(executed_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS executed_at
                  FROM trades
                 WHERE executed_at::date = $1::date
                 ORDER BY executed_at DESC NULLS LAST
                 LIMIT $2
                """,
                q, limit,
            )
        else:
            rows = await db.fetch(
                """
                SELECT id, trade_id, market_id, market_title, side, action,
                       price::float AS price, pnl::float AS pnl,
                       to_char(executed_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS executed_at
                  FROM trades
                 WHERE market_id ILIKE $1
                    OR coalesce(market_title, '') ILIKE $1
                    OR coalesce(trade_id, '') ILIKE $1
                 ORDER BY executed_at DESC NULLS LAST
                 LIMIT $2
                """,
                like, limit,
            )
        trades = [dict(r) for r in rows]
    except Exception as e:
        log.warning("search trades failed: %s", e)

    try:
        if is_date:
            rows = await db.fetch(
                """
                SELECT id, league, matchup, market_type,
                       consensus_probability, agreement_rate, approved,
                       recommended_action,
                       to_char(created_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS created_at
                  FROM swarm_debates
                 WHERE created_at::date = $1::date
                 ORDER BY created_at DESC NULLS LAST
                 LIMIT $2
                """,
                q, limit,
            )
        else:
            rows = await db.fetch(
                """
                SELECT id, league, matchup, market_type,
                       consensus_probability, agreement_rate, approved,
                       recommended_action,
                       to_char(created_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS created_at
                  FROM swarm_debates
                 WHERE matchup ILIKE $1
                    OR coalesce(league, '') ILIKE $1
                    OR coalesce(game_id, '') ILIKE $1
                    OR coalesce(game_context->>'kalshi_market_id', '') ILIKE $1
                 ORDER BY created_at DESC NULLS LAST
                 LIMIT $2
                """,
                like, limit,
            )
        debates = [dict(r) for r in rows]
    except Exception as e:
        log.warning("search debates failed: %s", e)

    try:
        if is_date:
            rows = await db.fetch(
                """
                SELECT id, vertical, picks_evaluated, trades_placed,
                       to_char(started_at, 'YYYY-MM-DD"T"HH24:MI:SSZ')   AS started_at,
                       to_char(completed_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS completed_at
                  FROM trading_cycles
                 WHERE started_at::date = $1::date
                 ORDER BY started_at DESC NULLS LAST
                 LIMIT $2
                """,
                q, limit,
            )
        elif q.isdigit():
            rows = await db.fetch(
                """
                SELECT id, vertical, picks_evaluated, trades_placed,
                       to_char(started_at, 'YYYY-MM-DD"T"HH24:MI:SSZ')   AS started_at,
                       to_char(completed_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS completed_at
                  FROM trading_cycles
                 WHERE id = $1
                 LIMIT $2
                """,
                int(q), limit,
            )
        else:
            rows = await db.fetch(
                """
                SELECT id, vertical, picks_evaluated, trades_placed,
                       to_char(started_at, 'YYYY-MM-DD"T"HH24:MI:SSZ')   AS started_at,
                       to_char(completed_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS completed_at
                  FROM trading_cycles
                 WHERE coalesce(vertical, '') ILIKE $1
                 ORDER BY started_at DESC NULLS LAST
                 LIMIT $2
                """,
                like, limit,
            )
        cycles = [dict(r) for r in rows]
    except Exception as e:
        log.warning("search cycles failed: %s", e)

    users: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []

    if include_admin:
        # ------------------------------------------------------------------
        # Admin-only sections: app_users + admin_audit_log. Operators
        # without admin always get empty lists so the palette can render
        # the sections unconditionally without leaking PII to non-admins.
        # ------------------------------------------------------------------
        try:
            if is_date:
                rows = await db.fetch(
                    """
                    SELECT clerk_user_id, email, display_name, approved,
                           is_admin,
                           to_char(last_seen_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS last_seen_at
                      FROM app_users
                     WHERE last_seen_at::date = $1::date
                     ORDER BY last_seen_at DESC NULLS LAST
                     LIMIT $2
                    """,
                    q, limit,
                )
            else:
                rows = await db.fetch(
                    """
                    SELECT clerk_user_id, email, display_name, approved,
                           is_admin,
                           to_char(last_seen_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS last_seen_at
                      FROM app_users
                     WHERE clerk_user_id ILIKE $1
                        OR coalesce(email, '') ILIKE $1
                        OR coalesce(display_name, '') ILIKE $1
                     ORDER BY last_seen_at DESC NULLS LAST
                     LIMIT $2
                    """,
                    like, limit,
                )
            users = [dict(r) for r in rows]
        except Exception as e:
            log.warning("search users failed: %s", e)

        try:
            if is_date:
                rows = await db.fetch(
                    """
                    SELECT id, action, actor_email, actor_user_id,
                           target_email, target_user_id,
                           to_char(created_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS created_at
                      FROM admin_audit_log
                     WHERE created_at::date = $1::date
                     ORDER BY created_at DESC NULLS LAST
                     LIMIT $2
                    """,
                    q, limit,
                )
            elif q.isdigit():
                # Numeric query: try the audit row id directly. Fall back
                # to substring against the textual columns so the operator
                # can still find a row by, say, an integer market id
                # embedded in the action label.
                rows = await db.fetch(
                    """
                    SELECT id, action, actor_email, actor_user_id,
                           target_email, target_user_id,
                           to_char(created_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS created_at
                      FROM admin_audit_log
                     WHERE id = $1
                        OR coalesce(action, '') ILIKE $2
                        OR coalesce(actor_email, '') ILIKE $2
                        OR coalesce(target_email, '') ILIKE $2
                     ORDER BY created_at DESC NULLS LAST
                     LIMIT $3
                    """,
                    int(q), like, limit,
                )
            else:
                rows = await db.fetch(
                    """
                    SELECT id, action, actor_email, actor_user_id,
                           target_email, target_user_id,
                           to_char(created_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS created_at
                      FROM admin_audit_log
                     WHERE coalesce(action, '') ILIKE $1
                        OR coalesce(actor_email, '') ILIKE $1
                        OR coalesce(target_email, '') ILIKE $1
                        OR coalesce(actor_user_id, '') ILIKE $1
                        OR coalesce(target_user_id, '') ILIKE $1
                     ORDER BY created_at DESC NULLS LAST
                     LIMIT $2
                    """,
                    like, limit,
                )
            audit = [dict(r) for r in rows]
        except Exception as e:
            log.warning("search audit failed: %s", e)

    billing: list[dict[str, Any]] = []
    if include_admin:
        # ------------------------------------------------------------------
        # Admin-only billing matches (task #632). Two row kinds share
        # one envelope so the palette renders them under a single
        # "Billing" group:
        #
        #   * ``kind="event"``   — the latest 5xx delivery row from
        #     ``rc_webhook_deliveries``, matched by ``event_id`` /
        #     last-error substring (or by calendar day on the ISO-date
        #     branch). Routes to the RC retry table on /system with a
        #     ``focus=rc-event-<id>`` highlight.
        #   * ``kind="customer"`` — Stripe (``subscriptions``) and RC
        #     (``rc_subscriptions`` joined to ``app_users`` for email)
        #     billing rows, matched on email / clerk user id / stripe
        #     customer id. Routes to the user detail with a
        #     ``focus=user-<clerk_user_id>`` highlight, mirroring the
        #     existing user-search hits.
        # ------------------------------------------------------------------

        # --- RC webhook events ---------------------------------------
        try:
            if is_date:
                rows = await db.fetch(
                    """
                    WITH latest AS (
                        SELECT DISTINCT ON (event_id)
                               event_id, status, http_status, event_type,
                               received_at, error
                          FROM rc_webhook_deliveries
                         WHERE event_id IS NOT NULL
                           AND received_at::date = $1::date
                         ORDER BY event_id, received_at DESC
                    ),
                    per_event AS (
                        SELECT event_id, count(*)::int AS attempts
                          FROM rc_webhook_deliveries
                         WHERE event_id IS NOT NULL
                           AND received_at::date = $1::date
                         GROUP BY event_id
                    )
                    SELECT l.event_id, l.event_type,
                           to_char(l.received_at, 'YYYY-MM-DD"T"HH24:MI:SSZ')
                               AS last_failed_at,
                           l.error AS last_error,
                           p.attempts
                      FROM latest l
                      JOIN per_event p USING (event_id)
                     WHERE l.status = 'http_5xx'
                     ORDER BY l.received_at DESC
                     LIMIT $2
                    """,
                    q, limit,
                )
            else:
                rows = await db.fetch(
                    """
                    WITH latest AS (
                        SELECT DISTINCT ON (event_id)
                               event_id, status, http_status, event_type,
                               received_at, error
                          FROM rc_webhook_deliveries
                         WHERE event_id IS NOT NULL
                           AND received_at >= now() - interval '30 days'
                         ORDER BY event_id, received_at DESC
                    ),
                    per_event AS (
                        SELECT event_id, count(*)::int AS attempts
                          FROM rc_webhook_deliveries
                         WHERE event_id IS NOT NULL
                           AND received_at >= now() - interval '30 days'
                         GROUP BY event_id
                    )
                    SELECT l.event_id, l.event_type,
                           to_char(l.received_at, 'YYYY-MM-DD"T"HH24:MI:SSZ')
                               AS last_failed_at,
                           l.error AS last_error,
                           p.attempts
                      FROM latest l
                      JOIN per_event p USING (event_id)
                     WHERE l.status = 'http_5xx'
                       AND (l.event_id ILIKE $1 OR coalesce(l.error, '') ILIKE $1)
                     ORDER BY l.received_at DESC
                     LIMIT $2
                    """,
                    like, limit,
                )
            for r in rows:
                billing.append(
                    {
                        "kind": "event",
                        "provider": "revenuecat",
                        "event_id": r["event_id"],
                        "event_type": r["event_type"],
                        "last_failed_at": r["last_failed_at"],
                        "last_error": r["last_error"],
                        "attempts": int(r["attempts"] or 0),
                    }
                )
        except Exception as e:
            log.warning("search billing events failed: %s", e)

        # --- Stripe customers ---------------------------------------
        try:
            if is_date:
                rows = await db.fetch(
                    """
                    SELECT user_id, email, tier, status, stripe_customer_id,
                           to_char(updated_at, 'YYYY-MM-DD"T"HH24:MI:SSZ')
                               AS updated_at
                      FROM subscriptions
                     WHERE updated_at::date = $1::date
                     ORDER BY updated_at DESC NULLS LAST
                     LIMIT $2
                    """,
                    q, limit,
                )
            else:
                rows = await db.fetch(
                    """
                    SELECT user_id, email, tier, status, stripe_customer_id,
                           to_char(updated_at, 'YYYY-MM-DD"T"HH24:MI:SSZ')
                               AS updated_at
                      FROM subscriptions
                     WHERE coalesce(email, '') ILIKE $1
                        OR coalesce(stripe_customer_id, '') ILIKE $1
                        OR coalesce(user_id, '') ILIKE $1
                     ORDER BY updated_at DESC NULLS LAST
                     LIMIT $2
                    """,
                    like, limit,
                )
            for r in rows:
                billing.append(
                    {
                        "kind": "customer",
                        "provider": "stripe",
                        "user_id": r["user_id"],
                        "email": r["email"],
                        "tier": r["tier"],
                        "status": r["status"],
                        "stripe_customer_id": r["stripe_customer_id"],
                        "updated_at": r["updated_at"],
                    }
                )
        except Exception as e:
            log.warning("search billing stripe customers failed: %s", e)

        # --- RevenueCat customers -----------------------------------
        # Only the latest row per (user_id|app_user_id) — RC writes one
        # row per event_id, so without DISTINCT ON the same customer
        # would surface multiple times.
        try:
            if is_date:
                rows = await db.fetch(
                    """
                    SELECT DISTINCT ON (coalesce(s.user_id, s.app_user_id))
                           s.user_id, s.app_user_id, s.tier, s.event_type,
                           u.email,
                           to_char(s.updated_at, 'YYYY-MM-DD"T"HH24:MI:SSZ')
                               AS updated_at
                      FROM rc_subscriptions s
                      LEFT JOIN app_users u
                             ON u.clerk_user_id = s.user_id
                     WHERE s.updated_at::date = $1::date
                     ORDER BY coalesce(s.user_id, s.app_user_id),
                              s.updated_at DESC
                     LIMIT $2
                    """,
                    q, limit,
                )
            else:
                rows = await db.fetch(
                    """
                    SELECT DISTINCT ON (coalesce(s.user_id, s.app_user_id))
                           s.user_id, s.app_user_id, s.tier, s.event_type,
                           u.email,
                           to_char(s.updated_at, 'YYYY-MM-DD"T"HH24:MI:SSZ')
                               AS updated_at
                      FROM rc_subscriptions s
                      LEFT JOIN app_users u
                             ON u.clerk_user_id = s.user_id
                     WHERE coalesce(u.email, '') ILIKE $1
                        OR coalesce(s.user_id, '') ILIKE $1
                        OR coalesce(s.app_user_id, '') ILIKE $1
                     ORDER BY coalesce(s.user_id, s.app_user_id),
                              s.updated_at DESC
                     LIMIT $2
                    """,
                    like, limit,
                )
            for r in rows:
                billing.append(
                    {
                        "kind": "customer",
                        "provider": "revenuecat",
                        "user_id": r["user_id"],
                        "app_user_id": r["app_user_id"],
                        "email": r["email"],
                        "tier": r["tier"],
                        "status": r["event_type"],
                        "updated_at": r["updated_at"],
                    }
                )
        except Exception as e:
            log.warning("search billing rc customers failed: %s", e)

    return {
        "trades": trades,
        "debates": debates,
        "cycles": cycles,
        "users": users,
        "audit": audit,
        "billing": billing,
    }


def _parse_iso_z(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        s = value.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _select_period_interval(span_minutes: float) -> int:
    """Pick a Kalshi candlestick bar size (1/60/1440 min) that keeps
    the returned series under a few hundred points for any window."""
    if span_minutes <= 240:
        return 1
    if span_minutes <= 60 * 24 * 7:
        return 60
    return 1440


async def _fetch_market_price_history(
    market_id: str,
    *,
    executed_at: str | None,
    debate_points: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Pull a continuous market price tick series from Kalshi.

    Returns a list of ``{t, model_prob: None, market_price}`` dicts
    suitable for merging into the swarm-debate series. Best-effort —
    silently returns ``[]`` on any error (missing creds, ticker that
    isn't a Kalshi market, http failure, etc.) so the caller still
    gets the debate-snapshot fallback.

    The window spans the trade's executed_at ± 60 minutes, extended
    to cover the earliest/latest debate timestamps so the merged
    series is contiguous. Bar size scales with span (1m / 1h / 1d)
    to keep the response small.
    """
    if not market_id:
        return []
    exec_ts = _parse_iso_z(executed_at)
    debate_times: list[datetime] = []
    for p in debate_points or []:
        dt = _parse_iso_z(p.get("t"))
        if dt is not None:
            debate_times.append(dt)

    if exec_ts is None and not debate_times:
        return []

    anchor = exec_ts or debate_times[0]
    candidates_lo = [anchor - timedelta(minutes=60)]
    candidates_hi = [anchor + timedelta(minutes=60)]
    candidates_lo.extend(debate_times)
    candidates_hi.extend(debate_times)
    start_dt = min(candidates_lo)
    end_dt = max(candidates_hi)
    # Don't run off the end of "now" — Kalshi rejects future windows
    # on settled markets in some cases and it's wasted work in any.
    now = datetime.now(timezone.utc)
    if end_dt > now:
        end_dt = now
    if start_dt >= end_dt:
        return []

    span_min = (end_dt - start_dt).total_seconds() / 60.0
    period_interval = _select_period_interval(span_min)
    start_ts = int(start_dt.timestamp())
    end_ts = int(end_dt.timestamp())

    # Cache-first: read whatever buckets we already have for this
    # window and ask the cache which ranges (if any) still need a
    # live Kalshi fetch. The periodic refresh loop in
    # ``app.kalshi.tick_cache_refresh`` keeps recently-traded markets
    # warm so this almost always returns missing=[] in steady state.
    cached_points, missing_ranges = await kalshi_tick_cache.fetch_window(
        market_id,
        period_interval=period_interval,
        start_ts=start_ts,
        end_ts=end_ts,
    )
    if not missing_ranges:
        return cached_points

    try:
        client = kalshi_client.KalshiClient()
    except Exception as e:
        log.info("trade detail candlesticks: kalshi client unavailable: %s", e)
        return cached_points

    fetched_points: list[dict[str, Any]] = []
    try:
        for lo, hi in missing_ranges:
            try:
                data = await client.get_market_candlesticks(
                    market_id,
                    start_ts=int(lo),
                    end_ts=int(hi),
                    period_interval=period_interval,
                )
            except kalshi_client.KalshiAPIError as e:
                log.info(
                    "kalshi candlesticks(%s) failed: http %s",
                    market_id, e.status,
                )
                continue
            except Exception as e:
                log.info("kalshi candlesticks(%s) error: %s", market_id, e)
                continue

            candles = (data or {}).get("candlesticks") or []
            if not candles:
                continue
            # Persist the fresh buckets back into the cache so the
            # next request (and the live operator dashboard) is served
            # entirely from Postgres. Best-effort — failures don't
            # block the in-memory render.
            await kalshi_tick_cache.upsert_candles(
                market_id,
                period_interval=period_interval,
                candles=candles,
            )
            fetched_points.extend(
                kalshi_tick_cache.candles_to_points(candles)
            )
    finally:
        try:
            await client.aclose()
        except Exception:
            pass

    if not fetched_points:
        return cached_points

    # Merge cached + freshly-fetched, deduping by ISO-Z timestamp so a
    # bucket that overlapped both sources is rendered once.
    merged: dict[str, dict[str, Any]] = {p["t"]: p for p in cached_points}
    for p in fetched_points:
        merged[p["t"]] = p
    return sorted(merged.values(), key=lambda p: p.get("t") or "")


def _select_polymarket_token_id(
    market: dict[str, Any] | None, trade_side: str | None
) -> str | None:
    """Pick the outcome token whose label matches the trade's side.

    Polymarket's ``/prices-history`` is per-token, but ``trades.market_id``
    stores the conditionId.  We resolve the conditionId via
    ``get_market`` and match on the outcome label ('yes' / 'no'). Falls
    back to the first non-empty token id so chart rendering still
    proceeds for markets with custom outcome labels.
    """
    tokens = (market or {}).get("tokens") or []
    side = (trade_side or "").strip().lower()
    if side in ("yes", "no"):
        for t in tokens:
            outcome = str((t or {}).get("outcome") or "").strip().lower()
            if outcome == side:
                tid = (t or {}).get("token_id")
                if tid:
                    return str(tid)
    for t in tokens:
        tid = (t or {}).get("token_id")
        if tid:
            return str(tid)
    return None


def _polymarket_history_to_points(
    history: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Normalise CLOB ``/prices-history`` rows into trade-detail points.

    Output rows match the Kalshi-side shape from
    :func:`kalshi_tick_cache.candles_to_points`: ISO-Z timestamp,
    ``model_prob`` always ``None`` so the front-end keeps the model
    line driven by debate snapshots, and ``market_price`` as a 0..1
    fraction (Polymarket prices are already decimals so no conversion
    is needed).
    """
    out: list[dict[str, Any]] = []
    for row in history or []:
        if not isinstance(row, dict):
            continue
        ts = row.get("t")
        if not isinstance(ts, (int, float)):
            continue
        try:
            price = float(row.get("p"))
        except (TypeError, ValueError):
            continue
        out.append(
            {
                "t": kalshi_tick_cache._to_iso_z(int(ts)),
                "model_prob": None,
                "market_price": price,
            }
        )
    return out


async def _fetch_polymarket_market_price_history(
    condition_id: str,
    *,
    trade_side: str | None,
    executed_at: str | None,
    debate_points: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Polymarket analogue of :func:`_fetch_market_price_history`.

    Returns a list of ``{t, model_prob: None, market_price}`` dicts in
    the same shape the swarm-debate series uses. Best-effort — silently
    returns ``[]`` on any error (network, client construction, missing
    outcome token, http failure) so the caller still falls back to the
    debate-snapshot series.

    The window mirrors the Kalshi helper: ``executed_at`` ± 60 minutes,
    extended to cover the earliest/latest debate timestamps so the
    merged series stays contiguous. Bar size scales with span (1m / 1h
    / 1d) via :func:`_select_period_interval`.
    """
    if not condition_id:
        return []
    exec_ts = _parse_iso_z(executed_at)
    debate_times: list[datetime] = []
    for p in debate_points or []:
        dt = _parse_iso_z(p.get("t"))
        if dt is not None:
            debate_times.append(dt)

    if exec_ts is None and not debate_times:
        return []

    anchor = exec_ts or debate_times[0]
    candidates_lo = [anchor - timedelta(minutes=60)]
    candidates_hi = [anchor + timedelta(minutes=60)]
    candidates_lo.extend(debate_times)
    candidates_hi.extend(debate_times)
    start_dt = min(candidates_lo)
    end_dt = max(candidates_hi)
    now = datetime.now(timezone.utc)
    if end_dt > now:
        end_dt = now
    if start_dt >= end_dt:
        return []

    span_min = (end_dt - start_dt).total_seconds() / 60.0
    fidelity = _select_period_interval(span_min)
    start_ts = int(start_dt.timestamp())
    end_ts = int(end_dt.timestamp())

    try:
        client = polymarket_client.PolymarketClient()
    except Exception as e:
        log.info(
            "trade detail polymarket history: client unavailable: %s", e
        )
        return []

    try:
        try:
            market = await client.get_market(condition_id)
        except polymarket_client.PolymarketAPIError as e:
            log.info(
                "polymarket get_market(%s) failed: http %s",
                condition_id, e.status,
            )
            return []
        except Exception as e:
            log.info("polymarket get_market(%s) error: %s", condition_id, e)
            return []

        token_id = _select_polymarket_token_id(market, trade_side)
        if not token_id:
            log.info(
                "polymarket prices-history skipped: no outcome token "
                "for condition %s side=%s",
                condition_id, trade_side,
            )
            return []

        try:
            history = await client.get_prices_history(
                token_id,
                start_ts=start_ts,
                end_ts=end_ts,
                fidelity=fidelity,
            )
        except polymarket_client.PolymarketAPIError as e:
            log.info(
                "polymarket prices-history(%s) failed: http %s",
                token_id, e.status,
            )
            return []
        except Exception as e:
            log.info("polymarket prices-history(%s) error: %s", token_id, e)
            return []
    finally:
        try:
            await client.aclose()
        except Exception:
            pass

    return _polymarket_history_to_points(history)


async def get_trade_detail(trade_ref: str) -> dict[str, Any] | None:
    """Return a single trade row + market-vs-model time series.

    `trade_ref` accepts either the integer ``trades.id`` (preferred —
    that's what the recent-trades table renders) or the UUID-ish
    ``trades.trade_id``.  Returns ``None`` if no row matches.

    The time series merges three sources so the market-vs-model overlay
    reflects exchange tick history rather than only the moments where
    a debate happened to run:

    1. swarm_debates rows for the same market — consensus model_prob
       plus the Kalshi snapshot at debate time (good for the model
       line, sparse for price).
    2. Kalshi market candlesticks for the window around the trade —
       continuous tick history (1m / 1h / 1d bars depending on span)
       so the market_price series is contiguous between debates.
       Best-effort: if Kalshi is unreachable or credentials are
       missing we silently fall back to source #1 only.
    3. The trade's own snapshot (trades.price + trades.model_prob)
       appended as an anchor at executed_at so the chart always
       shows at least one point.
    """
    ref = (trade_ref or "").strip()
    if not ref:
        return None

    try:
        if ref.isdigit():
            row = await db.fetchrow(
                """
                SELECT id, trade_id, market_id, market_title, vertical, league,
                       side, action,
                       quantity::float    AS quantity,
                       price::float       AS price,
                       fees::float        AS fees,
                       pnl::float         AS pnl,
                       model_prob::float  AS model_prob,
                       model_edge::float  AS model_edge,
                       kelly_fraction::float AS kelly_fraction,
                       trading_mode, exchange,
                       to_char(executed_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS executed_at
                  FROM trades
                 WHERE id = $1
                 LIMIT 1
                """,
                int(ref),
            )
        else:
            row = await db.fetchrow(
                """
                SELECT id, trade_id, market_id, market_title, vertical, league,
                       side, action,
                       quantity::float    AS quantity,
                       price::float       AS price,
                       fees::float        AS fees,
                       pnl::float         AS pnl,
                       model_prob::float  AS model_prob,
                       model_edge::float  AS model_edge,
                       kelly_fraction::float AS kelly_fraction,
                       trading_mode, exchange,
                       to_char(executed_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS executed_at
                  FROM trades
                 WHERE trade_id = $1
                 LIMIT 1
                """,
                ref,
            )
    except Exception as e:
        log.warning("get_trade_detail lookup failed: %s", e)
        return None

    if row is None:
        return None
    trade = dict(row)

    # Backfill market_title if missing.
    if trade.get("market_id") and not trade.get("market_title"):
        try:
            titles = await resolve_market_titles({trade["market_id"]})
            trade["market_title"] = titles.get(trade["market_id"])
        except Exception:
            pass

    series: list[dict[str, Any]] = []
    market_id = trade.get("market_id")
    if market_id:
        try:
            srows = await db.fetch(
                """
                SELECT
                    to_char(created_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS t,
                    consensus_probability::float                    AS model_prob,
                    nullif(game_context->>'kalshi_price', '')::float AS market_price
                  FROM swarm_debates
                 WHERE game_context->>'kalshi_market_id' = $1
                   AND created_at IS NOT NULL
                 ORDER BY created_at ASC
                 LIMIT 500
                """,
                market_id,
            )
            series = [dict(r) for r in srows]
        except Exception as e:
            log.warning("trade detail series failed: %s", e)

        # Pull continuous market-price history from whichever venue the
        # trade actually executed on so Polymarket trade-detail pages
        # don't get stuck with the sparse debate-snapshot fallback.
        # Unknown / missing exchange falls back to the Kalshi path
        # (historical default before the venue column was populated).
        exchange = (trade.get("exchange") or "").strip().lower()
        try:
            if exchange == "polymarket":
                tick_points = await _fetch_polymarket_market_price_history(
                    market_id,
                    trade_side=trade.get("side"),
                    executed_at=trade.get("executed_at"),
                    debate_points=series,
                )
            else:
                tick_points = await _fetch_market_price_history(
                    market_id,
                    executed_at=trade.get("executed_at"),
                    debate_points=series,
                )
        except Exception as e:
            log.warning("trade detail candlestick overlay failed: %s", e)
            tick_points = []
        if tick_points:
            series.extend(tick_points)
            series.sort(key=lambda p: (p.get("t") or ""))

    # Anchor: always append the trade's own snapshot so the chart isn't
    # empty even if no debate rows exist for this market.
    if trade.get("executed_at"):
        anchor_t = trade["executed_at"]
        if not any(p.get("t") == anchor_t for p in series):
            series.append(
                {
                    "t": anchor_t,
                    "model_prob": trade.get("model_prob"),
                    "market_price": trade.get("price"),
                }
            )
            series.sort(key=lambda p: p.get("t") or "")

    return {"trade": trade, "series": series}


async def get_vertical_performance(days: int = 30) -> dict[str, Any]:
    """Per-vertical (sports/weather/crypto) performance for the operator strip.

    Returns one entry per known vertical with rolling-window P&L, win rate,
    trade count, latest 24h Brier (from `calibration_snapshots`), plus a
    10-bucket calibration curve and a 10-bucket PIT-style histogram.

    Predicted probability is aligned to the trade's side (NO trades flip
    `model_prob` and the actual outcome) so the curves measure how well
    the *bet* was priced, not just P(YES).  All buckets are 0.05 wide
    (10 bins over [0,1]); buckets with no samples are still emitted with
    `n=0` so the front-end can render a continuous axis.

    Window is clamped to [1, 365] days; the dashboard widget only ever
    asks for 7 / 30 / 90.  Every per-vertical sub-query is wrapped so a
    single failure doesn't blank the whole strip.
    """
    days = max(1, min(int(days), 365))
    # Driven by `app.verticals` — verticals with
    # `include_in_dashboard_strip=True` show up here.  Existing order
    # (sports → weather → crypto) is preserved by the registry's
    # insertion order in `app/verticals/builtins.py`.
    from . import verticals as _vertical_registry

    verticals = _vertical_registry.dashboard_strip_verticals() or [
        "sports", "weather", "crypto"
    ]

    # Pre-fetch today's approved-debate counts grouped by league so we
    # can attribute each row to the right vertical without issuing one
    # query per tile.  ``swarm_debates`` doesn't carry a ``vertical``
    # column — it stores ``league`` — so we map league → vertical via
    # the registry's per-spec league filter (task #450).
    debates_today_by_league: dict[str, int] = {}
    try:
        drows = await db.fetch(
            """
            SELECT coalesce(league, '')                             AS league,
                   count(*)::int                                    AS n
              FROM swarm_debates
             WHERE approved = TRUE
               AND date_trunc('day', created_at AT TIME ZONE 'UTC')
                   = (now() AT TIME ZONE 'UTC')::date
             GROUP BY league
            """
        )
        debates_today_by_league = {
            (r["league"] or ""): int(r["n"] or 0) for r in drows
        }
    except Exception as e:
        log.warning("vertical_perf approved_debates_today: %s", e)

    # Build a set of every league that maps to a *non-sports* strip
    # vertical so the catch-all "sports" tile counts every approved
    # debate that didn't belong to a more specific vertical.
    non_sports_strip_leagues: set[str] = set()
    for _spec in _vertical_registry.all_specs():
        if _spec.name == "sports" or _spec.name not in verticals:
            continue
        for lg in _spec.leagues:
            non_sports_strip_leagues.add(lg)

    out: list[dict[str, Any]] = []
    for vertical in verticals:
        entry: dict[str, Any] = {
            "vertical": vertical,
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": None,
            "pnl": 0.0,
            "brier": None,
            "calibration": [],
            "pit": [],
            "open_exposure_usd": 0.0,
            "pnl_today": 0.0,
            "approved_debates_today": 0,
        }
        # Aggregate trade stats over the rolling window.  We use the
        # same UTC calendar-day lower bound as the daily series below
        # so the headline P&L matches the final point of the cumulative
        # sparkline.
        try:
            agg = await db.fetchrow(
                """
                SELECT count(*)::int                                    AS n,
                       coalesce(sum(pnl), 0)::float                     AS pnl,
                       sum((pnl > 0)::int)::int                         AS wins,
                       sum((pnl < 0)::int)::int                         AS losses,
                       (sum((pnl > 0)::int)::float
                        / nullif(count(*) filter (where pnl is not null), 0))
                                                                        AS win_rate
                  FROM trades
                 WHERE vertical = $1
                   AND executed_at >= ((now() at time zone 'utc')::date - ($2::int - 1))
                """,
                vertical, days,
            )
            if agg:
                entry["trades"] = int(agg["n"] or 0)
                entry["pnl"] = float(agg["pnl"] or 0.0)
                entry["wins"] = int(agg["wins"] or 0)
                entry["losses"] = int(agg["losses"] or 0)
                entry["win_rate"] = (
                    float(agg["win_rate"]) if agg["win_rate"] is not None else None
                )
        except Exception as e:
            log.warning("vertical_perf trade-agg %s: %s", vertical, e)

        # Latest 24h Brier from calibration_snapshots.
        try:
            entry["brier"] = await db.fetchval(
                """
                SELECT brier_score::float
                  FROM calibration_snapshots
                 WHERE vertical = $1 AND sub_category = '24h'
                 ORDER BY created_at DESC
                 LIMIT 1
                """,
                vertical,
            )
            if entry["brier"] is not None:
                entry["brier"] = float(entry["brier"])
        except Exception as e:
            log.warning("vertical_perf brier %s: %s", vertical, e)

        # Open USD exposure — sum of `positions.notional` for currently
        # open positions tagged with this vertical (task #450).
        try:
            n = await db.fetchval(
                """
                SELECT coalesce(sum(notional), 0)::float
                  FROM positions
                 WHERE vertical = $1
                   AND coalesce(status, 'open') = 'open'
                """,
                vertical,
            )
            entry["open_exposure_usd"] = float(n or 0.0)
        except Exception as e:
            log.warning("vertical_perf open_exposure %s: %s", vertical, e)

        # Today's realized P&L (UTC calendar day) for this vertical.
        try:
            n = await db.fetchval(
                """
                SELECT coalesce(sum(pnl), 0)::float
                  FROM trades
                 WHERE vertical = $1
                   AND executed_at >= (now() AT TIME ZONE 'UTC')::date
                """,
                vertical,
            )
            entry["pnl_today"] = float(n or 0.0)
        except Exception as e:
            log.warning("vertical_perf pnl_today %s: %s", vertical, e)

        # Approved-debate count today, attributed via league mapping.
        spec = _vertical_registry.get_spec(vertical)
        if vertical == "sports":
            # The catch-all tile picks up every approved debate whose
            # league did NOT map to a more specific strip vertical.
            entry["approved_debates_today"] = sum(
                n for lg, n in debates_today_by_league.items()
                if lg and lg not in non_sports_strip_leagues
            )
        else:
            leagues = list(spec.leagues) if spec else []
            entry["approved_debates_today"] = sum(
                debates_today_by_league.get(lg, 0) for lg in leagues
            )

        # Calibration buckets (predicted vs observed) and PIT histogram
        # over the same rolling window, computed from settled trades.
        try:
            rows = await db.fetch(
                """
                WITH settled AS (
                  SELECT t.model_prob::float                AS p_yes,
                         lower(coalesce(t.side, ''))        AS side_lc,
                         pm.actual_outcome::int             AS y
                    FROM trades t
                    JOIN trade_postmortems pm
                      ON pm.trade_id = coalesce(t.trade_id, t.id::text)
                   WHERE t.vertical = $1
                     AND t.executed_at >= ((now() at time zone 'utc')::date - ($2::int - 1))
                     AND t.model_prob IS NOT NULL
                     AND pm.actual_outcome IS NOT NULL
                ), aligned AS (
                  SELECT CASE WHEN side_lc IN ('no','n','sell')
                              THEN 1.0 - p_yes ELSE p_yes END AS p_side,
                         CASE WHEN side_lc IN ('no','n','sell')
                              THEN 1 - y       ELSE y       END AS y_side
                    FROM settled
                ), bucketed AS (
                  SELECT least(9, floor(p_side * 10)::int)            AS cal_bin,
                         CASE WHEN y_side = 1
                              THEN least(9, floor(p_side * 10)::int)
                              ELSE least(9, floor((1.0 - p_side) * 10)::int)
                         END                                          AS pit_bin,
                         p_side, y_side
                    FROM aligned
                )
                SELECT bin,
                       sum(cal_n)::int        AS cal_n,
                       sum(cal_p)::float      AS cal_p_sum,
                       sum(cal_y)::float      AS cal_y_sum,
                       sum(pit_n)::int        AS pit_n
                  FROM (
                    SELECT cal_bin AS bin,
                           1 AS cal_n, p_side AS cal_p, y_side::float AS cal_y,
                           0 AS pit_n
                      FROM bucketed
                    UNION ALL
                    SELECT pit_bin AS bin, 0, 0::float, 0::float, 1
                      FROM bucketed
                  ) u
                 GROUP BY bin
                 ORDER BY bin
                """,
                vertical, days,
            )
        except Exception as e:
            log.warning("vertical_perf curves %s: %s", vertical, e)
            rows = []

        cal_by_bin: dict[int, dict[str, Any]] = {}
        pit_by_bin: dict[int, int] = {}
        for r in rows:
            b = int(r["bin"])
            n = int(r["cal_n"] or 0)
            if n > 0:
                cal_by_bin[b] = {
                    "n": n,
                    "p_sum": float(r["cal_p_sum"] or 0.0),
                    "y_sum": float(r["cal_y_sum"] or 0.0),
                }
            pit_by_bin[b] = int(r["pit_n"] or 0)

        calibration: list[dict[str, Any]] = []
        pit: list[dict[str, Any]] = []
        for b in range(10):
            lo = b / 10.0
            hi = (b + 1) / 10.0
            mid = (lo + hi) / 2.0
            c = cal_by_bin.get(b)
            calibration.append({
                "bucket": round(mid, 3),
                "bucket_low": round(lo, 3),
                "bucket_high": round(hi, 3),
                "predicted": round(c["p_sum"] / c["n"], 4) if c else None,
                "observed":  round(c["y_sum"] / c["n"], 4) if c else None,
                "n": c["n"] if c else 0,
            })
            pit.append({
                "bucket": round(mid, 3),
                "bucket_low": round(lo, 3),
                "bucket_high": round(hi, 3),
                "count": pit_by_bin.get(b, 0),
            })
        entry["calibration"] = calibration
        entry["pit"] = pit

        # Daily series (cumulative P&L + daily Brier) over the rolling
        # window so the front-end can render trend sparklines under the
        # headline P&L number.  Days with no activity are still emitted
        # so the line stays continuous; brier is null on days with no
        # settled trades.
        daily: list[dict[str, Any]] = []
        try:
            drows = await db.fetch(
                """
                WITH bounds AS (
                  SELECT ((now() at time zone 'utc')::date - ($2::int - 1)) AS start_d,
                         (now() at time zone 'utc')::date                   AS end_d
                ),
                calendar AS (
                  SELECT generate_series(b.start_d, b.end_d, interval '1 day')::date AS d
                  FROM bounds b
                ),
                window_trades AS (
                  SELECT date_trunc('day', t.executed_at)::date AS d,
                         coalesce(t.pnl, 0)::float              AS pnl
                    FROM trades t, bounds b
                   WHERE t.vertical = $1
                     AND t.executed_at >= b.start_d
                ),
                daily_pnl AS (
                  SELECT d, sum(pnl)::float AS daily_pnl, count(*)::int AS trades
                    FROM window_trades
                   GROUP BY d
                ),
                settled AS (
                  SELECT date_trunc('day', t.executed_at)::date AS d,
                         CASE WHEN lower(coalesce(t.side, '')) IN ('no','n','sell')
                              THEN 1.0 - t.model_prob::float
                              ELSE t.model_prob::float END                       AS p_side,
                         CASE WHEN lower(coalesce(t.side, '')) IN ('no','n','sell')
                              THEN 1 - pm.actual_outcome::int
                              ELSE pm.actual_outcome::int END                    AS y_side
                    FROM trades t
                    JOIN trade_postmortems pm
                      ON pm.trade_id = coalesce(t.trade_id, t.id::text), bounds b
                   WHERE t.vertical = $1
                     AND t.executed_at >= b.start_d
                     AND t.model_prob IS NOT NULL
                     AND pm.actual_outcome IS NOT NULL
                ),
                daily_brier AS (
                  SELECT d,
                         avg(power(p_side - y_side, 2))::float AS brier,
                         count(*)::int                          AS n_settled
                    FROM settled
                   GROUP BY d
                )
                SELECT to_char(c.d, 'YYYY-MM-DD')                              AS date,
                       coalesce(dp.daily_pnl, 0)::float                        AS daily_pnl,
                       sum(coalesce(dp.daily_pnl, 0)) OVER (
                           ORDER BY c.d ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                       )::float                                                AS cumulative_pnl,
                       coalesce(dp.trades, 0)::int                             AS trades,
                       db.brier                                                AS brier,
                       coalesce(db.n_settled, 0)::int                          AS n_settled
                  FROM calendar c
                  LEFT JOIN daily_pnl   dp ON dp.d = c.d
                  LEFT JOIN daily_brier db ON db.d = c.d
                 ORDER BY c.d ASC
                """,
                vertical, days,
            )
            for r in drows:
                daily.append({
                    "date": r["date"],
                    "daily_pnl": float(r["daily_pnl"] or 0.0),
                    "cumulative_pnl": float(r["cumulative_pnl"] or 0.0),
                    "trades": int(r["trades"] or 0),
                    "brier": float(r["brier"]) if r["brier"] is not None else None,
                    "n_settled": int(r["n_settled"] or 0),
                })
        except Exception as e:
            log.warning("vertical_perf daily %s: %s", vertical, e)
        entry["daily"] = daily
        out.append(entry)

    return {"days": days, "verticals": out}


# Rolling windows surfaced by the operator-facing crypto accuracy
# rollup.  Hours so the SQL ``make_interval(hours => $1)`` clause stays
# uniform across windows.
_CRYPTO_ACCURACY_WINDOWS_HOURS: tuple[int, ...] = (24, 24 * 7)


async def _crypto_accuracy_window(window_hours: int) -> dict[str, Any]:
    """Compute the realised-accuracy rollup for one rolling window.

    The Kalshi crypto producer (see ``app.feeds.kalshi_crypto_markets``)
    upserts each live binary market into ``predictions`` with the
    market mid in ``kalshi_price`` and — once the
    ``kalshi_crypto_settlements`` settler observes Kalshi's resolution
    — stamps ``outcome`` / ``settle_price`` / ``settled_at`` per
    migration ``0022_predictions_settlement``.

    For each settled (non-void) row in the window we treat
    ``kalshi_price`` as the predicted YES probability and join it
    against ``settle_price`` (1.0 / 0.0) to produce a Brier score and
    hit rate.  Voids are excluded from scoring (they refund every
    contract) but counted separately so the operator can see
    coverage.

    Returns ``brier`` / ``hit_rate`` as ``None`` when no scorable rows
    exist so the panel can render a clear "no data" state instead of
    a misleading 0.0.
    """
    empty: dict[str, Any] = {
        "window_hours": int(window_hours),
        "n_settled": 0,
        "n_scored": 0,
        "n_void": 0,
        "brier": None,
        "hit_rate": None,
    }
    try:
        row = await db.fetchrow(
            """
            SELECT
              count(*) FILTER (
                WHERE settled_at IS NOT NULL
              )::int                                                  AS n_settled,
              count(*) FILTER (
                WHERE settled_at IS NOT NULL
                  AND lower(coalesce(outcome, '')) = 'void'
              )::int                                                  AS n_void,
              count(*) FILTER (
                WHERE settled_at IS NOT NULL
                  AND settle_price IS NOT NULL
                  AND kalshi_price IS NOT NULL
                  AND lower(coalesce(outcome, '')) IN ('yes', 'no')
              )::int                                                  AS n_scored,
              avg(power(kalshi_price - settle_price, 2)) FILTER (
                WHERE settled_at IS NOT NULL
                  AND settle_price IS NOT NULL
                  AND kalshi_price IS NOT NULL
                  AND lower(coalesce(outcome, '')) IN ('yes', 'no')
              )::float                                                AS brier,
              avg(
                CASE
                  WHEN (kalshi_price >= 0.5) = (settle_price >= 0.5)
                    THEN 1.0
                  ELSE 0.0
                END
              ) FILTER (
                WHERE settled_at IS NOT NULL
                  AND settle_price IS NOT NULL
                  AND kalshi_price IS NOT NULL
                  AND lower(coalesce(outcome, '')) IN ('yes', 'no')
              )::float                                                AS hit_rate
              FROM predictions
             WHERE league = 'CRYPTO'
               AND settled_at >= now() - make_interval(hours => $1)
            """,
            int(window_hours),
        )
    except Exception as e:  # noqa: BLE001 — defensive
        # Soft-fail: a transient DB blip or a fork without the
        # settlement migration shouldn't blank the operator panel.
        log.warning(
            "crypto_accuracy window=%sh failed: %s", window_hours, e,
        )
        return empty
    if row is None:
        return empty
    return {
        "window_hours": int(window_hours),
        "n_settled": int(row["n_settled"] or 0),
        "n_scored": int(row["n_scored"] or 0),
        "n_void": int(row["n_void"] or 0),
        "brier": float(row["brier"]) if row["brier"] is not None else None,
        "hit_rate": (
            float(row["hit_rate"]) if row["hit_rate"] is not None else None
        ),
    }


async def get_crypto_accuracy_rollup() -> dict[str, Any]:
    """Operator-console rollup of realised crypto-market accuracy.

    Computes a small Brier / hit-rate summary against the Kalshi
    crypto-binary predictions the
    ``app.feeds.kalshi_crypto_markets`` settler has stamped with
    ``outcome`` / ``settle_price`` (migration
    ``0022_predictions_settlement``).  Two rolling windows are
    surfaced — 24h and 7d — so the operator can spot drift between
    short-horizon noise and the steady-state signal without leaving
    the operator console.

    The predicted YES probability is ``predictions.kalshi_price`` (the
    market mid the producer wrote when the candidate was discovered),
    matching the rollup the task #644 spec called for: a brier score
    on ``kalshi_price`` vs ``settle_price`` plus a hit rate.  The
    deeper, model-vs-market joined report (with per-symbol slices and
    log-loss) lives behind the admin endpoint
    ``/api/admin/crypto/accuracy`` (see
    ``app.crypto.accuracy_report``); this rollup is intentionally
    lightweight and operator-visible.

    Each window is computed independently so a single failed query
    only nulls out one card rather than blanking the panel.
    """
    windows = []
    for hours in _CRYPTO_ACCURACY_WINDOWS_HOURS:
        windows.append(await _crypto_accuracy_window(hours))
    return {"league": "CRYPTO", "windows": windows}


async def get_recent_uw_signals(
    limit: int = 20,
    since_hours: int = 24,
    signal_type: str | None = None,
) -> list[dict[str, Any]]:
    """Most-recent Unusual Whales signals for the operator research feed.

    Reads from ``uw_signals`` (populated by :mod:`app.feeds.unusual_whales`)
    and projects a small, mobile-friendly subset of the raw payload —
    enough for an operator on the train to glance at flow alerts /
    dark-pool prints / per-ticker snapshots.
    """
    limit = max(1, min(int(limit), 100))
    since_hours = max(1, min(int(since_hours), 24 * 7))
    try:
        params: list[Any] = [since_hours, limit]
        type_filter = ""
        if signal_type:
            type_filter = "AND signal_type = $3"
            params.append(signal_type)
        rows = await db.fetch(
            f"""
            SELECT
              id,
              signal_type,
              ticker,
              to_char(occurred_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS occurred_at,
              to_char(fetched_at,  'YYYY-MM-DD"T"HH24:MI:SSZ') AS fetched_at,
              payload
              FROM uw_signals
             WHERE coalesce(occurred_at, fetched_at)
                   >= now() - ($1 || ' hours')::interval
               {type_filter}
             ORDER BY coalesce(occurred_at, fetched_at) DESC NULLS LAST
             LIMIT $2
            """,
            *params,
        )
        out: list[dict[str, Any]] = []
        for r in rows:
            payload = r["payload"] or {}
            if isinstance(payload, str):
                try:
                    import json as _json
                    payload = _json.loads(payload)
                except Exception:
                    payload = {}
            if not isinstance(payload, dict):
                payload = {}
            # Best-effort surface the most operator-relevant fields. UW
            # response shapes vary by endpoint; we tolerate misses.
            summary = (
                payload.get("alert_rule")
                or payload.get("rule")
                or payload.get("title")
                or payload.get("description")
                or payload.get("type")
                or payload.get("name")
            )
            size = (
                payload.get("size")
                or payload.get("volume")
                or payload.get("total_size")
                or payload.get("premium")
            )
            price = (
                payload.get("price")
                or payload.get("last_price")
                or payload.get("avg_price")
            )
            out.append({
                "id": r["id"],
                "signal_type": r["signal_type"],
                "ticker": r["ticker"],
                "occurred_at": r["occurred_at"],
                "fetched_at": r["fetched_at"],
                "summary": str(summary) if summary is not None else None,
                "size": size,
                "price": price,
            })
        return out
    except Exception as e:
        log.warning("recent_uw_signals failed: %s", e)
        return []


async def get_swarm_debate_by_id(debate_id: int) -> dict[str, Any] | None:
    """Fetch a single ``swarm_debates`` row by primary key.

    Returns ``None`` if no row exists. Same column projection as
    :func:`get_recent_swarm_debates` so the per-id endpoint and the
    list endpoint render identically on the mobile detail screen
    (which falls back to this when the debate has fallen off the
    cached recent feed, e.g. older than the top 25 or opened cold
    from a deep link / push notification).

    The ``analyst_verdicts`` and ``moderator_synthesis`` jsonb columns
    are returned raw — the calling endpoint is responsible for redacting
    them for sub-Pro callers via
    :func:`redact_swarm_debate_transcripts`.
    """
    try:
        row = await db.fetchrow(
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
            WHERE id = $1
            """,
            int(debate_id),
        )
        return dict(row) if row else None
    except Exception as e:
        log.warning("get_swarm_debate_by_id(%s) failed: %s", debate_id, e)
        return None


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
              game_context,
              to_char(created_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS created_at
            FROM swarm_debates
            ORDER BY created_at DESC NULLS LAST
            LIMIT $1
            """,
            limit,
        )
        out: list[dict[str, Any]] = []
        for r in rows:
            row = dict(r)
            # `game_context` is jsonb in the DB; asyncpg usually decodes
            # it to a dict, but older codecs may hand back a JSON string.
            # Normalize to a dict (or None) so the API contract matches
            # the OpenAPI schema and the frontend never has to JSON.parse
            # a stringified blob.
            ctx_raw = row.get("game_context")
            if isinstance(ctx_raw, str):
                try:
                    row["game_context"] = json.loads(ctx_raw)
                except Exception:
                    row["game_context"] = None
            elif ctx_raw is not None and not isinstance(ctx_raw, dict):
                row["game_context"] = None
            out.append(row)
        return out
    except Exception as e:
        log.warning("recent_swarm_debates failed: %s", e)
        return []


async def get_verticals_overview(days: int = 7) -> dict[str, Any]:
    """Aggregate per-vertical performance card backing
    ``/api/dashboard/verticals`` and the dedicated overview tab.

    For every vertical registered in :mod:`app.verticals` we report:

      * ``trades`` — settled + open trades in the rolling window
      * ``pnl`` — realized P&L in the window (USD)
      * ``win_rate`` — wins / settled
      * ``brier`` — most-recent 24h Brier from ``calibration_snapshots``
      * ``sharpe`` — annualized Sharpe of daily realized P&L in the
        window (``mean / stdev * sqrt(252)``).  ``None`` when fewer
        than 2 active trading days are present.
      * ``open_positions`` — count of open positions tagged with this
        vertical (``positions.vertical``)
      * ``edge_predicted_bps`` — average ``edge`` (price-bps) the bot
        predicted at entry, taken from ``trades.edge`` when available
      * ``edge_realized_bps`` — average realized edge per settled
        trade computed from ``trade_postmortems.actual_outcome`` and
        ``trades.price`` (i.e. ``(actual - p_entry) * 10_000`` per
        side-aligned leg)
      * ``last_cycle`` — most-recent ``trading_cycles`` row for this
        vertical (``started_at``, ``completed_at``, ``trades_placed``,
        ``picks_evaluated``, ``errors``)

    Plus a ``visibility`` payload with the admin-set hidden / pinned
    lists so the frontend can apply the operator override consistently.

    Every per-vertical sub-query is wrapped so a single failure
    doesn't blank the whole overview — the failing field is reported
    as ``None`` and the rest of the row populates normally.
    """
    days = max(1, min(int(days), 365))
    from . import verticals as _vertical_registry
    from . import runtime_settings as _runtime_settings

    visibility = await _runtime_settings.get_vertical_visibility()
    hidden = set(visibility.get("hidden") or [])
    pinned = list(visibility.get("pinned") or [])

    # Halts are a public read-only signal on this endpoint (task #302) so
    # non-admins viewing the dashboard can see which verticals are sat
    # out. The full halt-toggle controls remain admin-only on /system.
    try:
        halts = await _runtime_settings.get_vertical_halts()
    except Exception as e:
        log.warning("verticals_overview halts read failed: %s", e)
        halts = {}

    out: list[dict[str, Any]] = []
    for spec in _vertical_registry.all_specs():
        vertical = spec.name
        halt_meta = halts.get(vertical)
        entry: dict[str, Any] = {
            "vertical": vertical,
            "description": spec.description,
            "leagues": list(spec.leagues),
            "kelly_multiplier": spec.kelly_multiplier,
            "enabled": spec.enabled,
            "hidden": vertical in hidden,
            "pinned": vertical in pinned,
            "halted": halt_meta is not None,
            "halt": halt_meta,
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": None,
            "pnl": 0.0,
            "brier": None,
            "sharpe": None,
            "open_positions": 0,
            "edge_predicted_bps": None,
            "edge_realized_bps": None,
            "last_cycle": None,
        }

        # Trade aggregates (wins / pnl / win_rate / count).
        try:
            agg = await db.fetchrow(
                """
                SELECT count(*)::int                                    AS n,
                       coalesce(sum(pnl), 0)::float                     AS pnl,
                       sum((pnl > 0)::int)::int                         AS wins,
                       sum((pnl < 0)::int)::int                         AS losses,
                       (sum((pnl > 0)::int)::float
                        / nullif(count(*) filter (where pnl is not null), 0))
                                                                        AS win_rate,
                       avg(edge)::float                                 AS avg_edge
                  FROM trades
                 WHERE vertical = $1
                   AND executed_at >= now() - ($2::int || ' days')::interval
                """,
                vertical, days,
            )
            if agg:
                entry["trades"] = int(agg["n"] or 0)
                entry["pnl"] = float(agg["pnl"] or 0.0)
                entry["wins"] = int(agg["wins"] or 0)
                entry["losses"] = int(agg["losses"] or 0)
                if agg["win_rate"] is not None:
                    entry["win_rate"] = float(agg["win_rate"])
                if agg["avg_edge"] is not None:
                    # ``trades.edge`` is stored in price-fraction units
                    # (e.g. 0.04 = 4¢ on a $1 contract); convert to bps.
                    entry["edge_predicted_bps"] = float(agg["avg_edge"]) * 10_000.0
        except Exception as e:
            log.warning("verticals_overview agg %s: %s", vertical, e)

        # Latest 24h Brier from calibration_snapshots.
        try:
            entry["brier"] = await db.fetchval(
                """
                SELECT brier_score::float
                  FROM calibration_snapshots
                 WHERE vertical = $1 AND sub_category = '24h'
                 ORDER BY created_at DESC
                 LIMIT 1
                """,
                vertical,
            )
            if entry["brier"] is not None:
                entry["brier"] = float(entry["brier"])
        except Exception as e:
            log.warning("verticals_overview brier %s: %s", vertical, e)

        # Sharpe over daily realized P&L (annualized, 252 trading days).
        try:
            daily_rows = await db.fetch(
                """
                SELECT date_trunc('day', executed_at)::date AS d,
                       coalesce(sum(pnl), 0)::float          AS daily_pnl
                  FROM trades
                 WHERE vertical = $1
                   AND executed_at >= now() - ($2::int || ' days')::interval
                   AND pnl IS NOT NULL
                 GROUP BY d
                """,
                vertical, days,
            )
            samples = [float(r["daily_pnl"] or 0.0) for r in daily_rows]
            if len(samples) >= 2:
                import statistics as _stats
                import math as _math

                mean = _stats.fmean(samples)
                stdev = _stats.pstdev(samples)
                if stdev > 0:
                    entry["sharpe"] = round(mean / stdev * _math.sqrt(252.0), 4)
        except Exception as e:
            log.warning("verticals_overview sharpe %s: %s", vertical, e)

        # Open positions tagged with this vertical.
        try:
            n = await db.fetchval(
                """
                SELECT count(*)::int FROM positions
                 WHERE vertical = $1
                   AND coalesce(status,'open') = 'open'
                """,
                vertical,
            )
            entry["open_positions"] = int(n or 0)
        except Exception as e:
            log.warning("verticals_overview open_positions %s: %s", vertical, e)

        # Realized edge (bps) from settled postmortems.
        try:
            row = await db.fetchrow(
                """
                SELECT
                  avg(
                    CASE
                      WHEN lower(coalesce(t.side,'')) IN ('no','n','sell')
                        THEN ((1.0 - t.price) - (1.0 - pm.actual_outcome))
                      ELSE (pm.actual_outcome - t.price)
                    END
                  )::float                                          AS realized
                  FROM trades t
                  JOIN trade_postmortems pm
                    ON pm.trade_id = coalesce(t.trade_id, t.id::text)
                 WHERE t.vertical = $1
                   AND t.executed_at >= now() - ($2::int || ' days')::interval
                   AND pm.actual_outcome IS NOT NULL
                   AND t.price IS NOT NULL
                """,
                vertical, days,
            )
            if row and row["realized"] is not None:
                entry["edge_realized_bps"] = float(row["realized"]) * 10_000.0
        except Exception as e:
            log.warning("verticals_overview realized_edge %s: %s", vertical, e)

        # Most-recent cycle row for this vertical.
        try:
            cyc = await db.fetchrow(
                """
                SELECT id,
                       picks_evaluated,
                       trades_placed,
                       errors,
                       to_char(started_at,   'YYYY-MM-DD"T"HH24:MI:SSZ') AS started_at,
                       to_char(completed_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS completed_at
                  FROM trading_cycles
                 WHERE vertical = $1
                 ORDER BY started_at DESC NULLS LAST
                 LIMIT 1
                """,
                vertical,
            )
            if cyc:
                entry["last_cycle"] = dict(cyc)
        except Exception as e:
            log.warning("verticals_overview last_cycle %s: %s", vertical, e)

        out.append(entry)

    # Re-order: pinned verticals first (in pin order), then the rest in
    # registry order.
    if pinned:
        by_name = {e["vertical"]: e for e in out}
        ordered: list[dict[str, Any]] = []
        for name in pinned:
            e = by_name.pop(name, None)
            if e is not None:
                ordered.append(e)
        ordered.extend(e for e in out if e["vertical"] in by_name)
        out = ordered

    return {
        "days": days,
        "verticals": out,
        "visibility": {
            "hidden": sorted(hidden),
            "pinned": list(pinned),
        },
        "halts": halts,
    }


async def get_over_filled_orders_today() -> dict[str, Any]:
    """GTC orders that Kalshi over-filled in the current UTC day.

    Task #103 caps the rolled-up ``positions.quantity`` at the order's
    requested size and stamps ``positions.over_filled = TRUE`` whenever
    Kalshi delivers more contracts than the GTC asked for; this helper
    surfaces those rows so the operator console can flag reconciliation
    discrepancies at a glance.

    The "discarded" count is derived by joining ``order_fills`` (the
    immutable per-fill ledger) and subtracting the requested size from
    the sum of all fill ``count`` values for that order.  ``today`` is a
    UTC calendar day bounded by the most recent ``order_fills.received_at``
    so the badge always matches what the operator just saw in the logs.

    Returns ``{date_utc, count, items}``; on any DB failure the count is
    ``0`` and ``items`` is ``[]`` so the dashboard renders cleanly
    instead of erroring.
    """
    today_utc = datetime.now(tz=timezone.utc).date().isoformat()
    try:
        rows = await db.fetch(
            """
            SELECT p.order_id,
                   p.market_id,
                   p.side,
                   p.vertical,
                   p.filled_count                                   AS requested_count,
                   coalesce(sum(f.count), 0)::int                   AS filled_count,
                   greatest(coalesce(sum(f.count), 0) - p.filled_count, 0)::int
                                                                    AS discarded_count,
                   to_char(max(f.received_at), 'YYYY-MM-DD"T"HH24:MI:SSZ') AS last_fill_at
              FROM positions p
              JOIN order_fills f ON f.order_id = p.order_id
             WHERE p.over_filled = TRUE
               AND p.order_id IS NOT NULL
             GROUP BY p.order_id, p.market_id, p.side, p.vertical, p.filled_count
            HAVING max(f.received_at) >= date_trunc('day', now() AT TIME ZONE 'UTC')
             ORDER BY max(f.received_at) DESC
            """
        )
        items = [dict(r) for r in rows]
    except Exception as e:
        log.warning("over_filled_orders_today failed: %s", e)
        return {"date_utc": today_utc, "count": 0, "items": []}

    missing = {row["market_id"] for row in items if row.get("market_id")}
    titles: dict[str, str] = {}
    if missing:
        try:
            titles = await resolve_market_titles(missing)
        except Exception as e:
            log.warning("over_filled_orders title resolve failed: %s", e)
    for row in items:
        mid = row.get("market_id")
        row["market_title"] = titles.get(mid) if mid else None

    return {"date_utc": today_utc, "count": len(items), "items": items}


async def get_tournament_leaderboards(
    *,
    since_hours: int = 168,
    per_event_limit: int = 60,
    max_events: int = 20,
) -> dict[str, Any]:
    """Operator-facing snapshot of recent PGA / F1 tournament
    leaderboards (task #650).

    Lists every event with at least one row in
    ``sportradar_tournament_leaderboards`` in the trailing
    ``since_hours`` window (default 7 days), enriched with the league
    and display name from ``sportradar_schedules`` so the panel header
    can show ``PGA · The Masters`` instead of a raw event id.  Each
    event carries the position-ordered roster from
    :func:`app.feeds.sportradar.latest_tournament_leaderboard` plus the
    snapshot's ``fetched_at`` so the panel header can render a
    "fetched X minutes ago" badge.

    Returns ``{"events": [...]}`` — empty list when no snapshots have
    landed yet (operators see "no snapshots recorded yet" in the UI).
    Per-event failures are isolated so one missing snapshot doesn't
    blank the rest of the panel.
    """
    since_hours = max(1, min(720, int(since_hours)))
    per_event_limit = max(1, min(200, int(per_event_limit)))
    max_events = max(1, min(50, int(max_events)))

    try:
        rows = await db.fetch(
            """
            SELECT event_id,
                   max(fetched_at) AS fetched_at
              FROM sportradar_tournament_leaderboards
             WHERE fetched_at >= now() - make_interval(hours => $1)
             GROUP BY event_id
             ORDER BY max(fetched_at) DESC
             LIMIT $2
            """,
            since_hours,
            max_events,
        )
    except Exception as e:
        log.info("get_tournament_leaderboards distinct lookup skipped: %s", e)
        return {"events": []}

    if not rows:
        return {"events": []}

    event_ids = [str(r["event_id"]) for r in rows]
    schedule_meta: dict[str, dict[str, str]] = {}
    try:
        sched_rows = await db.fetch(
            """
            SELECT league, game_id, home_team, away_team, status
              FROM sportradar_schedules
             WHERE game_id = ANY($1::text[])
               AND upper(league) IN ('PGA', 'F1')
            """,
            event_ids,
        )
        for sr in sched_rows:
            schedule_meta[str(sr["game_id"])] = {
                "league": (sr["league"] or "").upper(),
                "name": (sr["home_team"] or sr["away_team"] or "").strip(),
                "status": (sr["status"] or "").lower(),
            }
    except Exception as e:
        log.debug(
            "get_tournament_leaderboards schedules lookup skipped: %s", e,
        )

    from .feeds.sportradar import latest_tournament_leaderboard

    events: list[dict[str, Any]] = []
    for r in rows:
        event_id = str(r["event_id"])
        try:
            snap = await latest_tournament_leaderboard(event_id)
        except Exception as e:  # noqa: BLE001
            log.debug(
                "get_tournament_leaderboards snapshot fetch failed for %s: %s",
                event_id, e,
            )
            snap = []

        meta = schedule_meta.get(event_id, {})
        fetched_at = r["fetched_at"]
        if hasattr(fetched_at, "isoformat"):
            fetched_at_iso = fetched_at.isoformat()
        elif fetched_at is None:
            fetched_at_iso = None
        else:
            fetched_at_iso = str(fetched_at)

        entries = [
            {
                "position": e.get("position"),
                "name": e.get("name") or "",
            }
            for e in snap[:per_event_limit]
            if e.get("name")
        ]
        events.append({
            "event_id": event_id,
            "league": meta.get("league") or "",
            "name": meta.get("name") or "",
            "status": meta.get("status") or "",
            "fetched_at": fetched_at_iso,
            "entries": entries,
        })
    return {"events": events}


async def get_tournament_leaderboard_timeline(
    event_id: str,
    *,
    max_snapshots: int | None = None,
    per_snapshot_limit: int = 200,
) -> dict[str, Any]:
    """Per-snapshot timeline for one PGA / F1 tournament so operators
    can scrub through how the field (and our priced Plackett-Luce
    probabilities) evolved over the life of the event (task #698).

    Reads every row of ``sportradar_tournament_leaderboards`` for the
    requested ``event_id``, groups them by ``fetched_at`` (one
    "snapshot" per unique timestamp), and for each snapshot returns
    the position-ordered roster joined with the Plackett-Luce
    probability the orchestrator would price off — derived through the
    same :func:`derive_field_ratings_from_leaderboard` →
    :func:`plackett_luce_from_mapping` chain the live cycle uses, so
    the timeline reproduces exactly what the live pipeline saw.

    Snapshots whose entries are too thin to drive Plackett-Luce
    (fewer than 2 named entrants) still surface in the timeline so
    operators can see the gap, but their entries carry
    ``probability=None`` rather than a degenerate uniform prior.

    The ``sportradar_schedules`` join surfaces the league + display
    name so the panel header can read ``PGA · The Masters`` instead
    of a raw event id.

    By default the helper returns *every* saved snapshot for the
    event so operators can scrub the full timeline — the whole point
    of the panel is "show me how the field evolved over the life of
    the tournament", and a silent truncation would betray that
    promise.  ``max_snapshots`` is left available as an explicit cap
    for callers that want to keep the trailing portion of a very long
    history; pass ``None`` (the default) for unbounded.

    Returns ``{event_id, league, name, status, snapshots: [...]}``
    with ``snapshots=[]`` when no history has been recorded yet
    (operators see "no snapshots recorded yet" in the UI).  The
    helper never raises: a missing table or DB hiccup degrades to the
    empty shape so a fresh dev DB doesn't blank the panel.
    """
    if not event_id:
        return {
            "event_id": "",
            "league": "",
            "name": "",
            "status": "",
            "snapshots": [],
        }

    if max_snapshots is not None:
        max_snapshots = max(1, int(max_snapshots))
    per_snapshot_limit = max(1, min(500, int(per_snapshot_limit)))

    event_id_str = str(event_id)
    try:
        rows = await db.fetch(
            """
            SELECT position, name, fetched_at
              FROM sportradar_tournament_leaderboards
             WHERE event_id = $1
             ORDER BY fetched_at ASC, position NULLS LAST, name
            """,
            event_id_str,
        )
    except Exception as e:
        log.info(
            "get_tournament_leaderboard_timeline lookup skipped for %s: %s",
            event_id_str, e,
        )
        return {
            "event_id": event_id_str,
            "league": "",
            "name": "",
            "status": "",
            "snapshots": [],
        }

    meta = {"league": "", "name": "", "status": ""}
    try:
        sched_rows = await db.fetch(
            """
            SELECT league, home_team, away_team, status
              FROM sportradar_schedules
             WHERE game_id = $1
               AND upper(league) IN ('PGA', 'F1')
             LIMIT 1
            """,
            event_id_str,
        )
        if sched_rows:
            sr = sched_rows[0]
            meta["league"] = (sr["league"] or "").upper()
            meta["name"] = (sr["home_team"] or sr["away_team"] or "").strip()
            meta["status"] = (sr["status"] or "").lower()
    except Exception as e:
        log.debug(
            "get_tournament_leaderboard_timeline schedule lookup skipped "
            "for %s: %s", event_id_str, e,
        )

    if not rows:
        return {
            "event_id": event_id_str,
            "league": meta["league"],
            "name": meta["name"],
            "status": meta["status"],
            "snapshots": [],
        }

    from .sports import derive_field_ratings_from_leaderboard
    from .predictor_n_way import plackett_luce_from_mapping

    grouped: dict[Any, list[dict[str, Any]]] = {}
    order: list[Any] = []
    for r in rows:
        ts = r["fetched_at"]
        if ts not in grouped:
            grouped[ts] = []
            order.append(ts)
        grouped[ts].append({"position": r["position"], "name": r["name"]})

    # By default operators see *every* saved snapshot — the panel's
    # whole purpose is "let me scrub the full history of this
    # tournament", and a silent truncation here would betray that.
    # An explicit ``max_snapshots`` keeps the trailing window for
    # callers that want a bounded slice (e.g. a future "last 24h"
    # toggle).
    if max_snapshots is not None and len(order) > max_snapshots:
        order = order[-max_snapshots:]

    snapshots: list[dict[str, Any]] = []
    for ts in order:
        entries_raw = grouped[ts]
        # Drop blank-name rows so the panel never renders an empty
        # "name" cell — mirrors get_tournament_leaderboards.
        entries_clean = [
            {"position": e["position"], "name": e["name"]}
            for e in entries_raw if (e["name"] or "").strip()
        ]

        ratings = derive_field_ratings_from_leaderboard(entries_clean)
        if len(ratings) >= 2:
            probs = plackett_luce_from_mapping(ratings)
        else:
            probs = {}

        capped = entries_clean[:per_snapshot_limit]
        rendered = [
            {
                "position": e["position"],
                "name": e["name"],
                "probability": probs.get(e["name"]),
            }
            for e in capped
        ]

        if hasattr(ts, "isoformat"):
            ts_iso = ts.isoformat()
        elif ts is None:
            ts_iso = None
        else:
            ts_iso = str(ts)

        snapshots.append({"fetched_at": ts_iso, "entries": rendered})

    return {
        "event_id": event_id_str,
        "league": meta["league"],
        "name": meta["name"],
        "status": meta["status"],
        "snapshots": snapshots,
    }


async def get_per_league_calibration() -> dict[str, Any]:
    """Most-recent per-league calibrator snapshots for the dashboard.

    Reads ``calibration_snapshots`` rows where ``vertical='sports'`` and
    ``sub_category LIKE 'per_league:%'``, picks the latest row per
    league, and returns a small payload the operator dashboard can plot
    directly: a sampled raw→calibrated curve plus the most recent
    Brier-before / Brier-after pair carried in the row's ``config``
    JSONB (written by ``calibrators._persist_calibrator``).
    """
    from . import calibrators as _calibrators

    try:
        rows = await db.fetch(
            """
            SELECT DISTINCT ON (sub_category)
                   sub_category,
                   sample_count,
                   brier_score::float           AS brier_score,
                   calibrator_state,
                   config,
                   drift_status,
                   to_char(created_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS fit_at
              FROM calibration_snapshots
             WHERE vertical = 'sports'
               AND sub_category LIKE 'per_league:%'
               AND calibrator_state IS NOT NULL
               AND COALESCE(drift_status->>'status', '') <> 'rolled_back'
             ORDER BY sub_category, created_at DESC
            """
        )
    except Exception as e:
        log.warning("per_league_calibration query failed: %s", e)
        return {"items": [], "supported": list(_calibrators.SUPPORTED_LEAGUES),
                "error": str(e)[:240]}

    import json as _json

    def _parse_jsonb(v: Any) -> dict[str, Any]:
        if v is None:
            return {}
        if isinstance(v, dict):
            return v
        if isinstance(v, (str, bytes, bytearray)):
            try:
                parsed = _json.loads(v)
                return parsed if isinstance(parsed, dict) else {}
            except (ValueError, TypeError):
                return {}
        return {}

    by_league: dict[str, dict[str, Any]] = {}
    for r in rows:
        sub = str(r["sub_category"] or "")
        if not sub.startswith("per_league:"):
            continue
        league = sub.split(":", 1)[1].strip().upper()
        if league not in _calibrators.SUPPORTED_LEAGUES:
            continue
        state = _parse_jsonb(r["calibrator_state"])
        cfg = _parse_jsonb(r["config"])
        kind = str(state.get("type") or "").lower()
        if kind not in ("isotonic", "platt"):
            continue

        # Sample 21 evenly-spaced raw probabilities and run them through
        # the same apply path the live trader uses, so the chart line is
        # exactly what the production calibrator would emit.
        steps = 21
        curve: list[dict[str, float]] = []
        for i in range(steps):
            p_raw = i / (steps - 1)
            try:
                if kind == "isotonic":
                    p_cal = _calibrators._apply_isotonic(p_raw, state)
                else:
                    p_cal = _calibrators._apply_platt(p_raw, state)
            except Exception:
                p_cal = p_raw
            p_cal = max(0.0, min(1.0, float(p_cal)))
            curve.append({"p_raw": round(p_raw, 4),
                          "p_cal": round(p_cal, 6)})

        brier_before = cfg.get("brier_before")
        brier_after = cfg.get("brier_after")
        if brier_after is None and r["brier_score"] is not None:
            brier_after = float(r["brier_score"])

        knots = None
        if kind == "isotonic":
            xs = state.get("x") or []
            ys = state.get("y") or []
            if isinstance(xs, list) and isinstance(ys, list) and len(xs) == len(ys):
                knots = {
                    "x": [float(v) for v in xs],
                    "y": [float(v) for v in ys],
                }

        by_league[league] = {
            "league": league,
            "type": kind,
            "sample_count": int(r["sample_count"] or 0),
            "fit_at": r["fit_at"],
            "brier_before": (
                float(brier_before) if brier_before is not None else None
            ),
            "brier_after": (
                float(brier_after) if brier_after is not None else None
            ),
            "curve": curve,
            "knots": knots,
        }

    items: list[dict[str, Any]] = []
    for lg in _calibrators.SUPPORTED_LEAGUES:
        if lg in by_league:
            items.append(by_league[lg])
        else:
            items.append({
                "league": lg,
                "type": None,
                "sample_count": 0,
                "fit_at": None,
                "brier_before": None,
                "brier_after": None,
                "curve": [],
                "knots": None,
            })

    return {
        "items": items,
        "supported": list(_calibrators.SUPPORTED_LEAGUES),
        "fitted_count": sum(1 for it in items if it["type"] is not None),
    }


async def get_recent_bankroll_events(
    limit: int = 200,
    *,
    exchange: str = "kalshi",
) -> dict[str, Any]:
    """Recent rows from ``bankroll_events`` plus the current balance.

    The continuous Kalshi balance sync (see ``app.bankroll_sync``) writes
    a row every time the live exchange balance moves; this helper hands
    that feed to the operator dashboard so the equity sparkline and the
    "live balance" tile both come from the same authoritative table.

    Returns ``{exchange, current_balance_cents, items, last_event_at}``;
    on any DB failure ``items`` is ``[]`` and ``current_balance_cents``
    is ``None`` so the dashboard renders cleanly instead of erroring.
    """
    limit = max(1, min(int(limit), 500))
    try:
        rows = await db.fetch(
            """
            SELECT id,
                   to_char(occurred_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS occurred_at,
                   exchange,
                   reason,
                   delta_cents,
                   balance_cents
              FROM bankroll_events
             WHERE exchange = $1
             ORDER BY id DESC
             LIMIT $2
            """,
            exchange, limit,
        )
        items = [dict(r) for r in rows]
    except Exception as e:
        log.warning("recent_bankroll_events failed: %s", e)
        return {
            "exchange": exchange,
            "current_balance_cents": None,
            "last_event_at": None,
            "items": [],
        }

    # Items are returned newest-first; the current balance is just the
    # most recent row's running balance.
    current = items[0]["balance_cents"] if items else None
    last_at = items[0]["occurred_at"] if items else None
    # Reverse so the dashboard sparkline can iterate chronologically
    # without re-sorting.
    items.reverse()
    return {
        "exchange": exchange,
        "current_balance_cents": int(current) if current is not None else None,
        "last_event_at": last_at,
        "items": items,
    }


async def get_crypto_ohlcv_retention_status() -> dict[str, Any]:
    """Operator-facing snapshot of the ``crypto_ohlcv`` retention sweep.

    Wraps :func:`app.crypto_ohlcv_retention.state` and adds two derived
    fields the dashboard needs to flag a misbehaving loop without
    re-implementing the same logic in the frontend:

    * ``seconds_since_last_tick`` — wall-clock seconds since the last
      successful sweep, or ``None`` when the loop has never ticked.
    * ``health`` — one of:

        - ``"disabled"`` — the loop is intentionally off
          (``AXIOM_CRYPTO_OHLCV_RETENTION_ENABLED=0``).
        - ``"error"`` — the most recent sweep raised an exception.
        - ``"stale"`` — the loop is meant to be running but either
          hasn't ticked yet (after a 2-interval grace) or its last
          tick is older than ``2 * interval_seconds``.
        - ``"ok"`` — running and ticked within the expected window.

    Always returns a dict; never raises.
    """
    import time

    from . import crypto_ohlcv_retention as _cor

    snap = _cor.state()

    last_tick = snap.get("last_tick_at")
    started_at = snap.get("started_at")
    interval = float(snap.get("interval_seconds") or _cor.DEFAULT_INTERVAL_SECONDS)
    grace = max(2.0 * interval, 60.0)

    now = time.time()
    seconds_since = (now - float(last_tick)) if last_tick else None

    if not snap.get("enabled", True):
        health = "disabled"
    elif snap.get("last_error"):
        health = "error"
    elif last_tick is None:
        # Never ticked — only flag once we've been up long enough that
        # a healthy loop *should* have completed at least one sweep.
        if started_at and (now - float(started_at)) > grace:
            health = "stale"
        elif not snap.get("running"):
            health = "stale"
        else:
            health = "ok"
    elif seconds_since is not None and seconds_since > grace:
        health = "stale"
    else:
        health = "ok"

    return {
        **snap,
        "seconds_since_last_tick": seconds_since,
        "health": health,
    }


async def get_crypto_backtest_retention_status() -> dict[str, Any]:
    """Operator-facing snapshot of the ``crypto_backtest_runs`` retention sweep.

    Wraps :func:`app.crypto_backtest_retention.state` and adds two
    derived fields the dashboard needs to flag a misbehaving loop
    without re-implementing the same logic in the frontend:

    * ``seconds_since_last_tick`` — wall-clock seconds since the last
      successful sweep, or ``None`` when the loop has never ticked.
    * ``health`` — one of:

        - ``"disabled"`` — the loop is intentionally off
          (``AXIOM_CRYPTO_BACKTEST_RETENTION_ENABLED=0``).
        - ``"error"`` — the most recent sweep raised an exception.
        - ``"stale"`` — the loop is meant to be running but either
          hasn't ticked yet (after a 2-interval grace) or its last
          tick is older than ``2 * interval_seconds``.
        - ``"ok"`` — running and ticked within the expected window.

    Always returns a dict; never raises.
    """
    import time

    from . import crypto_backtest_retention as _cbr

    snap = _cbr.state()

    last_tick = snap.get("last_tick_at")
    started_at = snap.get("started_at")
    interval = float(snap.get("interval_seconds") or _cbr.DEFAULT_INTERVAL_SECONDS)
    grace = max(2.0 * interval, 60.0)

    now = time.time()
    seconds_since = (now - float(last_tick)) if last_tick else None

    if not snap.get("enabled", True):
        health = "disabled"
    elif snap.get("last_error"):
        health = "error"
    elif last_tick is None:
        # Never ticked — only flag once we've been up long enough that
        # a healthy loop *should* have completed at least one sweep.
        if started_at and (now - float(started_at)) > grace:
            health = "stale"
        elif not snap.get("running"):
            health = "stale"
        else:
            health = "ok"
    elif seconds_since is not None and seconds_since > grace:
        health = "stale"
    else:
        health = "ok"

    return {
        **snap,
        "seconds_since_last_tick": seconds_since,
        "health": health,
    }


async def get_sportradar_leaderboards_retention_status() -> dict[str, Any]:
    """Operator-facing snapshot of the ``sportradar_tournament_leaderboards`` retention sweep.

    Wraps :func:`app.sportradar_leaderboards_retention.state` and adds
    two derived fields the dashboard needs to flag a misbehaving loop
    without re-implementing the same logic in the frontend:

    * ``seconds_since_last_tick`` — wall-clock seconds since the last
      successful sweep, or ``None`` when the loop has never ticked.
    * ``health`` — one of:

        - ``"disabled"`` — the loop is intentionally off
          (``AXIOM_SPORTRADAR_LEADERBOARDS_RETENTION_ENABLED=0``).
        - ``"error"`` — the most recent sweep raised an exception.
        - ``"stale"`` — the loop is meant to be running but either
          hasn't ticked yet (after a 2-interval grace) or its last
          tick is older than ``2 * interval_seconds``.
        - ``"ok"`` — running and ticked within the expected window.

    Always returns a dict; never raises.
    """
    import time

    from . import sportradar_leaderboards_retention as _srlr

    snap = _srlr.state()

    last_tick = snap.get("last_tick_at")
    started_at = snap.get("started_at")
    interval = float(snap.get("interval_seconds") or _srlr.DEFAULT_INTERVAL_SECONDS)
    grace = max(2.0 * interval, 60.0)

    now = time.time()
    seconds_since = (now - float(last_tick)) if last_tick else None

    if not snap.get("enabled", True):
        health = "disabled"
    elif snap.get("last_error"):
        health = "error"
    elif last_tick is None:
        if started_at and (now - float(started_at)) > grace:
            health = "stale"
        elif not snap.get("running"):
            health = "stale"
        else:
            health = "ok"
    elif seconds_since is not None and seconds_since > grace:
        health = "stale"
    else:
        health = "ok"

    return {
        **snap,
        "seconds_since_last_tick": seconds_since,
        "health": health,
    }


async def get_nfl_exposure() -> dict[str, Any]:
    """NFL per-league + per-game + Sunday-window exposure with cap headroom.

    Sources its numbers from the orchestrator's
    :func:`app.orchestrator.cycle._open_position_aggregates` so the
    dashboard panel is guaranteed to show the same buckets the risk gates
    are about to enforce, and pulls cap percentages straight from
    :data:`app.sports.leagues.LEAGUE_CONFIGS` so a knob change in code
    flows through without a UI rebuild.

    Each bucket carries:

    * ``open_usd`` — current open notional landing in this bucket
    * ``cap_pct`` / ``cap_usd`` — configured cap (None when no cap)
    * ``headroom_usd`` — ``max(0, cap_usd - open_usd)``
    * ``utilization`` — ``open_usd / cap_usd`` (None when no cap)
    * ``warn`` — True iff utilization >= 0.80 (the operator threshold the
      gate's "approaching the cap" warning fires on)

    The per-matchup list is sorted heaviest-first so the operator sees
    the matchup most likely to block the next bet at the top.
    """
    from .orchestrator.cycle import (
        _open_position_aggregates,
        _latest_bankroll,
    )
    from .sports.leagues import get_league_config

    league = "NFL"
    cfg = get_league_config(league)
    try:
        bankroll = await _latest_bankroll()
    except Exception as e:
        log.warning("nfl exposure bankroll read failed: %s", e)
        bankroll = None
    try:
        (
            _open_n,
            _open_notional,
            _per_vertical,
            per_league,
            per_matchup,
            per_sunday,
            _per_event,
        ) = await _open_position_aggregates()
    except Exception as e:
        log.warning("nfl exposure aggregate read failed: %s", e)
        per_league, per_matchup, per_sunday = {}, {}, {}

    league_open = float(per_league.get(league, 0.0))
    sunday_open = float(per_sunday.get(league, 0.0))
    matchups: list[tuple[str, float]] = []
    prefix = f"{league}:"
    for k, v in per_matchup.items():
        if k.startswith(prefix):
            matchups.append((k[len(prefix):], float(v)))
    matchups.sort(key=lambda r: -r[1])

    def _bucket(open_usd: float, cap_pct: float | None) -> dict[str, Any]:
        if cap_pct is None:
            return {
                "open_usd": open_usd,
                "cap_pct": None,
                "cap_usd": None,
                "headroom_usd": None,
                "utilization": None,
                "warn": False,
            }
        if bankroll is None or bankroll <= 0:
            return {
                "open_usd": open_usd,
                "cap_pct": cap_pct,
                "cap_usd": None,
                "headroom_usd": None,
                "utilization": None,
                "warn": False,
            }
        cap_usd = float(bankroll) * float(cap_pct)
        util = open_usd / cap_usd if cap_usd > 0 else None
        return {
            "open_usd": open_usd,
            "cap_pct": cap_pct,
            "cap_usd": cap_usd,
            "headroom_usd": max(0.0, cap_usd - open_usd),
            "utilization": util,
            "warn": util is not None and util >= 0.80,
        }

    per_league_bucket = _bucket(
        league_open, cfg.allocation_pct if cfg else None
    )
    sunday_bucket = _bucket(
        sunday_open, cfg.sunday_window_cap_pct if cfg else None
    )
    per_game_cap_pct = cfg.per_game_cap_pct if cfg else None
    per_matchup_buckets: list[dict[str, Any]] = []
    for matchup, open_usd in matchups:
        b = _bucket(open_usd, per_game_cap_pct)
        b["matchup"] = matchup
        per_matchup_buckets.append(b)

    # Attach the cap-warn dispatch history so the operator can tell
    # "the on-call already heard about this bucket" from "this bucket
    # is silently quiet".  Failure here is advisory — the rest of the
    # panel still renders.
    history = await _safe_recent_cap_warnings()
    by_bucket = history.get("by_bucket") or {}
    last_cycle = history.get("last_cycle")

    def _attach(bucket: dict[str, Any], key: str) -> None:
        bucket["recent_alert"] = _build_recent_alert(
            by_bucket.get(key), key, last_cycle
        )

    _attach(per_league_bucket, f"{league}:per_league")
    _attach(sunday_bucket, f"{league}:sunday_window")
    for m in per_matchup_buckets:
        _attach(m, f"{league}:per_matchup:{m['matchup']}")

    return {
        "league": league,
        "bankroll_usd": bankroll,
        "warn_threshold": 0.80,
        "per_league": per_league_bucket,
        "sunday_window": sunday_bucket,
        "per_matchup": per_matchup_buckets,
        "recent_warnings": _summarize_recent_warnings(history),
        # Surface the raw aggregates too so a roundtrip test can prove
        # the panel's buckets were derived from `_open_position_aggregates`
        # rather than re-queried with a different SQL.
        "raw": {
            "per_league_usd": float(per_league.get(league, 0.0)),
            "per_sunday_window_usd": float(per_sunday.get(league, 0.0)),
            "per_matchup_usd": {m: float(v) for m, v in matchups},
        },
    }


async def _safe_recent_cap_warnings() -> dict[str, Any]:
    """Wrap :func:`cap_warnings.recent_dispatch_history` so a notifications
    failure cannot break the exposure panel."""
    try:
        from .notifications import cap_warnings as _cap
        return await _cap.recent_dispatch_history()
    except Exception as e:  # pragma: no cover — advisory
        log.warning("recent_dispatch_history failed: %s", e)
        return {"alert_type": "nfl_cap_warn", "last_cycle": None, "by_bucket": {}}


def _build_recent_alert(
    row: dict[str, Any] | None,
    bucket_key: str,
    last_cycle: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Compose the per-bucket "recently paged" record consumed by the
    operator dashboard.

    Returns ``None`` only when the bucket has never paged *and* the
    most recent cycle did not touch it — that way the panel can render
    a placeholder instead of a stale label.
    """
    last_sent_at = row.get("last_sent_at") if row else None
    expires_at = row.get("expires_at") if row else None
    ttl_remaining = row.get("ttl_seconds_remaining") if row else None

    fired_last_cycle = bool(
        last_cycle and bucket_key in (last_cycle.get("fired") or [])
    )
    suppressed_last_cycle = bool(
        last_cycle and bucket_key in (last_cycle.get("suppressed") or [])
    )

    if (
        last_sent_at is None
        and not fired_last_cycle
        and not suppressed_last_cycle
    ):
        return None

    return {
        "last_sent_at": last_sent_at,
        "expires_at": expires_at,
        "ttl_seconds_remaining": ttl_remaining,
        "fired_last_cycle": fired_last_cycle,
        "suppressed_last_cycle": suppressed_last_cycle,
    }


def _summarize_recent_warnings(history: dict[str, Any]) -> dict[str, Any]:
    """Top-level summary the dashboard renders alongside the per-bucket
    detail — "last cap-warn check ran 12s ago, fired 1, suppressed 2"."""
    last_cycle = history.get("last_cycle")
    by_bucket = history.get("by_bucket") or {}
    if last_cycle is None:
        return {
            "ran_at": None,
            "threshold": None,
            "ttl_seconds": None,
            "fired_count": 0,
            "suppressed_count": 0,
            "tracked_buckets": len(by_bucket),
        }
    return {
        "ran_at": last_cycle.get("ran_at"),
        "threshold": last_cycle.get("threshold"),
        "ttl_seconds": last_cycle.get("ttl_seconds"),
        "fired_count": len(last_cycle.get("fired") or []),
        "suppressed_count": len(last_cycle.get("suppressed") or []),
        "tracked_buckets": len(by_bucket),
    }


async def get_leagues_exposure() -> dict[str, Any]:
    """Per-league exposure with cap headroom for every league with an
    open position.

    Generalises :func:`get_nfl_exposure` to the rest of
    :data:`app.sports.leagues.LEAGUE_CONFIGS`.  Sources every bucket from
    the same orchestrator snapshot
    (:func:`app.orchestrator.cycle._open_position_aggregates`) so the
    dashboard panel cannot drift from the risk gates' view of cap
    headroom.

    Each entry in ``leagues`` carries:

    * ``league`` — canonical uppercase league name
    * ``per_league`` — bucket against ``allocation_pct`` (always present)
    * ``sunday_window`` — bucket against ``sunday_window_cap_pct`` (only
      when the league configures a Sunday window — currently NFL)
    * ``per_event`` — list of event buckets against ``per_event_cap_pct``
      (only when the league configures a per-event cap — combat /
      individual sports: UFC, ATP, WTA, F1, PGA, ELECTIONS,
      EVENTS_ECON, ENTERTAINMENT)
    * ``per_matchup`` — list of matchup buckets against
      ``per_game_cap_pct``

    The returned ``leagues`` list is sorted by per-league utilization
    (highest first) so the operator sees the league closest to its cap
    at the top.  Leagues with zero open exposure are filtered out so the
    panel doesn't churn through 20 empty cards.
    """
    from .orchestrator.cycle import (
        _open_position_aggregates,
        _latest_bankroll,
    )
    from .sports.leagues import LEAGUE_CONFIGS, get_league_config

    try:
        bankroll = await _latest_bankroll()
    except Exception as e:
        log.warning("leagues exposure bankroll read failed: %s", e)
        bankroll = None
    try:
        (
            _open_n,
            _open_notional,
            _per_vertical,
            per_league,
            per_matchup,
            per_sunday,
            per_event,
        ) = await _open_position_aggregates()
    except Exception as e:
        log.warning("leagues exposure aggregate read failed: %s", e)
        per_league, per_matchup, per_sunday, per_event = {}, {}, {}, {}

    def _bucket(open_usd: float, cap_pct: float | None) -> dict[str, Any]:
        if cap_pct is None:
            return {
                "open_usd": open_usd,
                "cap_pct": None,
                "cap_usd": None,
                "headroom_usd": None,
                "utilization": None,
                "warn": False,
            }
        if bankroll is None or bankroll <= 0:
            return {
                "open_usd": open_usd,
                "cap_pct": cap_pct,
                "cap_usd": None,
                "headroom_usd": None,
                "utilization": None,
                "warn": False,
            }
        cap_usd = float(bankroll) * float(cap_pct)
        util = open_usd / cap_usd if cap_usd > 0 else None
        return {
            "open_usd": open_usd,
            "cap_pct": cap_pct,
            "cap_usd": cap_usd,
            "headroom_usd": max(0.0, cap_usd - open_usd),
            "utilization": util,
            "warn": util is not None and util >= 0.80,
        }

    # Union of leagues we know about (config) and leagues with open
    # exposure (snapshot).  This way an unconfigured league with an open
    # position still surfaces as "open notional, no cap" rather than
    # silently disappearing — and a configured league with no open
    # position never spams the panel.
    candidate_leagues: set[str] = set(LEAGUE_CONFIGS.keys())
    candidate_leagues.update(str(k).upper() for k in per_league.keys() if k)

    leagues_out: list[dict[str, Any]] = []
    for league in candidate_leagues:
        cfg = get_league_config(league)
        league_open = float(per_league.get(league, 0.0))
        sunday_open = float(per_sunday.get(league, 0.0))

        prefix = f"{league}:"
        matchups: list[tuple[str, float]] = sorted(
            (
                (k[len(prefix):], float(v))
                for k, v in per_matchup.items()
                if k.startswith(prefix)
            ),
            key=lambda r: -r[1],
        )
        events: list[tuple[str, float]] = sorted(
            (
                (k[len(prefix):], float(v))
                for k, v in per_event.items()
                if k.startswith(prefix)
            ),
            key=lambda r: -r[1],
        )

        # Skip leagues with no open exposure anywhere — keeps the panel
        # focused on what the operator actually needs to watch.
        if (
            league_open == 0.0
            and sunday_open == 0.0
            and not matchups
            and not events
        ):
            continue

        per_game_cap_pct = cfg.per_game_cap_pct if cfg else None
        per_matchup_buckets: list[dict[str, Any]] = []
        for matchup, open_usd in matchups:
            b = _bucket(open_usd, per_game_cap_pct)
            b["matchup"] = matchup
            per_matchup_buckets.append(b)

        entry: dict[str, Any] = {
            "league": league,
            "per_league": _bucket(
                league_open, cfg.allocation_pct if cfg else None
            ),
            "per_matchup": per_matchup_buckets,
        }
        if cfg and cfg.sunday_window_cap_pct is not None:
            entry["sunday_window"] = _bucket(sunday_open, cfg.sunday_window_cap_pct)
        if cfg and cfg.per_event_cap_pct is not None:
            per_event_cap_pct = cfg.per_event_cap_pct
            event_buckets: list[dict[str, Any]] = []
            for event_id, open_usd in events:
                b = _bucket(open_usd, per_event_cap_pct)
                b["event_id"] = event_id
                event_buckets.append(b)
            entry["per_event"] = event_buckets
        leagues_out.append(entry)

    # Heaviest-utilization league first, falling back to raw open notional
    # so leagues with no cap configured (utilization == None) sort by
    # exposure rather than landing in arbitrary dict order.
    def _sort_key(e: dict[str, Any]) -> tuple[float, float]:
        pl = e["per_league"]
        util = pl.get("utilization")
        return (
            -(float(util) if util is not None else -1.0),
            -float(pl.get("open_usd") or 0.0),
        )

    leagues_out.sort(key=_sort_key)

    # Attach the cap-warn dispatch history per league bucket — currently
    # only NFL has a background warn dispatcher, but the field is keyed
    # by `{league}:{bucket}` so the same code path picks up future
    # dispatchers (e.g. NBA per-game) without a UI rebuild.
    history = await _safe_recent_cap_warnings()
    by_bucket = history.get("by_bucket") or {}
    last_cycle = history.get("last_cycle")
    for entry in leagues_out:
        league = entry["league"]
        entry["per_league"]["recent_alert"] = _build_recent_alert(
            by_bucket.get(f"{league}:per_league"),
            f"{league}:per_league",
            last_cycle,
        )
        if "sunday_window" in entry:
            entry["sunday_window"]["recent_alert"] = _build_recent_alert(
                by_bucket.get(f"{league}:sunday_window"),
                f"{league}:sunday_window",
                last_cycle,
            )
        for m in entry.get("per_matchup", []) or []:
            key = f"{league}:per_matchup:{m['matchup']}"
            m["recent_alert"] = _build_recent_alert(
                by_bucket.get(key), key, last_cycle
            )
        for ev in entry.get("per_event", []) or []:
            key = f"{league}:per_event:{ev['event_id']}"
            ev["recent_alert"] = _build_recent_alert(
                by_bucket.get(key), key, last_cycle
            )

    return {
        "bankroll_usd": bankroll,
        "warn_threshold": 0.80,
        "leagues": leagues_out,
        "recent_warnings": _summarize_recent_warnings(history),
    }


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
        # `game_context` carries the per-debate audit blob — including
        # the Plackett-Luce per-entrant probability sketch that powers
        # the analyst tournament view.  Treat it like the rest of the
        # transcript and strip it for sub-Pro callers.
        clone.pop("game_context", None)
        clone["redacted"] = True
        out.append(clone)
    return out


# ---------------------------------------------------------------------------
# Per-entrant outright market resolution (analyst tournament view)
# ---------------------------------------------------------------------------
#
# The Plackett-Luce sketch we persist on tournament debate rows lists every
# entrant in the field along with its modelled win probability.  The analyst
# UI on the swarm-debates panel renders that as a per-entrant table — but
# only the *traded* entrant has a kalshi_market_id stamped on the debate.
# To let analysts validate the model's relative pricing across the field
# (and spot mispricings on legs we did not trade) we resolve each rival
# entrant to its own outright Kalshi market, surface the latest cached mid
# price, and build the public-site URL so the row can route there on click.

def _kalshi_public_market_url(market_id: str | None) -> str | None:
    """Build the public Kalshi market URL for a market ticker.

    Kalshi's public site uses ``<series>/<event>`` paths under
    ``https://kalshi.com/markets/`` — e.g. ``KXATPRG-25SEP-ALCARAZ``
    lives at ``https://kalshi.com/markets/kxatprg/25sep``.  Returns
    ``None`` when the input is empty so callers can decide whether to
    render a non-clickable row.
    """
    if not market_id:
        return None
    parts = market_id.split("-")
    series = parts[0].lower()
    if not series:
        return None
    if len(parts) >= 2 and parts[1]:
        return f"https://kalshi.com/markets/{series}/{parts[1].lower()}"
    return f"https://kalshi.com/markets/{series}"


def _normalize_entrant_token(name: str | None) -> str:
    """Strip non-alphanumerics + lowercase for fuzzy market title matching.

    Kalshi titles often reformat the entrant's name (extra punctuation,
    middle initials, capitalization) — collapsing both sides to their
    alnum projection lets ``Carlos Alcaraz`` match
    ``Will Carlos Alcaraz win Roland-Garros?`` without false positives.
    """
    if not name:
        return ""
    return "".join(c for c in name.lower() if c.isalnum())


async def resolve_entrant_outright_markets(
    traded_market_id: str | None,
    entrants: list[str],
    *,
    traded_entrant: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Resolve each entrant in a Plackett-Luce field to its outright leg.

    For every name in ``entrants`` we try to find the sibling outright
    Kalshi market under the same event prefix as ``traded_market_id``
    (everything before the last ``-`` segment).  Sibling tickers come
    from the cached ``market_title_cache`` rows whose ticker shares the
    event prefix; we then pick the one whose human-readable title (or
    ticker suffix as a fallback) contains the entrant's name.

    The traded entrant is always force-resolved to ``traded_market_id``
    when ``traded_entrant`` is supplied — even if the title isn't yet
    cached for that ticker — so the analyst can always pivot back to
    the traded leg from the table.

    Returns ``{entrant: {kalshi_market_id, mid_cents, kalshi_url}}``;
    entrants that can't be resolved are simply omitted so the frontend
    falls back to a non-clickable row with no price.
    """
    out: dict[str, dict[str, Any]] = {}
    if not traded_market_id or not entrants:
        return out

    parts = traded_market_id.split("-")
    if len(parts) < 2:
        return out
    event_prefix = "-".join(parts[:-1]) + "-"

    siblings: list[tuple[str, str]] = []
    try:
        rows = await db.fetch(
            """
            SELECT ticker, title
              FROM market_title_cache
             WHERE ticker LIKE $1
            """,
            event_prefix + "%",
        )
        for r in rows:
            ticker = r.get("ticker") or ""
            title = r.get("title") or ""
            if ticker:
                siblings.append((ticker, title))
    except Exception as e:
        log.warning(
            "resolve_entrant_outright_markets title lookup failed for %s: %s",
            event_prefix, e,
        )

    resolved: dict[str, str] = {}
    for entrant in entrants:
        token = _normalize_entrant_token(entrant)
        if not token:
            continue
        # Prefer a title-substring match — the public title is the
        # canonical human-readable form Kalshi exposes.
        match: str | None = None
        for ticker, title in siblings:
            if token in _normalize_entrant_token(title):
                match = ticker
                break
        # Fall back to a ticker-suffix match (some entrants surface as
        # ``KX...-ALCARAZ`` even before the title cache is warmed).
        if match is None:
            for ticker, _title in siblings:
                suffix = ticker.rsplit("-", 1)[-1] if "-" in ticker else ticker
                if token in _normalize_entrant_token(suffix):
                    match = ticker
                    break
        if match is not None:
            resolved[entrant] = match

    # Force the traded entrant onto the traded ticker so the analyst
    # can always click back to it even when the title cache is cold.
    if traded_entrant:
        for entrant in entrants:
            if entrant == traded_entrant:
                resolved[entrant] = traded_market_id
                break

    tickers = sorted({t for t in resolved.values()})
    ticks: dict[str, int] = {}
    if tickers:
        try:
            tick_rows = await db.fetch(
                """
                WITH ranked AS (
                    SELECT t.market_ticker, t.close_cents, t.mean_cents,
                           ROW_NUMBER() OVER (
                               PARTITION BY t.market_ticker
                               ORDER BY t.bucket_ts DESC
                           ) AS rn
                      FROM market_price_ticks t
                     WHERE t.market_ticker = ANY($1::text[])
                )
                SELECT market_ticker, close_cents, mean_cents
                  FROM ranked
                 WHERE rn = 1
                """,
                tickers,
            )
            for r in tick_rows:
                tk = r.get("market_ticker")
                # Prefer the bucket mean (smoother for an analyst eyeball
                # comparison against the model probability) and fall back
                # to the close when only the close was recorded.
                cents = r.get("mean_cents")
                if cents is None:
                    cents = r.get("close_cents")
                if tk and cents is not None:
                    try:
                        ticks[tk] = int(cents)
                    except (TypeError, ValueError):
                        pass
        except Exception as e:
            log.warning(
                "resolve_entrant_outright_markets tick lookup failed: %s", e,
            )

    for entrant, ticker in resolved.items():
        out[entrant] = {
            "kalshi_market_id": ticker,
            "mid_cents": ticks.get(ticker),
            "kalshi_url": _kalshi_public_market_url(ticker),
        }
    return out


async def get_entrant_outright_markets_for_debate(
    debate_id: int,
) -> dict[str, Any] | None:
    """Backs the ``/api/dashboard/debates/{debate_id}/entrant-markets``
    endpoint: loads the debate's ``game_context`` (which carries both
    the traded ``kalshi_market_id`` and the Plackett-Luce sketch
    listing every entrant in the field), and resolves the per-entrant
    outright leg map via :func:`resolve_entrant_outright_markets`.

    Returns ``None`` when the debate doesn't exist; returns an empty
    ``entrants`` map when the debate has no Plackett-Luce sketch (e.g.
    a binary moneyline debate) so the caller can render an explicit
    "no field" state instead of a 404.
    """
    try:
        row = await db.fetchrow(
            """
            SELECT id, game_context
              FROM swarm_debates
             WHERE id = $1
            """,
            int(debate_id),
        )
    except Exception as e:
        log.warning(
            "get_entrant_outright_markets_for_debate(%s) lookup failed: %s",
            debate_id, e,
        )
        return None
    if row is None:
        return None

    ctx_raw = row.get("game_context") if isinstance(row, dict) else row["game_context"]
    if isinstance(ctx_raw, str):
        try:
            ctx = json.loads(ctx_raw)
        except Exception:
            ctx = None
    elif isinstance(ctx_raw, dict):
        ctx = ctx_raw
    else:
        ctx = None
    ctx = ctx or {}

    traded_market_id = ctx.get("kalshi_market_id")
    sketch = ctx.get("plackett_luce_sketch") or {}
    if not isinstance(sketch, dict):
        sketch = {}
    probs = sketch.get("probabilities") or {}
    if not isinstance(probs, dict):
        probs = {}
    entrants = [str(k) for k in probs.keys()]
    traded_entrant_raw = sketch.get("entrant")
    traded_entrant = str(traded_entrant_raw) if isinstance(traded_entrant_raw, str) else None

    entrant_map = await resolve_entrant_outright_markets(
        str(traded_market_id) if traded_market_id else None,
        entrants,
        traded_entrant=traded_entrant,
    )
    return {
        "debate_id": int(debate_id),
        "traded_market_id": traded_market_id,
        "traded_entrant": traded_entrant,
        "entrants": entrant_map,
    }


# ---------------------------------------------------------------------------
# Live Games — in-progress sports markets read-model
# ---------------------------------------------------------------------------

# Window (seconds) of midgame_updates activity considered fresh enough to
# render on the Live Games dashboard.  6 hours covers a full game's worth of
# tick history for any major league while still cutting noise from days-old
# settled markets.
_LIVE_GAMES_WINDOW_SEC = int(6 * 3600)

# Activity inside this window (and not at game-end) is treated as "live now".
_LIVE_NOW_WINDOW_SEC = int(45 * 60)


def _market_id_to_game_id(market_id: str) -> str:
    """Best-effort game id parse from a Kalshi market_id like
    ``KXNBAPTS-26APR02SASLAC-SASVWEMBANYAMA1-25``.  We strip the trailing
    ``-NN`` strike segment + the prop-name segment so multiple prop markets
    on the same game group together.  Returns the original id if the
    pattern doesn't match."""
    if not market_id:
        return market_id
    parts = market_id.split("-")
    # Pattern observed in production:
    #   PREFIX-DATEMATCH-PLAYERPROP-STRIKE
    #   KXNBAPTS-26APR02SASLAC-SASVWEMBANYAMA1-25
    # We want PREFIX-DATEMATCH so the SAS@LAC game collapses regardless of
    # which player prop we're looking at.
    if len(parts) >= 2:
        return "-".join(parts[:2])
    return market_id


async def get_live_games(
    *,
    limit: int = 50,
    window_seconds: int | None = None,
) -> dict[str, Any]:
    """Return a snapshot of in-progress sports markets.

    Reads the most recent row per ``market_id`` from ``midgame_updates``
    (the live-pipeline tick table that the in-game trading loop writes
    into) within the supplied window, joins the most recent matching
    ``swarm_debates`` row when one exists for the same market, and
    attaches the latest cached Kalshi tick from ``market_price_ticks``.

    The shape is intentionally flat-list-with-grouping-keys so the
    frontend can group by ``league`` itself without needing the API to
    pre-shard the response.
    """
    window = int(window_seconds or _LIVE_GAMES_WINDOW_SEC)
    limit = max(1, min(200, int(limit)))
    cutoff_epoch = (datetime.now(timezone.utc) - timedelta(seconds=window)).timestamp()
    fresh_epoch = (datetime.now(timezone.utc) - timedelta(seconds=_LIVE_NOW_WINDOW_SEC)).timestamp()
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    items: list[dict[str, Any]] = []
    try:
        rows = await db.fetch(
            """
            WITH ranked AS (
                SELECT m.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY m.market_id
                           ORDER BY m.created_at DESC
                       ) AS rn
                  FROM midgame_updates m
                 WHERE m.created_at >= $1
            )
            SELECT market_id, league, action, home_score, away_score,
                   period, time_remaining, game_progress,
                   pre_game_prob, updated_prob, market_price,
                   current_edge, signal_json, created_at
              FROM ranked
             WHERE rn = 1
             ORDER BY created_at DESC
             LIMIT $2
            """,
            cutoff_epoch,
            limit,
        )
    except Exception as e:
        log.warning("get_live_games midgame_updates query failed: %s", e)
        rows = []

    market_ids = [r["market_id"] for r in rows if r.get("market_id")]
    debates_by_market: dict[str, dict[str, Any]] = {}
    ticks_by_market: dict[str, dict[str, Any]] = {}

    if market_ids:
        try:
            debate_rows = await db.fetch(
                """
                WITH ranked AS (
                    SELECT d.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY d.game_context->>'kalshi_market_id'
                               ORDER BY d.created_at DESC
                           ) AS rn
                      FROM swarm_debates d
                     WHERE d.game_context->>'kalshi_market_id' = ANY($1::text[])
                )
                SELECT id, league, matchup, market_type, recommended_action,
                       consensus_probability, agreement_rate, approved,
                       confidence_band_low, confidence_band_high,
                       to_char(created_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS created_at,
                       game_context->>'kalshi_market_id' AS market_id
                  FROM ranked
                 WHERE rn = 1
                """,
                market_ids,
            )
            for r in debate_rows:
                mid = r["market_id"]
                if mid:
                    debates_by_market[mid] = {
                        "id": r["id"],
                        "matchup": r["matchup"],
                        "market_type": r["market_type"],
                        "recommended_action": r["recommended_action"],
                        "consensus_probability": (
                            float(r["consensus_probability"])
                            if r["consensus_probability"] is not None else None
                        ),
                        "agreement_rate": (
                            float(r["agreement_rate"])
                            if r["agreement_rate"] is not None else None
                        ),
                        "approved": bool(r["approved"]) if r["approved"] is not None else None,
                        "confidence_band_low": (
                            float(r["confidence_band_low"])
                            if r["confidence_band_low"] is not None else None
                        ),
                        "confidence_band_high": (
                            float(r["confidence_band_high"])
                            if r["confidence_band_high"] is not None else None
                        ),
                        "created_at": r["created_at"],
                    }
        except Exception as e:
            log.warning("get_live_games debate join failed: %s", e)

        try:
            tick_rows = await db.fetch(
                """
                WITH ranked AS (
                    SELECT t.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY t.market_ticker
                               ORDER BY t.bucket_ts DESC
                           ) AS rn
                      FROM market_price_ticks t
                     WHERE t.market_ticker = ANY($1::text[])
                )
                SELECT market_ticker,
                       close_cents, mean_cents,
                       to_char(bucket_ts, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS bucket_ts,
                       to_char(fetched_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS fetched_at
                  FROM ranked
                 WHERE rn = 1
                """,
                market_ids,
            )
            for r in tick_rows:
                ticks_by_market[r["market_ticker"]] = {
                    "close_cents": (int(r["close_cents"]) if r["close_cents"] is not None else None),
                    "mean_cents": (int(r["mean_cents"]) if r["mean_cents"] is not None else None),
                    "bucket_ts": r["bucket_ts"],
                    "fetched_at": r["fetched_at"],
                }
        except Exception as e:
            log.warning("get_live_games tick join failed: %s", e)

    is_live_now_count = 0
    for r in rows:
        mid = r.get("market_id") or ""
        progress = r.get("game_progress")
        created = r.get("created_at")
        is_fresh = bool(created is not None and float(created) >= fresh_epoch)
        is_in_progress = bool(progress is not None and float(progress) < 1.0)
        is_live = is_fresh and is_in_progress
        if is_live:
            is_live_now_count += 1
        # ISO-8601 created_at for the frontend.
        try:
            created_iso = datetime.fromtimestamp(
                float(created), tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ") if created is not None else None
        except (OverflowError, OSError, ValueError):
            created_iso = None
        items.append({
            "market_id": mid,
            "game_id": _market_id_to_game_id(mid),
            "league": r.get("league") or "OTHER",
            "is_live": is_live,
            "action": r.get("action"),
            "home_score": r.get("home_score"),
            "away_score": r.get("away_score"),
            "period": r.get("period"),
            "time_remaining": r.get("time_remaining"),
            "game_progress": (
                float(r["game_progress"]) if r.get("game_progress") is not None else None
            ),
            "pre_game_prob": (
                float(r["pre_game_prob"]) if r.get("pre_game_prob") is not None else None
            ),
            "updated_prob": (
                float(r["updated_prob"]) if r.get("updated_prob") is not None else None
            ),
            "market_price": (
                float(r["market_price"]) if r.get("market_price") is not None else None
            ),
            "current_edge": (
                float(r["current_edge"]) if r.get("current_edge") is not None else None
            ),
            "last_update": created_iso,
            "swarm": debates_by_market.get(mid),
            "tick": ticks_by_market.get(mid),
        })

    return {
        "items": items,
        "is_live_now_count": is_live_now_count,
        "window_seconds": window,
        "fetched_at": fetched_at,
    }


async def get_live_game_context(market_id: str, *, history_limit: int = 12) -> dict[str, Any]:
    """Build a context bundle for the chatbot answering questions about
    a single live market.  Pulls the latest N midgame_updates (timeline),
    the most recent swarm debate for the market, and the last K cached
    Kalshi ticks.  All optional — any missing piece is reported as None
    so the LLM can be told the data is unavailable rather than hallucinate.
    """
    history_limit = max(1, min(60, int(history_limit)))
    timeline: list[dict[str, Any]] = []
    debate: dict[str, Any] | None = None
    ticks: list[dict[str, Any]] = []

    try:
        rows = await db.fetch(
            """
            SELECT market_id, league, action, home_score, away_score,
                   period, time_remaining, game_progress,
                   pre_game_prob, updated_prob, market_price,
                   current_edge, created_at
              FROM midgame_updates
             WHERE market_id = $1
             ORDER BY created_at DESC
             LIMIT $2
            """,
            market_id, history_limit,
        )
        for r in rows:
            try:
                created_iso = datetime.fromtimestamp(
                    float(r["created_at"]), tz=timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ") if r["created_at"] is not None else None
            except (OverflowError, OSError, ValueError):
                created_iso = None
            timeline.append({
                "league": r["league"],
                "action": r["action"],
                "home_score": r["home_score"],
                "away_score": r["away_score"],
                "period": r["period"],
                "time_remaining": r["time_remaining"],
                "game_progress": (
                    float(r["game_progress"]) if r["game_progress"] is not None else None
                ),
                "pre_game_prob": (
                    float(r["pre_game_prob"]) if r["pre_game_prob"] is not None else None
                ),
                "updated_prob": (
                    float(r["updated_prob"]) if r["updated_prob"] is not None else None
                ),
                "market_price": (
                    float(r["market_price"]) if r["market_price"] is not None else None
                ),
                "current_edge": (
                    float(r["current_edge"]) if r["current_edge"] is not None else None
                ),
                "ts": created_iso,
            })
    except Exception as e:
        log.warning("get_live_game_context timeline failed for %s: %s", market_id, e)

    try:
        d = await db.fetchrow(
            """
            SELECT id, league, matchup, market_type, recommended_action,
                   consensus_probability, agreement_rate, approved,
                   confidence_band_low, confidence_band_high,
                   to_char(created_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS created_at,
                   game_context, moderator_synthesis
              FROM swarm_debates
             WHERE game_context->>'kalshi_market_id' = $1
             ORDER BY created_at DESC
             LIMIT 1
            """,
            market_id,
        )
        if d:
            mod = d["moderator_synthesis"]
            if isinstance(mod, str):
                try:
                    mod = json.loads(mod)
                except Exception:
                    mod = None
            debate = {
                "id": d["id"],
                "matchup": d["matchup"],
                "market_type": d["market_type"],
                "recommended_action": d["recommended_action"],
                "consensus_probability": (
                    float(d["consensus_probability"])
                    if d["consensus_probability"] is not None else None
                ),
                "agreement_rate": (
                    float(d["agreement_rate"]) if d["agreement_rate"] is not None else None
                ),
                "approved": bool(d["approved"]) if d["approved"] is not None else None,
                "confidence_band_low": (
                    float(d["confidence_band_low"])
                    if d["confidence_band_low"] is not None else None
                ),
                "confidence_band_high": (
                    float(d["confidence_band_high"])
                    if d["confidence_band_high"] is not None else None
                ),
                "created_at": d["created_at"],
                "synthesis": (mod.get("synthesis") if isinstance(mod, dict) else None),
            }
    except Exception as e:
        log.warning("get_live_game_context debate failed for %s: %s", market_id, e)

    try:
        rows = await db.fetch(
            """
            SELECT close_cents, mean_cents,
                   to_char(bucket_ts, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS bucket_ts
              FROM market_price_ticks
             WHERE market_ticker = $1
             ORDER BY bucket_ts DESC
             LIMIT 30
            """,
            market_id,
        )
        for r in rows:
            ticks.append({
                "close_cents": (int(r["close_cents"]) if r["close_cents"] is not None else None),
                "mean_cents": (int(r["mean_cents"]) if r["mean_cents"] is not None else None),
                "bucket_ts": r["bucket_ts"],
            })
    except Exception as e:
        log.warning("get_live_game_context ticks failed for %s: %s", market_id, e)

    return {
        "market_id": market_id,
        "timeline": timeline,
        "swarm_debate": debate,
        "ticks": ticks,
    }


# ---------------------------------------------------------------------------
# Upcoming-games (today's matchups + pre-game context for the chatbot)
# ---------------------------------------------------------------------------
#
# These mirror the live-games shape so the frontend can render both
# sections from one page.  The "market_id" we expose here is a synthetic
# key of the form ``sched:<league>:<game_id>`` (see
# ``app.live_games_chat.UPCOMING_KEY_PREFIX``) — the chatbot endpoint
# detects this prefix and routes to ``get_upcoming_game_context``
# instead of the live-game one.

_UPCOMING_KEY_PREFIX = "sched:"
_UPCOMING_DEFAULT_HOURS = 24
_UPCOMING_INTANGIBLES_HOURS = 48


def _build_upcoming_key(league: str, game_id: str) -> str:
    return f"{_UPCOMING_KEY_PREFIX}{league}:{game_id}"


def _parse_upcoming_key(market_id: str) -> tuple[str, str] | None:
    """Parse ``sched:<league>:<game_id>`` -> (league, game_id).  Returns
    ``None`` for any other shape so callers can fall back to the live
    path."""
    if not market_id or not market_id.startswith(_UPCOMING_KEY_PREFIX):
        return None
    rest = market_id[len(_UPCOMING_KEY_PREFIX):]
    if ":" not in rest:
        return None
    league, _, game_id = rest.partition(":")
    league = (league or "").strip()
    game_id = (game_id or "").strip()
    if not league or not game_id:
        return None
    return league, game_id


async def get_upcoming_games(
    *,
    window_hours: int = _UPCOMING_DEFAULT_HOURS,
    limit: int = 50,
) -> dict[str, Any]:
    """Return today's (and near-future) scheduled sports matchups.

    Reads ``sportradar_schedules`` for rows whose ``scheduled`` falls
    inside the supplied window starting from now.  For each row we
    attach: how many ``intangibles_reports`` have landed for the
    matchup in the last 48h, whether a recent pre-game ``swarm_debates``
    row mentions the matchup, and a synthetic ``market_id`` the
    chatbot endpoint understands.

    Wrapped in try/except per query so a single missing table can't
    blank the whole strip — same defensive pattern used by
    ``get_live_games``.
    """
    window_hours = max(1, min(96, int(window_hours)))
    limit = max(1, min(200, int(limit)))
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=window_hours)
    fetched_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    items: list[dict[str, Any]] = []
    try:
        rows = await db.fetch(
            """
            SELECT league, game_id, home_team, away_team,
                   to_char(scheduled, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS scheduled,
                   status, venue
              FROM sportradar_schedules
             WHERE scheduled >= $1
               AND scheduled <= $2
             ORDER BY scheduled ASC
             LIMIT $3
            """,
            now,
            horizon,
            limit,
        )
    except Exception as e:
        log.warning("get_upcoming_games schedules query failed: %s", e)
        rows = []

    # Pre-fetch intangibles counts grouped by (sport ILIKE league, teams
    # ILIKE either side) — done in a single pass so we don't issue N+1
    # queries.  We bucket by lower(league) for case-insensitive match.
    intangibles_by_key: dict[tuple[str, str], int] = {}
    if rows:
        intangibles_cutoff = now - timedelta(hours=_UPCOMING_INTANGIBLES_HOURS)
        try:
            ir_rows = await db.fetch(
                """
                SELECT lower(sport) AS sport, lower(coalesce(teams, '')) AS teams
                  FROM intangibles_reports
                 WHERE scanned_at >= $1
                """,
                intangibles_cutoff,
            )
        except Exception as e:
            log.debug("get_upcoming_games intangibles query failed: %s", e)
            ir_rows = []
        # Bucket per (sport, teams-lower) for fuzzy match below.
        ir_by_sport: dict[str, list[str]] = {}
        for ir in ir_rows:
            ir_by_sport.setdefault(ir["sport"] or "", []).append(ir["teams"] or "")
        for r in rows:
            league_l = (r["league"] or "").lower()
            home_l = (r["home_team"] or "").lower()
            away_l = (r["away_team"] or "").lower()
            count = 0
            for teams_l in ir_by_sport.get(league_l, []):
                if not teams_l:
                    continue
                if (home_l and home_l in teams_l) or (away_l and away_l in teams_l):
                    count += 1
            intangibles_by_key[(league_l, r["game_id"])] = count

    for r in rows:
        league = r["league"] or ""
        game_id = r["game_id"] or ""
        if not league or not game_id:
            continue
        items.append({
            "market_id": _build_upcoming_key(league, game_id),
            "league": league,
            "home_team": r["home_team"] or "",
            "away_team": r["away_team"] or "",
            "scheduled": r["scheduled"],
            "status": r["status"] or "",
            "venue": r["venue"] or "",
            "intangibles_count": intangibles_by_key.get(
                ((league or "").lower(), game_id), 0
            ),
        })

    return {
        "items": items,
        "count": len(items),
        "window_hours": window_hours,
        "fetched_at": fetched_at,
    }


async def _fetch_team_injuries_generic(
    league: str, team_name: str
) -> list[dict[str, Any]]:
    """League-agnostic injury fetch: pulls the cached
    ``sportradar_injuries`` rows for ``(league, team_name)`` with no
    position filtering.  Used as the catch-all for leagues that don't
    have a dedicated context builder (NBA, MLB, NHL, etc.)."""
    if not team_name:
        return []
    try:
        rows = await db.fetch(
            """
            SELECT player_name, position, status, description
              FROM sportradar_injuries
             WHERE league = $1 AND team_name ILIKE $2
             ORDER BY status, position, player_name
             LIMIT 30
            """,
            league,
            f"%{team_name}%",
        )
    except Exception as e:
        log.debug(
            "generic injuries DB read failed for %s/%s: %s",
            league, team_name, e,
        )
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        status = (r["status"] or "").strip()
        if not status:
            continue
        out.append({
            "player": r["player_name"] or "",
            "position": (r["position"] or "").upper(),
            "status": status,
            "description": (r["description"] or "")[:200],
        })
    return out


async def _build_player_status_for_league(
    league: str, home_team: str, away_team: str
) -> dict[str, Any] | None:
    """Dispatch to the league-specific context builder when one exists,
    otherwise return per-team raw injury lists.  Returns ``None`` when
    no source has anything to say about the matchup so the chatbot can
    surface "no player_status block"."""
    league_norm = (league or "").upper()
    try:
        if league_norm == "NFL":
            from .sports import fetch_nfl_player_status  # noqa: PLC0415
            return await fetch_nfl_player_status(home_team, away_team)
        from .sports import is_soccer_league, fetch_soccer_lineup_status  # noqa: PLC0415
        if is_soccer_league(league):
            return await fetch_soccer_lineup_status(league, home_team, away_team)
    except Exception as e:
        log.debug(
            "league context builder failed for %s: %s — falling back to generic",
            league, e,
        )
    home_inj, away_inj = await asyncio.gather(
        _fetch_team_injuries_generic(league, home_team),
        _fetch_team_injuries_generic(league, away_team),
    )
    if not home_inj and not away_inj:
        return None
    return {
        "home": {"team": home_team, "injuries": home_inj},
        "away": {"team": away_team, "injuries": away_inj},
    }


async def get_upcoming_game_context(market_id: str) -> dict[str, Any]:
    """Build a pre-game context bundle for the chatbot answering
    questions about a single upcoming matchup.

    Pulls the schedule row, league-aware player_status (NFL injuries +
    snap counts, soccer lineup status, generic injuries elsewhere),
    recent ``intangibles_reports`` rows for the matchup, the most
    recent pre-game ``swarm_debates`` verdict if one mentions the
    matchup, and the latest cached Kalshi ticks for any market that
    looks like it tracks this matchup.

    All four sub-queries are isolated so a single missing source
    yields ``None``/``[]`` for that field rather than blanking the
    whole bundle.
    """
    parsed = _parse_upcoming_key(market_id)
    if parsed is None:
        return {
            "market_id": market_id,
            "schedule": None,
            "player_status": None,
            "intangibles": [],
            "recent_swarm_debate": None,
            "ticks": [],
            "error": "invalid_upcoming_key",
        }
    league, game_id = parsed

    schedule: dict[str, Any] | None = None
    home_team = away_team = ""
    try:
        srow = await db.fetchrow(
            """
            SELECT league, game_id, home_team, away_team,
                   to_char(scheduled, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS scheduled,
                   status, venue, payload
              FROM sportradar_schedules
             WHERE league = $1 AND game_id = $2
             LIMIT 1
            """,
            league, game_id,
        )
        if srow:
            home_team = srow["home_team"] or ""
            away_team = srow["away_team"] or ""
            payload = srow["payload"]
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = None
            schedule = {
                "league": srow["league"],
                "game_id": srow["game_id"],
                "home_team": home_team,
                "away_team": away_team,
                "scheduled": srow["scheduled"],
                "status": srow["status"],
                "venue": srow["venue"],
                "payload": payload if isinstance(payload, dict) else None,
            }
    except Exception as e:
        log.warning(
            "get_upcoming_game_context schedule query failed for %s/%s: %s",
            league, game_id, e,
        )

    player_status = await _build_player_status_for_league(
        league, home_team, away_team
    )

    intangibles: list[dict[str, Any]] = []
    if home_team and away_team:
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(
                hours=_UPCOMING_INTANGIBLES_HOURS
            )
            home_l = home_team.lower()
            away_l = away_team.lower()
            # Require BOTH team names to appear in the ``teams`` field.
            # Earlier we used OR which produced false positives — e.g.
            # the substring ``jets`` could match an unrelated soccer
            # club called Jets FC, or ``united`` could match dozens of
            # English clubs.  Pre-game intangibles_reports always carry
            # both team names in ``teams`` (formatted ``A vs B`` or
            # ``A @ B`` by the scanner), so AND is the correct join.
            ir_rows = await db.fetch(
                """
                SELECT provider, market_type, side, edge_pct, fair_prob,
                       book_prob, sportsbook, teams, payload,
                       to_char(scanned_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS scanned_at
                  FROM intangibles_reports
                 WHERE lower(sport) = $1
                   AND scanned_at >= $2
                   AND coalesce(teams, '') <> ''
                   AND lower(teams) LIKE '%' || $3 || '%'
                   AND lower(teams) LIKE '%' || $4 || '%'
                 ORDER BY scanned_at DESC
                 LIMIT 25
                """,
                (league or "").lower(),
                cutoff,
                home_l,
                away_l,
            )
            for r in ir_rows:
                payload = r["payload"]
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except Exception:
                        payload = None
                intangibles.append({
                    "provider": r["provider"],
                    "market_type": r["market_type"],
                    "side": r["side"],
                    "edge_pct": (
                        float(r["edge_pct"]) if r["edge_pct"] is not None else None
                    ),
                    "fair_prob": (
                        float(r["fair_prob"]) if r["fair_prob"] is not None else None
                    ),
                    "book_prob": (
                        float(r["book_prob"]) if r["book_prob"] is not None else None
                    ),
                    "sportsbook": r["sportsbook"],
                    "teams": r["teams"],
                    "payload": payload if isinstance(payload, dict) else None,
                    "scanned_at": r["scanned_at"],
                })
        except Exception as e:
            log.warning(
                "get_upcoming_game_context intangibles failed for %s/%s: %s",
                league, game_id, e,
            )

    debate: dict[str, Any] | None = None
    if home_team and away_team:
        try:
            home_l = home_team.lower()
            away_l = away_team.lower()
            # Same OR -> AND tightening as above + scope by league so a
            # cross-league matchup with overlapping team names (e.g.
            # MLB ``Yankees`` vs minor-league affiliates) can't bleed
            # the wrong debate into the chatbot.
            d = await db.fetchrow(
                """
                SELECT id, league, matchup, market_type, recommended_action,
                       consensus_probability, agreement_rate, approved,
                       confidence_band_low, confidence_band_high,
                       to_char(created_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS created_at,
                       moderator_synthesis
                  FROM swarm_debates
                 WHERE created_at >= now() - interval '7 days'
                   AND lower(coalesce(matchup, '')) <> ''
                   AND lower(matchup) LIKE '%' || $1 || '%'
                   AND lower(matchup) LIKE '%' || $2 || '%'
                   AND ($3 = '' OR lower(coalesce(league, '')) = $3)
                 ORDER BY created_at DESC
                 LIMIT 1
                """,
                home_l,
                away_l,
                (league or "").lower(),
            )
            if d:
                mod = d["moderator_synthesis"]
                if isinstance(mod, str):
                    try:
                        mod = json.loads(mod)
                    except Exception:
                        mod = None
                debate = {
                    "id": d["id"],
                    "matchup": d["matchup"],
                    "market_type": d["market_type"],
                    "recommended_action": d["recommended_action"],
                    "consensus_probability": (
                        float(d["consensus_probability"])
                        if d["consensus_probability"] is not None else None
                    ),
                    "agreement_rate": (
                        float(d["agreement_rate"])
                        if d["agreement_rate"] is not None else None
                    ),
                    "approved": (
                        bool(d["approved"]) if d["approved"] is not None else None
                    ),
                    "confidence_band_low": (
                        float(d["confidence_band_low"])
                        if d["confidence_band_low"] is not None else None
                    ),
                    "confidence_band_high": (
                        float(d["confidence_band_high"])
                        if d["confidence_band_high"] is not None else None
                    ),
                    "created_at": d["created_at"],
                    "synthesis": (
                        mod.get("synthesis") if isinstance(mod, dict) else None
                    ),
                }
        except Exception as e:
            log.warning(
                "get_upcoming_game_context debate failed for %s/%s: %s",
                league, game_id, e,
            )

    ticks: list[dict[str, Any]] = []
    if home_team and away_team:
        try:
            home_l = home_team.lower()
            away_l = away_team.lower()
            # Kalshi tickers are short opaque codes (e.g. ``KXNFLGAME-…-PHIBUF``)
            # so substring matching ``Eagles`` against the ticker rarely
            # works.  The ``market_title_cache`` table on the other hand
            # holds the human-readable Kalshi title (e.g. "Will the
            # Eagles beat the Bills on Nov 5?") — match BOTH team names
            # against the title to find the real market for this game,
            # then pull ticks for those tickers.  We also keep the
            # legacy ticker-substring fallback (still requiring BOTH
            # team names) for tickers whose title isn't cached yet.
            tick_rows = await db.fetch(
                """
                WITH title_matched AS (
                    SELECT ticker AS market_ticker
                      FROM market_title_cache
                     WHERE coalesce(title, '') <> ''
                       AND lower(title) LIKE '%' || $1 || '%'
                       AND lower(title) LIKE '%' || $2 || '%'
                     LIMIT 5
                ),
                ticker_matched AS (
                    SELECT DISTINCT market_ticker
                      FROM market_price_ticks
                     WHERE bucket_ts >= now() - interval '7 days'
                       AND lower(market_ticker) LIKE '%' || $1 || '%'
                       AND lower(market_ticker) LIKE '%' || $2 || '%'
                     LIMIT 5
                ),
                matched_markets AS (
                    SELECT market_ticker FROM title_matched
                    UNION
                    SELECT market_ticker FROM ticker_matched
                )
                SELECT t.market_ticker, t.close_cents, t.mean_cents,
                       to_char(t.bucket_ts, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS bucket_ts
                  FROM market_price_ticks t
                  JOIN matched_markets m ON m.market_ticker = t.market_ticker
                 WHERE t.bucket_ts >= now() - interval '7 days'
                 ORDER BY t.bucket_ts DESC
                 LIMIT 30
                """,
                home_l,
                away_l,
            )
            for r in tick_rows:
                ticks.append({
                    "market_ticker": r["market_ticker"],
                    "close_cents": (
                        int(r["close_cents"]) if r["close_cents"] is not None else None
                    ),
                    "mean_cents": (
                        int(r["mean_cents"]) if r["mean_cents"] is not None else None
                    ),
                    "bucket_ts": r["bucket_ts"],
                })
        except Exception as e:
            log.warning(
                "get_upcoming_game_context ticks failed for %s/%s: %s",
                league, game_id, e,
            )

    return {
        "market_id": market_id,
        "schedule": schedule,
        "player_status": player_status,
        "intangibles": intangibles,
        "recent_swarm_debate": debate,
        "ticks": ticks,
    }
