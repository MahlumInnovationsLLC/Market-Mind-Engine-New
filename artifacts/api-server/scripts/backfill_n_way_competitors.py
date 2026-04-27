"""Backfill ``competitor_id`` / ``competitor_name`` into ``trades.metadata``
for combat & individual-sport (n-way outright) trades.

Per task #616.  The orchestrator now stamps the per-competitor tag at
trade insert-time via ``app.orchestrator.n_way.derive_n_way_competitor``
so the settlement pipeline can apply the correct per-leg outcome on the
actual race / tournament winner.  Existing un-settled n-way trades that
pre-date the orchestrator change have no
``metadata->>'competitor_id'`` and would mis-score on settlement
(silent fallback to the binary ``outcome_yes`` — every losing
competitor's leg flagged as a model miss instead of the deterministic
NO it actually is).

This script walks every un-settled n-way trade
(``settled_at IS NULL`` AND ``vertical = 'sports'``
AND league has ``LeagueConfig.n_way_market = True``) and:

  * resolves the Kalshi market title via ``resolve_titles`` (cached),
  * pulls the per-event competitor field from the ``predictions``
    row's ``game_context`` (the n-way producer persists the
    ``n_way_field`` competitor list there),
  * runs the same ``derive_n_way_competitor`` helper the orchestrator
    uses,
  * stamps the result into ``metadata->>'competitor_id'`` (and
    ``competitor_name`` when available) with a ``jsonb_set`` UPDATE
    that preserves any other metadata keys.

Trades whose competitor cannot be derived (no title, no field,
no ``home.name`` signal) are left alone — settlement falls back to the
binary path for them, which is the same behavior they had before the
script ran.

Usage::

    python -m scripts.backfill_n_way_competitors --dry-run
    python -m scripts.backfill_n_way_competitors

The script is **idempotent** — re-running it is a no-op for trades
that already carry a ``competitor_id`` value (the WHERE filter
excludes them) and identical for trades that don't.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Any

from app import db
from app.kalshi.market_titles import resolve_titles as resolve_market_titles
from app.orchestrator.n_way import derive_n_way_competitor
from app.sports.leagues import LEAGUE_CONFIGS


log = logging.getLogger("backfill_n_way_competitors")


def _n_way_leagues() -> list[str]:
    return [k for k, cfg in LEAGUE_CONFIGS.items() if cfg.n_way_market]


# ``trades`` has no direct entrant column; we recover the competitor
# field from the matching ``predictions`` row's ``game_context`` jsonb,
# which the n-way producer populates with the ``n_way_field`` list.
# The candidate also typically carries the per-leg entrant under its
# ``home.name`` field — we surface ``predictions.home_team`` for the
# same reason (the producer stores it there for outright markets).
_SELECT_SQL = """
    SELECT DISTINCT ON (t.id)
        t.id,
        t.market_id,
        t.market_title,
        t.side,
        p.game_context AS prediction_context
      FROM trades t
      LEFT JOIN LATERAL (
          SELECT pp.game_context
            FROM predictions pp
           WHERE pp.kalshi_market_id = t.market_id
           ORDER BY pp.created_at DESC NULLS LAST
           LIMIT 1
      ) p ON true
     WHERE t.settled_at IS NULL
       AND t.vertical = 'sports'
       AND COALESCE(t.league, '') = ANY($1::text[])
       AND COALESCE(t.metadata->>'competitor_id', '') = ''
       AND COALESCE(t.metadata->>'competitor_name', '') = ''
       -- Cursor pagination on t.id is required so ``--dry-run`` makes
       -- forward progress: in dry-run mode no UPDATE fires, so the
       -- ``metadata->>'competitor_id' IS NULL`` filter would re-return
       -- the same batch every pass.  In wet runs it's still safer
       -- (no reliance on the UPDATE narrowing the candidate set).
       AND t.id > $2
     ORDER BY t.id
     LIMIT $3
"""


# We use jsonb_set twice (one per key) so missing keys are added
# without disturbing any unrelated metadata fields.  Bind both on the
# same UPDATE so a single round-trip handles either / both.
_UPDATE_SQL = """
    UPDATE trades
       SET metadata = (
           CASE
             WHEN $1::text IS NOT NULL THEN
               jsonb_set(
                   CASE
                     WHEN $2::text IS NOT NULL THEN
                       jsonb_set(
                           COALESCE(metadata, '{}'::jsonb),
                           '{competitor_name}',
                           to_jsonb($2::text),
                           true
                       )
                     ELSE COALESCE(metadata, '{}'::jsonb)
                   END,
                   '{competitor_id}',
                   to_jsonb($1::text),
                   true
               )
             WHEN $2::text IS NOT NULL THEN
               jsonb_set(
                   COALESCE(metadata, '{}'::jsonb),
                   '{competitor_name}',
                   to_jsonb($2::text),
                   true
               )
             ELSE COALESCE(metadata, '{}'::jsonb)
           END
       )
     WHERE id = $3
"""


def _coerce_context(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except Exception:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _candidate_from_row(r: dict[str, Any]) -> dict[str, Any]:
    """Reconstruct the n-way candidate shape from a trade row + the
    matching ``predictions.game_context``.

    ``game_context`` is the producer's stash for the per-event
    competitor field (under ``n_way_field``) and the YES-side entrant
    (under ``home`` / ``selection`` / ``team``).  When the column is
    missing or empty we still return whatever we have on the trade row
    so the helper's title-only fallback can still try.
    """
    ctx = _coerce_context(r.get("prediction_context"))
    cand: dict[str, Any] = {
        "side": r.get("side"),
    }
    if "n_way_field" in ctx:
        cand["n_way_field"] = ctx["n_way_field"]
    for key in ("entrant", "selection", "team", "home", "competitor_id",
                "entrant_id"):
        if key in ctx:
            cand[key] = ctx[key]
    return cand


async def run(*, batch_size: int, dry_run: bool) -> dict[str, int]:
    leagues = _n_way_leagues()
    if not leagues:
        log.warning("no n-way leagues configured (n_way_market=True)")
        return {"scanned": 0, "updated": 0, "skipped": 0}

    stats = {"scanned": 0, "updated": 0, "skipped": 0}
    last_id = 0
    while True:
        rows = await db.fetch(_SELECT_SQL, leagues, last_id, batch_size)
        if not rows:
            break
        # Resolve missing titles in one shot — most rows already have
        # market_title persisted but the helper is cheap on a cache hit.
        missing_titles = [
            r["market_id"]
            for r in rows
            if r.get("market_id") and not r.get("market_title")
        ]
        title_lookup: dict[str, str | None] = {}
        if missing_titles:
            try:
                title_lookup = await resolve_market_titles(missing_titles)
            except Exception as e:
                log.warning("resolve_titles failed: %s", e)

        for r in rows:
            stats["scanned"] += 1
            title = r.get("market_title") or title_lookup.get(r.get("market_id"))
            cand = _candidate_from_row(dict(r))
            comp = derive_n_way_competitor(market_title=title, candidate=cand)
            if not comp:
                stats["skipped"] += 1
                log.debug(
                    "skip trade id=%s market=%s — no competitor derivable "
                    "(title=%r side=%r)",
                    r["id"], r.get("market_id"), title, r.get("side"),
                )
                continue
            cid = comp.get("competitor_id")
            cname = comp.get("competitor_name")
            if dry_run:
                stats["updated"] += 1
                log.info(
                    "[dry-run] would set trade id=%s market=%s "
                    "competitor_id=%r competitor_name=%r",
                    r["id"], r.get("market_id"), cid, cname,
                )
                continue
            await db.execute(_UPDATE_SQL, cid, cname, r["id"])
            stats["updated"] += 1
            log.info(
                "set trade id=%s market=%s competitor_id=%r competitor_name=%r",
                r["id"], r.get("market_id"), cid, cname,
            )

        # Advance the cursor to the largest id we've just seen so the
        # next pass returns strictly newer rows even in --dry-run mode.
        last_id = max(int(r["id"]) for r in rows)
        # Exit early once a batch comes back smaller than the requested
        # size — cursor pagination guarantees the next SELECT is empty.
        if len(rows) < batch_size:
            break
    return stats


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument(
        "--batch-size", type=int, default=500,
        help="Trades scanned per pass (default 500)",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Compute the competitor for each trade but skip the UPDATE",
    )
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    stats = asyncio.run(run(batch_size=args.batch_size, dry_run=args.dry_run))
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
