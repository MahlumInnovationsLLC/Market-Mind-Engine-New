"""Operator CLI: backfill rolled-up `positions` rows for historical orders.

Task #103 added partial-fill rollup going forward (see
``app.kalshi.lifecycle._upsert_position_rollup``), but orders that filled
in pieces *before* that change still have one ``order_lifecycle`` row per
partial and no rolled-up ``positions`` row keyed by ``order_id``. The PnL
post-mortem pipeline reads from ``positions``, so for those historical
orders it sees fragmented data.

This one-shot job walks every ``order_id`` that has at least one
``filled`` row in ``order_lifecycle`` and writes (or updates) a single
rolled-up ``positions`` row mirroring the live rollup logic:

  * ``quantity``      — sum of partial-fill counts, CAPPED at the
                        order's requested size (the ``submitted`` row's
                        ``contracts``). Over-fill never inflates exposure.
  * ``avg_price``     — weighted average price across partials, expressed
                        as a unit price (0..1) to match the convention
                        used by ``orchestrator/paper.py`` so ``_trade_pnl``
                        stays consistent.
  * ``filled_count``  — same as quantity (mirrors lifecycle behaviour).
  * ``over_filled``   — true when raw fill total exceeded the request.
  * ``status``        — ``closed`` if filled to capacity, else ``open``.

Idempotency: the script SKIPS any ``order_id`` that already has a row in
``positions`` keyed by ``order_id``, so re-running is safe and won't
overwrite values written by the live rollup path.

Usage (from the api-server directory):

    # Replay every historical order that lacks a rolled-up position
    python scripts/backfill_position_rollups.py

    # Smoke test: process at most 50 orders
    python scripts/backfill_position_rollups.py --limit 50

    # Dry run: report what would change without writing
    python scripts/backfill_position_rollups.py --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

# Allow `python scripts/backfill_position_rollups.py` from api-server.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db  # noqa: E402

log = logging.getLogger("backfill_position_rollups")


@dataclass
class BackfillResult:
    scanned: int = 0
    skipped_existing: int = 0
    skipped_no_request: int = 0
    skipped_no_fills: int = 0
    written: int = 0
    over_filled: int = 0


# Pull every individual `filled` lifecycle row, ordered chronologically
# within each order. We accept partials in arrival order — same as the
# live ``handle_ws_fill`` path — so an over-fill's discarded contracts
# never contribute to the weighted-average price.
_FILLS_SQL = """
SELECT
    order_id,
    id,
    contracts,
    price_cents,
    side,
    market_id,
    vertical
FROM order_lifecycle
WHERE event_type = 'filled'
ORDER BY order_id, id
"""

# Original requested size — pulled from the `submitted` row that wrote
# the order to the audit log. Used as the cap for over-fill detection.
_REQUESTED_SQL = """
SELECT order_id, MAX(contracts)::bigint AS requested_count
FROM order_lifecycle
WHERE event_type = 'submitted'
GROUP BY order_id
"""


async def _existing_position_ids() -> set[str]:
    """Return order_ids that already have a rolled-up positions row."""
    rows = await db.fetch(
        "SELECT order_id FROM positions WHERE order_id IS NOT NULL"
    )
    return {r["order_id"] for r in rows if r["order_id"]}


async def _insert_position(
    *,
    order_id: str,
    market_id: str,
    side: str,
    vertical: str,
    quantity: int,
    avg_price_cents: int,
    over_filled: bool,
    closed: bool,
) -> None:
    """Mirror app.kalshi.lifecycle._upsert_position_rollup for backfill.

    Uses the same `ON CONFLICT (order_id) DO UPDATE` shape so re-runs
    after a partially-failed batch are safe; the existence check in the
    main loop prevents stomping on rows the live path has already
    written. The unit-price (0..1) convention matches the live helper.
    """
    avg_unit = float(avg_price_cents) / 100.0
    status = "closed" if closed and quantity > 0 else "open"
    # `ON CONFLICT DO NOTHING` (NOT `DO UPDATE`) so a row written by the
    # live rollup path between our snapshot of `existing_position_ids`
    # and this INSERT is never overwritten. The pre-check still does the
    # bulk filtering; this is the strict-non-overwrite safety net.
    await db.execute(
        """
        INSERT INTO positions (
            order_id, market_id, side, vertical, exchange,
            quantity, avg_price, status, filled_count, over_filled
        ) VALUES (
            $1, $2, $3, $4, 'kalshi',
            $5, $6, $7, $5, $8
        )
        ON CONFLICT (order_id) DO NOTHING
        """,
        order_id, market_id, side, vertical,
        int(quantity), avg_unit, status, bool(over_filled),
    )


def _rollup_one_order(
    fills: list[dict[str, Any]], requested: int
) -> tuple[int, int, bool]:
    """Replay an order's partials in arrival order, capping at requested.

    Mirrors ``app.kalshi.lifecycle.handle_ws_fill``: each fill is
    accepted only up to the order's remaining capacity, and the
    weighted-average price is computed over ACCEPTED contracts only —
    discarded contracts from an over-fill never skew the price.

    Returns ``(capped_quantity, avg_price_cents, over_filled)``.
    """
    accepted_qty = 0
    weighted_price_qty = 0
    over_filled = False
    for f in fills:
        count = int(f["contracts"] or 0)
        price = int(f["price_cents"] or 0)
        if count <= 0:
            continue
        remaining = max(0, requested - accepted_qty) if requested > 0 else count
        accepted = min(count, remaining)
        if count > remaining and requested > 0:
            over_filled = True
        if accepted <= 0:
            continue
        weighted_price_qty += price * accepted
        accepted_qty += accepted
    avg_price_cents = (
        int(round(weighted_price_qty / accepted_qty)) if accepted_qty > 0 else 0
    )
    return accepted_qty, avg_price_cents, over_filled


async def run_backfill(
    *, limit: int | None = None, dry_run: bool = False
) -> BackfillResult:
    """Walk historical filled orders and write missing position rollups."""
    res = BackfillResult()
    existing = await _existing_position_ids()
    fill_rows = await db.fetch(_FILLS_SQL)
    requested_rows = await db.fetch(_REQUESTED_SQL)
    requested_by_order: dict[str, int] = {
        r["order_id"]: int(r["requested_count"] or 0) for r in requested_rows
    }

    # Group fills by order_id, preserving the (order_id, id) ordering
    # the SQL already produced.
    fills_by_order: dict[str, list[dict[str, Any]]] = {}
    for r in fill_rows:
        fills_by_order.setdefault(r["order_id"], []).append(dict(r))

    for order_id, fills in fills_by_order.items():
        res.scanned += 1
        if order_id in existing:
            res.skipped_existing += 1
            continue
        if not fills:
            res.skipped_no_fills += 1
            continue

        if order_id not in requested_by_order:
            # No `submitted` row to anchor the cap. Don't guess — the
            # live rollup helper assumes the request is the source of
            # truth for over-fill detection. Skip and surface the count.
            res.skipped_no_request += 1
            log.warning(
                "skipping %s: no `submitted` lifecycle row to size the cap",
                order_id,
            )
            continue
        requested = requested_by_order[order_id]

        capped_qty, avg_price_cents, over_filled = _rollup_one_order(
            fills, requested
        )
        if capped_qty <= 0:
            res.skipped_no_fills += 1
            continue

        if over_filled:
            res.over_filled += 1

        first = fills[0]
        if dry_run:
            log.info(
                "[dry-run] would write order_id=%s qty=%d avg=%dc over_filled=%s",
                order_id, capped_qty, avg_price_cents, over_filled,
            )
        else:
            # Mirror app.kalshi.lifecycle.handle_ws_fill — the live
            # rollup path always writes status='open' (closed=False) and
            # leaves the settlement pipeline to flip rows to 'closed'.
            # Matching keeps `coalesce(status,'open')='open'` reads
            # consistent between historical and live rollups.
            await _insert_position(
                order_id=order_id,
                market_id=first["market_id"],
                side=first["side"],
                vertical=first["vertical"] or "sports",
                quantity=capped_qty,
                avg_price_cents=avg_price_cents,
                over_filled=over_filled,
                closed=False,
            )
        res.written += 1

        if limit is not None and res.written >= limit:
            break

    return res


def _summary(res: BackfillResult, *, dry_run: bool) -> str:
    verb = "would write" if dry_run else "wrote"
    return (
        f"scanned={res.scanned} {verb}={res.written} "
        f"skipped_existing={res.skipped_existing} "
        f"skipped_no_request={res.skipped_no_request} "
        f"skipped_no_fills={res.skipped_no_fills} "
        f"over_filled={res.over_filled}"
    )


async def _main(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    res = await run_backfill(limit=args.limit, dry_run=args.dry_run)
    print(_summary(res, dry_run=args.dry_run))
    await db.close_pool()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Backfill rolled-up `positions` rows for historical "
                    "orders that filled in pieces before task #103."
    )
    p.add_argument(
        "--limit", type=int, default=None,
        help="Cap how many missing-rollup orders to write this run.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Report what would change without touching `positions`.",
    )
    args = p.parse_args()
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
