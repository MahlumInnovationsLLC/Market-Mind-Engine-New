"""Resolve an Edge Terminal pick → Kalshi market ticker.

Three-layer lookup, in order:

  1. Scraper-supplied ``kalshi_ticker`` (DOM truth — trusted as-is).
  2. ``market_title_cache`` filtered by the series prefix(es) that
     correspond to the signal's *vertical + pick_type* (e.g. NHL +
     spread → ``KXNHLSPREAD``).
  3. Live ``KalshiClient.list_markets(series_ticker=...)`` for the
     same prefixes — results are persisted to ``market_title_cache``
     so subsequent matches inside the same poll cycle are cache hits.

Scoring per candidate is team-name overlap + (for totals/spreads) a
numeric-line bonus.  We disqualify anything that doesn't contain the
picked team and we require a minimum score so a random market in the
same series can't latch onto an unrelated signal.

Public API:

  * ``resolve(signal) -> str | None`` — back-compat, used by the
    executor's hot path.
  * ``resolve_match(signal) -> ResolveResult`` — same lookup, but
    returns the candidate list and a human-readable reason so the
    dashboard widget can show *why* a signal did/didn't match.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from .. import db
from ..config import get_settings
from ..kalshi.client import KalshiAPIError, KalshiClient
from . import llm_resolver


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Vertical + pick_type → Kalshi series prefix(es), in preference order.
# ---------------------------------------------------------------------------
# Kalshi names sports markets by series like ``KXMLBGAME`` (moneyline),
# ``KXMLBSPREAD`` (run line), ``KXMLBTOTAL`` (over/under).  The mapping
# below is what the Edge Terminal scraper produces today; unknown
# combinations fall back to a single GAME prefix (moneyline) on the
# assumption that's the most common Kalshi listing.
#
# Soccer is the exception: Kalshi does NOT list a generic ``KXSOCCER*``
# series (those tickers exist but carry no markets).  Instead every
# competition has its own GAME / SPREAD / TOTAL series — EPL, LaLiga,
# Serie A, Ligue 1, MLS, plus the European cups UCL / UEL (e.g.
# ``KXUCLSPREAD-…PSGARS-…`` for a PSG vs Arsenal Champions League leg).
# The scraper only tags the vertical as ``soccer`` (not the league), so
# we fan every soccer pick out across all competition series and let the
# team-name + line scorer latch onto the right one.  Bundesliga is kept
# in the list even when off-season (an empty series just yields no
# candidates).
_SOCCER_GAME = [
    "KXEPLGAME", "KXLALIGAGAME", "KXSERIEAGAME", "KXLIGUE1GAME",
    "KXMLSGAME", "KXUCLGAME", "KXUELGAME", "KXBUNDESGAME",
]
_SOCCER_SPREAD = [
    "KXEPLSPREAD", "KXLALIGASPREAD", "KXSERIEASPREAD", "KXLIGUE1SPREAD",
    "KXMLSSPREAD", "KXUCLSPREAD", "KXUELSPREAD", "KXBUNDESSPREAD",
]
_SOCCER_TOTAL = [
    "KXEPLTOTAL", "KXLALIGATOTAL", "KXSERIEATOTAL", "KXLIGUE1TOTAL",
    "KXMLSTOTAL", "KXUCLTOTAL", "KXUELTOTAL", "KXBUNDESTOTAL",
]
_SERIES_BY_VERTICAL_PICK: dict[tuple[str, str], list[str]] = {
    ("mlb", "moneyline"): ["KXMLBGAME"],
    ("mlb", "spread"):    ["KXMLBSPREAD"],
    ("mlb", "total"):     ["KXMLBTOTAL"],
    ("nhl", "moneyline"): ["KXNHLGAME"],
    ("nhl", "spread"):    ["KXNHLSPREAD"],
    ("nhl", "total"):     ["KXNHLTOTAL"],
    ("nba", "moneyline"): ["KXNBAGAME"],
    ("nba", "spread"):    ["KXNBASPREAD"],
    ("nba", "total"):     ["KXNBATOTAL"],
    ("ncaamb", "moneyline"): ["KXNCAAMBGAME"],
    ("ncaamb", "spread"):    ["KXNCAAMBSPREAD"],
    ("ncaamb", "total"):     ["KXNCAAMBTOTAL"],
    ("ncaafb", "moneyline"): ["KXNCAAFBGAME"],
    ("ncaafb", "spread"):    ["KXNCAAFBSPREAD"],
    ("ncaafb", "total"):     ["KXNCAAFBTOTAL"],
    ("nfl", "moneyline"): ["KXNFLGAME"],
    ("nfl", "spread"):    ["KXNFLSPREAD"],
    ("nfl", "total"):     ["KXNFLTOTAL"],
    ("soccer", "moneyline"): _SOCCER_GAME,
    ("soccer", "spread"):    _SOCCER_SPREAD,
    ("soccer", "total"):     _SOCCER_TOTAL,
    ("tennis", "moneyline"): ["KXATPMATCH", "KXWTAMATCH"],
}


# ---------------------------------------------------------------------------
# Dynamic series discovery (tag-based) — "assume Kalshi can trade anything".
# ---------------------------------------------------------------------------
# The static map above only knows a handful of leagues, so a perfectly
# tradeable fixture in a competition we never hardcoded (international
# friendlies, Nations League, World Cup, EFL Championship, Liga MX, …)
# would hard-fail with "no kalshi series mapped".  Kalshi tags every
# Sports series with its sport ("Soccer", "Baseball", …); we use that tag
# to DISCOVER every series for a vertical + pick_type at runtime and union
# it with the static list.  New competitions Kalshi adds are then covered
# automatically with no code change.
#
# Scoped to soccer for now: soccer is the vertical with dozens of parallel
# competitions (and the one that was silently dropping friendlies).  The
# single-league sports (MLB/NHL/NBA/NFL) already resolve precisely from
# their one static series, and tennis uses MATCH (not GAME) markets, so we
# deliberately do NOT auto-discover those to avoid latching onto the wrong
# market type.  Extend ``_VERTICAL_SPORT_TAG`` to widen coverage.
_VERTICAL_SPORT_TAG: dict[str, str] = {
    "soccer": "Soccer",
}
_PICK_TYPE_SUFFIX: dict[str, str] = {
    "moneyline": "GAME",
    "spread": "SPREAD",
    "total": "TOTAL",
}
# Series whose ticker ends with our suffix but represent a DIFFERENT bet
# (half-time, team totals, both-teams-to-score, playoff-series aggregates)
# must be excluded so a single full-game pick never latches onto them.
_SERIES_EXCLUDE_MARKERS = ("1H", "2H", "TEAMTOTAL", "BTTS", "SERIES")

# In-process cache of the full Sports series list — it changes rarely, so
# we refresh at most every few hours and never block the no-network
# (dashboard) path on it.
_SERIES_DISCOVERY_TTL = 6 * 3600.0  # seconds
_sports_series_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


async def _all_sports_series(*, allow_network: bool) -> list[dict[str, Any]]:
    """Return Kalshi's full ``category="Sports"`` series list, cached.

    When the cache is cold and ``allow_network`` is False (the dashboard's
    no-HTTP path) returns an empty list so we never serialise a series
    fetch on that hot path.
    """
    now = time.monotonic()
    cached = _sports_series_cache.get("sports")
    if cached and (now - cached[0]) < _SERIES_DISCOVERY_TTL:
        return cached[1]
    if not allow_network:
        return cached[1] if cached else []
    settings = get_settings()
    if not settings.kalshi_api_key_id or not settings.kalshi_private_key:
        return cached[1] if cached else []
    try:
        async with KalshiClient(settings=settings, timeout=8.0) as c:
            data = await c.list_series(category="Sports")
    except Exception as e:  # noqa: BLE001 — never let discovery break resolve
        log.warning("kalshi series discovery failed: %s", e)
        return cached[1] if cached else []
    series = data.get("series") if isinstance(data, dict) else None
    if not isinstance(series, list):
        series = []
    _sports_series_cache["sports"] = (now, series)
    return series


async def _discover_series(
    vertical: str, pick_type: str, *, allow_network: bool,
) -> list[str]:
    """Discover every Kalshi series ticker for ``vertical`` + ``pick_type``.

    Filters the live Sports series list by the vertical's sport tag and the
    pick_type's ticker suffix (GAME/SPREAD/TOTAL), dropping non-full-game
    bet variants.  Returns ``[]`` for verticals we don't auto-discover.
    """
    tag = _VERTICAL_SPORT_TAG.get(vertical)
    suffix = _PICK_TYPE_SUFFIX.get(pick_type)
    if not tag or not suffix:
        return []
    out: list[str] = []
    for s in await _all_sports_series(allow_network=allow_network):
        ticker = (s.get("ticker") or "").upper()
        if not ticker.endswith(suffix):
            continue
        if any(m in ticker for m in _SERIES_EXCLUDE_MARKERS):
            continue
        if tag not in (s.get("tags") or []):
            continue
        out.append(ticker)
    return out


_STOPWORDS = {
    "vs", "at", "the", "of", "in", "and", "or", "on", "to",
    "game", "match", "winner", "over", "under", "yes", "no",
    "spread", "total", "moneyline", "team", "wins", "win",
    "goals", "runs", "points",
}

_NUM_PREFIX = re.compile(r"^\d")


# Token (lowercased team noun / city) → Kalshi short codes that appear
# inside tickers and/or as title fragments ("A's" → "ath"/"as").
# Only verticals we've actually seen in the wild; extend as needed.
_TEAM_ALIASES: dict[str, set[str]] = {
    # MLB
    "diamondbacks": {"ari", "az"}, "arizona": {"ari", "az"},
    "braves": {"atl"}, "atlanta": {"atl"},
    "orioles": {"bal"}, "baltimore": {"bal"},
    "red": {"bos"}, "boston": {"bos"},
    "cubs": {"chc"},
    "sox": {"bos", "cws", "chw"},
    "white": {"cws", "chw"},
    "reds": {"cin"}, "cincinnati": {"cin"},
    "guardians": {"cle"}, "cleveland": {"cle"},
    "rockies": {"col"}, "colorado": {"col"},
    "tigers": {"det"}, "detroit": {"det"},
    "astros": {"hou"}, "houston": {"hou"},
    "royals": {"kc", "kcr"}, "kansas": {"kc", "kcr"},
    "angels": {"laa", "ana"}, "anaheim": {"laa", "ana"},
    "dodgers": {"lad"},
    "marlins": {"mia", "fla"}, "miami": {"mia"},
    "brewers": {"mil"}, "milwaukee": {"mil"},
    "twins": {"min"}, "minnesota": {"min"},
    "mets": {"nym"},
    "yankees": {"nyy"},
    "athletics": {"oak", "ath", "as"}, "oakland": {"oak", "ath"},
    "phillies": {"phi"}, "philadelphia": {"phi"},
    "pirates": {"pit"}, "pittsburgh": {"pit"},
    "padres": {"sd", "sdp"},
    "giants": {"sf", "sfg"}, "francisco": {"sf", "sfg"},
    "mariners": {"sea"}, "seattle": {"sea"},
    "cardinals": {"stl"}, "louis": {"stl"},
    "rays": {"tb", "tbr"}, "tampa": {"tb", "tbr"},
    "texas": {"tex"},
    "blue": {"tor"}, "jays": {"tor"}, "toronto": {"tor"},
    "nationals": {"wsh", "was"}, "washington": {"wsh", "was"},
    "chicago": {"chc", "cws", "chw"},
    "york": {"nyy", "nym", "nyr", "nyi"},
    "angeles": {"lad", "laa", "lal", "lac", "lak"},
    # NHL
    "ducks": {"ana"}, "coyotes": {"ari"}, "bruins": {"bos"},
    "sabres": {"buf"}, "buffalo": {"buf"}, "flames": {"cgy"},
    "calgary": {"cgy"}, "hurricanes": {"car"}, "carolina": {"car"},
    "blackhawks": {"chi"}, "avalanche": {"col"}, "blue": {"cbj", "stl", "tor"},
    "jackets": {"cbj"}, "columbus": {"cbj"}, "stars": {"dal"},
    "dallas": {"dal"}, "wings": {"det"}, "oilers": {"edm"},
    "edmonton": {"edm"}, "panthers": {"fla"}, "florida": {"fla"},
    "kings": {"lak", "lal"}, "wild": {"min"}, "canadiens": {"mtl"},
    "montreal": {"mtl"}, "predators": {"nsh"}, "nashville": {"nsh"},
    "devils": {"njd"}, "jersey": {"njd"}, "islanders": {"nyi"},
    "rangers": {"nyr", "tex"}, "senators": {"ott"}, "ottawa": {"ott"},
    "flyers": {"phi"}, "penguins": {"pit"}, "sharks": {"sj", "sjs"},
    "san": {"sj", "sjs", "sd", "sf"}, "jose": {"sj", "sjs"},
    "kraken": {"sea"}, "lightning": {"tb", "tbl"},
    "maple": {"tor"}, "leafs": {"tor"}, "canucks": {"van"},
    "vancouver": {"van"}, "golden": {"vgk"}, "knights": {"vgk"},
    "vegas": {"vgk"}, "capitals": {"wsh"}, "jets": {"wpg", "nyj"},
    "winnipeg": {"wpg"},
    # NBA
    "hawks": {"atl"}, "celtics": {"bos"}, "nets": {"bkn", "bro"},
    "brooklyn": {"bkn", "bro"}, "hornets": {"cha"}, "charlotte": {"cha"},
    "bulls": {"chi"}, "cavaliers": {"cle"}, "mavericks": {"dal"},
    "nuggets": {"den"}, "denver": {"den"}, "pistons": {"det"},
    "warriors": {"gs", "gsw"}, "state": {"gs", "gsw"},
    "rockets": {"hou"}, "pacers": {"ind"}, "indiana": {"ind"},
    "clippers": {"lac"}, "lakers": {"lal"},
    "grizzlies": {"mem"}, "memphis": {"mem"}, "heat": {"mia"},
    "bucks": {"mil"}, "timberwolves": {"min"}, "pelicans": {"no", "nop"},
    "orleans": {"no", "nop"}, "knicks": {"nyk"},
    "thunder": {"okc"}, "oklahoma": {"okc"}, "city": {"okc", "kc"},
    "magic": {"orl"}, "orlando": {"orl"}, "sixers": {"phi"},
    "76ers": {"phi"}, "suns": {"phx", "pho"}, "phoenix": {"phx", "pho"},
    "trail": {"por"}, "blazers": {"por"}, "portland": {"por"},
    "kings": {"sac", "lak"}, "sacramento": {"sac"},
    "spurs": {"sa", "sas"}, "antonio": {"sa", "sas"},
    "raptors": {"tor"}, "jazz": {"uta"}, "utah": {"uta"},
    "wizards": {"wsh", "was"},
}

# City/place words shared by co-located rivals (Yankees/Mets, Dodgers/Angels,
# Cubs/White Sox, Giants/Padres). They cannot prove which side of a matchup a
# market names, so the matchup-integrity check ignores them for side identity:
# we require a *distinctive* token (the nickname) or a side-distinct code.
# Erring toward "no match" here is intentional — a wrong-game trade is worse
# than no trade.
_GENERIC_PLACE_TOKENS = {"new", "york", "los", "angeles", "san", "chicago"}


# Soccer national teams: lowercase name token -> Kalshi/FIFA 3-letter code.
# Kalshi tickers abbreviate national sides with the FIFA code (Romania->ROU,
# Netherlands->NED, Spain->ESP) which the generic first-three-letters
# heuristic gets WRONG ("rom"/"net"/"spa"), so a perfectly tradeable
# international fixture never matched.  Only single-token names + a handful of
# safe distinctive tokens for multi-word nations are mapped; ambiguous tokens
# (e.g. "korea", "congo") are deliberately omitted so we never prove the wrong
# side.  Codes that already equal the first three letters are still listed so
# the source of truth is explicit and collisions (Chile=CHI vs China=CHN,
# India=IND vs Indonesia=IDN) resolve correctly.
_SOCCER_NATIONS: dict[str, str] = {
    # UEFA
    "romania": "rou", "georgia": "geo", "spain": "esp", "netherlands": "ned",
    "germany": "ger", "france": "fra", "england": "eng", "portugal": "por",
    "italy": "ita", "belgium": "bel", "croatia": "cro", "switzerland": "sui",
    "denmark": "den", "poland": "pol", "austria": "aut", "serbia": "srb",
    "ukraine": "ukr", "sweden": "swe", "norway": "nor", "scotland": "sco",
    "wales": "wal", "hungary": "hun", "turkey": "tur", "greece": "gre",
    "czechia": "cze", "czech": "cze", "slovakia": "svk", "slovenia": "svn",
    "russia": "rus", "finland": "fin", "iceland": "isl", "albania": "alb",
    "armenia": "arm", "azerbaijan": "aze", "belarus": "blr", "bulgaria": "bul",
    "bosnia": "bih", "cyprus": "cyp", "estonia": "est", "kazakhstan": "kaz",
    "kosovo": "kvx", "latvia": "lva", "lithuania": "ltu", "luxembourg": "lux",
    "malta": "mlt", "moldova": "mda", "montenegro": "mne", "macedonia": "mkd",
    "israel": "isr", "andorra": "and", "liechtenstein": "lie",
    "gibraltar": "gib", "northern": "nir",
    # CONMEBOL
    "brazil": "bra", "argentina": "arg", "uruguay": "uru", "colombia": "col",
    "chile": "chi", "peru": "per", "ecuador": "ecu", "paraguay": "par",
    "bolivia": "bol", "venezuela": "ven",
    # CONCACAF
    "usa": "usa", "mexico": "mex", "canada": "can", "honduras": "hon",
    "panama": "pan", "jamaica": "jam",
    # AFC
    "japan": "jpn", "australia": "aus", "iran": "irn", "qatar": "qat",
    "iraq": "irq", "china": "chn", "india": "ind", "indonesia": "idn",
    "thailand": "tha", "vietnam": "vie", "uzbekistan": "uzb",
    # CAF
    "nigeria": "nga", "egypt": "egy", "morocco": "mar", "senegal": "sen",
    "ghana": "gha", "cameroon": "cmr", "algeria": "alg", "tunisia": "tun",
    "mali": "mli", "kenya": "ken",
    # multi-word distinctive tokens
    "saudi": "ksa", "ivory": "civ", "costa": "crc", "marino": "smr",
}


def _alias_codes(toks: set[str]) -> set[str]:
    out: set[str] = set()
    for t in toks:
        codes = _TEAM_ALIASES.get(t)
        if codes:
            out |= codes
    return out


def _nation_codes(toks: set[str]) -> set[str]:
    out: set[str] = set()
    for t in toks:
        code = _SOCCER_NATIONS.get(t)
        if code:
            out.add(code)
    return out


@dataclass
class ResolveResult:
    ticker: str | None
    reason: str
    candidates: list[dict[str, Any]] = field(default_factory=list)
    # Structured disposition so callers can branch without parsing the
    # human-readable ``reason`` string (which is free to change wording):
    #   "matched"     — a ticker was resolved (direct/cache/live/llm)
    #   "no_series"   — this vertical/pick_type has no Kalshi series mapped
    #                   (genuinely unsupported market type)
    #   "no_match"    — series exist but no candidate scored high enough
    kind: str = "matched"
    # Recommended Kalshi side ("YES"/"NO") when the *resolver* — not the
    # scraper — dictates it.  Set for spreads, where the handicap sign and the
    # target market are coupled (a "+1.5" underdog cover is NO on the
    # opponent's "wins by over 1.5" market, never YES on the pick team's).
    # ``None`` means "no opinion — honour the scraper's side".
    side: str | None = None


def _tokens(s: str | None) -> set[str]:
    if not s:
        return set()
    return {
        w.lower()
        for w in re.findall(r"[A-Za-z][A-Za-z0-9]{1,}", s)
        if w.lower() not in _STOPWORDS
    }


def _split_matchup(matchup: str) -> tuple[set[str], set[str]]:
    """Split an ``Away @ Home`` / ``Away vs Home`` string into two token sets."""
    parts = re.split(r"\s+(?:@|vs\.?|at)\s+", matchup or "", maxsplit=1, flags=re.IGNORECASE)
    if len(parts) != 2:
        all_t = _tokens(matchup)
        return all_t, all_t
    return _tokens(parts[0]), _tokens(parts[1])


def _ordered_side_tokens(s: str) -> list[str]:
    return [
        w.lower()
        for w in re.findall(r"[A-Za-z][A-Za-z0-9]{1,}", s or "")
        if w.lower() not in _STOPWORDS
    ]


def _matchup_sides(matchup: str) -> tuple[list[str], list[str]]:
    """Ordered token lists for the away/home sides (order preserved so we
    can build acronyms like ``Paris Saint-Germain`` → ``PSG``)."""
    parts = re.split(
        r"\s+(?:@|vs\.?|at)\s+", matchup or "", maxsplit=1, flags=re.IGNORECASE,
    )
    if len(parts) != 2:
        toks = _ordered_side_tokens(matchup)
        return toks, toks
    return _ordered_side_tokens(parts[0]), _ordered_side_tokens(parts[1])


def _side_evidence(tokens: list[str]) -> tuple[set[str], set[str]]:
    """Return ``(name_tokens, codes)`` used to detect one matchup side in
    a market.  ``name_tokens`` are full words matched against the market's
    title/ticker tokens; ``codes`` are short forms matched as substrings
    inside the *ticker* (where Kalshi concatenates team codes, e.g.
    ``PSGARS``).  Codes cover three abbreviation styles so we tolerate
    Kalshi shorthand without a hand-maintained club map:

      * curated aliases (``cleveland`` → ``cle``),
      * the first three letters of each word (``arsenal`` → ``ars``),
      * the ordered acronym of a multi-word side (``psg``).
    """
    tset = set(tokens)
    # Don't expand aliases for shared city/place words — "york" aliases to
    # every NY team (nyy/nym/nyr/nyi), which would let a Mets market satisfy
    # a Yankees side.  The ordered acronym (NYY) and the nickname's own alias
    # stay, and remain side-distinct (NYY != NYM).
    codes: set[str] = set(_alias_codes(tset - _GENERIC_PLACE_TOKENS))
    # FIFA national-team codes (Romania->ROU, Netherlands->NED) that the
    # first-three-letters heuristic below would get wrong.
    codes |= _nation_codes(tset)
    for t in tokens:
        if len(t) >= 3:
            codes.add(t[:3])
    if len(tokens) >= 2:
        codes.add("".join(t[0] for t in tokens))
    return tset, codes


def _side_present(
    cand_tokens: set[str],
    team_code_seg: str,
    name_tokens: set[str],
    codes: set[str],
) -> bool:
    """True if a matchup side appears in the candidate: a full-word token in
    the title/ticker, or a short code inside the ticker's *team-code
    segment* (``PSGARS``).  Codes are matched only against that segment —
    not the whole ticker — so a trigram can't collide with the series
    prefix or outcome suffix."""
    if not name_tokens and not codes:
        return True  # nothing to assert against — don't block
    # A bare city/place word ("New York") is shared by co-located rivals and
    # can't prove a side.  Match by name only when EVERY distinctive token is
    # present — a single shared token ("Manchester" in both Manchester City
    # and Manchester United) must not prove the side.  Abbreviated names that
    # drop tokens (e.g. "PSG") are rescued by the ticker team-code segment
    # below.
    distinctive = name_tokens - _GENERIC_PLACE_TOKENS
    if distinctive and distinctive <= cand_tokens:
        return True
    for c in codes:
        if len(c) >= 2 and c.upper() in team_code_seg:
            return True
    return False


_MONTHS = {
    m: i for i, m in enumerate(
        ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
         "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"],
        start=1,
    )
}
_TICKER_DATE_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})")
_TICKER_TEAMSEG_RE = re.compile(r"^\d{2}[A-Z]{3}\d{2}\d*([A-Z].*)$")


def _ticker_team_code(ticker: str) -> str:
    """Uppercased team-code segment of a Kalshi ticker — the chunk after the
    embedded date/time and before the outcome suffix
    (``…-26MAY30PSGARS-2`` → ``PSGARS``).  Falls back to the whole ticker
    when no date-prefixed segment is found."""
    for p in (ticker or "").split("-"):
        m = _TICKER_TEAMSEG_RE.match(p)
        if m:
            return m.group(1).upper()
    return (ticker or "").upper()


def _ticker_date(ticker: str) -> date | None:
    """Parse the ``YYMONDD`` date Kalshi embeds after the series prefix
    (``KXMLBSPREAD-26MAY30PSGARS-…`` → 2026-05-30).  ``None`` if absent."""
    m = _TICKER_DATE_RE.search(ticker or "")
    if not m:
        return None
    month = _MONTHS.get(m.group(2))
    if not month:
        return None
    try:
        return date(2000 + int(m.group(1)), month, int(m.group(3)))
    except ValueError:
        return None


def _coerce_date(v: Any) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str) and v.strip():
        try:
            return date.fromisoformat(v.strip()[:10])
        except ValueError:
            return None
    return None


def _picked_team_tokens(
    selection: str, away_t: set[str], home_t: set[str]
) -> tuple[set[str], set[str]]:
    """Return ``(picked_team_tokens, other_team_tokens)``.

    ``selection`` is something like ``"Athletics"``, ``"Athletics +1.5"``,
    or ``"UNDER 10"``.  For totals we can't infer a team from the
    selection, so we return both sets as the "picked" side — the title
    must still mention one of the matchup's teams to score.
    """
    sel_t = {
        t for t in _tokens(selection)
        if not _NUM_PREFIX.match(t)
    }
    if not sel_t:
        return (away_t | home_t), set()
    if sel_t & away_t:
        return away_t, home_t
    if sel_t & home_t:
        return home_t, away_t
    # Couldn't disambiguate — use selection tokens as the must-have set.
    return sel_t, (away_t | home_t) - sel_t


def _extract_line(selection: str) -> float | None:
    m = re.search(r"[-+]?(\d+(?:\.\d+)?)", selection or "")
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _spread_sign(selection: str) -> int | None:
    """Return the handicap sign of a spread selection.

    ``+1`` for a "+1.5" underdog cover (e.g. "Giants +1.5"), ``-1`` for a
    "-1.5" favourite (e.g. "Giants -1.5"), and ``None`` when no explicit
    sign precedes the line number.

    Kalshi lists spreads only as "<team> wins by over X.5 runs" markets, so:
      * "-X.5" (favourite) = YES on the *pick team's* over-X.5 market;
      * "+X.5" (underdog)  = NO  on the *opponent's*  over-X.5 market
        (the pick covers unless the opponent wins by over X.5).
    The sign therefore picks BOTH which team's market to target and which
    side to buy — getting it wrong bets the exact opposite of the pick.
    """
    m = re.search(r"([-+])\s*\d+(?:\.\d+)?", selection or "")
    if not m:
        return None
    return 1 if m.group(1) == "+" else -1


def _score(
    cand: dict[str, str],
    *,
    picked: set[str],
    other: set[str],
    picked_codes: set[str],
    other_codes: set[str],
    pick_type: str,
    line: float | None,
) -> int:
    title = (cand.get("title") or "")
    ticker = (cand.get("ticker") or "")
    title_t = _tokens(title)
    ticker_t = _tokens(ticker)
    all_t = title_t | ticker_t

    pick_hits = len(picked & all_t)
    # Substring match for short codes inside ticker (e.g. "ATH" inside
    # "KXMLBSPREAD-26MAY262140SEAATH-ATH2") and title fragments like "A's".
    ticker_up = ticker.upper()
    title_up = title.upper()
    code_hits = sum(
        1 for c in picked_codes
        if c.upper() in ticker_up or c.upper() in title_up
    )
    if pick_hits == 0 and code_hits == 0 and (picked or picked_codes):
        return 0  # disqualify

    # Heavy reward for the *trailing* ticker segment containing the picked
    # code — that's the bucket that uniquely identifies which side / line.
    tail = ticker.rsplit("-", 1)[-1].upper() if "-" in ticker else ""
    tail_pick_hit = any(c.upper() in tail for c in picked_codes)
    tail_other_hit = any(c.upper() in tail for c in other_codes)

    s = 3 * pick_hits + 2 * code_hits
    s += 2 * len(other & all_t)
    s += sum(
        1 for c in other_codes
        if c.upper() in ticker_up or c.upper() in title_up
    )
    if tail_pick_hit and not tail_other_hit:
        s += 4
    elif tail_other_hit and not tail_pick_hit:
        # Wrong side of the spread.
        s -= 4
    if line is not None and pick_type in ("total", "spread"):
        nums = re.findall(r"\d+(?:\.\d+)?", title) + re.findall(r"\d+(?:\.\d+)?", ticker)
        line_str = str(int(line)) if line == int(line) else str(line)
        if line_str in nums:
            s += 4
        # Spreads: tail like "ATH2" → numeric 2 means "wins by over 1.5".
        # Map line 1.5→2, 2.5→3, etc.
        if pick_type == "spread":
            target = int(line + 0.5) if line != int(line) else int(line) + 1
            tail_nums = re.findall(r"\d+", tail)
            if str(target) in tail_nums:
                s += 4
        # Totals: tail like "-10" → integer line bucket.
        if pick_type == "total":
            tail_nums = re.findall(r"\d+", tail)
            rounded = int(round(line))
            if str(rounded) in tail_nums:
                s += 4
    return s


# ---------------------------------------------------------------------------
# Candidate fetchers
# ---------------------------------------------------------------------------

async def _cache_candidates(prefixes: list[str]) -> list[dict[str, str]]:
    if not prefixes:
        return []
    # Build OR-of-LIKE filter to allow asyncpg server-side prefix scan.
    where = " OR ".join(f"ticker LIKE ${i+1}" for i in range(len(prefixes)))
    sql = (
        "SELECT ticker, title FROM market_title_cache "
        f"WHERE {where} "
        "ORDER BY fetched_at DESC NULLS LAST LIMIT 500"
    )
    try:
        rows = await db.fetch(sql, *[f"{p}-%" for p in prefixes])
    except Exception as e:
        log.warning("market_title_cache prefix lookup failed: %s", e)
        return []
    out: list[dict[str, str]] = []
    for r in rows:
        title = (r["title"] if "title" in r.keys() else "") or ""
        out.append({"ticker": str(r["ticker"]), "title": str(title)})
    return out


async def _persist(ticker: str, title: str) -> None:
    try:
        await db.execute(
            """
            INSERT INTO market_title_cache (ticker, title, fetched_at)
            VALUES ($1, $2, now())
            ON CONFLICT (ticker) DO UPDATE
              SET title = EXCLUDED.title, fetched_at = EXCLUDED.fetched_at
            """,
            ticker, title,
        )
    except Exception as e:
        log.warning("market_title_cache write failed for %s: %s", ticker, e)


async def _live_candidates(prefixes: list[str]) -> list[dict[str, str]]:
    """Hit ``GET /markets?series_ticker=…`` for each prefix; cache+return."""
    settings = get_settings()
    if not settings.kalshi_api_key_id or not settings.kalshi_private_key:
        return []
    out: list[dict[str, str]] = []
    try:
        async with KalshiClient(settings=settings, timeout=8.0) as c:
            for series in prefixes:
                try:
                    data = await c.list_markets(
                        series_ticker=series, status="open", limit=200,
                    )
                except KalshiAPIError as e:
                    log.info(
                        "kalshi list_markets(%s) failed: http %s",
                        series, getattr(e, "status", "?"),
                    )
                    continue
                except Exception as e:
                    log.info("kalshi list_markets(%s) error: %s", series, e)
                    continue
                markets = data.get("markets") if isinstance(data, dict) else None
                if not isinstance(markets, list):
                    continue
                for m in markets:
                    if not isinstance(m, dict):
                        continue
                    ticker = m.get("ticker")
                    title = m.get("title") or m.get("subtitle") or ""
                    if ticker and title:
                        row = {"ticker": str(ticker), "title": str(title)}
                        out.append(row)
                        await _persist(row["ticker"], row["title"])
    except Exception as e:
        log.warning("kalshi live candidate fetch failed: %s", e)
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_MIN_SCORE = 5


def _has_team_overlap(
    cand: dict[str, str],
    *,
    picked: set[str],
    other: set[str],
    picked_codes: set[str],
    other_codes: set[str],
) -> bool:
    """True if ``cand`` mentions at least one of the signal's teams.

    Used to vet the LLM fallback: the LLM is good at fuzzy team matching
    but will happily latch a totals/over-under line onto an *unrelated*
    fixture (e.g. matching ``UNDER 2.5`` for ``India @ Zimbabwe`` onto a
    Ligue 1 Nantes–Toulouse total).  We refuse any LLM pick whose market
    title/ticker shares no team token or alias code with the matchup, so
    we never bet the wrong game just because the number lined up.
    """
    title = cand.get("title") or ""
    ticker = cand.get("ticker") or ""
    all_t = _tokens(title) | _tokens(ticker)
    if (picked & all_t) or (other & all_t):
        return True
    hay = (title + " " + ticker).upper()
    for c in picked_codes | other_codes:
        if c and c.upper() in hay:
            return True
    return False


async def resolve_match(
    signal: dict[str, Any],
    *,
    live_fallback: bool = True,
) -> ResolveResult:
    """Resolve a signal and return ticker + reason + scored candidates.

    ``live_fallback=False`` skips the Kalshi ``list_markets`` call on a
    cache miss -- callers that resolve dozens of signals per request
    (e.g. the dashboard widget polled every 30s) MUST pass ``False`` so
    they don't serialise N HTTP round-trips on the event loop.
    """
    pick_type = (signal.get("pick_type") or "moneyline").lower().strip()
    selection = signal.get("selection") or ""
    direct = (signal.get("kalshi_ticker") or "").strip()
    # A scraper-supplied ticker is normally authoritative, but a SIGNED spread
    # ("+X.5" / "-X.5") is the exception: the scraper resolves the handicap to
    # the FAVOURITE's own "wins by over X.5" market, which is precisely the
    # wrong-market / wrong-side bug this resolver exists to correct.  Force
    # signed spreads through the sign-aware resolution below so the target
    # market AND the side are derived here rather than trusting the scraper.
    direct_ok = bool(direct) and not (
        pick_type == "spread" and _spread_sign(selection) is not None
    )
    if direct_ok:
        try:
            hit = await db.fetchval(
                "SELECT 1 FROM market_title_cache WHERE ticker=$1", direct,
            )
        except Exception:
            hit = None
        if hit is not None:
            return ResolveResult(
                direct, "scraper-supplied ticker (cache hit)", [], kind="matched",
            )
        return ResolveResult(direct, "scraper-supplied ticker", [], kind="matched")

    vertical = (signal.get("vertical") or "").lower().strip()
    matchup = signal.get("matchup") or ""
    line = _extract_line(selection)
    sign = _spread_sign(selection)

    # A soccer half-goal handicap is mathematically a MONEYLINE bet — a 0.5
    # line can never push: "<team> -0.5" == that team must win; "<team> +0.5"
    # == that team wins OR draws == the opponent does NOT win.  Kalshi lists no
    # 0.5 spread market (only 1.5/2.5 "wins by over X.5" lines), so a "+0.5"
    # underdog like "Romania +0.5" used to die as skipped_unresolved.  Route
    # these to the 3-way GAME (winner) series and derive the side from the sign
    # below.  Scoped to soccer because that's the vertical with a draw outcome.
    soccer_ml_spread = (
        vertical == "soccer" and pick_type == "spread"
        and line is not None and abs(line) == 0.5 and sign is not None
    )
    effective_pick_type = "moneyline" if soccer_ml_spread else pick_type

    static_prefixes = _SERIES_BY_VERTICAL_PICK.get((vertical, effective_pick_type), [])
    # Union the static list with every series Kalshi tags for this sport +
    # pick_type, so competitions we never hardcoded (international
    # friendlies, Nations League, World Cup, …) still resolve.  Static
    # entries stay first to preserve their preference order.
    discovered = await _discover_series(
        vertical, effective_pick_type, allow_network=live_fallback,
    )
    seen_prefix: set[str] = set()
    prefixes: list[str] = []
    for p in (*static_prefixes, *discovered):
        if p not in seen_prefix:
            seen_prefix.add(p)
            prefixes.append(p)
    if not prefixes:
        return ResolveResult(
            None,
            f"no kalshi series mapped for vertical={vertical!r} pick_type={pick_type!r}",
            [],
            kind="no_series",
        )

    away_words, home_words = _matchup_sides(matchup)
    away_name, away_codes = _side_evidence(away_words)
    home_name, home_codes = _side_evidence(home_words)

    # Pick which matchup side the selection names, then reuse the SAME rich
    # code evidence (curated alias + FIFA nation code + first-three-letters +
    # multi-word acronym) that the both-teams gate uses.  The old path scored
    # with curated-alias codes ONLY, which are empty for soccer / international
    # sides, so a market that genuinely named both teams scored 0 and was
    # discarded.  Aligning the scorer with the gate is what lets soccer resolve.
    sel_words = {t for t in _tokens(selection) if not _NUM_PREFIX.match(t)}
    if sel_words & set(away_words):
        picked, picked_codes = away_name, away_codes
        other, other_codes = home_name, home_codes
    elif sel_words & set(home_words):
        picked, picked_codes = home_name, home_codes
        other, other_codes = away_name, away_codes
    else:
        # Selection doesn't name a side (e.g. a totals OVER/UNDER) — require
        # either team and score against the union so the title must still
        # name the fixture.
        picked = other = away_name | home_name
        picked_codes = other_codes = away_codes | home_codes

    # Spread handicap sign couples the target market with the side.  Kalshi
    # only lists "<team> wins by over X.5 runs" markets, so a "+X.5" underdog
    # cover must be expressed as NO on the OPPONENT's over-X.5 market, while a
    # "-X.5" favourite is YES on the PICK team's over-X.5 market.  For a "+"
    # spread we therefore score toward the opponent's market (swap the
    # picked/other evidence so the scorer rewards the opponent's tail code +
    # line bucket) and recommend the NO side; for "-" we keep the pick team's
    # market and recommend YES.  A soccer 0.5 handicap routed to the winner
    # market follows the same coupling: "+0.5" (win or draw) is NO on the
    # OPPONENT's winner; "-0.5" (must win) is YES on the PICK team's winner.
    # With no explicit sign we leave the side to the scraper and score as before.
    recommended_side: str | None = None
    s_picked, s_other = picked, other
    s_picked_codes, s_other_codes = picked_codes, other_codes
    if soccer_ml_spread or pick_type == "spread":
        if sign == 1:
            s_picked, s_other = other, picked
            s_picked_codes, s_other_codes = other_codes, picked_codes
            recommended_side = "NO"
        elif sign == -1:
            recommended_side = "YES"
    signal_date = _coerce_date(signal.get("signal_date"))

    def _date_ok(cand: dict[str, str]) -> bool:
        # Reject markets dated to a different day than the signal's game.
        # A team plays at most once per day, so picked-team + correct date
        # almost uniquely identifies the market — this is what kills the
        # stale/closed markets that fall into the "no liquidity" trap.
        if signal_date is None:
            return True
        td = _ticker_date(cand.get("ticker") or "")
        if td is None:
            return True
        return abs((td - signal_date).days) <= 1

    def _both_teams(cand: dict[str, str]) -> bool:
        # Require BOTH sides of the matchup to appear, so a market sharing
        # only the picked team's code but with the WRONG opponent (e.g.
        # WSH@CLE for a BOS@CLE signal) can never be matched.
        title = cand.get("title") or ""
        ticker = cand.get("ticker") or ""
        cand_tokens = _tokens(title) | _tokens(ticker)
        seg = _ticker_team_code(ticker)
        return (
            _side_present(cand_tokens, seg, away_name, away_codes)
            and _side_present(cand_tokens, seg, home_name, home_codes)
        )

    def _rank(rows: list[dict[str, str]]) -> tuple[int, dict[str, str] | None]:
        best_s, best = 0, None
        for r in rows:
            if not (_date_ok(r) and _both_teams(r)):
                continue
            sc = _score(
                r,
                picked=s_picked, other=s_other,
                picked_codes=s_picked_codes, other_codes=s_other_codes,
                pick_type=effective_pick_type, line=line,
            )
            if sc > best_s:
                best_s, best = sc, r
        return best_s, best

    cands = await _cache_candidates(prefixes)
    best_score, best = _rank(cands)
    used_live = False

    if best_score < _MIN_SCORE and live_fallback:
        live = await _live_candidates(prefixes)
        if live:
            used_live = True
            seen = {c["ticker"] for c in cands}
            cands = cands + [c for c in live if c["ticker"] not in seen]
            best_score, best = _rank(cands)

    scored = sorted(
        (
            {
                "ticker": c["ticker"],
                "title": c["title"],
                "score": _score(
                    c,
                    picked=s_picked, other=s_other,
                    picked_codes=s_picked_codes, other_codes=s_other_codes,
                    pick_type=effective_pick_type, line=line,
                ),
            }
            for c in cands
        ),
        key=lambda d: -int(d["score"]),
    )
    preview = [s for s in scored if int(s["score"]) > 0][:5]

    if best is None or best_score < _MIN_SCORE:
        # Final tier: ask an LLM to pick from the candidate set. This
        # rescues signals where the team isn't in ``_TEAM_ALIASES`` or
        # the heuristic line-match misses (e.g. ATP/WTA player names,
        # foreign soccer leagues, NCAA exotics). The LLM is constrained
        # to tickers we showed it, so it can't hallucinate.
        llm_ticker: str | None = None
        llm_reason = ""
        # Only let the LLM choose among markets dated to the signal's game
        # — never resurrect a stale/closed fixture on a date mismatch.
        scored_dated = [s for s in scored if _date_ok(s)]
        if live_fallback and scored_dated:
            llm_ticker, llm_reason = await llm_resolver.llm_pick(signal, scored_dated)
        if llm_ticker:
            chosen_row = next(
                (c for c in cands if c["ticker"] == llm_ticker), None
            )
            if chosen_row is not None and _both_teams(chosen_row):
                return ResolveResult(
                    llm_ticker, llm_reason, preview, kind="matched",
                    side=recommended_side,
                )
            # The LLM matched a market that doesn't name BOTH teams — a
            # line-only false positive on an unrelated or wrong-opponent
            # fixture.  Decline rather than place a bet on the wrong game.
            llm_reason = (
                f"llm pick {llm_ticker} rejected: market does not name both "
                f"teams in {matchup!r}"
            )
        n_dated = sum(1 for c in cands if _date_ok(c))
        n_both = sum(1 for c in cands if _date_ok(c) and _both_teams(c))
        reason = (
            f"no kalshi market match — series={prefixes}, "
            f"candidates={len(cands)}, date_ok={n_dated}, both_teams={n_both}, "
            f"live_queried={used_live}, top_score={best_score}"
        )
        if llm_reason:
            reason += f"; {llm_reason}"
        return ResolveResult(None, reason, preview, kind="no_match")
    return ResolveResult(
        best["ticker"],
        f"matched via {'live' if used_live else 'cache'} (score={best_score}, series={prefixes[0]})",
        preview,
        kind="matched",
        side=recommended_side,
    )


async def resolve(signal: dict[str, Any]) -> str | None:
    """Back-compat wrapper used by ``executor._fire_one``."""
    return (await resolve_match(signal)).ticker
