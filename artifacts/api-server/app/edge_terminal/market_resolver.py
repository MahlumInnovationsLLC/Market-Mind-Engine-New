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
from dataclasses import dataclass, field
from typing import Any

from .. import db
from ..config import get_settings
from ..kalshi.client import KalshiAPIError, KalshiClient


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Vertical + pick_type → Kalshi series prefix(es), in preference order.
# ---------------------------------------------------------------------------
# Kalshi names sports markets by series like ``KXMLBGAME`` (moneyline),
# ``KXMLBSPREAD`` (run line), ``KXMLBTOTAL`` (over/under).  The mapping
# below is what the Edge Terminal scraper produces today; unknown
# combinations fall back to a single GAME prefix (moneyline) on the
# assumption that's the most common Kalshi listing.
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
    ("soccer", "moneyline"): ["KXSOCCERGAME", "KXEPLGAME", "KXBUNDESGAME", "KXLALIGAGAME"],
    ("soccer", "total"):     ["KXSOCCERTOTAL"],
    ("tennis", "moneyline"): ["KXATPMATCH", "KXWTAMATCH"],
}


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


def _alias_codes(toks: set[str]) -> set[str]:
    out: set[str] = set()
    for t in toks:
        codes = _TEAM_ALIASES.get(t)
        if codes:
            out |= codes
    return out


@dataclass
class ResolveResult:
    ticker: str | None
    reason: str
    candidates: list[dict[str, Any]] = field(default_factory=list)


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


async def resolve_match(signal: dict[str, Any]) -> ResolveResult:
    """Resolve a signal and return ticker + reason + scored candidates."""
    direct = (signal.get("kalshi_ticker") or "").strip()
    if direct:
        try:
            hit = await db.fetchval(
                "SELECT 1 FROM market_title_cache WHERE ticker=$1", direct,
            )
        except Exception:
            hit = None
        if hit is not None:
            return ResolveResult(direct, "scraper-supplied ticker (cache hit)", [])
        return ResolveResult(direct, "scraper-supplied ticker", [])

    vertical = (signal.get("vertical") or "").lower().strip()
    pick_type = (signal.get("pick_type") or "moneyline").lower().strip()
    matchup = signal.get("matchup") or ""
    selection = signal.get("selection") or ""

    prefixes = _SERIES_BY_VERTICAL_PICK.get((vertical, pick_type), [])
    if not prefixes:
        return ResolveResult(
            None,
            f"no kalshi series mapped for vertical={vertical!r} pick_type={pick_type!r}",
            [],
        )

    away_t, home_t = _split_matchup(matchup)
    picked, other = _picked_team_tokens(selection, away_t, home_t)
    picked_codes = _alias_codes(picked)
    other_codes = _alias_codes(other)
    line = _extract_line(selection)

    def _rank(rows: list[dict[str, str]]) -> tuple[int, dict[str, str] | None]:
        best_s, best = 0, None
        for r in rows:
            sc = _score(
                r,
                picked=picked, other=other,
                picked_codes=picked_codes, other_codes=other_codes,
                pick_type=pick_type, line=line,
            )
            if sc > best_s:
                best_s, best = sc, r
        return best_s, best

    cands = await _cache_candidates(prefixes)
    best_score, best = _rank(cands)
    used_live = False

    if best_score < _MIN_SCORE:
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
                    picked=picked, other=other,
                    picked_codes=picked_codes, other_codes=other_codes,
                    pick_type=pick_type, line=line,
                ),
            }
            for c in cands
        ),
        key=lambda d: -int(d["score"]),
    )
    preview = [s for s in scored if int(s["score"]) > 0][:5]

    if best is None or best_score < _MIN_SCORE:
        reason = (
            f"no kalshi market match — series={prefixes}, "
            f"candidates={len(cands)}, live_queried={used_live}, "
            f"top_score={best_score}"
        )
        return ResolveResult(None, reason, preview)
    return ResolveResult(
        best["ticker"],
        f"matched via {'live' if used_live else 'cache'} (score={best_score}, series={prefixes[0]})",
        preview,
    )


async def resolve(signal: dict[str, Any]) -> str | None:
    """Back-compat wrapper used by ``executor._fire_one``."""
    return (await resolve_match(signal)).ticker
