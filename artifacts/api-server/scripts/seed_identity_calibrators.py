"""Seed identity calibrators for every supported league that currently
has no learned snapshot.

For leagues with insufficient settled-trade history (the 90-day
``refit_all`` window can't meet ``MIN_REFIT_SAMPLES=10``), we install
an explicit identity calibrator: an isotonic curve with knots
``x=[0,1] y=[0,1]`` so ``apply()`` returns the raw probability
unchanged. The snapshot is tagged ``status='identity_seed'`` with the
actual sample count we observed at install time so the data gap is
documented rather than silent. The next nightly ``refit_all()`` will
overwrite any league that crosses the threshold with a real learned
calibrator.
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import calibrators, db  # noqa: E402


IDENTITY_STATE = {
    "type": "isotonic",
    "x": [0.0, 1.0],
    "y": [0.0, 1.0],
    "n": 0,
}


async def _league_sample_count(league: str) -> int:
    row = await db.fetchval(
        """
        SELECT count(*)
          FROM trades t
          JOIN trade_postmortems pm
            ON pm.trade_id = coalesce(t.trade_id, t.id::text)
         WHERE t.settled_at IS NOT NULL
           AND t.model_prob IS NOT NULL
           AND pm.actual_outcome IS NOT NULL
           AND upper(t.league) = $1
        """,
        league,
    )
    return int(row or 0)


async def _existing_loaded_leagues() -> set[str]:
    rows = await db.fetch(
        """
        SELECT DISTINCT split_part(sub_category, ':', 2) AS league
          FROM calibration_snapshots
         WHERE vertical = 'sports'
           AND sub_category LIKE 'per_league:%'
        """
    )
    return {str(r["league"]).upper() for r in rows if r["league"]}


async def _persist_identity(league: str, n: int) -> int | None:
    state = dict(IDENTITY_STATE)
    state["fit_at"] = datetime.now(timezone.utc).isoformat()
    snap_id = await db.fetchval(
        """
        INSERT INTO calibration_snapshots
            (vertical, sub_category, ece, mce, brier_score, reliability,
             resolution, sample_count, calibrator_state, drift_status, config)
        VALUES ('sports', $1, 0.0, 0.0, 0.0, NULL, NULL, $2,
                $3::jsonb,
                jsonb_build_object('status', 'identity_seed',
                                   'kind', 'identity',
                                   'window_days', 0),
                jsonb_build_object('source', 'seed_identity_calibrators',
                                   'reason', 'insufficient_settled_trades',
                                   'min_required',
                                       $4::int,
                                   'actual_settled_count', $2::int))
        RETURNING id
        """,
        f"per_league:{league}",
        int(n),
        json.dumps(state),
        int(getattr(calibrators, "MIN_REFIT_SAMPLES", 10)),
    )
    return int(snap_id) if snap_id is not None else None


async def main() -> int:
    supported = list(calibrators.SUPPORTED_LEAGUES)
    existing = await _existing_loaded_leagues()
    missing = [lg for lg in supported if lg not in existing]
    print(f"supported leagues: {len(supported)}")
    print(f"already have a snapshot: {len(existing)} -> {sorted(existing)}")
    print(f"missing -> seeding identity for: {len(missing)} -> {missing}")
    if not missing:
        print("nothing to do.")
        return 0
    seeded: list[tuple[str, int, int | None]] = []
    for league in missing:
        n = await _league_sample_count(league)
        snap_id = await _persist_identity(league, n)
        seeded.append((league, n, snap_id))
        print(f"  {league:<14} settled={n:<4} snapshot_id={snap_id}")
    # Refresh in-memory state so the next apply() picks up the new rows.
    await calibrators.load_all()
    print(f"\nseeded {sum(1 for _,_,sid in seeded if sid)} identity calibrators")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
