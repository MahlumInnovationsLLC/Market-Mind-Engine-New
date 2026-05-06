"""Bootstrap fit for the per-league WEATHER calibrator (task #770).

Task #767 added ``WEATHER`` to ``calibrators.SUPPORTED_LEAGUES`` and
the new ``gate_uncalibrated_vertical`` risk gate now blocks every
weather candidate until a fitted ``per_league:WEATHER`` snapshot
exists. Until that snapshot is fit, weather signals either sit idle
or run through the operator raw-mode override (which surfaces as a
RED row in the daily honesty report's Section 5).

The nightly per-league refit (``app.calibrators.refit_all``) won't
pick weather up on its own:

* The live ``trades`` x ``trade_postmortems`` source needs settled
  weather trades to exist — but the gate has been blocking them, so
  the live source is empty by construction (the chicken-and-egg the
  bootstrap is here to break).
* The V1 archive's weather rows live with ``vertical='weather'`` and
  ``league='unknown'`` — the league-aligned ``v1_settled`` branch
  filters those out (it requires a known YES/NO side label and a
  resolved league).

What we *do* have is :func:`app.settlement.retrain._load_precip_emos_observations`'s
backing table, ``precip_emos_observations`` — the precip predictor
upserts a row per market and the settlement pipeline stamps
``settled_at`` + ``actual_outcome_yes`` on resolution. That's a
ready-made labelled corpus of (predicted YES probability, realized
YES outcome) pairs scoped to the WEATHER vertical.

This bootstrap:

  1. Pulls every settled row from ``precip_emos_observations``.
  2. Reconstructs the predictor's YES-aligned probability per row by
     flipping ``predicted_p_above`` for ``direction='below'`` markets
     (``below`` contracts resolve YES when the realized accumulation
     is **below** the threshold, so YES prob = 1 - p_above).
  3. Fits isotonic when we clear ``ISOTONIC_MIN_SAMPLES`` else Platt.
  4. Persists a ``per_league:WEATHER`` ``calibration_snapshots`` row
     with ``role='champion'`` so the loader picks it up on the next
     ``load_all()``.

Idempotent: each invocation appends a new champion snapshot.  The
loader's ``DISTINCT ON (sub_category, role) ... ORDER BY created_at
DESC`` picks the most recent row, so running twice is harmless — the
second fit just supersedes the first.

Usage::

    python -m scripts.bootstrap_weather_calibrator
    python -m scripts.bootstrap_weather_calibrator --dry-run
    python -m scripts.bootstrap_weather_calibrator --max-rows 5000

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
    SELECT predicted_p_above::float       AS predicted_p_above,
           direction::text                AS direction,
           actual_outcome_yes::int        AS actual_outcome_yes
      FROM precip_emos_observations
     WHERE settled_at IS NOT NULL
       AND actual_outcome_yes IS NOT NULL
       AND predicted_p_above IS NOT NULL
       AND actual_outcome_yes IN (0, 1)
       AND direction IN ('above', 'below')
     ORDER BY settled_at ASC
"""


async def _fetch_pairs(max_rows: int | None) -> tuple[list[float], list[int]]:
    """Pull side-aligned (p_yes, y_yes) pairs from settled precip
    observations.

    ``predicted_p_above`` is always P(precip > threshold).  For
    ``direction='above'`` contracts the YES probability is exactly
    ``p_above``; for ``direction='below'`` contracts the YES side is
    "precip < threshold", so YES prob = ``1 - p_above``.
    ``actual_outcome_yes`` is YES-aligned by construction (the
    settlement pipeline stamps 1 iff the contract direction won), so
    it's used as-is on both sides.
    """
    present = await db.fetchval(
        "SELECT to_regclass('public.precip_emos_observations') IS NOT NULL"
    )
    if not present:
        raise SystemExit(
            "precip_emos_observations not found — has migration "
            "0017_precip_emos_observations been applied?"
        )

    sql = _SELECT_SQL
    if max_rows is not None and max_rows > 0:
        sql = sql + f"\n     LIMIT {int(max_rows)}"

    rows = await db.fetch(sql)
    probs: list[float] = []
    ys: list[int] = []
    for r in rows:
        p_raw = r["predicted_p_above"]
        y_raw = r["actual_outcome_yes"]
        d_raw = r["direction"]
        if p_raw is None or y_raw is None or d_raw is None:
            continue
        try:
            p_above = float(p_raw)
            y = int(y_raw)
        except (TypeError, ValueError):
            continue
        if y not in (0, 1):
            continue
        if math.isnan(p_above) or math.isinf(p_above):
            continue
        direction = str(d_raw).strip().lower()
        if direction not in ("above", "below"):
            continue
        # Clamp defensively — a stray > 1.0 from a degenerate gamma
        # SF would distort the isotonic fit otherwise.
        p_above = max(0.0, min(1.0, p_above))
        p_yes = p_above if direction == "above" else (1.0 - p_above)
        probs.append(p_yes)
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
        help="Cap the number of settled rows loaded (testing only).",
    )
    args = parser.parse_args()

    probs, ys = await _fetch_pairs(args.max_rows)
    n = len(probs)
    if n < calibrators.MIN_REFIT_SAMPLES:
        out: dict[str, Any] = {
            "ok": False,
            "league": "WEATHER",
            "sample_count": n,
            "reason": (
                f"below MIN_REFIT_SAMPLES "
                f"({calibrators.MIN_REFIT_SAMPLES}) — precip_emos_observations "
                "carries too few settled rows; nothing fitted."
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
            "WEATHER", state, n, brier_before, brier_after,
            source_breakdown={"precip_emos_observations_bootstrap": n},
        )

    summary: dict[str, Any] = {
        "ok": True,
        "league": "WEATHER",
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
