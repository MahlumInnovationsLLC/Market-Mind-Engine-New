"""Train + persist per-(city, metric) GRU+EMOS coefficients for the
temperature vertical (task #254).

Joins ``weather_hrrr_forecasts`` against per-day high/low aggregates
of ``weather_observations`` and per-day ``weather_climatology`` rows,
fits the GRU output head + EMOS NGR coefficients via
:mod:`app.weather.training`, and writes the promoted artefacts into
``weather_temp_trained_models``.  The live predictor consults the
table at request time.

Examples
--------
Train every (city, metric) on the trailing 90 days, write a JSON
backtest report, and skip the DB write for any pair whose held-out
Brier doesn't beat the deterministic baseline:

    python -m scripts.train_weather_temp --report ./out/weather_temp.json

Train just NYC + MIA on the trailing 30 days and a 20% holdout:

    python -m scripts.train_weather_temp \\
        --cities NYC MIA --days 30 --holdout-frac 0.2
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

# Allow `python scripts/train_weather_temp.py` to import the api-server's
# `app` package.
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from app import db  # noqa: E402
from app.weather import training  # noqa: E402
from app.weather.tickers import SUPPORTED_CITIES, SUPPORTED_METRICS  # noqa: E402


log = logging.getLogger("weather_temp.trainer")


def _result_to_dict(r: training.TrainResult) -> dict[str, Any]:
    return {
        "city": r.city,
        "metric": r.metric,
        "n_samples": r.n_samples,
        "n_train": r.n_train,
        "n_holdout": r.n_holdout,
        "promoted": r.promoted,
        "baseline_brier": r.baseline_brier,
        "holdout_brier": r.holdout_brier,
        "train_brier": r.train_brier,
        "improvement_pct": (
            100.0 * (r.baseline_brier - r.holdout_brier) / r.baseline_brier
            if (r.baseline_brier and r.holdout_brier) else None
        ),
        "fit_at": r.fit_at,
        "error": r.error,
    }


async def _amain(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cities = args.cities or list(SUPPORTED_CITIES)
    metrics = args.metrics or list(SUPPORTED_METRICS)

    started_at = datetime.now(timezone.utc).isoformat()
    log.info("[temp-train] starting cities=%s metrics=%s days=%d",
             cities, metrics, args.days)
    results = await training.train_all(
        cities=cities,
        metrics=metrics,
        days=args.days,
        holdout_frac=args.holdout_frac,
    )
    promoted = sum(1 for r in results if r.promoted)
    skipped = len(results) - promoted
    log.info("[temp-train] done — promoted=%d skipped=%d total=%d",
             promoted, skipped, len(results))

    report = {
        "started_at": started_at,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "cities": cities,
        "metrics": metrics,
        "days": args.days,
        "holdout_frac": args.holdout_frac,
        "promoted": promoted,
        "skipped": skipped,
        "results": [_result_to_dict(r) for r in results],
    }
    if args.report:
        os.makedirs(os.path.dirname(os.path.abspath(args.report)) or ".",
                    exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, default=str)
        log.info("[temp-train] wrote report -> %s", args.report)
    else:
        print(json.dumps(report, indent=2, default=str))

    await db.close_pool()
    return 0 if promoted > 0 else 1


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cities", nargs="*", default=None,
                   help="Cities to train (default: every supported city).")
    p.add_argument("--metrics", nargs="*", default=None,
                   help="Metrics to train (default: HIGH LOW TEMP).")
    p.add_argument("--days", type=int, default=training.DEFAULT_HISTORY_DAYS,
                   help="Trailing window (in days) of HRRR snapshots / "
                        "observations to load per (city, metric).")
    p.add_argument("--holdout-frac", type=float,
                   default=training.DEFAULT_HOLDOUT_FRAC,
                   help="Fraction of the trailing window held out for the "
                        "Brier-improvement gate.")
    p.add_argument("--report", default=None,
                   help="If set, write a JSON backtest report to this path.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return asyncio.run(_amain(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
