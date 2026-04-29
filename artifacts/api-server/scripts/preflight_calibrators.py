"""Preflight gate 2/4: per-league calibrator-snapshots SQL check.

Verifies that the ``calibration_snapshots`` table holds at least one
``status='fitted'`` row for every league listed in
``REQUIRED_LEAGUES`` (NBA / NFL / NCAAB / NHL / MLB — the V1 main
verticals that ship with real Platt curves after the V1-archive
import + nightly refit).

Why this is a gate — without fitted calibrators every sports candidate
trades against the meta-model raw probability (typically off by 4-8%
on live-season data) and the swarm gatekeeper threshold tuning that
follows is meaningless. So a Mac Studio cutover MUST not proceed if
those leagues are still cold-start (``identity_seed`` /
``market_prior``).

Exit code:
    0 — every required league has a fitted calibrator artifact.
    1 — at least one league missing OR the DB is unreachable. Stderr
        carries the per-league status table so the operator can see at
        a glance which league still needs a refit.

Run with::

    /home/runner/workspace/.pythonlibs/bin/python3 \\
        artifacts/api-server/scripts/preflight_calibrators.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402


REQUIRED_LEAGUES: tuple[str, ...] = ("NBA", "NFL", "NCAAB", "NHL", "MLB")


async def _check() -> int:
    try:
        rows = await db.fetch(
            """
            SELECT DISTINCT ON (sub_category)
                   sub_category,
                   COALESCE(calibrator_state->>'type', '?')   AS kind,
                   sample_count,
                   created_at
              FROM calibration_snapshots
             WHERE vertical = 'sports'
               AND sub_category LIKE 'per_league:%'
             ORDER BY sub_category, created_at DESC
            """
        )
    except Exception as e:  # noqa: BLE001
        print(f"[preflight calibrators] DB query failed: {e}", file=sys.stderr)
        return 1

    by_league: dict[str, dict[str, object]] = {}
    for r in rows:
        sub = str(r["sub_category"] or "")
        if not sub.startswith("per_league:"):
            continue
        league = sub.split(":", 1)[1].strip().upper()
        by_league[league] = {
            "kind": r["kind"],
            "n": int(r["sample_count"] or 0),
            "fitted_at": r["created_at"],
        }

    print("league      | kind       | samples | fitted_at")
    print("------------+------------+---------+--------------------------")
    missing: list[str] = []
    for lg in REQUIRED_LEAGUES:
        info = by_league.get(lg)
        if info is None:
            print(f"{lg:<11} | <missing>  |       0 | -")
            missing.append(lg)
            continue
        kind = str(info["kind"])
        # We accept 'isotonic' or 'platt' as fitted; cold-start sentinels
        # ('identity_seed', 'market_prior') do NOT pass.
        if kind not in ("isotonic", "platt"):
            print(
                f"{lg:<11} | {kind:<10} | {info['n']:>7} | {info['fitted_at']!s}"
                "  (cold-start, NOT fitted)"
            )
            missing.append(lg)
            continue
        print(f"{lg:<11} | {kind:<10} | {info['n']:>7} | {info['fitted_at']!s}")

    if missing:
        print(
            f"\n[preflight calibrators] FAIL — missing fitted calibrators for: "
            f"{','.join(missing)}",
            file=sys.stderr,
        )
        return 1
    print(
        f"\n[preflight calibrators] PASS — all {len(REQUIRED_LEAGUES)} required "
        "leagues have fitted Platt/isotonic calibrators."
    )
    return 0


def main() -> int:
    # Surface the DSN dependency early so a missing SUPABASE_DB_PASSWORD
    # fails the gate with a clear message instead of an opaque asyncpg
    # traceback. `app.config.Settings._build_dsn` consumes only
    # SUPABASE_DB_PASSWORD (it is the canonical knob); there is no
    # SUPABASE_DB_DSN fallback.
    if not os.getenv("SUPABASE_DB_PASSWORD"):
        print(
            "[preflight calibrators] SUPABASE_DB_PASSWORD must be set",
            file=sys.stderr,
        )
        return 1
    try:
        return asyncio.run(_check())
    finally:
        try:
            asyncio.run(db.close_pool())
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
