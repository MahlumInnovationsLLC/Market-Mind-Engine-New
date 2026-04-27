"""Synthesize game schedules from Kalshi sports markets.

The Sportradar trial expires and the existing schedule pipeline stops
refreshing the cached ``sportradar_schedules`` table.  Without a
backstop, the chatbot's "Today's Games" lookahead empties out as the
cached window rolls past.

This module is the second half of the combo backstop (the first being
:mod:`app.feeds.espn_scoreboard`): for every league we trade, Kalshi
*already* knows what games are upcoming because their game-outcome
markets list the matchup right in the title (e.g. "Will the Eagles
beat the Bills on Nov 2?") and ship a ``close_time`` that closely
tracks kickoff.  We mine that signal back into a schedule row.

Each row writes into the same ``sportradar_schedules`` table the
Sportradar pipeline writes to, with ``game_id = kalshi:<event_ticker>``
so it can never collide with a native Sportradar id and so the
provenance is self-evident.  No downstream code (``get_upcoming_games``,
``get_upcoming_game_context``, ``/api/upcoming-games``, the
team-name-AND fuzzy matcher) needs to change.

Skipped feed (``enabled=False``) when Kalshi credentials are missing,
matching how the other API-keyed feeds advertise themselves.
"""
from __future__ import annotations

import json as _json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from .. import db
from ..config import get_settings
from ..kalshi import client as kalshi_client_mod
from .registry import Feed, registry


log = logging.getLogger("axiom.feeds.kalshi_schedules")


# ---------------------------------------------------------------------------
# Provider config
# ---------------------------------------------------------------------------

# Curated mapping of Kalshi sports series_tickers → canonical league
# label we write into ``sportradar_schedules.league``.  Operators can
# extend this list without a code change via the
# ``AXIOM_KALSHI_SCHEDULE_SERIES`` env var:
#
#     AXIOM_KALSHI_SCHEDULE_SERIES="KXNFLGAME=NFL,KXNBAGAME=NBA"
#
# Any series Kalshi doesn't actually offer is silently ignored at poll
# time (their ``list_markets`` returns an empty page) so an
# over-inclusive default is harmless.
_DEFAULT_SERIES_TO_LEAGUE: dict[str, str] = {
    "KXNFLGAME":      "NFL",
    "KXNBAGAME":      "NBA",
    "KXMLBGAME":      "MLB",
    "KXNHLGAME":      "NHL",
    "KXMLSGAME":      "MLS",
    "KXEPLGAME":      "EPL",
    "KXLALIGAGAME":   "LALIGA",
    "KXSERIEAGAME":   "SERIE_A",
    "KXBUNDESGAME":   "BUNDESLIGA",
    "KXLIGUE1GAME":   "LIGUE_1",
    "KXUCLGAME":      "UCL",
    "KXNCAAFGAME":    "NCAAF",
    "KXNCAABGAME":    "NCAAB",
}

_PAGE_LIMIT: int = 200
_MAX_PAGES: int = 5
_DEFAULT_CADENCE_SECS: float = 300.0  # 5 minutes; matches the Sportradar schedule cadence.


def _series_to_league() -> dict[str, str]:
    """Resolve the active series→league map (env override + defaults)."""
    raw = (os.environ.get("AXIOM_KALSHI_SCHEDULE_SERIES") or "").strip()
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
    raw = os.environ.get("AXIOM_KALSHI_SCHEDULES_CADENCE_SECS")
    try:
        v = float(raw) if raw is not None else _DEFAULT_CADENCE_SECS
    except (TypeError, ValueError):
        return _DEFAULT_CADENCE_SECS
    return max(60.0, v)


# ---------------------------------------------------------------------------
# Title → (home, away) parsing
# ---------------------------------------------------------------------------
#
# Kalshi titles for game-outcome markets follow a small handful of
# templates ("Will the Eagles beat the Bills?", "Eagles vs Bills",
# "Eagles @ Bills - Nov 2").  We keep parsing intentionally simple:
# extract a left-of vs right-of-separator pair, then strip the common
# noise prefixes ("Will the ", "Will ").  Anything that doesn't match
# is dropped so a weird novelty market title can't poison a schedule
# row.
#
# Important: we *do not* assume which side is home and which is away
# from the title alone.  Kalshi's market metadata sometimes carries
# that hint as ``yes_sub_title``/``no_sub_title``; otherwise we assume
# home = right side which is the dominant English convention
# ("Bears @ Packers" → away=Bears, home=Packers).  Downstream the
# chatbot's team-name-AND matcher only requires *both* names appear in
# the matchup title; whether home/away is swapped doesn't break it.

_NOISE_PREFIXES = (
    "will the ",
    "will ",
    "who wins: ",
    "winner: ",
)
_VS_PATTERNS = (
    re.compile(r"\s+@\s+"),
    re.compile(r"\s+vs\.?\s+", re.IGNORECASE),
    re.compile(r"\s+v\s+", re.IGNORECASE),
    # "Eagles at Bills" — common ESPN-style notation. Only matched as a
    # standalone separator (whitespace on both sides), so team names
    # that *contain* "at" (Atlanta, Atletico, Florida State) are
    # unaffected.
    re.compile(r"\s+at\s+", re.IGNORECASE),
)
_BEATS_PATTERN = re.compile(
    r"^(?P<a>.+?)\s+(?:beat|beats|defeat|defeats)\s+(?P<b>.+?)\??$",
    re.IGNORECASE,
)


def _strip_noise(title: str) -> str:
    s = title.strip()
    low = s.lower()
    for p in _NOISE_PREFIXES:
        if low.startswith(p):
            return s[len(p):].strip()
    return s


def _strip_trailing_date(side: str) -> str:
    """Drop trailing " - Nov 2" / " on Nov 2" / "?" tails commonly
    appended to titles, plus a leading article ("the ") that the
    "X beat the Y" template leaves behind on the second side."""
    s = side.strip().rstrip("?").strip()
    s = re.sub(r"\s*[-–—]\s*\w+\s+\d{1,2}(?:,?\s+\d{4})?$", "", s).strip()
    s = re.sub(r"\s+on\s+\w+\s+\d{1,2}(?:,?\s+\d{4})?$", "", s, flags=re.IGNORECASE).strip()
    if s.lower().startswith("the "):
        s = s[4:].strip()
    return s


def _parse_teams_from_title(title: str | None) -> tuple[str, str] | None:
    """Best-effort (away, home) extraction. Returns ``None`` when the
    title doesn't look like a head-to-head matchup."""
    if not title or not isinstance(title, str):
        return None
    cleaned = _strip_noise(title)
    # "X beats Y" → assume X is the YES side, which is conventionally
    # the home team in Kalshi sports markets, but we record it in the
    # *away* slot to match the dominant "@/vs" parser convention
    # (away on the left).  As noted above, downstream matching is
    # symmetric so this is safe.
    m = _BEATS_PATTERN.match(cleaned)
    if m:
        a = _strip_trailing_date(m.group("a"))
        b = _strip_trailing_date(m.group("b"))
        if a and b and a.lower() != b.lower():
            return a, b
    for pat in _VS_PATTERNS:
        parts = pat.split(cleaned, maxsplit=1)
        if len(parts) == 2:
            a = _strip_trailing_date(parts[0])
            b = _strip_trailing_date(parts[1])
            if a and b and a.lower() != b.lower():
                return a, b
    return None


# ---------------------------------------------------------------------------
# Kalshi market shape helpers
# ---------------------------------------------------------------------------

def _parse_close_time(raw: Any) -> datetime | None:
    """Identical to the helper in :mod:`app.feeds.kalshi_crypto_markets`
    but duplicated here so neither feed has to import the other."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if not isinstance(raw, str) or not raw.strip():
        return None
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _event_key(market: dict[str, Any]) -> str | None:
    """Resolve the parent event ticker for a market; fall back to the
    market ticker so a Kalshi event with a single market still gets a
    schedule row written."""
    for k in ("event_ticker", "event", "parent_event_ticker"):
        v = market.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    t = market.get("ticker")
    if isinstance(t, str) and t.strip():
        return t.strip()
    return None


def _venue_from_market(market: dict[str, Any]) -> str | None:
    for k in ("venue", "location"):
        v = market.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


# ---------------------------------------------------------------------------
# Synthesis: collapse all markets in an event into one schedule row
# ---------------------------------------------------------------------------

def _collapse_event(
    league: str, series: str, markets: list[dict[str, Any]],
) -> tuple | None:
    """Pick the best (away, home, scheduled) signal across every market
    in an event and produce a single ``sportradar_schedules`` row.

    Returns ``None`` if no market in the bucket carries a parseable
    matchup title — better to skip the event than emit a row with
    "Unknown vs Unknown".
    """
    if not markets:
        return None
    event_key = _event_key(markets[0]) or ""
    if not event_key:
        return None

    # Earliest close_time across the event's markets is the best
    # proxy for kickoff (binary game markets close at game start;
    # outcome markets close shortly after final).
    earliest: datetime | None = None
    title_pick: tuple[str, str] | None = None
    title_used: str | None = None
    venue: str | None = None
    statuses: set[str] = set()
    for m in markets:
        ct = _parse_close_time(m.get("close_time"))
        if ct is not None and (earliest is None or ct < earliest):
            earliest = ct
        if title_pick is None:
            parsed = _parse_teams_from_title(m.get("title"))
            if parsed is not None:
                title_pick = parsed
                title_used = m.get("title")
        if venue is None:
            venue = _venue_from_market(m)
        s = (m.get("status") or "").strip().lower()
        if s:
            statuses.add(s)

    if title_pick is None:
        return None

    away, home = title_pick
    # Coarse status: if any market in the event is still open, the
    # game is "scheduled"; otherwise it's "closed" (our downstream
    # only uses this for display).
    status: str
    if "open" in statuses:
        status = "scheduled"
    elif statuses & {"settled", "finalized", "determined", "resolved"}:
        status = "closed"
    elif "closed" in statuses:
        status = "closed"
    else:
        status = "scheduled"

    payload = {
        "source": "kalshi",
        "kalshi_event_ticker": event_key,
        "kalshi_series": series,
        "kalshi_market_count": len(markets),
        "matched_title": title_used,
    }
    return (
        league,
        f"kalshi:{event_key}",
        home,
        away,
        earliest,
        status,
        venue,
        _json.dumps(payload, default=str),
    )


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

async def _upsert_rows(rows: Iterable[tuple]) -> int:
    rows = [r for r in rows if r is not None]
    if not rows:
        return 0
    # Let DB failures propagate: the scheduler catches and records them
    # in FeedStatus so a broken writer can't impersonate a healthy feed.
    async with db.acquire() as conn, conn.transaction():
        await conn.executemany(
            """
            INSERT INTO sportradar_schedules
              (league, game_id, home_team, away_team, scheduled,
               status, venue, payload, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, NOW())
            ON CONFLICT (league, game_id) DO UPDATE SET
              home_team  = EXCLUDED.home_team,
              away_team  = EXCLUDED.away_team,
              scheduled  = EXCLUDED.scheduled,
              status     = EXCLUDED.status,
              venue      = EXCLUDED.venue,
              payload    = EXCLUDED.payload,
              updated_at = NOW()
            """,
            rows,
        )
    return len(rows)


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------

async def _list_series_markets(
    client: kalshi_client_mod.KalshiClient, series: str,
) -> list[dict[str, Any]]:
    """Walk every page of open markets under ``series``.  Mirrors the
    crypto-markets walker and short-circuits on Kalshi errors so a
    single bad series can't sink the whole poll."""
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


def _bucket_by_event(
    markets: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for m in markets:
        key = _event_key(m)
        if not key:
            continue
        out.setdefault(key, []).append(m)
    return out


async def poll_kalshi_schedules() -> int:
    """One poll: walk every configured Kalshi sports series, group by
    event, synthesize a schedule row per event, upsert.

    Returns the total number of schedule rows touched.  Soft-fails
    (returns 0) when Kalshi credentials aren't configured — the
    registration code already advertises the feed as skipped in that
    case so the operator console surfaces the reason.
    """
    settings = get_settings()
    if not (settings.kalshi_api_key_id and settings.kalshi_private_key):
        return 0

    series_map = _series_to_league()
    rows_to_upsert: list[tuple] = []
    series_seen: list[str] = []
    series_with_rows: list[str] = []
    try:
        async with kalshi_client_mod.KalshiClient(settings=settings) as client:
            for series, league in series_map.items():
                series_seen.append(series)
                markets = await _list_series_markets(client, series)
                if not markets:
                    continue
                buckets = _bucket_by_event(markets)
                produced = 0
                for _evt, evt_markets in buckets.items():
                    row = _collapse_event(league, series, evt_markets)
                    if row is not None:
                        rows_to_upsert.append(row)
                        produced += 1
                if produced:
                    series_with_rows.append(series)
    except Exception as e:  # noqa: BLE001
        log.warning("kalshi_schedules poll crashed: %s", e)
        return 0

    written = await _upsert_rows(rows_to_upsert)
    registry.set_metadata(
        "kalshi_schedules",
        series_polled=series_seen,
        series_with_rows=series_with_rows,
        last_total_rows=written,
    )
    return written


def register_kalshi_schedules_feed() -> None:
    """Register the Kalshi schedule synthesizer with the global feed
    registry.  Idempotent on hot reload; advertises a clear
    ``skipped_reason`` when Kalshi credentials are missing."""
    name = "kalshi_schedules"
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
        poll=poll_kalshi_schedules,
        enabled=has_creds,
        skipped_reason=skipped,
    ))
    log.info(
        "kalshi schedules feed registered (enabled=%s, cadence=%.1fs)",
        has_creds, _cadence_seconds(),
    )
