"""Backfill historical Kalshi fills into the local `trades` table.

Why this exists
---------------
The orchestrator records every order it places into `trades`, but if you
trade on Kalshi outside the bot (UI, another client, or a previous
deployment that lost state), those fills never make it into our local
DB. The reconciler only walks back ~5 minutes per tick (its `min_ts`
defaults to ``time.time() - 300``), so deep history is never picked up.

This script asks Kalshi's `/portfolio/fills` endpoint for the full
history since ``--since`` and inserts each new fill into `trades`,
keyed on `trade_id`. Existing rows are left alone (idempotent).

Usage
-----
    python scripts/backfill_kalshi_fills.py --since 2025-01-01
    python scripts/backfill_kalshi_fills.py --since-days 30 --dry-run
    python scripts/backfill_kalshi_fills.py --since-days 7 --verbose

Requires KALSHI_API_KEY_ID + KALSHI_API_PRIVATE_KEY in env, same as the
running server. Safe to re-run — duplicates are skipped on `trade_id`.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

# Allow running as `python scripts/backfill_kalshi_fills.py` from anywhere.
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import db  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.kalshi.client import KalshiClient  # noqa: E402

log = logging.getLogger("backfill_kalshi_fills")


# Ticker prefix → vertical. Mirrors app/verticals/builtins.py. Keep in sync.
_PREFIX_VERTICAL: list[tuple[str, str]] = [
    ("KXBTC", "crypto"), ("KXETH", "crypto"),
    ("KXNFL", "sports"), ("KXNBA", "sports"), ("KXMLB", "sports"),
    ("KXNHL", "sports"), ("KXNCAAF", "sports"), ("KXNCAAB", "sports"),
    ("KXMLS", "soccer"), ("KXEPL", "soccer"), ("KXLALIGA", "soccer"),
    ("KXSERIEA", "soccer"), ("KXBUNDES", "soccer"), ("KXLIGUE1", "soccer"),
    ("KXUCL", "soccer"),
    ("KXHIGH", "weather"), ("KXLOW", "weather"),
    ("KXTEMP", "weather"), ("KXRAIN", "weather"),
    ("KXOIL", "commodities"), ("KXGOLD", "commodities"),
    ("KXNATGAS", "commodities"), ("KXCORN", "commodities"),
    ("KXSOY", "commodities"), ("KXWHEAT", "commodities"),
    ("KXCOPPER", "commodities"), ("KXSILVER", "commodities"),
    ("KXCOFFEE", "commodities"), ("KXSUGAR", "commodities"),
    ("KXLLM", "tech_ai"), ("KXGPT", "tech_ai"), ("KXCLAUDE", "tech_ai"),
    ("KXLLAMA", "tech_ai"), ("KXAI", "tech_ai"), ("KXNVDA", "tech_ai"),
    ("KXTSMC", "tech_ai"), ("KXMLPERF", "tech_ai"), ("KXGPU", "tech_ai"),
    ("KXIPO", "tech_ai"),
    ("KXUFC", "combat_individual"), ("KXATP", "combat_individual"),
    ("KXWTA", "combat_individual"), ("KXF1", "combat_individual"),
    ("KXPGA", "combat_individual"),
    ("KXPRES", "elections"), ("KXGOV", "elections"), ("KXSEN", "elections"),
    ("KXHOUSE", "elections"), ("KXFRPRES", "elections"), ("KXUKPM", "elections"),
    ("KXDEPM", "elections"), ("KXCAPM", "elections"), ("KXAUPM", "elections"),
    ("KXELE", "elections"),
    ("KXCPI", "events_econ"), ("KXPPI", "events_econ"), ("KXGDP", "events_econ"),
    ("KXFOMC", "events_econ"), ("KXNFP", "events_econ"),
    ("KXJOBLESS", "events_econ"), ("KXRETAIL", "events_econ"),
    ("KXPCE", "events_econ"), ("KXISM", "events_econ"),
    ("KXCONFIDENCE", "events_econ"), ("KXHOUSING", "events_econ"),
    ("KXBOX", "entertainment"), ("KXOSCAR", "entertainment"),
    ("KXEMMY", "entertainment"), ("KXGRAMMY", "entertainment"),
    ("KXSPOTIFY", "entertainment"), ("KXSTREAM", "entertainment"),
    ("KXRT", "entertainment"),
]
# Sort longest-first so e.g. "KXNCAAF" matches before "KXN".
_PREFIX_VERTICAL.sort(key=lambda x: -len(x[0]))


def _vertical_for(ticker: str | None) -> str | None:
    if not ticker:
        return None
    t = ticker.upper()
    for prefix, vertical in _PREFIX_VERTICAL:
        if t.startswith(prefix):
            return vertical
    return None


def _parse_ts(raw: Any) -> datetime | None:
    """Kalshi returns `created_time` as ISO8601 UTC with `Z` suffix."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(float(raw), tz=timezone.utc)
    s = str(raw).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _qty_dollars(fill: dict[str, Any]) -> tuple[Decimal | None, Decimal | None]:
    """Extract (quantity, fill_price_dollars) handling new + legacy schemas.

    New Kalshi response (observed 2026-04):
        count_fp:           string decimal contracts, e.g. '39.00'
        yes_price_dollars:  string decimal price 0–1, e.g. '0.5400'
        no_price_dollars:   string decimal price 0–1, e.g. '0.4600'
    Legacy (kept for safety):
        count:              integer contract count
        yes_price/no_price: integer cents 1-99
    """
    side = (fill.get("side") or "").lower()
    # quantity — count_fp is dollar-decimal contracts, NOT fixed-point cents
    qty: Decimal | None = None
    if fill.get("count") is not None:
        try: qty = Decimal(str(fill["count"]))
        except Exception: qty = None
    elif fill.get("count_fp") is not None:
        try: qty = Decimal(str(fill["count_fp"]))
        except Exception: qty = None
    # price (Kalshi binary contracts settle 0–1)
    price: Decimal | None = None
    if side == "yes":
        if fill.get("yes_price_dollars") is not None:
            try: price = Decimal(str(fill["yes_price_dollars"]))
            except Exception: pass
        elif fill.get("yes_price") is not None:
            try: price = Decimal(str(fill["yes_price"])) / Decimal("100")
            except Exception: pass
    elif side == "no":
        if fill.get("no_price_dollars") is not None:
            try: price = Decimal(str(fill["no_price_dollars"]))
            except Exception: pass
        elif fill.get("no_price") is not None:
            try: price = Decimal(str(fill["no_price"])) / Decimal("100")
            except Exception: pass
    return qty, price


def _tag_placed_by(qty: Any, price: Any) -> str:
    """Per-row provenance tag for backfilled Kalshi fills.

    Implements the operator's $400 bimodal heuristic (see migration
    0039 + 2026-04-30 directive in replit.md): notional > $400 ->
    ``'manual_ui'`` (the operator never has the autotrader place bets
    that large; live probe of 5,725 trades found 135 over $400 and
    ZERO of them carrying orchestrator metadata), otherwise
    ``'orchestrator'``.

    Defensive fallback: when qty or price is missing or non-numeric,
    return ``'orchestrator'`` rather than ``'unknown'``.  Per the
    operator's "NO unknowns" directive, the only acceptable fallback
    is system-side — it is strictly safer to mis-attribute a small
    edge-case row to the orchestrator than to leave it unlabeled and
    hide it from both Manual and System dashboard buckets.
    """
    if qty is None or price is None:
        return "orchestrator"
    try:
        # Decimal math (not float) so the $400 boundary is exact —
        # consolidated buckets pass Decimals already, and the crypto
        # path passes floats that may not round-trip cleanly through
        # float multiplication near the threshold.
        notional = Decimal(str(price)) * Decimal(str(qty))
    except (TypeError, ValueError, ArithmeticError):
        return "orchestrator"
    return "manual_ui" if notional > Decimal("400") else "orchestrator"


def _fee_dollars(fill: dict[str, Any]) -> Decimal:
    """Kalshi `fee_cost` is reported as a decimal-dollar string ('0.070000').

    Older payloads may use integer cents — heuristic: if the raw value is
    a non-string and >= 100, treat it as cents.
    """
    raw = fill.get("fee_cost")
    if raw is None:
        return Decimal("0")
    try:
        d = Decimal(str(raw))
    except Exception:
        return Decimal("0")
    # Anything coming back as a string is already in dollars per current API.
    if isinstance(raw, str):
        return d
    # Fallback for legacy integer cents
    if d >= 100:
        return d / Decimal("100")
    return d


async def _existing_trade_ids(trade_ids: list[str]) -> set[str]:
    """Bulk membership check so we don't INSERT-then-CONFLICT 1k times."""
    if not trade_ids:
        return set()
    rows = await db.fetch(
        "SELECT trade_id FROM trades WHERE trade_id = ANY($1)",
        trade_ids,
    )
    return {r["trade_id"] for r in rows}


async def _iter_fills(
    client: KalshiClient,
    *,
    min_ts: int,
    page_size: int = 200,
    max_pages: int | None = None,
):
    """Yield fills page-by-page from `/portfolio/fills`.

    Bypasses the safety cap in `KalshiClient.list_fills` (5 pages, 1000 rows
    total) by walking the cursor ourselves. Returns chunks (list[dict])
    so the caller can batch DB writes.
    """
    cursor: str | None = None
    page = 0
    while True:
        params: dict[str, Any] = {"limit": min(200, max(1, int(page_size))), "min_ts": int(min_ts)}
        if cursor:
            params["cursor"] = cursor
        data = await client._request("GET", "/portfolio/fills", params=params)
        fills = (data or {}).get("fills") or []
        cursor = (data or {}).get("cursor")
        page += 1
        log.debug("page %d: %d fills, cursor=%s", page, len(fills), bool(cursor))
        yield fills
        if not cursor:
            return
        if max_pages is not None and page >= max_pages:
            log.warning("max_pages=%d reached, stopping pagination", max_pages)
            return


async def backfill(
    *,
    since_ts: int,
    max_pages: int | None,
    dry_run: bool,
    verbose: bool,
) -> dict[str, int]:
    settings = get_settings()
    if not settings.kalshi_api_key_id or not settings.kalshi_private_key:
        raise RuntimeError("Kalshi credentials not configured (KALSHI_API_KEY_ID + KALSHI_API_PRIVATE_KEY)")

    log.info(
        "backfilling Kalshi fills since %s (env=%s)",
        datetime.fromtimestamp(since_ts, tz=timezone.utc).isoformat(),
        settings.kalshi_env,
    )

    stats = {
        "fetched": 0, "already_present": 0, "inserted": 0,
        "skipped_bad": 0, "pages": 0,
        "crypto_rows": 0, "consolidated_rows": 0, "consolidated_dropped_pre": 0,
    }

    # Cross-page aggregation buckets for non-crypto. We MUST aggregate
    # across the entire fetch window (not per-page) because a single
    # (ticker, side, day) combo can span multiple API pages — page-local
    # aggregation would write a partial row on page N then drop the
    # remaining sub-fills on page N+1 via ON CONFLICT.
    # Bucket key includes `action` so buy/sell never coalesce into one
    # unsigned-quantity bucket (would distort cost basis).
    non_crypto_agg: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    crypto_rows: list[tuple] = []

    async with KalshiClient(settings=settings, timeout=20.0) as client:
        async for page in _iter_fills(client, min_ts=since_ts, max_pages=max_pages):
            stats["pages"] += 1
            stats["fetched"] += len(page)
            if not page:
                continue

            usable = [f for f in page if f.get("trade_id")]
            stats["skipped_bad"] += len(page) - len(usable)
            if not usable:
                continue

            # For crypto: skip fills already in DB by real trade_id.
            # For non-crypto: we always aggregate (DB uses synthetic
            # daily IDs so per-fill duplicate check is irrelevant).
            crypto_candidates = [f for f in usable if _vertical_for(f.get("ticker") or f.get("market_ticker")) == "crypto"]
            non_crypto_candidates = [f for f in usable if _vertical_for(f.get("ticker") or f.get("market_ticker")) != "crypto"]

            if crypto_candidates:
                ids = [str(f["trade_id"]) for f in crypto_candidates]
                already = await _existing_trade_ids(ids)
                stats["already_present"] += len(already)
                new_crypto = [f for f in crypto_candidates if str(f["trade_id"]) not in already]
            else:
                new_crypto = []

            for f in new_crypto:
                ticker = f.get("ticker") or f.get("market_ticker")
                qty, price = _qty_dollars(f)
                fee = _fee_dollars(f)
                executed_at = _parse_ts(f.get("created_time") or f.get("ts"))
                if executed_at is not None and executed_at.tzinfo is not None:
                    executed_at = executed_at.astimezone(timezone.utc).replace(tzinfo=None)
                side = (f.get("side") or "").lower() or None
                action = (f.get("action") or "").lower() or None
                if qty is None or price is None or executed_at is None or not ticker or not side:
                    stats["skipped_bad"] += 1
                    continue
                initial_pnl = -(qty * price) - fee if action == "buy" else None
                metadata = {
                    "source": "kalshi_fills_backfill",
                    "fill_id": f.get("fill_id"),
                    "order_id": f.get("order_id"),
                    "is_taker": f.get("is_taker"),
                    "subaccount_number": f.get("subaccount_number"),
                    "raw": f,
                }
                crypto_rows.append((
                    str(f["trade_id"]), ticker, None, "crypto", side, action,
                    qty, price, fee, initial_pnl,
                    "live", "kalshi", executed_at,
                    json.dumps(metadata, default=str),
                ))

            for f in non_crypto_candidates:
                ticker = f.get("ticker") or f.get("market_ticker")
                qty, price = _qty_dollars(f)
                fee = _fee_dollars(f)
                executed_at = _parse_ts(f.get("created_time") or f.get("ts"))
                if executed_at is not None and executed_at.tzinfo is not None:
                    executed_at = executed_at.astimezone(timezone.utc).replace(tzinfo=None)
                vertical = _vertical_for(ticker)
                side = (f.get("side") or "").lower() or None
                action = (f.get("action") or "").lower() or None
                if qty is None or price is None or executed_at is None or not ticker or not side:
                    stats["skipped_bad"] += 1
                    continue
                day_str = executed_at.date().isoformat()
                bucket_key = (ticker, side, day_str, action or "buy")
                bucket = non_crypto_agg.setdefault(bucket_key, {
                    "qty": Decimal("0"),
                    "px_weighted": Decimal("0"),
                    "fees": Decimal("0"),
                    "earliest": executed_at,
                    "vertical": vertical,
                    "action": action,
                    "fill_count": 0,
                    "first_trade_id": str(f["trade_id"]),
                    "raw_fill_ids": [],
                })
                bucket["qty"] += qty
                bucket["px_weighted"] += qty * price
                bucket["fees"] += fee
                bucket["fill_count"] += 1
                bucket["raw_fill_ids"].append(f.get("fill_id") or str(f["trade_id"]))
                if executed_at < bucket["earliest"]:
                    bucket["earliest"] = executed_at

            log.info("page %d processed: +%d crypto rows pending, %d non-crypto buckets so far",
                     stats["pages"], len(new_crypto), len(non_crypto_agg))

    # === Done paginating. Now flush. ===
    consolidated_rows: list[tuple] = []
    for (ticker, side, day_str, action), b in non_crypto_agg.items():
        qty = b["qty"]
        if qty <= 0:
            continue
        avg_price = b["px_weighted"] / qty
        fee = b["fees"]
        initial_pnl = -(qty * avg_price) - fee if action == "buy" else None
        # Include action in synthetic ID so buy/sell same-day rows don't collide.
        consol_trade_id = f"consol-{ticker}-{side}-{action}-{day_str}"
        metadata = {
            "source": "kalshi_fills_backfill",
            "consolidated": True,
            "action": action,
            "fill_count": b["fill_count"],
            "raw_fill_ids": b["raw_fill_ids"][:50],
            "first_kalshi_trade_id": b["first_trade_id"],
        }
        consolidated_rows.append((
            consol_trade_id, ticker, None, b["vertical"], side, action,
            qty, avg_price, fee, initial_pnl,
            "live", "kalshi", b["earliest"],
            json.dumps(metadata, default=str),
        ))

    log.info("aggregation complete: %d crypto rows, %d consolidated non-crypto rows",
             len(crypto_rows), len(consolidated_rows))

    if dry_run:
        stats["crypto_rows"] = len(crypto_rows)
        stats["consolidated_rows"] = len(consolidated_rows)
        stats["inserted"] = len(crypto_rows) + len(consolidated_rows)
        if verbose:
            for r in (crypto_rows + consolidated_rows)[:8]:
                log.info("[dry-run] would insert trade_id=%s ticker=%s side=%s qty=%s px=%s ts=%s",
                         r[0], r[1], r[4], r[6], r[7], r[12])
        return stats

    # Idempotent reruns: drop our prior consolidated rows in this
    # window before re-inserting. We touch ONLY rows tagged with our
    # source AND consolidated=true — never the orchestrator's own
    # daily rows or any other writer's rows.
    if consolidated_rows:
        since_dt = datetime.fromtimestamp(since_ts, tz=timezone.utc).replace(tzinfo=None)
        deleted = await db.execute(
            """
            DELETE FROM trades
             WHERE metadata->>'source' = 'kalshi_fills_backfill'
               AND (metadata->>'consolidated')::boolean = true
               AND executed_at >= $1
            """,
            since_dt,
        )
        # asyncpg returns 'DELETE n' status string
        try:
            stats["consolidated_dropped_pre"] = int(str(deleted).split()[-1])
        except Exception:
            pass
        log.info("dropped %s prior consolidated rows in window before re-insert", stats["consolidated_dropped_pre"])

    # Insert crypto first (per-fill, dedup by real trade_id), then consolidated.
    #
    # ``placed_by`` is derived per-row from the operator's $400
    # bimodal heuristic (see migration 0039 docstring + 2026-04-30
    # operator directive in replit.md): ``price * quantity > 400`` ->
    # ``'manual_ui'``, otherwise ``'orchestrator'``.  This script is a
    # SAFETY-NET importer that observes fills already executed on
    # Kalshi, so we can't see the original initiator at backfill time
    # — but the bimodal heuristic is reliable (live probe: 135 / 5,725
    # trades over $400, ZERO of them carrying orchestrator metadata).
    # The ``ON CONFLICT DO NOTHING`` below means any row already
    # inserted by the live path keeps its real provenance; only
    # genuinely-orphaned rows fall through to the heuristic — and
    # crucially, none of them land at ``'unknown'`` anymore (per
    # migration 0040 cleanup + operator's "NO unknowns" directive).
    #
    # Index 6 = quantity, index 7 = price in the row tuples (mirrors
    # the column order on the INSERT below).  ``_tag_placed_by`` is a
    # module-level helper so it can be unit-tested in isolation.
    if crypto_rows:
        crypto_rows_tagged = [(*r, _tag_placed_by(r[6], r[7])) for r in crypto_rows]
        await db.executemany(
            """
            INSERT INTO trades (
                trade_id, market_id, market_title, vertical, side, action,
                quantity, price, fees, pnl,
                trading_mode, exchange, executed_at, metadata, placed_by
            ) VALUES (
                $1, $2, $3, $4, $5, $6,
                $7, $8, $9, $10,
                $11, $12, $13, $14::jsonb, $15
            )
            ON CONFLICT DO NOTHING
            """,
            crypto_rows_tagged,
        )
        stats["crypto_rows"] = len(crypto_rows)
    if consolidated_rows:
        # Live-row safeguard: since live Kalshi WS fills now write
        # per-fill rows into `trades` (see
        # `app.kalshi.lifecycle._insert_live_trade_row`), a
        # consolidated row for the same (ticker, side, action, day)
        # bucket would double-count quantity, fees, and PnL on the
        # dashboard. Drop any consolidated bucket that already has
        # at least one live per-fill row in the window. The live
        # rows ARE the source of truth — they carry the real fill_id,
        # order_id, and accurate per-slice PnL.
        live_keys: set[tuple[str, str, str, str]] = set()
        try:
            live_rows = await db.fetch(
                """
                SELECT market_id, side, action,
                       to_char((executed_at AT TIME ZONE 'UTC')::date, 'YYYY-MM-DD') AS day
                  FROM trades
                 WHERE exchange = 'kalshi'
                   AND trading_mode = 'live'
                   AND metadata->>'source' = 'kalshi_ws_fill'
                   AND executed_at >= $1
                """,
                datetime.fromtimestamp(since_ts, tz=timezone.utc).replace(tzinfo=None),
            )
            for lr in live_rows:
                live_keys.add((
                    str(lr["market_id"]),
                    str(lr["side"]),
                    str(lr["action"]),
                    str(lr["day"]),
                ))
        except Exception as e:
            log.warning("live-row safeguard query failed; inserting all consolidated rows: %s", e)
        if live_keys:
            before = len(consolidated_rows)
            consolidated_rows = [
                r for r in consolidated_rows
                # row tuple: (trade_id, ticker, _, vertical, side, action, qty, price, ...,
                #             executed_at @ index 12)
                if (str(r[1]), str(r[4]), str(r[5]),
                    r[12].date().isoformat() if hasattr(r[12], "date") else "") not in live_keys
            ]
            stats["consolidated_dropped_live_overlap"] = before - len(consolidated_rows)
            if stats["consolidated_dropped_live_overlap"]:
                log.info(
                    "skipped %d consolidated rows that overlap live WS per-fill rows",
                    stats["consolidated_dropped_live_overlap"],
                )
        consolidated_rows_tagged = [(*r, _tag_placed_by(r[6], r[7])) for r in consolidated_rows]
        await db.executemany(
            """
            INSERT INTO trades (
                trade_id, market_id, market_title, vertical, side, action,
                quantity, price, fees, pnl,
                trading_mode, exchange, executed_at, metadata, placed_by
            ) VALUES (
                $1, $2, $3, $4, $5, $6,
                $7, $8, $9, $10,
                $11, $12, $13, $14::jsonb, $15
            )
            ON CONFLICT DO NOTHING
            """,
            consolidated_rows_tagged,
        )
        stats["consolidated_rows"] = len(consolidated_rows)
    stats["inserted"] = stats["crypto_rows"] + stats["consolidated_rows"]
    return stats


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = p.add_mutually_exclusive_group()
    g.add_argument("--since", type=str, help="ISO date (e.g. 2025-01-01) or unix epoch seconds")
    g.add_argument("--since-days", type=int, default=30, help="Backfill this many days of history (default 30)")
    p.add_argument("--max-pages", type=int, default=None, help="Cap pagination (default: walk full history)")
    p.add_argument("--dry-run", action="store_true", help="Fetch + map, but don't INSERT")
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args()


def _resolve_since(args: argparse.Namespace) -> int:
    if args.since:
        s = args.since
        if s.isdigit():
            return int(s)
        # ISO date
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    return int(time.time() - args.since_days * 86400)


def main() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    since_ts = _resolve_since(args)

    stats = asyncio.run(backfill(
        since_ts=since_ts,
        max_pages=args.max_pages,
        dry_run=args.dry_run,
        verbose=args.verbose,
    ))

    print()
    print("=" * 60)
    print("Kalshi fills backfill complete")
    print("=" * 60)
    print(f"  pages walked      : {stats['pages']}")
    print(f"  fills fetched     : {stats['fetched']}")
    print(f"  already in DB     : {stats['already_present']}")
    print(f"  rows inserted     : {stats['inserted']}{' (dry-run)' if args.dry_run else ''}")
    print(f"  skipped (no id)   : {stats['skipped_bad']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
