"""Operator CLI: prime `crypto_ohlcv` with the last N days of bars.

Usage (from the api-server directory):

    # Default: 90 days of BTC + ETH at 1m / 5m / 15m / 1h
    python scripts/backfill_crypto.py

    # Smaller smoke window
    python scripts/backfill_crypto.py --days 7

    # Limit to BTC only
    python scripts/backfill_crypto.py --symbols BTC

The job is idempotent: the (symbol, granularity, ts) unique index on
`crypto_ohlcv` collapses re-runs into upserts.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

# Allow running as `python scripts/backfill_crypto.py` from api-server.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db  # noqa: E402
from app.feeds.crypto import backfill  # noqa: E402


async def _main(args: argparse.Namespace) -> int:
    t0 = time.perf_counter()
    symbols = [s.strip().upper() for s in (args.symbols or "").split(",") if s.strip()] or None
    summary = await backfill(days=args.days, symbols=symbols)
    print("--- crypto backfill complete ---")
    print(json.dumps(summary, indent=2))
    print(f"total_bars={sum(summary.values())} wall_clock={time.perf_counter() - t0:.1f}s")
    await db.close_pool()
    return 0 if any(v > 0 for v in summary.values()) else 1


def main() -> int:
    p = argparse.ArgumentParser(
        description="Backfill historical crypto OHLCV bars (CoinAPI primary, CoinGecko fallback)."
    )
    p.add_argument("--days", type=int, default=90, help="History window in days (default 90).")
    p.add_argument(
        "--symbols", default=None,
        help="Comma-separated symbols to backfill (default BTC,ETH).",
    )
    args = p.parse_args()
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
