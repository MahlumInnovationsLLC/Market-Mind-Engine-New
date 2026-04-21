"""Operator CLI: submit a $1 paper order through the lifecycle.

Run from the api-server directory:

    python scripts/submit_paper_order.py KXBTC15M-26APR201715-15 --side yes --price 50

Writes a row to `order_lifecycle` (paper=true), publishes to the in-process
bus, and prints the resulting record. NEVER touches Kalshi.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Allow running as `python scripts/submit_paper_order.py` from the api-server dir.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db  # noqa: E402
from app.kalshi import OrderRequest, submit_order  # noqa: E402


async def _main(args: argparse.Namespace) -> int:
    req = OrderRequest(
        market_ticker=args.ticker,
        side=args.side,
        action=args.action,
        count=args.count,
        limit_price_cents=args.price,
        type=args.type,
        time_in_force=args.tif,
        vertical=args.vertical,
        context={"source": "submit_paper_order.py"},
    )
    rec = await submit_order(req, force_paper=True)
    print(json.dumps(rec.to_dict(), default=str, indent=2))
    await db.close_pool()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Submit a $1 paper order to the Kalshi lifecycle.")
    p.add_argument("ticker", help="Kalshi market ticker, e.g. KXBTC15M-26APR201715-15")
    p.add_argument("--side", choices=["yes", "no"], default="yes")
    p.add_argument("--action", choices=["buy", "sell"], default="buy")
    p.add_argument("--count", type=int, default=1, help="contracts (default 1 = ~$1 at 50¢)")
    p.add_argument("--price", type=int, default=50, help="limit price in cents (1..99)")
    p.add_argument("--type", choices=["limit", "market"], default="limit")
    p.add_argument("--tif", choices=["GTC", "IOC", "FOK"], default="GTC")
    p.add_argument("--vertical", default="sports")
    args = p.parse_args()
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
