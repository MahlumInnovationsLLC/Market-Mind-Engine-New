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


async def search_global(q: str, limit: int = 10) -> dict[str, list[dict[str, Any]]]:
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
    """
    q = (q or "").strip()
    if not q:
        return {"trades": [], "debates": [], "cycles": []}
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

    return {"trades": trades, "debates": debates, "cycles": cycles}


async def get_trade_detail(trade_ref: str) -> dict[str, Any] | None:
    """Return a single trade row + market-vs-model time series.

    `trade_ref` accepts either the integer ``trades.id`` (preferred —
    that's what the recent-trades table renders) or the UUID-ish
    ``trades.trade_id``.  Returns ``None`` if no row matches.

    The time series is built from ``swarm_debates`` rows that share the
    same Kalshi market_id (via ``game_context->>'kalshi_market_id'``):
    each debate carries both the consensus model probability *and* the
    Kalshi price snapshot at debate time, which is exactly the
    market-vs-model overlay the spec asks for.  The trade's own
    snapshot (``trades.price`` and ``trades.model_prob``) is appended
    as the anchor point at ``executed_at`` so the chart always shows at
    least one data point.
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
        }
        # Aggregate trade stats over the rolling window.
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
                   AND executed_at >= now() - ($2::int || ' days')::interval
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
                     AND t.executed_at >= now() - ($2::int || ' days')::interval
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
        out.append(entry)

    return {"days": days, "verticals": out}


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

    out: list[dict[str, Any]] = []
    for spec in _vertical_registry.all_specs():
        vertical = spec.name
        entry: dict[str, Any] = {
            "vertical": vertical,
            "description": spec.description,
            "leagues": list(spec.leagues),
            "kelly_multiplier": spec.kelly_multiplier,
            "enabled": spec.enabled,
            "hidden": vertical in hidden,
            "pinned": vertical in pinned,
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
        clone["redacted"] = True
        out.append(clone)
    return out
