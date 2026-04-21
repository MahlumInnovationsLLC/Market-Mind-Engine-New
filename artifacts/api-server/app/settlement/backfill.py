"""One-shot backfill of historical Kalshi settlements.

There are thousands of un-settled Kalshi trades sitting in `trades`
because the periodic settlement scheduler only began running recently.
Without their post-mortems and persona Brier updates, the gatekeeper
swarm cannot weight personas by their real historical accuracy.

This module replays every market that still has un-settled trades:
for each one we either look up the final result on Kalshi or use an
operator-supplied CSV (``market_id,outcome_yes[,settle_price]``), then
hand off to `settle_market()` which already handles every downstream
write idempotently.

Re-running the backfill is safe: `settle_market` only updates rows
where ``settled_at IS NULL`` and the postmortem / online-learning
inserts are guarded by trade-id existence checks.
"""
from __future__ import annotations

import asyncio
import csv
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .. import db
from .pipeline import _parse_kalshi_outcome, settle_market


log = logging.getLogger(__name__)


@dataclass
class BackfillProgress:
    state: str = "idle"          # idle | running | completed | failed
    started_at: str | None = None
    completed_at: str | None = None
    duration_seconds: float = 0.0
    source: str | None = None    # "kalshi" | "csv"
    markets_total: int = 0
    markets_processed: int = 0
    markets_settled: int = 0
    markets_skipped_unresolved: int = 0
    markets_failed: int = 0
    trades_settled: int = 0
    persona_metrics_updated: int = 0
    alerts_raised: int = 0
    current_market: str | None = None
    errors: list[str] = field(default_factory=list)
    last_error: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_seconds": round(self.duration_seconds, 3),
            "source": self.source,
            "markets_total": self.markets_total,
            "markets_processed": self.markets_processed,
            "markets_settled": self.markets_settled,
            "markets_skipped_unresolved": self.markets_skipped_unresolved,
            "markets_failed": self.markets_failed,
            "trades_settled": self.trades_settled,
            "persona_metrics_updated": self.persona_metrics_updated,
            "alerts_raised": self.alerts_raised,
            "current_market": self.current_market,
            "errors": list(self.errors[-10:]),
            "last_error": self.last_error,
        }


# Shared module-level state — one backfill at a time.  An asyncio.Lock
# enforces "no concurrent backfills"; the operator-console endpoint
# returns 409 if another run is in flight.
_progress = BackfillProgress()
_lock = asyncio.Lock()


def get_status() -> dict[str, Any]:
    return _progress.snapshot()


def is_running() -> bool:
    """True iff a backfill run currently holds the single-flight lock.

    Used by the API endpoint to deterministically reject concurrent
    POSTs (the in-process state field flips to "running" only after
    `run_backfill` actually starts, which is racy against two near-
    simultaneous start requests)."""
    return _lock.locked()


async def _list_unsettled_kalshi_markets() -> list[str]:
    rows = await db.fetch(
        """
        SELECT DISTINCT market_id
          FROM trades
         WHERE settled_at IS NULL
           AND exchange = 'kalshi'
           AND market_id IS NOT NULL
         ORDER BY market_id
        """,
    )
    return [r["market_id"] for r in rows]


def _load_csv_overrides(csv_path: str) -> dict[str, tuple[int, float | None]]:
    """Parse an operator-supplied CSV.

    Format (header optional):
        market_id,outcome_yes[,settle_price]
        KXNFL-...,1
        KXBTC-...,0,42.5
    """
    overrides: dict[str, tuple[int, float | None]] = {}
    with open(csv_path, "r", newline="") as fh:
        reader = csv.reader(fh)
        for i, row in enumerate(reader):
            if not row or all(not c.strip() for c in row):
                continue
            # Skip header row
            if i == 0 and not row[1].strip().lstrip("-").isdigit():
                continue
            mid = row[0].strip()
            try:
                outcome = int(row[1].strip())
            except (ValueError, IndexError):
                log.warning("backfill csv: skipping bad row %d: %r", i, row)
                continue
            if outcome not in (0, 1):
                log.warning("backfill csv: outcome must be 0/1, got %r", outcome)
                continue
            settle: float | None = None
            if len(row) >= 3 and row[2].strip():
                try:
                    settle = float(row[2].strip())
                except ValueError:
                    settle = None
            overrides[mid] = (outcome, settle)
    return overrides


async def _resolve_via_kalshi(market_ids: list[str], on_progress) -> int:
    """Drive `settle_market` for any market Kalshi reports as final.

    Returns the count of markets we actually settled.  Markets not yet
    resolved on Kalshi are recorded as `markets_skipped_unresolved` so
    the operator can see how many are still pending.
    """
    from ..config import get_settings
    from ..kalshi.client import KalshiAPIError, KalshiClient

    s = get_settings()
    if not s.kalshi_api_key_id or not s.kalshi_private_key:
        raise RuntimeError(
            "kalshi credentials missing — supply --csv or configure KALSHI_API_KEY_ID/KALSHI_PRIVATE_KEY"
        )

    settled = 0
    async with KalshiClient(settings=s, timeout=10.0) as kc:
        for mid in market_ids:
            _progress.current_market = mid
            try:
                raw = await kc.get_market(mid)
            except KalshiAPIError as e:
                _progress.markets_failed += 1
                msg = f"{mid}: kalshi {e.status}"
                _progress.errors.append(msg)
                _progress.last_error = msg
                _progress.markets_processed += 1
                on_progress()
                continue
            except Exception as e:
                _progress.markets_failed += 1
                msg = f"{mid}: {e}"
                _progress.errors.append(msg)
                _progress.last_error = msg
                _progress.markets_processed += 1
                on_progress()
                continue

            parsed = _parse_kalshi_outcome(raw)
            if not parsed:
                _progress.markets_skipped_unresolved += 1
                _progress.markets_processed += 1
                on_progress()
                continue
            outcome_yes, settle_price = parsed
            try:
                res = await settle_market(
                    mid, outcome_yes=outcome_yes, settle_price=settle_price
                )
                _progress.markets_settled += 1
                _progress.trades_settled += int(res.get("trades_settled", 0))
                _progress.persona_metrics_updated += int(
                    res.get("persona_metrics_updated", 0)
                )
                _progress.alerts_raised += int(res.get("alerts_raised", 0))
                settled += 1
            except Exception as e:
                _progress.markets_failed += 1
                msg = f"{mid} settle: {e}"
                _progress.errors.append(msg)
                _progress.last_error = msg
            _progress.markets_processed += 1
            on_progress()
    return settled


async def _resolve_via_csv(
    market_ids: list[str],
    overrides: dict[str, tuple[int, float | None]],
    on_progress,
) -> int:
    settled = 0
    for mid in market_ids:
        _progress.current_market = mid
        if mid not in overrides:
            _progress.markets_skipped_unresolved += 1
            _progress.markets_processed += 1
            on_progress()
            continue
        outcome_yes, settle_price = overrides[mid]
        try:
            res = await settle_market(
                mid, outcome_yes=outcome_yes, settle_price=settle_price
            )
            _progress.markets_settled += 1
            _progress.trades_settled += int(res.get("trades_settled", 0))
            _progress.persona_metrics_updated += int(
                res.get("persona_metrics_updated", 0)
            )
            _progress.alerts_raised += int(res.get("alerts_raised", 0))
            settled += 1
        except Exception as e:
            _progress.markets_failed += 1
            msg = f"{mid} settle: {e}"
            _progress.errors.append(msg)
            _progress.last_error = msg
        _progress.markets_processed += 1
        on_progress()
    return settled


async def run_backfill(
    *,
    csv_path: str | None = None,
    limit: int | None = None,
    on_progress=None,
) -> dict[str, Any]:
    """Replay every un-settled Kalshi market through `settle_market`.

    Args:
        csv_path: optional path to ``market_id,outcome_yes[,settle_price]``
            overrides.  When provided, *only* CSV markets are settled
            (Kalshi is not contacted) and any un-settled markets missing
            from the CSV are reported as ``markets_skipped_unresolved``.
        limit: cap how many markets to process this run (handy for ops dry
            runs over a few thousand markets).
        on_progress: optional callback invoked after each market with the
            current snapshot.  Used by the CLI to print live progress.
    """
    if _lock.locked():
        raise RuntimeError("backfill already running")
    on_progress = on_progress or (lambda: None)
    async with _lock:
        global _progress
        _progress = BackfillProgress(
            state="running",
            started_at=datetime.now(timezone.utc).isoformat(),
            source="csv" if csv_path else "kalshi",
        )
        on_progress()
        t0 = time.perf_counter()
        try:
            market_ids = await _list_unsettled_kalshi_markets()
            if limit is not None:
                market_ids = market_ids[: max(0, int(limit))]
            _progress.markets_total = len(market_ids)
            on_progress()

            if not market_ids:
                _progress.state = "completed"
            else:
                if csv_path:
                    overrides = _load_csv_overrides(csv_path)
                    await _resolve_via_csv(market_ids, overrides, on_progress)
                else:
                    await _resolve_via_kalshi(market_ids, on_progress)
                _progress.state = "completed"
        except Exception as e:
            _progress.state = "failed"
            _progress.last_error = str(e)
            _progress.errors.append(str(e))
            log.exception("backfill failed: %s", e)
        finally:
            _progress.current_market = None
            _progress.duration_seconds = time.perf_counter() - t0
            _progress.completed_at = datetime.now(timezone.utc).isoformat()
            on_progress()
        return _progress.snapshot()
