"""Cross-book line-shopping lookup for sports candidates (task #828).

Reads the most recent `line_snapshots` row per book for a given
(sport, market_type, matchup), converts decimal odds to implied
probability, and returns the best available implied probability
across the configured book set.

Used by ``gate_best_line`` to:
  * Block a Kalshi order when a traditional sportsbook is offering a
    meaningfully better implied price (operator gets a `best_line_alert`
    and places the bet manually).
  * Re-route a Kalshi order to Polymarket when Polymarket has a better
    price for the same market.

Latency budget: 50 ms p95.  Fails open on every error path so an
odds-feed outage cannot silently halt sports trading.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from .. import db


log = logging.getLogger("axiom.risk.best_line_lookup")


# Mapping from internal league code -> The Odds API ``sport`` value.
# Same shape as `app.feeds.odds_api` registrations.  Anything not in
# this map is unsupported by the gate (it fails-open / passes).
_LEAGUE_TO_ODDS_API_SPORT: dict[str, str] = {
    "NBA":   "basketball_nba",
    "WNBA":  "basketball_wnba",
    "NCAAB": "basketball_ncaab",
    "NHL":   "icehockey_nhl",
    "MLB":   "baseball_mlb",
    "NFL":   "americanfootball_nfl",
    "NCAAF": "americanfootball_ncaaf",
    "EPL":   "soccer_epl",
    "LALIGA":"soccer_spain_la_liga",
    "SERIEA":"soccer_italy_serie_a",
    "BUNDES":"soccer_germany_bundesliga",
    "LIGUE1":"soccer_france_ligue_one",
    "MLS":   "soccer_usa_mls",
    "UCL":   "soccer_uefa_champs_league",
}


# Translate the candidate-side market_type to the odds_api `market_type`
# value persisted in `line_snapshots`.  We only support the three
# market kinds the task scopes (ML / SPREAD / TOTAL).
_MARKET_KIND_MAP: dict[str, str] = {
    "moneyline": "h2h",
    "ml":        "h2h",
    "h2h":       "h2h",
    "spread":    "spreads",
    "spreads":   "spreads",
    "handicap":  "spreads",
    "total":     "totals",
    "totals":    "totals",
    "ou":        "totals",
}


# Books we treat as "Kalshi/Polymarket equivalents" (excluded from the
# 'traditional sportsbook' comparison because they are the venues we
# already execute on).
_OWN_VENUES: frozenset[str] = frozenset({"kalshi", "polymarket"})


# The Odds API book keys for the major US sportsbooks we care about.
# Anything that lands in `line_snapshots` is considered; this set just
# names the books we consider "traditional sportsbooks" for the
# manual-placement alert.
TRADITIONAL_BOOKS: frozenset[str] = frozenset({
    "draftkings", "fanduel", "betmgm", "caesars", "pointsbetus",
    "williamhill_us", "betrivers", "wynnbet", "betonlineag",
    "bovada", "mybookieag", "lowvig", "betus",
})


@dataclass(frozen=True)
class BookQuote:
    book: str
    implied_prob: float
    odds_value: float
    age_seconds: float


@dataclass(frozen=True)
class BestLineResult:
    """Outcome of one best-line lookup.

    ``best_book`` / ``best_implied`` are the single best price across
    every book (including Polymarket / Kalshi).  ``best_traditional``
    is the best price restricted to traditional sportsbooks — the
    operator can only manually place on those.  ``polymarket_implied``
    is surfaced separately so the gate can decide between re-route vs
    alert+block.
    """
    quotes: list[BookQuote]
    best_book: str | None
    best_implied: float | None
    best_traditional_book: str | None
    best_traditional_implied: float | None
    polymarket_implied: float | None
    kalshi_implied: float | None
    error: str | None = None


def league_to_odds_api_sport(league: str | None) -> str | None:
    if not league:
        return None
    return _LEAGUE_TO_ODDS_API_SPORT.get(str(league).strip().upper())


def normalize_market_kind(market_type: str | None) -> str | None:
    if not market_type:
        return None
    return _MARKET_KIND_MAP.get(str(market_type).strip().lower())


def decimal_to_implied(odds_value: float | None) -> float | None:
    """Decimal odds -> implied probability (0..1).  Returns None when
    odds are missing / non-positive / <=1 (no edge possible)."""
    if odds_value is None:
        return None
    try:
        v = float(odds_value)
    except (TypeError, ValueError):
        return None
    if v <= 1.0:
        return None
    return 1.0 / v


async def lookup_best_line(
    *,
    league: str | None,
    market_type: str | None,
    home_team: str | None,
    away_team: str | None,
    max_age_seconds: int = 300,
    timeout_seconds: float = 0.05,
) -> BestLineResult:
    """Return the best implied price across every book that quoted the
    matchup in the last ``max_age_seconds``.

    Matching is done by (sport, market_type) + fuzzy team match on
    ``home_team`` / ``away_team`` — internal game ids and odds_api game
    ids live in different namespaces, so team-name match is the only
    cross-namespace key that works without a dedicated mapping table.

    Returns an all-None result (with ``error``) on any failure so the
    gate fails-open.
    """
    empty = BestLineResult(
        quotes=[], best_book=None, best_implied=None,
        best_traditional_book=None, best_traditional_implied=None,
        polymarket_implied=None, kalshi_implied=None,
    )
    sport = league_to_odds_api_sport(league)
    kind = normalize_market_kind(market_type)
    if not sport or not kind or not (home_team and away_team):
        return empty
    home = str(home_team).strip()
    away = str(away_team).strip()
    if not home or not away:
        return empty

    sql = """
        SELECT DISTINCT ON (book)
               book,
               odds_value,
               extract(epoch from (now() - "timestamp"))::float AS age_seconds
          FROM line_snapshots
         WHERE sport = $1
           AND market_type = $2
           AND "timestamp" > now() - make_interval(secs => $3)
           AND (
                 home_team ILIKE $4 OR away_team ILIKE $4
              OR home_team ILIKE $5 OR away_team ILIKE $5
           )
           AND odds_value IS NOT NULL
         ORDER BY book, "timestamp" DESC
    """
    try:
        rows = await asyncio.wait_for(
            db.fetch(
                sql,
                sport,
                kind,
                int(max_age_seconds),
                f"%{home}%",
                f"%{away}%",
            ),
            timeout=float(timeout_seconds) * 4,  # generous outer cap; gate
                                                  # has its own 50ms guard
        )
    except asyncio.TimeoutError:
        log.warning(
            "best-line lookup timed out league=%s mt=%s",
            league, market_type,
        )
        return BestLineResult(
            quotes=[], best_book=None, best_implied=None,
            best_traditional_book=None, best_traditional_implied=None,
            polymarket_implied=None, kalshi_implied=None,
            error="timeout",
        )
    except Exception as e:  # pragma: no cover — fails open
        log.warning("best-line lookup failed: %s", e)
        return BestLineResult(
            quotes=[], best_book=None, best_implied=None,
            best_traditional_book=None, best_traditional_implied=None,
            polymarket_implied=None, kalshi_implied=None,
            error=f"{type(e).__name__}: {e}"[:200],
        )

    quotes: list[BookQuote] = []
    for r in rows or []:
        book_raw = (r.get("book") or "").strip().lower()
        if not book_raw:
            continue
        ip = decimal_to_implied(r.get("odds_value"))
        if ip is None:
            continue
        quotes.append(BookQuote(
            book=book_raw,
            implied_prob=ip,
            odds_value=float(r.get("odds_value")),
            age_seconds=float(r.get("age_seconds") or 0.0),
        ))

    if not quotes:
        return empty

    # For YES-side comparison the "best" price for us-as-buyer is the
    # *lowest* implied probability (we pay less for the same outcome).
    # The caller (gate_best_line) re-frames this depending on side.
    # Here we just return every quote + convenience accessors.
    best_q = min(quotes, key=lambda q: q.implied_prob)
    trad = [q for q in quotes if q.book in TRADITIONAL_BOOKS]
    best_trad = min(trad, key=lambda q: q.implied_prob) if trad else None

    poly = next((q.implied_prob for q in quotes if q.book == "polymarket"), None)
    kal = next((q.implied_prob for q in quotes if q.book == "kalshi"), None)

    return BestLineResult(
        quotes=quotes,
        best_book=best_q.book,
        best_implied=best_q.implied_prob,
        best_traditional_book=best_trad.book if best_trad else None,
        best_traditional_implied=best_trad.implied_prob if best_trad else None,
        polymarket_implied=poly,
        kalshi_implied=kal,
    )


def implied_delta_bps(our_implied: float, other_implied: float) -> int:
    """Return the absolute implied-prob delta in basis points.

    A 2% implied-prob improvement == 200 bps.
    """
    return int(round(abs(float(our_implied) - float(other_implied)) * 10_000))
