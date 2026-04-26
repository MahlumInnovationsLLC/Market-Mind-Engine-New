"""Backfill ``three_way_leg`` into ``trades.metadata`` for soccer trades.

Per task #444.  The orchestrator now stamps the per-leg tag at trade
insert-time via ``app.orchestrator.three_way.derive_three_way_leg`` so
the settlement pipeline can apply the correct h/d/a outcome on a draw.
Existing un-settled soccer trades that pre-date the orchestrator change
have no ``metadata->>'three_way_leg'`` and would mis-score on a draw
(silent fallback to the binary ``outcome_yes``).

This script walks every un-settled soccer trade
(``settled_at IS NULL`` AND ``vertical = 'sports'``
AND league derivable as one of the soccer leagues with
``three_way_market = True``) and:

  * resolves the Kalshi market title via ``resolve_titles`` (cached),
  * runs the same ``derive_three_way_leg`` helper the orchestrator uses,
  * stamps the result into ``metadata->>'three_way_leg'`` with a
    ``jsonb_set`` UPDATE that preserves any other metadata keys.

Trades whose leg cannot be derived (no title, no team-name match) are
left alone — settlement falls back to the binary path for them, which
is the same behavior they had before the script ran.

Usage::

    python -m scripts.backfill_soccer_three_way_legs --dry-run
    python -m scripts.backfill_soccer_three_way_legs

The script is **idempotent** — re-running it is a no-op for trades
that already carry a ``three_way_leg`` value (the WHERE filter excludes
them) and identical for trades that don't.
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
from app.orchestrator.three_way import derive_three_way_leg
from app.sports.leagues import LEAGUE_CONFIGS


log = logging.getLogger("backfill_soccer_three_way_legs")


def _soccer_leagues() -> list[str]:
    return [k for k, cfg in LEAGUE_CONFIGS.items() if cfg.three_way_market]


_SELECT_SQL = """
    SELECT DISTINCT ON (t.id)
        t.id,
        t.market_id,
        t.market_title,
        t.side,
        g.home_team,
        g.away_team
      FROM trades t
      -- ``trades`` has no direct ``game_id`` FK; reach the game through
      -- the ``predictions`` row that produced this market_id (the
      -- orchestrator persists predictions with both kalshi_market_id
      -- and game_id) so we can recover home/away team names for the
      -- title-parsing fallback in ``derive_three_way_leg``.
      LEFT JOIN LATERAL (
          SELECT p.game_id
            FROM predictions p
           WHERE p.kalshi_market_id = t.market_id
           ORDER BY p.created_at DESC NULLS LAST
           LIMIT 1
      ) pp ON true
      LEFT JOIN games g ON g.game_id = pp.game_id
     WHERE t.settled_at IS NULL
       AND t.vertical = 'sports'
       AND COALESCE(t.league, '') = ANY($1::text[])
       AND COALESCE(t.metadata->>'three_way_leg', '') = ''
       -- Cursor pagination on t.id is required so ``--dry-run`` makes
       -- forward progress: in dry-run mode no UPDATE fires, so the
       -- ``metadata->>'three_way_leg' IS NULL`` filter would re-return
       -- the same batch every pass and the loop would never terminate
       -- on datasets larger than ``$3``.  In wet runs it's still safer
       -- (no reliance on the UPDATE narrowing the candidate set).
       AND t.id > $2
     ORDER BY t.id
     LIMIT $3
"""


_UPDATE_SQL = """
    UPDATE trades
       SET metadata = jsonb_set(
               COALESCE(metadata, '{}'::jsonb),
               '{three_way_leg}',
               to_jsonb($1::text),
               true
           )
     WHERE id = $2
"""


async def run(*, batch_size: int, dry_run: bool) -> dict[str, int]:
    leagues = _soccer_leagues()
    if not leagues:
        log.warning("no soccer leagues configured (three_way_market=True)")
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
            cand: dict[str, Any] = {
                "side": r.get("side"),
                "home_team": r.get("home_team"),
                "away_team": r.get("away_team"),
            }
            leg = derive_three_way_leg(market_title=title, candidate=cand)
            if leg is None:
                stats["skipped"] += 1
                log.debug(
                    "skip trade id=%s market=%s — no leg derivable "
                    "(title=%r home=%r away=%r side=%r)",
                    r["id"], r.get("market_id"), title,
                    r.get("home_team"), r.get("away_team"), r.get("side"),
                )
                continue
            if dry_run:
                stats["updated"] += 1
                log.info(
                    "[dry-run] would set trade id=%s market=%s leg=%s",
                    r["id"], r.get("market_id"), leg,
                )
                continue
            await db.execute(_UPDATE_SQL, leg, r["id"])
            stats["updated"] += 1
            log.info(
                "set trade id=%s market=%s leg=%s",
                r["id"], r.get("market_id"), leg,
            )

        # Advance the cursor to the largest id we've just seen so the
        # next pass returns strictly newer rows even in --dry-run mode
        # (which never narrows the candidate set via UPDATE).
        last_id = max(int(r["id"]) for r in rows)
        # We can also exit early once a batch comes back smaller than
        # the requested size — the cursor pagination guarantees the
        # next SELECT would be empty.
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
        help="Compute the leg for each trade but skip the UPDATE",
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
