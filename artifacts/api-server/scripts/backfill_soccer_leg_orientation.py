"""Backfill ``leg_orientation`` into ``predictions.metadata`` for
soccer markets produced before the leg parser shipped (task #892).

Background
----------
:mod:`app.feeds.kalshi_sports_markets` now parses the 3-way leg
(``HOME`` / ``DRAW`` / ``AWAY``) out of a Kalshi soccer market title at
upsert time and stashes it in ``metadata.leg_orientation``.  Rows that
were already in the ``predictions`` table when the producer shipped do
not carry the label — every cycle the live predictor
(:mod:`app.learning.soccer_live`) falls back to its title-parsing
helper for those rows, doing extra work and exposing a small risk of
producer/consumer drift if the two parsers ever diverge.

By default this one-off script walks every OPEN soccer-series row
whose metadata blob doesn't already carry a recognised
``leg_orientation`` and:

  * Runs the same ``_parse_leg_orientation`` helper the producer uses,
    against ``metadata->>'title'`` + ``home_team`` + ``away_team``.
  * Persists the result with a ``jsonb_set`` UPDATE so any other
    metadata keys are preserved.

Rows whose title doesn't fit the moneyline / draw template are left
alone — the live predictor will keep using its leg-agnostic blend for
them, which is the same behavior they have today.

"Open soccer-series row" means:
  * ``settled_at IS NULL`` (the producer only upserts open markets and
    a settled row is not interesting to in-game prediction anyway), AND
  * ``metadata->>'kalshi_series'`` is one of the soccer 3-way series in
    :data:`app.feeds.kalshi_sports_markets._SOCCER_SERIES`.

``--include-settled`` (task #898) widens the scan to also touch
already-settled soccer rows so historical per-leg Brier / calibration
backtests can group by leg without an inline title-parsing fallback.
The settled rows feed no live consumer; this is purely an analytics
hygiene pass.

Usage::

    python -m scripts.backfill_soccer_leg_orientation --dry-run
    python -m scripts.backfill_soccer_leg_orientation
    python -m scripts.backfill_soccer_leg_orientation --include-settled

The script is **idempotent** — the SELECT filters out rows that
already carry a valid leg label, so re-running it is a no-op for
previously-backfilled markets.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from app import db
from app.feeds.kalshi_sports_markets import (
    _SOCCER_SERIES,
    _parse_leg_orientation,
)


log = logging.getLogger("backfill_soccer_leg_orientation")


_VALID_LEGS = ("HOME", "DRAW", "AWAY")


# Two query variants so the default (open-only) pass keeps the
# ``settled_at IS NULL`` filter that matches the live predictor's
# scope, while ``--include-settled`` drops it for the analytics pass.
_SELECT_BASE = """
    SELECT p.id,
           p.kalshi_market_id,
           p.home_team,
           p.away_team,
           p.metadata
      FROM predictions p
     WHERE {settled_filter}
           p.kalshi_market_id IS NOT NULL
       AND COALESCE(p.metadata->>'kalshi_series', '') = ANY($1::text[])
       -- Only revisit rows whose persisted leg label is missing or
       -- unrecognised.  Already-correct rows are skipped so re-runs
       -- are cheap no-ops.
       AND COALESCE(upper(p.metadata->>'leg_orientation'), '')
           NOT IN ('HOME', 'DRAW', 'AWAY')
       -- Cursor pagination on p.id so ``--dry-run`` makes forward
       -- progress: dry-run never narrows the candidate set via UPDATE,
       -- so without a cursor we'd re-fetch the same batch forever.
       AND p.id > $2
     ORDER BY p.id
     LIMIT $3
"""


_SELECT_OPEN_SQL = _SELECT_BASE.format(
    settled_filter="p.settled_at IS NULL\n       AND",
)
_SELECT_ALL_SQL = _SELECT_BASE.format(settled_filter="")


_UPDATE_SQL = """
    UPDATE predictions
       SET metadata = jsonb_set(
               COALESCE(metadata, '{}'::jsonb),
               '{leg_orientation}',
               to_jsonb($1::text),
               true
           )
     WHERE id = $2
"""


def _coerce_metadata(value):
    """``metadata`` may come back as either a dict or a JSON string
    depending on the asyncpg jsonb codec registration.  Coerce to a
    dict (empty on anything unparseable) so the caller can treat it
    uniformly."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except Exception:
            return {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


async def run(
    *,
    batch_size: int,
    dry_run: bool,
    include_settled: bool = False,
) -> dict[str, int]:
    series = sorted(_SOCCER_SERIES)
    stats = {"scanned": 0, "updated": 0, "skipped": 0}
    last_id = 0
    select_sql = _SELECT_ALL_SQL if include_settled else _SELECT_OPEN_SQL
    while True:
        rows = await db.fetch(select_sql, series, last_id, batch_size)
        if not rows:
            break
        for r in rows:
            stats["scanned"] += 1
            meta = _coerce_metadata(r.get("metadata"))
            title = meta.get("title")
            leg = _parse_leg_orientation(
                title, r.get("home_team"), r.get("away_team"),
            )
            if leg not in _VALID_LEGS:
                stats["skipped"] += 1
                log.debug(
                    "skip prediction id=%s market=%s — no leg derivable "
                    "(title=%r home=%r away=%r)",
                    r["id"], r.get("kalshi_market_id"), title,
                    r.get("home_team"), r.get("away_team"),
                )
                continue
            if dry_run:
                stats["updated"] += 1
                log.info(
                    "[dry-run] would set prediction id=%s market=%s leg=%s",
                    r["id"], r.get("kalshi_market_id"), leg,
                )
            else:
                await db.execute(_UPDATE_SQL, leg, r["id"])
                stats["updated"] += 1
                log.info(
                    "set prediction id=%s market=%s leg=%s",
                    r["id"], r.get("kalshi_market_id"), leg,
                )

        last_id = max(int(r["id"]) for r in rows)
        if len(rows) < batch_size:
            break
    return stats


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument(
        "--batch-size", type=int, default=500,
        help="Prediction rows scanned per pass (default 500)",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Compute the leg for each row but skip the UPDATE",
    )
    p.add_argument(
        "--include-settled", action="store_true",
        help=(
            "Also stamp leg_orientation on already-settled soccer rows "
            "so historical per-leg backtests don't need an inline "
            "title-parsing fallback (task #898). Default scans only "
            "open rows, matching the live predictor's scope."
        ),
    )
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    stats = asyncio.run(
        run(
            batch_size=args.batch_size,
            dry_run=args.dry_run,
            include_settled=args.include_settled,
        )
    )
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
