"""Discover live Kalshi sports markets and feed them into the orchestrator
as candidate predictions.

Background
----------
The orchestrator's :func:`app.orchestrator.cycle._candidate_predictions`
SELECT pulls from the ``predictions`` table, filtering on
``kalshi_market_id IS NOT NULL`` (only Kalshi-backed contracts can be
traded by the live submission path).  For crypto, the producer is
:mod:`app.feeds.kalshi_crypto_markets` — it walks ``KXBTC*`` / ``KXETH*``
markets and upserts a row per market.

For sports we previously had **no producer at all**: the schedule
synthesizer (:mod:`app.feeds.kalshi_schedules`) collapses every
event's markets down to a single ``sportradar_schedules`` row for the
chatbot's "Today's Games" lookahead, but it never writes into
``predictions``.  Result: ``_candidate_predictions('sports')`` returned
zero rows, and every cycle reported ``picks_evaluated=0`` for sports
verticals — a silent dead pipeline that this module fixes.

What this feed does
-------------------
* Walks every configured Kalshi sports series (NFL/NBA/MLB/NHL/soccer/
  NCAA) at a 60s cadence.
* For each open binary market, builds a ``predictions`` row keyed to
  the per-market ticker (so each tradeable contract gets its own row,
  not collapsed by event like ``kalshi_schedules`` does).
* Parses the matchup out of the market title using the same
  :mod:`app.feeds.kalshi_schedules` parser the schedule synthesizer
  already uses, so home/away naming stays consistent across the two
  features.

League names are deliberately aligned with
:mod:`app.verticals.builtins` (``SERIEA``, ``BUNDES``, ``LIGUE1``)
rather than the kalshi_schedules conventions (``SERIE_A`` etc.) — the
``soccer`` vertical's league filter expects the verticals-registry
form, and a row written under ``SERIE_A`` would never be picked up.

Public surface:
    register_kalshi_sports_markets_feed() — wired into app.main on startup.
"""
from __future__ import annotations

import json as _json
import logging
import os
import re
from typing import Any, Iterable

from .. import db
from ..config import get_settings
from ..kalshi import client as kalshi_client_mod
from .kalshi_schedules import (
    _event_key,
    _parse_close_time,
    _parse_teams_from_title,
)
from .registry import Feed, registry


log = logging.getLogger("axiom.feeds.kalshi_sports_markets")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Series → canonical league label written into ``predictions.league``.
#
# Names MUST match the verticals registry (``app/verticals/builtins.py``)
# so the soccer vertical's ``leagues=("EPL", "LALIGA", "SERIEA",
# "BUNDES", "LIGUE1", "MLS", "UCL")`` filter actually picks them up.
# (The kalshi_schedules feed uses different conventions — ``SERIE_A``,
# ``BUNDESLIGA``, ``LIGUE_1`` — because its consumer is the chatbot's
# schedule lookup, not the orchestrator.)
#
# The ``sports`` vertical itself uses ``leagues=()`` (catch-all) so any
# row written here is automatically eligible for sports cycles too.
#
# Operators can extend or override at deploy time via
#   AXIOM_KALSHI_SPORTS_MARKETS_SERIES="KXNFLGAME=NFL,KXNBAGAME=NBA"
_DEFAULT_SERIES_TO_LEAGUE: dict[str, str] = {
    "KXNFLGAME":      "NFL",
    "KXNBAGAME":      "NBA",
    "KXMLBGAME":      "MLB",
    "KXNHLGAME":      "NHL",
    "KXMLSGAME":      "MLS",
    "KXEPLGAME":      "EPL",
    "KXLALIGAGAME":   "LALIGA",
    "KXSERIEAGAME":   "SERIEA",
    "KXBUNDESGAME":   "BUNDES",
    "KXLIGUE1GAME":   "LIGUE1",
    "KXUCLGAME":      "UCL",
    "KXNCAAFGAME":    "NCAAF",
    "KXNCAABGAME":    "NCAAB",
}

_PAGE_LIMIT: int = 200
_MAX_PAGES: int = 5
_DEFAULT_CADENCE_SECS: float = 60.0


def _series_to_league() -> dict[str, str]:
    raw = (os.environ.get("AXIOM_KALSHI_SPORTS_MARKETS_SERIES") or "").strip()
    if not raw:
        return dict(_DEFAULT_SERIES_TO_LEAGUE)
    out: dict[str, str] = {}
    for chunk in raw.split(","):
        if "=" not in chunk:
            continue
        series, league = chunk.split("=", 1)
        series = series.strip()
        league = league.strip().upper()
        if series and league:
            out[series] = league
    return out or dict(_DEFAULT_SERIES_TO_LEAGUE)


def _cadence_seconds() -> float:
    raw = os.environ.get("AXIOM_KALSHI_SPORTS_MARKETS_CADENCE_SECS")
    if not raw:
        return _DEFAULT_CADENCE_SECS
    try:
        v = float(raw)
    except ValueError:
        return _DEFAULT_CADENCE_SECS
    # Floor at 30s — Kalshi's per-series ``list_markets`` is paged but
    # not free, and sports schedules don't refresh meaningfully faster
    # than that.
    return max(30.0, v)


# ---------------------------------------------------------------------------
# Pricing helper (mirrors kalshi_crypto_markets._mid_price_dollars).
# Duplicated rather than imported to keep the two feeds independent —
# pricing semantics for sports vs crypto could legitimately diverge in
# the future (e.g. a sports-specific liquidity floor).
# ---------------------------------------------------------------------------

def _mid_price_dollars(market: dict[str, Any]) -> float | None:
    """Best-effort YES mid-price in dollars (0..1) from a Kalshi market row.

    Kalshi's live ``GET /markets`` response now ships the YES book under
    the ``*_dollars`` suffixed fields (``yes_bid_dollars`` /
    ``yes_ask_dollars`` / ``last_price_dollars``) which are already
    expressed in dollars on a 0..1 scale.  The legacy un-suffixed
    fields (``yes_bid`` / ``yes_ask`` / ``last_price``) used cents on
    a 0..100 scale and have been removed from the live response — but
    we still accept them as a fallback so unit-test fixtures that
    pre-date the API rename keep working without churn.

    Order of fallbacks across sides: (bid+ask)/2 → last_price → ask →
    bid.  Returns ``None`` when no price signal is present at all so
    the upsert leaves ``predictions.kalshi_price`` NULL — the
    orchestrator treats a NULL price as "not yet seeded" and skips
    the row, preventing a book-vs-fair edge calc against a phantom mid.
    """
    def _to_num(v: Any) -> float | None:
        if v is None:
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        if f < 0:
            return None
        return f

    def _resolve(dollars_key: str, cents_key: str) -> float | None:
        d = _to_num(market.get(dollars_key))
        if d is not None:
            return d
        c = _to_num(market.get(cents_key))
        if c is not None:
            return c / 100.0
        return None

    bid_d = _resolve("yes_bid_dollars", "yes_bid")
    ask_d = _resolve("yes_ask_dollars", "yes_ask")
    last_d = _resolve("last_price_dollars", "last_price")

    if bid_d is not None and ask_d is not None and ask_d >= bid_d:
        return (bid_d + ask_d) / 2.0
    if last_d is not None:
        return last_d
    if ask_d is not None:
        return ask_d
    if bid_d is not None:
        return bid_d
    return None


# ---------------------------------------------------------------------------
# Soccer 3-way leg orientation parsing (task #880)
# ---------------------------------------------------------------------------
#
# Kalshi soccer game markets are 3-way: a single event splits into a
# HOME-win, DRAW, and AWAY-win market, each its own ticker.  The
# binary YES on those tickers means "this leg occurred".  The live
# in-game predictor (:mod:`app.learning.soccer_live`) can use the
# leg label to apply a closed-form Dixon-Coles / Poisson update
# against the live score — but only if it knows which leg YES is
# pricing.  We parse the leg out of the Kalshi title here (the
# producer) and stash it in ``metadata.leg_orientation`` so the live
# predictor doesn't have to re-parse on every cycle.

_DRAW_KEYWORDS = (
    "draw", " tie", "tied", "ends level", "level after",
    "stalemate", "share the points",
)
# "Will Manchester City beat Arsenal?", "Manchester City beats Arsenal",
# "Manchester City defeats Arsenal", "Will Manchester City win?"
_WIN_SUBJECT_RE = re.compile(
    r"^\s*(?:will\s+)?(?:the\s+)?(?P<subject>.+?)\s+"
    r"(?:beat|beats|defeat|defeats|win|wins)\b",
    re.IGNORECASE,
)


def _normalise_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower()) if s else ""


def _parse_leg_orientation(
    title: str | None,
    home_team: str | None,
    away_team: str | None,
) -> str | None:
    """Return ``"HOME"`` / ``"DRAW"`` / ``"AWAY"`` for a Kalshi soccer
    game-market title when we can identify which 3-way leg the YES
    side is pricing; ``None`` when the title isn't a recognised
    moneyline / draw template.

    The match against home/away is normalised (lowercased, alphanum
    only) so "Man. City" matches "Manchester City" loosely — false
    positives are unlikely because the producer already knows the
    canonical names for the event.
    """
    if not title:
        return None
    low = title.lower()
    for kw in _DRAW_KEYWORDS:
        if kw in low:
            return "DRAW"
    m = _WIN_SUBJECT_RE.match(title)
    if not m:
        return None
    subject = _normalise_name(m.group("subject"))
    if not subject:
        return None
    home_n = _normalise_name(home_team or "")
    away_n = _normalise_name(away_team or "")
    # Substring match either direction so "City" matches "Manchester City"
    # and vice versa.  Home checked first because Kalshi titles are
    # marginally more likely to phrase a home-side YES.
    if home_n and (subject in home_n or home_n in subject):
        return "HOME"
    if away_n and (subject in away_n or away_n in subject):
        return "AWAY"
    return None


# Series tickers that price soccer 3-way leagues — leg orientation
# parsing is only meaningful for these.  Other sports (NFL/NBA/...)
# are 2-way moneylines whose YES is always the home/away side per the
# title parser; the live predictor for those sports lands in a later
# task and does not consume ``leg_orientation``.
_SOCCER_SERIES: frozenset[str] = frozenset({
    "KXMLSGAME", "KXEPLGAME", "KXLALIGAGAME", "KXSERIEAGAME",
    "KXBUNDESGAME", "KXLIGUE1GAME", "KXUCLGAME",
})


# ---------------------------------------------------------------------------
# Candidate construction
# ---------------------------------------------------------------------------

def _candidate_from_market(
    market: dict[str, Any], *, league: str, series: str,
) -> dict[str, Any] | None:
    """Build a predictions-row payload for a single Kalshi sports market.

    Returns ``None`` for markets we can't safely map:

    * No per-market ticker (can't dedupe via the unique index).
    * No parseable matchup in the title (would write an "Unknown vs
      Unknown" candidate that the orchestrator can't display).

    Does NOT require a price — a freshly-listed market with no
    quotes yet still gets a row so the cycle can pick it up the moment
    a quote appears (the cycle itself filters out NULL-price rows).
    """
    ticker = (market.get("ticker") or market.get("market_ticker") or "").strip()
    if not ticker:
        return None

    # Parse the matchup title.  The schedule feed already does this for
    # event-level rows; we reuse the same parser so home/away naming is
    # 1:1 with what the chatbot displays for the same event.
    parsed_teams = _parse_teams_from_title(market.get("title"))
    if parsed_teams is None:
        # Skip exotic / prop / novelty markets whose titles don't
        # describe a head-to-head matchup — they'd otherwise pollute
        # the candidate list and the orchestrator can't price them
        # against a moneyline anyway.
        return None
    away, home = parsed_teams
    matchup = f"{away} @ {home}"

    event_ticker = _event_key(market) or ticker
    game_id = f"kalshi:{event_ticker}"
    close_time = _parse_close_time(market.get("close_time"))
    price = _mid_price_dollars(market)

    # Leg orientation is only meaningful for the soccer 3-way series —
    # see ``_SOCCER_SERIES``.  Stored on the candidate's metadata blob
    # so the live in-game soccer predictor (task #880) can pick THIS
    # leg's Dixon-Coles posterior without re-parsing the title every
    # cycle.  ``None`` for non-soccer markets and for soccer titles we
    # can't confidently classify (the live predictor falls back to its
    # leg-agnostic blend in that case).
    leg_orientation: str | None = None
    if series in _SOCCER_SERIES:
        leg_orientation = _parse_leg_orientation(
            market.get("title"), home, away,
        )

    return {
        "game_id": game_id,
        "league": league,
        "matchup": matchup,
        "home_team": home,
        "away_team": away,
        "kalshi_market_id": ticker,
        "kalshi_price": price,
        "game_time": close_time,
        # Persisted into ``predictions.metadata`` (jsonb).  Downstream
        # readers don't currently SELECT this column — _candidate_predictions
        # only pulls 11 specific scalar columns — but we keep the full
        # provenance blob for forensics, alert payloads, and any future
        # consumer that wants to know which Kalshi market a candidate
        # originated from without re-fetching the venue.
        "metadata": {
            "kalshi_market_id": ticker,
            "kalshi_event_ticker": event_ticker,
            "kalshi_series": series,
            "kalshi_price": price,
            "title": market.get("title"),
            "status": market.get("status"),
            "close_time": close_time.isoformat() if close_time else None,
            # Preserve both the new ``*_dollars`` (live API, 0..1
            # dollars) and the legacy un-suffixed (cents, 0..100)
            # field names so any forensics consumer that knows either
            # convention can read the same blob without conditional
            # logic.  Whichever side the API returned will be non-null.
            "yes_bid": market.get("yes_bid"),
            "yes_ask": market.get("yes_ask"),
            "last_price": market.get("last_price"),
            "yes_bid_dollars": market.get("yes_bid_dollars"),
            "yes_ask_dollars": market.get("yes_ask_dollars"),
            "last_price_dollars": market.get("last_price_dollars"),
            "home_team": home,
            "away_team": away,
            "leg_orientation": leg_orientation,
            "source": "kalshi_sports_markets",
        },
    }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

# The partial unique index on
# ``predictions(kalshi_market_id) WHERE kalshi_market_id IS NOT NULL``
# is what makes each ticker idempotent across polls.
#
# Column choice: the predictions table has a ``metadata`` jsonb column
# (default ``{}``) and NO ``game_context`` / ``updated_at`` columns.
# An earlier version of this feed (and the crypto producer it was
# modeled on) tried to write into ``game_context``/``updated_at``,
# which made every upsert silently raise — visible only as
# ``column "game_context" of relation "predictions" does not exist``
# warnings in the per-row defensive ``except``.  Rewriting against
# the actual column names is what unblocks the candidate flow.
_UPSERT_SQL = """
INSERT INTO predictions
       (game_id, league, matchup, home_team, away_team,
        kalshi_market_id, kalshi_price, game_time, metadata)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
ON CONFLICT (kalshi_market_id) WHERE kalshi_market_id IS NOT NULL
DO UPDATE SET
    league       = EXCLUDED.league,
    matchup      = EXCLUDED.matchup,
    home_team    = EXCLUDED.home_team,
    away_team    = EXCLUDED.away_team,
    kalshi_price = EXCLUDED.kalshi_price,
    game_time    = EXCLUDED.game_time,
    metadata     = EXCLUDED.metadata;
"""


async def _upsert_candidates(rows: Iterable[dict[str, Any]]) -> int:
    """Idempotent per-row upsert — returns rows touched.

    Each row is its own statement so a single Kalshi market with a
    weird payload can't take the rest of the page down (matches the
    crypto producer's defensive posture).
    """
    from datetime import timezone as _tz

    n = 0
    for row in rows:
        # ``predictions.game_time`` is ``timestamp WITHOUT time zone``
        # but Kalshi's close times come back tz-aware.  asyncpg won't
        # silently coerce; without this normalization every row would
        # be dropped in the per-row ``except`` below with a confusing
        # "can't subtract offset-naive and offset-aware datetimes"
        # error.  Convert to naive UTC at the bind boundary only —
        # the producer's row dict stays tz-aware so callers/tests
        # don't have to think about it.
        gt = row.get("game_time")
        if gt is not None and getattr(gt, "tzinfo", None) is not None:
            gt = gt.astimezone(_tz.utc).replace(tzinfo=None)
        try:
            await db.execute(
                _UPSERT_SQL,
                row["game_id"],
                row["league"],
                row.get("matchup"),
                row.get("home_team"),
                row.get("away_team"),
                row["kalshi_market_id"],
                row.get("kalshi_price"),
                gt,
                _json.dumps(row.get("metadata") or {}, default=str),
            )
            n += 1
        except Exception as e:  # noqa: BLE001
            log.warning(
                "predictions upsert failed for %s: %s",
                row.get("kalshi_market_id"),
                e,
            )
    return n


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------

async def _list_series_markets(
    client: kalshi_client_mod.KalshiClient, series: str,
) -> list[dict[str, Any]]:
    """Walk every page of open markets under ``series``.  Identical
    shape to the crypto producer's walker; duplicated so a future
    sports-specific filter (status, date range) can land here without
    coupling the two feeds."""
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(_MAX_PAGES):
        try:
            page = await client.list_markets(
                limit=_PAGE_LIMIT,
                status="open",
                series_ticker=series,
                cursor=cursor,
            )
        except Exception as e:  # noqa: BLE001
            log.info("kalshi list_markets failed for %s: %s", series, e)
            break
        body = page or {}
        markets = body.get("markets") or []
        if not isinstance(markets, list):
            markets = []
        out.extend(m for m in markets if isinstance(m, dict))
        cursor = body.get("cursor") or body.get("next_cursor")
        if not cursor:
            break
    return out


async def poll_kalshi_sports_markets() -> int:
    """One poll: walk every configured Kalshi sports series and upsert
    a ``predictions`` candidate row per market.

    Returns the total rows touched so the feed registry exposes
    realised work on the operator console.  Soft-fails to 0 when
    Kalshi credentials aren't configured (registration already
    advertises a clear ``skipped_reason`` in that case).
    """
    settings = get_settings()
    if not (settings.kalshi_api_key_id and settings.kalshi_private_key):
        return 0

    series_map = _series_to_league()
    rows: list[dict[str, Any]] = []
    series_seen: list[str] = []
    series_with_rows: list[str] = []
    try:
        async with kalshi_client_mod.KalshiClient(settings=settings) as client:
            for series, league in series_map.items():
                series_seen.append(series)
                markets = await _list_series_markets(client, series)
                produced = 0
                for m in markets:
                    cand = _candidate_from_market(m, league=league, series=series)
                    if cand is not None:
                        rows.append(cand)
                        produced += 1
                if produced:
                    series_with_rows.append(series)
    except Exception as e:  # noqa: BLE001
        log.warning("kalshi_sports_markets poll crashed: %s", e)
        return 0

    written = await _upsert_candidates(rows) if rows else 0
    registry.set_metadata(
        "kalshi_sports_markets",
        series_polled=series_seen,
        series_with_rows=series_with_rows,
        last_total_rows=written,
    )
    return written


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_kalshi_sports_markets_feed() -> None:
    """Wire the discovery poller into the global feed registry.

    Idempotent so a hot reload doesn't crash the lifespan; advertises a
    clear ``skipped_reason`` when Kalshi credentials are missing so the
    operator console renders ``enabled=false`` rather than silently
    swallowing the registration.
    """
    name = "kalshi_sports_markets"
    if registry.status_of(name):
        return
    settings = get_settings()
    has_creds = bool(
        settings.kalshi_api_key_id and settings.kalshi_private_key
    )
    skipped = (
        None if has_creds
        else "KALSHI_API_KEY_ID / KALSHI_API_PRIVATE_KEY not set"
    )
    registry.register(Feed(
        name=name,
        cadence_seconds=_cadence_seconds(),
        poll=poll_kalshi_sports_markets,
        enabled=has_creds,
        skipped_reason=skipped,
    ))
    log.info(
        "kalshi sports markets feed registered (enabled=%s, cadence=%.1fs)",
        has_creds, _cadence_seconds(),
    )
