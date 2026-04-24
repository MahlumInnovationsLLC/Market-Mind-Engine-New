"""Train + persist the production commodity LGB+XGB ensembles.

Pulls bars from ``commodities_ohlcv`` per (symbol, timeframe), trains
one stack per timeframe via :mod:`app.commodities.training`, writes the
artifacts at the canonical paths the model loader looks for, emits a
JSON backtest report comparing trained vs. baseline on a held-out tail,
and (with ``--refit-calibrator``) triggers a per-league calibrator
refit so the COMMODITIES isotonic / Platt curve picks up the freshly
deployed stack on the next boot.

Examples
--------
Local smoke run (writes to ./out/saved/commodities/):
    python -m scripts.train_commodities --output-dir ./out --report ./out/report.json

Production run that drops artifacts into the model loader's cache so
the next call to ``predict_commodity_binary`` picks them up without
restarting the API:
    AXIOM_MODEL_CACHE_DIR=/var/cache/axiom-models \\
        python -m scripts.train_commodities \\
            --output-dir /var/cache/axiom-models/models \\
            --refit-calibrator \\
            --report /var/log/commodities-train.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

# Allow `python scripts/train_commodities.py` to import the api-server's
# `app` package.
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from app import db  # noqa: E402
from app.commodities import training  # noqa: E402
from app.commodities.models import CANONICAL_PATHS  # noqa: E402
from app.commodities.tickers import (  # noqa: E402
    SUPPORTED_SYMBOLS,
    SUPPORTED_TIMEFRAMES,
)


log = logging.getLogger("commodities.trainer")


async def _load_bars(
    *, symbols: list[str], timeframes: list[str], days: int,
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Pull bars from the DB grouped by ``[timeframe][symbol]``."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    out: dict[str, dict[str, list[dict[str, Any]]]] = {tf: {} for tf in timeframes}
    for tf in timeframes:
        for sym in symbols:
            rows = await db.fetch(
                """
                SELECT ts, o, h, l, c, v
                  FROM commodities_ohlcv
                 WHERE symbol = $1 AND granularity = $2
                   AND ts >= $3 AND ts <= $4
                 ORDER BY ts ASC
                """,
                sym, tf, start, end,
            )
            bars = [dict(r) for r in rows]
            if bars:
                out[tf][sym] = bars
            log.info("[trainer] loaded %d bars for %s/%s", len(bars), sym, tf)
    return out


async def _maybe_refit_calibrator() -> dict[str, Any]:
    from app import calibrators

    summary = await calibrators.refit_all()
    return summary


def _resolve_output_path(output_dir: str, canonical: str) -> str:
    """``canonical`` looks like ``models/saved/commodities/lgb_xgb_1h.pkl``.

    The model loader scans relative to ``CACHE_ROOT/models``, so when
    the operator points ``--output-dir`` at ``CACHE_ROOT`` we want the
    full ``models/...`` prefix.  When they point at the extracted
    ``models/`` directly we strip it.  Heuristic: if ``output_dir``
    already ends in ``models`` we drop the leading ``models/`` segment.
    """
    rel = canonical
    norm_dir = os.path.normpath(output_dir)
    if os.path.basename(norm_dir) == "models" and rel.startswith("models/"):
        rel = rel[len("models/"):]
    return os.path.join(output_dir, rel)


async def _amain(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    symbols = args.symbols or list(SUPPORTED_SYMBOLS)
    timeframes = args.timeframes or list(SUPPORTED_TIMEFRAMES)

    bars = await _load_bars(
        symbols=symbols, timeframes=timeframes, days=args.days,
    )
    stacks = training.train_all_timeframes(
        bars,
        timeframes=timeframes,
        lookback=args.lookback,
        max_rows=args.max_rows,
    )
    if not stacks:
        log.error(
            "[trainer] no stacks produced (insufficient bars for any "
            "timeframe).  Backfill commodities_ohlcv and re-run."
        )
        return 2

    saved: dict[str, str] = {}
    report: dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "symbols": symbols,
        "timeframes": list(stacks.keys()),
        "days": args.days,
        "stacks": {},
    }
    for tf, stack in stacks.items():
        canonical = CANONICAL_PATHS[tf]
        out_path = _resolve_output_path(args.output_dir, canonical)
        training.save_artifact(stack, out_path)
        saved[tf] = out_path
        cmp_ = training.compare_to_baseline(
            stack, bars[tf], timeframe=tf, lookback=args.lookback,
        )
        report["stacks"][tf] = {
            "artifact_path": out_path,
            "metadata": stack.metadata,
            "comparison": cmp_,
        }
        log.info(
            "[trainer] %s -> %s  (val_brier=%.4f)",
            tf, out_path, stack.metadata.get("val_brier", float("nan")),
        )

    if args.refit_calibrator:
        try:
            cal_summary = await _maybe_refit_calibrator()
            report["calibrator_refit"] = {
                "fitted": cal_summary.get("fitted"),
                "skipped": cal_summary.get("skipped"),
                "errors": cal_summary.get("errors"),
                "duration_seconds": cal_summary.get("duration_seconds"),
            }
        except Exception as e:  # pragma: no cover — defensive
            log.warning("[trainer] calibrator refit failed: %s", e)
            report["calibrator_refit"] = {"error": str(e)}

    report["completed_at"] = datetime.now(timezone.utc).isoformat()

    if args.report:
        os.makedirs(os.path.dirname(os.path.abspath(args.report)) or ".", exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, default=str)
        log.info("[trainer] wrote report -> %s", args.report)
    else:
        print(json.dumps(report, indent=2, default=str))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output-dir", default="out",
                   help="Directory to write trained artifacts under.  "
                        "Combined with the canonical relative path.")
    p.add_argument("--report", default=None,
                   help="If set, write a JSON backtest report to this path.")
    p.add_argument("--days", type=int, default=120,
                   help="How many days of history to pull from commodities_ohlcv.")
    p.add_argument("--lookback", type=int, default=training.DEFAULT_LOOKBACK,
                   help="Min bars warmed up before a row is generated.")
    p.add_argument("--max-rows", type=int, default=training.DEFAULT_MAX_TRAIN_ROWS,
                   help="Cap on synthesised training rows per timeframe.")
    p.add_argument("--symbols", nargs="*", default=None,
                   help="Symbols to train on (default: all SUPPORTED_SYMBOLS).")
    p.add_argument("--timeframes", nargs="*", default=None,
                   help="Timeframes to train (default: 1h 4h 1d).")
    p.add_argument("--refit-calibrator", action="store_true",
                   help="Also kick the per-league calibrator refit.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return asyncio.run(_amain(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
