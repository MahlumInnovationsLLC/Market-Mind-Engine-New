"""Positive-EV scanner — outlier.bet substitute (task: go-live data sub).

Outlier.bet was our positive-EV signal source for sports betting until
CloudFront/WAF started serving the SPA shell instead of
``/api/v3/positive-ev/scan`` in late April 2026, leaving the
``intangibles_reports`` table empty and the orchestrator unable to
fade or follow public-soft-book mispricings.

Rather than chase another third-party scraper that can be blocked at
any moment, this module derives the same signal in-process from The
Odds API, which we already poll for ``line_snapshots`` and which has
a stable contract under ``THEODDSAPI_KEY``.  The math:

  1. For each (game_id, market_type) and each book we collect that
     book's outcomes (home/away/draw for h2h, over/under for totals,
     home/away for spreads at a given point).

  2. We compute that book's de-vigged fair probability per outcome:
     ``fair_book[i] = (1/odds[i]) / sum_j (1/odds[j])``.

  3. We compute the cross-book consensus per outcome as the median of
     the per-book de-vigged fair probabilities.  Median (vs. mean) is
     robust to a single soft book mispricing the line — exactly the
     case we're trying to detect on the OTHER side.

  4. For each (book, outcome) we compute the bettor's edge against
     that book's quoted price (the raw, vigged implied probability
     ``1/odds``):

         edge_pct = (consensus_fair - book_implied) / book_implied * 100

     A positive ``edge_pct`` above ``AXIOM_POSITIVE_EV_THRESHOLD``
     (default 1.0%) means the consensus says this outcome is more
     likely than this book is pricing it at — i.e. there is positive
     expected value in taking that side at that book.

The output schema (``intangibles_reports``) matches the outlier.bet
shape one-for-one so downstream consumers (orchestrator
``score_intangibles`` lookup, audit UI cards) are unaffected by the
provider swap.  Rows are tagged ``provider='axiom-positive-ev'`` so
operators can distinguish them from any historic outlier.bet rows.

The feed is registered alongside ``outlier_bet`` (which we keep
registered so the dashboard surfaces its dead-upstream reason) and
defaults to a 5-minute cadence.  When ``THE_ODDS_API_KEY`` is unset
it skips cleanly via the registry's ``skipped_reason`` mechanism.
"""
from __future__ import annotations

import json as _json
import logging
import os
import time
from statistics import median
from typing import Any

from .. import db
from .registry import Feed, registry
from .odds_api import (
    DEFAULT_ODDS_SPORTS,
    get_recent_games_for_sport,
)


log = logging.getLogger("axiom.feeds.positive_ev")


# Markets we de-vig.  We deliberately exclude exotics where the de-vig
# math is per-leg dependent (e.g. multi-way player props that ship as
# separate 2-way markets per player).  Operator can override.
DEFAULT_PEV_MARKETS = os.environ.get(
    "AXIOM_POSITIVE_EV_MARKETS", "h2h,spreads,totals"
)

# Minimum edge in *percentage points of book-implied probability* to
# be persisted.  1.0% is the historical outlier.bet "EV+" floor; lower
# this for development noise testing or raise it to reduce write volume.
DEFAULT_PEV_THRESHOLD_PCT = float(
    os.environ.get("AXIOM_POSITIVE_EV_THRESHOLD") or "1.0"
)

# Cadence — kept conservative because each cycle is one HTTP call per
# sport and The Odds API trial tier is quota-limited.  300s matches the
# old outlier.bet cadence so per-cycle write volume is comparable.
DEFAULT_PEV_CADENCE = max(60, int(os.environ.get("AXIOM_POSITIVE_EV_CADENCE") or "300"))

# Books we do NOT use as part of the consensus (e.g. exchange/peer-to-peer
# books with a different vig model would skew the median).  Comma-separated.
_PEV_EXCLUDE_BOOKS = {
    b.strip().lower()
    for b in (os.environ.get("AXIOM_POSITIVE_EV_EXCLUDE_BOOKS") or "").split(",")
    if b.strip()
}

# Minimum number of distinct books required to trust a consensus median.
# Below this we skip the outcome entirely — a "consensus" of one or two
# books is not a consensus.
MIN_CONSENSUS_BOOKS = max(
    2, int(os.environ.get("AXIOM_POSITIVE_EV_MIN_BOOKS") or "3")
)


def _outcome_key(market_type: str, name: str | None, point: float | None) -> str:
    """Stable identifier for an outcome WITHIN a (game, market) group.

    For h2h: just the team/draw name.  For spreads/totals we include
    the line value because "Over 220.5" and "Over 221.5" are
    different outcomes whose probabilities are not directly comparable.
    """
    name = (name or "").strip()
    if market_type in ("spreads", "totals") and point is not None:
        return f"{name}:{point}"
    return name


def _devig(odds_for_outcomes: list[float]) -> list[float] | None:
    """Convert a book's decimal odds for one market into de-vigged fair
    probabilities (Shin/proportional method — close enough for soft books).

    Returns ``None`` when input is degenerate (any odds <= 1.0, sum of
    raw implieds <= 1, etc.) so the caller can skip the row instead of
    polluting the consensus median with garbage.
    """
    if not odds_for_outcomes or any(o is None or o <= 1.0 for o in odds_for_outcomes):
        return None
    raw = [1.0 / o for o in odds_for_outcomes]
    total = sum(raw)
    if total <= 1.0:
        # No vig (or a giveaway); de-vigging would amplify noise.
        return None
    return [r / total for r in raw]


# Maximum age (seconds) of an odds_api cache entry that we'll still
# trust for derivation.  Set to 4× the_odds_api default cadence (30s)
# so a single missed cycle doesn't starve us, but a sustained outage
# of the upstream feed cleanly stops emitting positive-EV signals
# rather than emitting them off stale lines.
_PEV_CACHE_MAX_AGE_S = float(
    os.environ.get("AXIOM_POSITIVE_EV_CACHE_MAX_AGE_S") or "120"
)


def derive_positive_ev(
    games: list[dict[str, Any]],
    *,
    threshold_pct: float = DEFAULT_PEV_THRESHOLD_PCT,
    min_consensus_books: int = MIN_CONSENSUS_BOOKS,
) -> list[dict[str, Any]]:
    """Pure function: take The-Odds-API per-game payloads and return
    intangibles_reports-shaped rows for every (book, outcome) whose
    edge exceeds the threshold.

    This is exposed (not just an internal helper) so the unit test can
    feed in a fixture and assert on the math without any network or
    DB coupling.
    """
    out: list[dict[str, Any]] = []
    now_ts = time.time()

    for g in games or []:
        gid = str(g.get("id") or "")
        if not gid:
            continue
        sport = g.get("sport_key") or "unknown"
        home = g.get("home_team") or ""
        away = g.get("away_team") or ""
        teams_label = f"{away} @ {home}" if home and away else (home or away or "")

        # Group quotes by (market_type, outcome_key) -> {book: (raw_implied, fair_prob)}
        # We need both raw_implied (for the bettor's edge) and fair_prob
        # (for the cross-book consensus).
        per_outcome: dict[tuple[str, str], dict[str, tuple[float, float, float]]] = {}
        # Track each book's outcome metadata for output context.
        per_outcome_meta: dict[tuple[str, str], dict[str, Any]] = {}

        for bk in g.get("bookmakers", []) or []:
            book = (bk.get("key") or bk.get("title") or "").lower()
            if not book or book in _PEV_EXCLUDE_BOOKS:
                continue
            for mk in bk.get("markets", []) or []:
                mtype = mk.get("key") or ""
                outcomes = mk.get("outcomes") or []
                if not outcomes:
                    continue

                # Group outcomes by their `point` so that each market the
                # book is offering (e.g. totals at 220.5 vs 221.5) is
                # devigged on its own outcome set.
                by_point: dict[Any, list[dict[str, Any]]] = {}
                for o in outcomes:
                    by_point.setdefault(o.get("point"), []).append(o)

                for point, group in by_point.items():
                    # Parse prices defensively — a single malformed odd in
                    # a payload (very rare, but observed historically when
                    # books push promos through the price field) must NOT
                    # blow up the entire poll cycle.  We require ALL prices
                    # in a market group to parse cleanly because de-vig
                    # math is meaningless without the full outcome set.
                    try:
                        odds_floats = [float(o.get("price")) for o in group]
                    except (TypeError, ValueError):
                        log.debug(
                            "positive_ev: skipping malformed market %s/%s in game %s",
                            mtype, point, gid,
                        )
                        continue
                    fair_list = _devig(odds_floats)
                    if fair_list is None or len(fair_list) != len(group):
                        continue
                    for outcome, fair in zip(group, fair_list):
                        name = outcome.get("name")
                        price = outcome.get("price")
                        if price is None or price <= 1.0:
                            continue
                        raw_imp = 1.0 / float(price)
                        key = (mtype, _outcome_key(mtype, name, point))
                        per_outcome.setdefault(key, {})[book] = (raw_imp, fair, float(price))
                        per_outcome_meta.setdefault(key, {
                            "name": name,
                            "point": point,
                            "market_type": mtype,
                        })

        # Now compute consensus per outcome and emit edges.
        for key, books in per_outcome.items():
            if len(books) < min_consensus_books:
                continue
            fair_probs = [v[1] for v in books.values()]
            consensus = median(fair_probs)
            mtype, _ok = key
            meta = per_outcome_meta.get(key, {})
            for book, (raw_imp, fair, price) in books.items():
                if raw_imp <= 0:
                    continue
                edge_pct = (consensus - raw_imp) / raw_imp * 100.0
                if edge_pct < threshold_pct:
                    continue
                # external_id encodes the full coordinates so the same
                # (game, market, outcome, book) collapses onto one row
                # within a single scan window via our ON CONFLICT clause.
                external_id = f"{gid}:{mtype}:{meta.get('name')}:{meta.get('point')}:{book}"
                out.append({
                    "external_id": external_id,
                    "sport": sport,
                    "market_type": mtype,
                    "side": meta.get("name"),
                    "edge_pct": round(edge_pct, 4),
                    "fair_prob": round(consensus, 6),
                    "book_prob": round(raw_imp, 6),
                    "sportsbook": book,
                    "teams": teams_label,
                    "payload": {
                        "decimal_odds": price,
                        "book_fair_prob": round(fair, 6),
                        "n_books_in_consensus": len(books),
                        "point": meta.get("point"),
                        "outcome_name": meta.get("name"),
                        "home_team": home,
                        "away_team": away,
                    },
                    "scanned_at": now_ts,
                })
    return out


async def _persist_signals(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    written = 0
    for r in rows:
        try:
            await db.execute(
                """
                INSERT INTO intangibles_reports
                    (provider, sport, external_id, market_type, side,
                     edge_pct, fair_prob, book_prob, sportsbook, teams,
                     payload, scanned_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                        $11::jsonb, to_timestamp($12))
                ON CONFLICT (provider, external_id, scanned_at)
                DO NOTHING
                """,
                "axiom-positive-ev",
                str(r.get("sport") or "unknown").lower(),
                str(r.get("external_id") or ""),
                r.get("market_type"),
                r.get("side"),
                float(r.get("edge_pct")) if r.get("edge_pct") is not None else None,
                float(r.get("fair_prob")) if r.get("fair_prob") is not None else None,
                float(r.get("book_prob")) if r.get("book_prob") is not None else None,
                r.get("sportsbook"),
                r.get("teams"),
                _json.dumps(r.get("payload") or {}),
                float(r.get("scanned_at") or time.time()),
            )
            written += 1
        except Exception as e:
            # Per-row resilience matches outlier_bet's pattern — one bad
            # row never drops the entire scan.
            log.warning("positive_ev persist row failed: %s", e)
    return written


async def _poll_positive_ev() -> int:
    """One derivation cycle.  Reads the most recent in-memory payload
    from ``odds_api`` for each configured sport and emits positive-EV
    signals.  Crucially, this performs ZERO HTTP calls: the upstream
    ``the_odds_api`` feed already polls those endpoints every 30s and
    publishes the parsed payloads via ``get_recent_games_for_sport``.
    Sharing one quota avoids racing the primary line_snapshots feed
    into 429s on the trial tier.

    Returns the number of intangibles_reports rows written this cycle.
    Sports whose cache is stale (older than ``_PEV_CACHE_MAX_AGE_S``)
    or empty are silently skipped — this is the correct behavior when
    ``the_odds_api`` is itself rate-limited or down, because acting on
    a 5-minute-old line is worse than emitting no signal.
    """
    written = 0
    sports_with_data = 0
    sports_stale = 0
    for sport in [s.strip() for s in DEFAULT_ODDS_SPORTS if s.strip()]:
        games = get_recent_games_for_sport(sport, max_age_s=_PEV_CACHE_MAX_AGE_S)
        if games is None:
            sports_stale += 1
            continue
        sports_with_data += 1
        try:
            rows = derive_positive_ev(games)
        except Exception as e:
            # Single-sport derivation failure should not abort the cycle
            # for other sports — keep the_odds_api shape change isolated.
            log.warning("positive_ev derive failed for %s: %s", sport, e)
            continue
        written += await _persist_signals(rows)
    if sports_with_data == 0:
        # Operator visibility: cache empty means odds_api hasn't run yet
        # this process lifetime (cold-start), or hasn't run successfully
        # in a while.  Caller (scheduler) records this as a normal cycle
        # that simply produced 0 rows; no exception needed.
        log.info(
            "positive_ev: no fresh odds_api cache for any sport "
            "(stale=%d) — skipping cycle",
            sports_stale,
        )
    return written


def register_positive_ev_feed() -> None:
    if registry.status_of("positive_ev"):
        return
    api_key = (
        os.environ.get("THE_ODDS_API_KEY")
        or os.environ.get("THEODDSAPI_KEY")
        or os.environ.get("ODDS_API_KEY")
    )
    registry.register(Feed(
        name="positive_ev",
        cadence_seconds=float(DEFAULT_PEV_CADENCE),
        poll=_poll_positive_ev,
        enabled=bool(api_key),
        skipped_reason=(
            None
            if api_key
            else "THE_ODDS_API_KEY not set (positive-EV substitute for outlier.bet)"
        ),
    ))
