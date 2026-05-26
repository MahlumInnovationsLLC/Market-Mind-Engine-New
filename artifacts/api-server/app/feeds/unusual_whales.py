"""Unusual Whales (UW) feed.

UW exposes options-flow alerts, dark-pool prints, and per-ticker
snapshots over a REST API gated by a bearer token.  We do simple polls
on independent cadences and write each row into `uw_signals` keyed by
(signal_type, dedupe_key) so re-polling the same window is a no-op.

Auth: ``UNUSUAL_WHALES_API_KEY`` (alias ``UW_API_TOKEN`` accepted) sent
as ``Authorization: Bearer <key>``.

Endpoints (per UW v1 docs at the time of writing — the response schema
is intentionally treated as opaque JSON because UW has been known to add
fields without notice; we only rely on a small set for indexing):

  * Flow alerts        — ``GET /api/option-trades/flow-alerts``
  * Dark-pool recent   — ``GET /api/darkpool/recent``
  * Ticker snapshot    — ``GET /api/stock/{ticker}/info``  (per-ticker;
    the watchlist is configurable via ``AXIOM_UW_SNAPSHOT_TICKERS``)

Out of scope (per task spec): mining the rows into model features —
that's the AutoResearch task.
"""
from __future__ import annotations

import asyncio
import hashlib
import json as _json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

from .. import db
from .odds_api import _retry_after_seconds
from .registry import Feed, registry
from .scheduler import RateLimited


log = logging.getLogger("axiom.feeds.uw")


UW_BASE = os.environ.get("AXIOM_UW_BASE_URL") or "https://api.unusualwhales.com"
DEFAULT_FLOW_CADENCE = max(15, int(os.environ.get("AXIOM_UW_FLOW_CADENCE") or "60"))
DEFAULT_DARKPOOL_CADENCE = max(30, int(os.environ.get("AXIOM_UW_DARKPOOL_CADENCE") or "120"))
DEFAULT_SNAPSHOT_CADENCE = max(60, int(os.environ.get("AXIOM_UW_SNAPSHOT_CADENCE") or "300"))
SNAPSHOT_TICKERS = [
    t.strip().upper()
    for t in (os.environ.get("AXIOM_UW_SNAPSHOT_TICKERS") or "SPY,QQQ,IWM").split(",")
    if t.strip()
]


def _api_key() -> str | None:
    return os.environ.get("UNUSUAL_WHALES_API_KEY") or os.environ.get("UW_API_TOKEN")


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "User-Agent": "axiom-predict/1.0 (+feeds.unusual_whales)",
    }


async def _get_json(provider: str, url: str, api_key: str, params: dict | None = None) -> Any:
    """Fetch JSON from UW.  Raises RateLimited on 429, RuntimeError on auth/HTTP errors."""
    timeout = httpx.Timeout(15.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(url, headers=_headers(api_key), params=params)
    if r.status_code in (401, 403):
        raise RuntimeError(f"unusual_whales auth rejected ({r.status_code})")
    if r.status_code == 429:
        raise RateLimited(provider, retry_after=_retry_after_seconds(r) or 60.0)
    if r.status_code == 404:
        return None
    if r.status_code >= 400:
        raise RuntimeError(f"unusual_whales HTTP {r.status_code}: {r.text[:200]}")
    try:
        return r.json()
    except Exception:
        return None


def _extract_rows(payload: Any) -> list[dict[str, Any]]:
    """UW endpoints variously return ``{"data": [...]}``, ``{"results": [...]}``,
    or a bare list.  Normalise to ``list[dict]`` and skip non-dict items."""
    if payload is None:
        return []
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = (
            payload.get("data")
            or payload.get("results")
            or payload.get("items")
            or payload.get("flow_alerts")
            or payload.get("trades")
            or []
        )
        if not isinstance(items, list) and isinstance(payload.get("data"), dict):
            # Some endpoints wrap a single object — still record it.
            items = [payload["data"]]
    else:
        return []
    return [r for r in items if isinstance(r, dict)]


def _parse_ts(raw: Any) -> datetime | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        # Heuristic: treat values larger than 1e12 as ms epochs.
        try:
            v = float(raw)
            if v > 1e12:
                v /= 1000.0
            return datetime.fromtimestamp(v, tz=timezone.utc)
        except Exception:
            return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return None


def _dedupe_key(row: dict[str, Any], fallback_seed: str) -> str:
    """Prefer the row's own id when present, otherwise hash a stable subset.

    Keeping the key short (sha1) bounds the index size even on a fast
    flow-alert feed.
    """
    for key in ("id", "alert_id", "trade_id", "uuid", "_id"):
        v = row.get(key)
        if v:
            return str(v)
    seed = fallback_seed + "|" + _json.dumps(row, sort_keys=True, default=str)
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()


def _row_ticker(row: dict[str, Any]) -> str | None:
    for key in ("ticker", "symbol", "underlying", "underlying_symbol"):
        v = row.get(key)
        if v:
            return str(v).upper()
    return None


def _row_occurred_at(row: dict[str, Any]) -> datetime | None:
    for key in (
        "executed_at", "occurred_at", "timestamp", "time", "created_at",
        "trade_time", "executed_at_ms", "tape_time",
    ):
        if key in row:
            ts = _parse_ts(row[key])
            if ts is not None:
                return ts
    return None


async def _persist_signals(signal_type: str, rows: list[dict[str, Any]]) -> int:
    """Insert UW rows into `uw_signals`, ignoring dedupe collisions."""
    if not rows:
        return 0
    db_rows: list[tuple] = []
    for r in rows:
        ticker = _row_ticker(r)
        occurred = _row_occurred_at(r)
        seed = f"{signal_type}|{ticker or ''}|{occurred.isoformat() if occurred else ''}"
        key = _dedupe_key(r, seed)
        db_rows.append((
            signal_type,
            ticker,
            occurred,
            _json.dumps(r, default=str),
            key,
        ))
    try:
        async with db.acquire() as conn, conn.transaction():
            # ON CONFLICT DO NOTHING — repolls of the same window are no-ops.
            await conn.executemany(
                """
                INSERT INTO uw_signals
                  (signal_type, ticker, occurred_at, payload, dedupe_key, fetched_at)
                VALUES ($1, $2, $3, $4::jsonb, $5, NOW())
                ON CONFLICT (signal_type, dedupe_key) DO NOTHING
                """,
                db_rows,
            )
    except Exception as e:
        log.warning(
            "uw_signals upsert failed for %s: %s: %r",
            signal_type,
            type(e).__name__,
            e,
            exc_info=True,
        )
        raise
    return len(db_rows)


# ---------------------------------------------------------------------------
# Pollers
# ---------------------------------------------------------------------------


async def _poll_flow_alerts() -> int:
    api_key = _api_key()
    if not api_key:
        raise RuntimeError("UNUSUAL_WHALES_API_KEY not set")
    payload = await _get_json(
        "unusual_whales:flow",
        f"{UW_BASE}/api/option-trades/flow-alerts",
        api_key,
    )
    rows = _extract_rows(payload)
    return await _persist_signals("flow_alert", rows)


async def _poll_darkpool() -> int:
    api_key = _api_key()
    if not api_key:
        raise RuntimeError("UNUSUAL_WHALES_API_KEY not set")
    payload = await _get_json(
        "unusual_whales:darkpool",
        f"{UW_BASE}/api/darkpool/recent",
        api_key,
    )
    rows = _extract_rows(payload)
    return await _persist_signals("darkpool_print", rows)


async def _poll_snapshots() -> int:
    api_key = _api_key()
    if not api_key:
        raise RuntimeError("UNUSUAL_WHALES_API_KEY not set")
    if not SNAPSHOT_TICKERS:
        return 0
    written = 0
    for ticker in SNAPSHOT_TICKERS:
        try:
            payload = await _get_json(
                f"unusual_whales:snapshot:{ticker}",
                f"{UW_BASE}/api/stock/{ticker}/info",
                api_key,
            )
        except RateLimited:
            raise
        except Exception as e:
            log.warning("uw snapshot fetch failed for %s: %s", ticker, e)
            continue
        if not payload:
            continue
        # /info returns an object, not a list — wrap it.
        body = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(body, dict):
            continue
        body.setdefault("ticker", ticker)
        # Use the request time as the occurred_at when UW doesn't carry one;
        # combined with the ticker that gives a stable-ish dedupe key per
        # poll cycle (we don't want every poll to write a new row, so we
        # round to the cadence to coalesce within the same window).
        body.setdefault("snapshot_at", _round_to_cadence_iso(DEFAULT_SNAPSHOT_CADENCE))
        written += await _persist_signals("snapshot", [body])
    return written


def _round_to_cadence_iso(cadence_seconds: int) -> str:
    now = datetime.now(tz=timezone.utc).timestamp()
    bucket = int(now // cadence_seconds) * cadence_seconds
    return datetime.fromtimestamp(bucket, tz=timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_unusual_whales_feeds() -> None:
    """Register UW flow / dark-pool / snapshot pollers (idempotent)."""
    api_key = _api_key()
    skipped = None if api_key else "UNUSUAL_WHALES_API_KEY not set"

    if registry.status_of("unusual_whales_flow") is None:
        registry.register(Feed(
            name="unusual_whales_flow",
            cadence_seconds=float(DEFAULT_FLOW_CADENCE),
            poll=_poll_flow_alerts,
            enabled=bool(api_key),
            skipped_reason=skipped,
        ))
    if registry.status_of("unusual_whales_darkpool") is None:
        registry.register(Feed(
            name="unusual_whales_darkpool",
            cadence_seconds=float(DEFAULT_DARKPOOL_CADENCE),
            poll=_poll_darkpool,
            enabled=bool(api_key),
            skipped_reason=skipped,
        ))
    if registry.status_of("unusual_whales_snapshot") is None:
        registry.register(Feed(
            name="unusual_whales_snapshot",
            cadence_seconds=float(DEFAULT_SNAPSHOT_CADENCE),
            poll=_poll_snapshots,
            enabled=bool(api_key),
            skipped_reason=skipped,
        ))
