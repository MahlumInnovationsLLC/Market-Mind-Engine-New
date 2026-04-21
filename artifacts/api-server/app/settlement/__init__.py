"""Market settlement -> post-mortems -> persona update -> drift wiring.

Public entry points:
- run_cycle()                 — one-shot settlement sweep over open kalshi positions
- settle_market(market_id, outcome, settle_price=None) — manual/backfill entry
- scheduler                   — long-running periodic settlement loop
- settlement_event_hub        — pub/sub for the settlement pipeline UI
"""
from __future__ import annotations

from .pipeline import settle_market, run_cycle, CycleResult
from .scheduler import scheduler
from .backfill import (
    run_backfill,
    get_status as get_backfill_status,
    is_running as backfill_is_running,
)
from .streams import settlement_event_hub

__all__ = [
    "settle_market",
    "run_cycle",
    "run_backfill",
    "get_backfill_status",
    "CycleResult",
    "scheduler",
    "settlement_event_hub",
]
