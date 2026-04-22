"""Free-tier signal gating.

Enforces the two free-plan rules called out on the pricing page:

* **15-minute signal delay.**  Free callers only ever see signals
  whose ``created_at`` is older than ``FREE_TIER_DELAY_MINUTES``
  ago.  Newer rows are dropped from list responses so the live edge
  is reserved for paid tiers.

* **5 distinct markets/day.**  A free caller may evaluate at most
  ``FREE_TIER_DAILY_MARKET_LIMIT`` distinct markets per UTC day.
  Re-opening the same market does not consume additional quota.
  The 6th distinct market returns HTTP 402 with an
  ``upgrade_required`` payload so the dashboard can render the
  upsell.

The counter resets at UTC midnight implicitly: the underlying
``free_tier_market_views`` table partitions on a ``DATE`` column
computed from ``now() AT TIME ZONE 'utc'``.

Founder/Pro/Elite callers bypass both checks — the helpers in this
module are only invoked from the free-caller branches in ``main.py``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from fastapi import HTTPException

from . import db


log = logging.getLogger("axiom.free_tier")


# ---------------------------------------------------------------------------
# Tunables (kept module-level so tests can monkeypatch them)
# ---------------------------------------------------------------------------


FREE_TIER_DELAY_MINUTES = 15
FREE_TIER_DAILY_MARKET_LIMIT = 5


# ---------------------------------------------------------------------------
# 15-minute delay
# ---------------------------------------------------------------------------


def _parse_iso(ts: Any) -> datetime | None:
    """Best-effort ISO-8601 parse.  Returns ``None`` for unparseable input.

    The dashboard read-models render timestamps with ``to_char(...,
    'YYYY-MM-DD"T"HH24:MI:SSZ')`` so the trailing ``Z`` is the common
    case; we also accept ``+00:00`` and naive strings (treated as UTC)
    for safety.
    """
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    if not isinstance(ts, str) or not ts:
        return None
    try:
        cleaned = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def apply_signal_delay(
    items: Iterable[dict[str, Any]],
    *,
    timestamp_key: str = "created_at",
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return only the items whose ``timestamp_key`` is older than
    ``FREE_TIER_DELAY_MINUTES`` minutes.

    Items missing or with an unparseable timestamp are *dropped* rather
    than leaked — the signal delay is a hard contractual requirement,
    so a row we can't time-stamp is treated as too-fresh-to-show.
    """
    cutoff = (now or _now_utc()) - timedelta(minutes=FREE_TIER_DELAY_MINUTES)
    out: list[dict[str, Any]] = []
    for it in items:
        dt = _parse_iso(it.get(timestamp_key))
        if dt is None:
            continue
        if dt <= cutoff:
            out.append(it)
    return out


# ---------------------------------------------------------------------------
# 5-markets/day quota
# ---------------------------------------------------------------------------


class FreeTierQuotaError(HTTPException):
    """Raised by :func:`enforce_market_view_quota` when the caller has
    already evaluated ``FREE_TIER_DAILY_MARKET_LIMIT`` distinct markets
    today.  Surfaced to the client as HTTP 402 with a structured
    ``upgrade_required`` payload."""

    def __init__(self, *, viewed_today: int, market_id: str | None) -> None:
        super().__init__(
            status_code=402,
            detail={
                "error": "upgrade_required",
                "reason": "free_tier_daily_market_limit",
                "limit": FREE_TIER_DAILY_MARKET_LIMIT,
                "viewed_today": viewed_today,
                "market_id": market_id,
                "resets_at": "00:00 UTC",
            },
        )


async def count_markets_viewed_today(user_id: str) -> int:
    """How many distinct markets ``user_id`` has evaluated today (UTC).

    Returns ``0`` on any DB failure — failing open here would let the
    quota silently lapse, so we instead let the caller's own DB write
    surface the error if there's a deeper problem.
    """
    try:
        n = await db.fetchval(
            """
            SELECT count(*)::int
              FROM free_tier_market_views
             WHERE user_id = $1
               AND view_date = (now() AT TIME ZONE 'utc')::date
            """,
            user_id,
        )
        return int(n or 0)
    except Exception as e:
        log.warning("count_markets_viewed_today failed: %s", e)
        return 0


async def enforce_market_view_quota(user_id: str, market_id: str) -> int:
    """Atomically record ``user_id`` opening ``market_id`` today, and
    raise :class:`FreeTierQuotaError` if it pushes them past the cap.

    Returns the post-insert distinct-market count for the day so the
    caller can surface the remaining quota in response headers if it
    wants to.

    Re-evaluating a market the user already opened today is free
    (``ON CONFLICT DO NOTHING`` short-circuits and the existing count
    stays put), matching the spec's "distinct market" wording.
    """
    if not user_id or not market_id:
        # Defensive: don't burn quota on malformed input.  Caller is
        # responsible for 400-ing in that case.
        return await count_markets_viewed_today(user_id)

    try:
        async with db.acquire() as conn:
            async with conn.transaction():
                # Serialize concurrent quota checks for the same
                # (user, UTC day) so two parallel requests at count=4
                # can't both see "room for one more" and end up
                # inserting two distinct markets.  The lock is
                # transaction-scoped (released on commit/rollback) and
                # never crosses to a different user or a different day.
                await conn.execute(
                    """
                    SELECT pg_advisory_xact_lock(
                        hashtextextended(
                            $1 || '|' ||
                            (now() AT TIME ZONE 'utc')::date::text,
                            0
                        )
                    )
                    """,
                    user_id,
                )
                already = await conn.fetchval(
                    """
                    SELECT 1 FROM free_tier_market_views
                     WHERE user_id = $1
                       AND view_date = (now() AT TIME ZONE 'utc')::date
                       AND market_id = $2
                    """,
                    user_id, market_id,
                )
                count_today = await conn.fetchval(
                    """
                    SELECT count(*)::int FROM free_tier_market_views
                     WHERE user_id = $1
                       AND view_date = (now() AT TIME ZONE 'utc')::date
                    """,
                    user_id,
                )
                count_today = int(count_today or 0)
                if not already and count_today >= FREE_TIER_DAILY_MARKET_LIMIT:
                    raise FreeTierQuotaError(
                        viewed_today=count_today, market_id=market_id
                    )
                await conn.execute(
                    """
                    INSERT INTO free_tier_market_views
                        (user_id, view_date, market_id)
                    VALUES ($1, (now() AT TIME ZONE 'utc')::date, $2)
                    ON CONFLICT (user_id, view_date, market_id) DO NOTHING
                    """,
                    user_id, market_id,
                )
                return count_today if already else count_today + 1
    except FreeTierQuotaError:
        raise
    except Exception as e:
        # Fail closed: a paid-feature gate must never silently lapse
        # because the quota table couldn't be reached.  Surface a 503
        # so the dashboard can render a "try again" state instead of
        # quietly serving the Pro view to free callers.
        log.exception("enforce_market_view_quota failed; failing closed: %s", e)
        raise HTTPException(
            status_code=503,
            detail={
                "error": "quota_check_unavailable",
                "reason": "free_tier_quota_db_error",
            },
        )
