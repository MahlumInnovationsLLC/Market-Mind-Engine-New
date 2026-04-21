"""Operator CLI: backfill historical Kalshi settlements.

Replays every market with un-settled trades through the settlement
pipeline so post-mortems and persona Brier scores get caught up.

Usage (from the api-server directory):

    # Pull resolutions from Kalshi for every un-settled market
    python scripts/backfill_kalshi_settlements.py

    # Limit to the first 100 markets (smoke test)
    python scripts/backfill_kalshi_settlements.py --limit 100

    # Use an operator-supplied CSV instead of contacting Kalshi
    python scripts/backfill_kalshi_settlements.py --csv resolved.csv

CSV format (header optional):
    market_id,outcome_yes[,settle_price]
    KXNFL-...,1
    KXBTC-...,0,42.5

The job is idempotent: trades that already have `settled_at` are
skipped automatically.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

# Allow running as `python scripts/backfill_kalshi_settlements.py` from api-server.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db  # noqa: E402
from app.settlement import get_backfill_status, run_backfill  # noqa: E402


def _print_progress(last_line: list[str]) -> None:
    s = get_backfill_status()
    line = (
        f"[{s['state']}] processed={s['markets_processed']}/{s['markets_total']} "
        f"settled={s['markets_settled']} unresolved={s['markets_skipped_unresolved']} "
        f"failed={s['markets_failed']} trades={s['trades_settled']} "
        f"personas={s['persona_metrics_updated']} alerts={s['alerts_raised']}"
        f" current={s['current_market'] or '-'}"
    )
    if line != (last_line[0] if last_line else None):
        print(line, flush=True)
        if last_line:
            last_line[0] = line
        else:
            last_line.append(line)


async def _main(args: argparse.Namespace) -> int:
    last: list[str] = []
    t0 = time.perf_counter()

    def _on_progress() -> None:
        _print_progress(last)

    final = await run_backfill(
        csv_path=args.csv, limit=args.limit, on_progress=_on_progress
    )
    print("\n--- backfill complete ---")
    print(json.dumps(final, indent=2, default=str))
    print(f"wall_clock={time.perf_counter() - t0:.1f}s")
    await db.close_pool()
    return 0 if final.get("state") == "completed" else 1


def main() -> int:
    p = argparse.ArgumentParser(
        description="Backfill historical Kalshi settlements for un-settled trades."
    )
    p.add_argument(
        "--csv", default=None,
        help="Optional CSV of operator-supplied results "
             "(market_id,outcome_yes[,settle_price]).",
    )
    p.add_argument(
        "--limit", type=int, default=None,
        help="Cap how many un-settled markets to process this run.",
    )
    args = p.parse_args()
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
