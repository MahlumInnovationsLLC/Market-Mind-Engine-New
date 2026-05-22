"""Niche-sport calibrator seed helper (task #831).

Inserts an **identity isotonic seed** snapshot into
``calibration_snapshots`` for each of ATP / WTA / F1 / PGA / UFC.
The seed is the smallest artifact that satisfies the loader's
contract:

  * ``vertical = 'sports'``
  * ``sub_category = 'per_league:<LEAGUE>'``
  * ``role = 'champion'``
  * ``calibrator_state = {"type":"isotonic","x":[0,1],"y":[0,1],...}``

The two-point isotonic ``x=[0,1] → y=[0,1]`` is a true identity in
the runtime apply path (linear interpolation between the two anchors
returns ``p`` for every ``p`` in [0, 1]).  An "identity Platt" with
``a=1, b=0`` would actually run ``sigmoid(p)`` — NOT a pass-through —
so the isotonic shape is the only safe seed that doesn't silently
mis-price live trades.

Seeding does NOT silently mis-price live trades — it just unblocks
``gate_uncalibrated_vertical`` so the operator can flip a niche
vertical on once the real fit lands.

**This helper is operator-invoked.**  It is NOT wired into the
boot sequence or the nightly refit loop.  The intended flow is:

  1. Backfill job (follow-up task) populates ``niche_sport_outcomes``
     with ≥5 years of settled outcomes per sport.
  2. Real isotonic / Platt fit replaces the seed snapshot via the
     existing :mod:`app.calibrators` refit machinery.
  3. Operator flips ``risk.uncalibrated_raw_mode_<vertical>=false``
     (or simply leaves it false — the gate is now satisfied because
     the snapshot row exists with a non-empty ``calibrator_state``).

Until step 1 lands, calling :func:`seed_all` is a deliberate
operator decision — the runbook entry spells out the trade-off.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from .. import db


log = logging.getLogger("axiom.niche_sports.calibrator_scaffold")


# Per-sport (league) seed targets.  Mirrors the SUPPORTED_LEAGUES list
# in :mod:`app.calibrators` but is scoped to the four niche verticals
# this task covers.  ATP and WTA are seeded independently so the per-
# league snapshot row exists for both.
SEED_LEAGUES: tuple[str, ...] = ("ATP", "WTA", "F1", "PGA", "UFC")


def _identity_isotonic_state() -> dict[str, Any]:
    """Return the JSON-serialisable identity isotonic state.

    Two-point isotonic where ``x=[0,1]`` and ``y=[0,1]`` — the runtime
    apply path linearly interpolates between the anchors, so every
    ``p`` in [0, 1] returns ``p`` unchanged.  This is the only seed
    shape that's a true pass-through under :func:`app.calibrators.apply`.
    """
    return {
        "type": "isotonic",
        "x": [0.0, 1.0],
        "y": [0.0, 1.0],
        "n": 0,
        "fit_at": time.time(),
        "seed": True,
        "task": 831,
    }


async def seed_all(
    *, leagues: tuple[str, ...] | None = None, source: str = "task_831_seed"
) -> dict[str, Any]:
    """Insert an identity Platt seed snapshot for each league.

    Idempotent: ``calibration_snapshots`` is append-only (one row per
    refit) but the loader's ``DISTINCT ON (sub_category, role)`` keeps
    only the most recent row per ``(league, role)``, so calling this
    helper twice merely produces two history rows with the second
    superseding the first — no schema-level conflict.

    Returns ``{seeded: int, leagues: list, errors: list}``.
    """
    targets = tuple(leagues or SEED_LEAGUES)
    state_json = json.dumps(_identity_isotonic_state())
    out: dict[str, Any] = {"seeded": 0, "leagues": [], "errors": []}
    for league in targets:
        sub = f"per_league:{league.strip().upper()}"
        try:
            await db.execute(
                """
                INSERT INTO calibration_snapshots (
                    vertical, sub_category, role,
                    calibrator_state, config, drift_status,
                    sample_count, brier_score
                ) VALUES (
                    'sports', $1, 'champion',
                    $2::jsonb, $3::jsonb, $4::jsonb,
                    0, NULL
                )
                """,
                sub,
                state_json,
                json.dumps({"seed": True, "source": source}),
                json.dumps({"status": "seed"}),
            )
            out["seeded"] += 1
            out["leagues"].append(league)
        except Exception as e:  # pragma: no cover — defensive
            log.warning(
                "[niche_sports] seed failed for %s: %s: %s",
                league, type(e).__name__, e,
            )
            out["errors"].append(
                {"league": league, "error": f"{type(e).__name__}: {e}"[:200]}
            )
    return out
