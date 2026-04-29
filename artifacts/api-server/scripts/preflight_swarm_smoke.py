"""Preflight gate 3/4: end-to-end swarm round-trip.

Runs ``app.swarm.runner.run_debate`` against a synthetic candidate so
the operator can confirm — before flipping the trading mode to live —
that the configured personas, the LLM provider routing (cloud / local
/ hybrid via ``AXIOM_LLM_MODE``), the moderator round, and the
gatekeeper veto all complete without raising. Exit code is the gate.

Pass criterion (all of):
    * ``run_debate`` returns a dict.
    * ``transcript`` is present and non-empty (at least one persona
      emitted a verdict — partial swarms don't count as a pass).
    * No exception escapes ``run_debate``.

Run with::

    AXIOM_LLM_MODE=local /home/runner/workspace/.pythonlibs/bin/python3 \\
        artifacts/api-server/scripts/preflight_swarm_smoke.py

The candidate uses ``vertical='sports'`` + ``league='NBA'`` so the
persona resolver lands on the same configuration the live trading
loop uses for sports markets — anything narrower would risk passing
the gate against an unused persona slot.
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


SYNTHETIC_CANDIDATE: dict[str, object] = {
    "game_id": f"preflight-{int(time.time())}",
    "matchup": "Preflight Lakers @ Preflight Warriors",
    "league": "NBA",
    "vertical": "sports",
    "market_type": "moneyline",
    "side": "home",
    "current_price": 0.524,
    "kalshi_price": 0.524,
    "context": (
        "Preflight smoke test only. Lakers 7-3 last 10. Warriors missing "
        "their PG (knee). Implied YES probability 52.4%. This candidate "
        "must NOT result in a real order — preflight runs against the "
        "swarm runner only and never touches the execution path."
    ),
}


async def _run() -> int:
    from app.swarm.runner import run_debate  # late import for env effects

    started = time.perf_counter()
    try:
        result = await run_debate(SYNTHETIC_CANDIDATE)
    except Exception as e:  # noqa: BLE001
        print(f"[preflight swarm] FAIL — run_debate raised: {e}", file=sys.stderr)
        return 1
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    if not isinstance(result, dict):
        print(
            f"[preflight swarm] FAIL — run_debate returned {type(result).__name__}, "
            "expected dict",
            file=sys.stderr,
        )
        return 1

    transcript = result.get("transcript") or []
    if not transcript:
        print(
            "[preflight swarm] FAIL — empty transcript (no persona emitted a "
            "verdict)",
            file=sys.stderr,
        )
        return 1

    verdict = result.get("verdict") or result.get("decision")
    confidence = result.get("confidence")
    providers = sorted({
        str(t.get("provider") or "?")
        for t in transcript
        if isinstance(t, dict)
    })
    mode = os.getenv("AXIOM_LLM_MODE", "cloud")
    print(
        f"[preflight swarm] PASS — mode={mode} verdict={verdict!r} "
        f"confidence={confidence!r} providers={providers} "
        f"transcript_rows={len(transcript)} elapsed_ms={elapsed_ms:.0f}"
    )
    return 0


def main() -> int:
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
