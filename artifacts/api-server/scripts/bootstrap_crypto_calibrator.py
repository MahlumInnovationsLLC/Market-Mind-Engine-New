"""Bootstrap fit for the per-league CRYPTO calibrator (task #754).

V1's archive (``v1_archive.trade_analyses``) carries the entire
labelled crypto-binary backlog: ~595k rows that V1 wrote with
``vertical='crypto'`` and the V1 ``league='unknown'`` sentinel.  The
nightly per-league refit (``app.calibrators.refit_all``) won't pick
these up because:

* The V1 CTE filters on ``created_at > now() - interval '90 days'``
  but the archive ``created_at`` reflects when V1 *placed* the trade,
  which is mostly months/years old.
* Even with the side-flip + crypto-vertical fold-in landed in
  ``app/calibrators.py::_fetch_training_data`` for forward
  compatibility, the 595k existing rows still won't pass the
  90-day filter.

So this is a one-shot bootstrap that scans the *full* V1 archive
crypto subset, fits an isotonic regressor on
``(predicted_prob, actual_outcome)`` pairs, and persists a
``calibration_snapshots`` row with
``sub_category='per_league:CRYPTO'`` so the calibrator loader picks
it up on the next ``load_all()``.

Idempotent: each invocation appends a new snapshot row.  The loader
picks the most recent ``per_league:CRYPTO`` row at boot, so running
this twice is harmless — the second fit just supersedes the first.

Usage::

    python -m scripts.bootstrap_crypto_calibrator
    python -m scripts.bootstrap_crypto_calibrator --dry-run
    python -m scripts.bootstrap_crypto_calibrator --max-rows 100000

The script prints a JSON summary to stdout (sample count, Brier
before/after, snapshot id) so a CI/runbook step can grep on it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve()
_API_SERVER_ROOT = _HERE.parent.parent
if str(_API_SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_SERVER_ROOT))

from app import calibrators, db  # noqa: E402


_SELECT_SQL = """
    SELECT predicted_prob::float       AS predicted_prob,
           actual_outcome::int         AS actual_outcome
      FROM v1_archive.trade_analyses
     WHERE lower(coalesce(vertical, '')) = 'crypto'
       AND predicted_prob IS NOT NULL
       AND actual_outcome IS NOT NULL
       AND actual_outcome IN (0, 1)
"""


async def _fetch_pairs(max_rows: int | None) -> tuple[list[float], list[int]]:
    """Pull (predicted_prob, actual_outcome) pairs from the V1 archive.

    Bounds ``predicted_prob`` to ``[0, 1]`` defensively because V1
    recorded a non-trivial fraction of >1.0 entries (per
    migration 0037) — those are model misfit noise that the
    calibrator we're about to fit is meant to correct, but feeding
    raw out-of-range floats into IsotonicRegression will distort
    the fit.
    """
    present = await db.fetchval(
        "SELECT to_regclass('v1_archive.trade_analyses') IS NOT NULL"
    )
    if not present:
        raise SystemExit(
            "v1_archive.trade_analyses not found — has migration 0037 "
            "been applied and the V1 import landed?"
        )

    sql = _SELECT_SQL
    if max_rows is not None and max_rows > 0:
        sql = sql + f"\n     LIMIT {int(max_rows)}"

    rows = await db.fetch(sql)
    probs: list[float] = []
    ys: list[int] = []
    for r in rows:
        p_raw = r["predicted_prob"]
        y_raw = r["actual_outcome"]
        if p_raw is None or y_raw is None:
            continue
        try:
            p = float(p_raw)
            y = int(y_raw)
        except (TypeError, ValueError):
            continue
        if y not in (0, 1):
            continue
        if math.isnan(p) or math.isinf(p):
            continue
        probs.append(max(0.0, min(1.0, p)))
        ys.append(y)
    return probs, ys


def _fit(probs: list[float], ys: list[int]) -> dict[str, Any]:
    """Pick isotonic when we clear the threshold, else Platt."""
    if len(probs) >= calibrators.ISOTONIC_MIN_SAMPLES:
        return calibrators._fit_isotonic(probs, ys)
    return calibrators._fit_platt(probs, ys)


def _apply_one(state: dict[str, Any], p: float) -> float:
    if state.get("type") == "isotonic":
        return calibrators._apply_isotonic(p, state)
    return calibrators._apply_platt(p, state)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Fit + report metrics, but skip the calibration_snapshots insert.",
    )
    parser.add_argument(
        "--max-rows", type=int, default=None,
        help="Cap the number of V1 rows loaded (testing only).",
    )
    args = parser.parse_args()

    probs, ys = await _fetch_pairs(args.max_rows)
    n = len(probs)
    if n < calibrators.MIN_REFIT_SAMPLES:
        out: dict[str, Any] = {
            "ok": False,
            "league": "CRYPTO",
            "sample_count": n,
            "reason": (
                f"below MIN_REFIT_SAMPLES "
                f"({calibrators.MIN_REFIT_SAMPLES}) — V1 crypto archive "
                "is empty or below the floor; nothing fitted."
            ),
        }
        print(json.dumps(out, indent=2))
        return 1

    state = _fit(probs, ys)
    brier_before = calibrators._brier(probs, ys)
    calibrated = [_apply_one(state, p) for p in probs]
    brier_after = calibrators._brier(calibrated, ys)
    brier_delta = (
        (brier_after - brier_before)
        if brier_before is not None and brier_after is not None
        else None
    )

    snap_id: int | None = None
    if not args.dry_run:
        snap_id = await calibrators._persist_calibrator(
            "CRYPTO", state, n, brier_before, brier_after,
            source_breakdown={"v1_archive_bootstrap": n},
        )

    summary: dict[str, Any] = {
        "ok": True,
        "league": "CRYPTO",
        "sample_count": n,
        "calibrator_type": state.get("type"),
        "brier_before": brier_before,
        "brier_after": brier_after,
        "brier_delta": brier_delta,
        "snapshot_id": snap_id,
        "dry_run": bool(args.dry_run),
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
