"""Preflight gate 4/4: one full sports-orchestrator cycle in paper mode.

Invokes ``app.orchestrator.cycle.run_cycle('sports')`` exactly once.
A clean run end-to-end exercises:

    * candidate fetch (predictions + Kalshi prices)
    * the meta-model + per-league calibrators
    * the 17 risk gates
    * the swarm debate path (gatekeeper veto evaluation)
    * the paper-execution stub (NO live order goes out — gated by
      ``AXIOM_TRADING_MODE``)

Pass criterion: ``run_cycle`` returns a ``CycleResult`` (or compatible
dict-like) without raising. Zero placed orders is fine — the gate
proves the loop completes a cycle, not that the swarm approved a
trade.

This script DELIBERATELY refuses to run unless ``AXIOM_TRADING_MODE``
is explicitly set to ``paper``: the bash wrapper passes it, but a
human running this directly should fail loudly rather than risk
accidentally hitting the live execution path.

Run with::

    AXIOM_TRADING_MODE=paper /home/runner/workspace/.pythonlibs/bin/python3 \\
        artifacts/api-server/scripts/preflight_paper_cycle.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


async def _run() -> int:
    from app.orchestrator.cycle import run_cycle  # late import for env effects

    started = time.perf_counter()
    try:
        result = await run_cycle("sports")
    except Exception as e:  # noqa: BLE001
        print(
            f"[preflight paper] FAIL — run_cycle('sports') raised: {e}",
            file=sys.stderr,
        )
        return 1
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    # CycleResult is a dataclass; fall back to dict for forward compat.
    # Field names match `app.orchestrator.cycle.CycleResult` exactly so
    # the operator-facing summary doesn't quietly print zeroes when the
    # dataclass evolves (e.g. a renamed counter). `errors` is a list on
    # the live struct — surface its length, not the list itself.
    def _get(obj: object, name: str, default: object = None) -> object:
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)

    fresh_debates = _get(result, "fresh_debates", 0) or 0
    picks_evaluated = _get(result, "picks_evaluated", 0) or 0
    trades_placed = _get(result, "trades_placed", 0) or 0
    raw_errors = _get(result, "errors", []) or []
    error_count = len(raw_errors) if isinstance(raw_errors, (list, tuple)) else int(raw_errors)
    print(
        f"[preflight paper] PASS — vertical=sports mode=paper "
        f"fresh_debates={fresh_debates} picks_evaluated={picks_evaluated} "
        f"trades_placed={trades_placed} errors={error_count} "
        f"elapsed_ms={elapsed_ms:.0f}"
    )
    return 0


def main() -> int:
    mode = (os.getenv("AXIOM_TRADING_MODE") or "").lower()
    if mode != "paper":
        print(
            "[preflight paper] REFUSED — AXIOM_TRADING_MODE must be set to "
            "'paper' to run this gate (got: "
            f"{mode!r}). Aborting to avoid accidental live execution.",
            file=sys.stderr,
        )
        return 1
    try:
        return asyncio.run(_run())
    finally:
        try:
            from app import db
            asyncio.run(db.close_pool())
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
