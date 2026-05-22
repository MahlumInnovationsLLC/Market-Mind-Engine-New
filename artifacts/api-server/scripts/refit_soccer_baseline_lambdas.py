"""Manual entry point for the per-league soccer baseline-lambda refit.

The settlement scheduler invokes :func:`soccer_lambda_fitter.maybe_refit`
on a weekly cadence (task #903).  This script is the on-demand
equivalent: run it from the API container shell to force an immediate
refit (e.g. after a backfill that loaded a big chunk of historical
scorelines, or to validate a tuning change without waiting up to a
week).

Usage
-----

    python -m scripts.refit_soccer_baseline_lambdas

The script reuses the production cycle entry point so the persisted
table and the predictor's in-memory cache end up in the same state as
they would after a scheduled run.  Per-league results are printed as
JSON so the operator can capture the output in an audit log.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict

from app.learning import soccer_lambda_fitter


log = logging.getLogger(__name__)


async def _main() -> int:
    cr = await soccer_lambda_fitter.run_cycle()
    payload = {
        "started_at": cr.started_at,
        "completed_at": cr.completed_at,
        "duration_seconds": cr.duration_seconds,
        "leagues_examined": cr.leagues_examined,
        "leagues_fitted": cr.leagues_fitted,
        "leagues_skipped": cr.leagues_skipped,
        "persisted": cr.persisted,
        "errors": cr.errors,
        "per_league": [asdict(r) for r in cr.per_league],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if not cr.errors else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(asyncio.run(_main()))
