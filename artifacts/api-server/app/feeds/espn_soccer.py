"""ESPN public soccer-scoreboard fallback for settlement.

Sportradar is the primary settlement source for soccer (3-way h/d/a)
markets.  This module provides an independent secondary read of the
same match outcome from ESPN's free, key-less public scoreboard API,
so a Sportradar incident — rate-limit storm, schedule mismatch,
regional outage — can no longer translate directly into mis-settled
tickets.

The settlement pipeline calls :func:`fetch_match_summary` with the
home/away team names and scheduled kickoff already pulled from
``sportradar_schedules`` and asks ESPN for the same shape Sportradar
returns: ``{"result", "home_team", "away_team", "home_score",
"away_score", "status", "source"}``.  Match identity is recovered by
fuzzy team-name matching on the ESPN scoreboard for the relevant
date — soccer leagues run a handful of fixtures per day so this is
both cheap and unambiguous.

The module sits behind :func:`is_enabled` (env flag
``AXIOM_ESPN_SOCCER_DISABLED=1`` opts out) so an operator can park
the fallback if ESPN itself starts misbehaving.  It never raises —
every error path returns ``None`` and logs at INFO so the parent
agreement check can decide to enqueue for review rather than crash.
"""
from __future__ import annotations

import logging
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx


log = logging.getLogger("axiom.feeds.espn_soccer")


# ---------------------------------------------------------------------------
# League routing.  Soccer-only — matches the LEAGUE_CONFIGS entries with
# `three_way_market=True`.  Each value is the ESPN URL slug for the
# league (e.g. ``eng.1`` for the EPL).
# ---------------------------------------------------------------------------

_ESPN_SOCCER_LEAGUES: dict[str, str] = {
    "EPL":     "eng.1",
    "LALIGA":  "esp.1",
    "SERIEA":  "ita.1",
    "BUNDES":  "ger.1",
    "LIGUE1":  "fra.1",
    "MLS":     "usa.1",
    "UCL":     "uefa.champions",
}

_BASE_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer"
_HTTP_TIMEOUT_SECS: float = 8.0


# ESPN finished-game state markers; see espn_scoreboard._competition_status
# for the broader enum (we only care about ``post`` / ``final`` here).
_FINAL_STATES: frozenset[str] = frozenset(
    {"post", "final", "ft", "aet", "pen"}
)


def is_enabled() -> bool:
    """Operator opt-out for the ESPN soccer fallback."""
    raw = (os.environ.get("AXIOM_ESPN_SOCCER_DISABLED") or "").strip().lower()
    return raw not in {"1", "true", "yes", "on", "y"}


def supported_leagues() -> set[str]:
    """League codes for which an ESPN fallback URL is wired up."""
    return set(_ESPN_SOCCER_LEAGUES)


def _scoreboard_url(league: str, when: datetime | None) -> str | None:
    slug = _ESPN_SOCCER_LEAGUES.get(league.upper()) if league else None
    if slug is None:
        return None
    base = f"{_BASE_URL}/{slug}/scoreboard"
    if when is None:
        return base
    # ESPN accepts a ``dates=YYYYMMDD`` query param.  We pass the date
    # in the league-local sense by using the scheduled kickoff's UTC
    # date — close enough for the fixture window since the scoreboard
    # also returns games scheduled within ~36h either side.
    d = when.astimezone(timezone.utc).strftime("%Y%m%d")
    return f"{base}?dates={d}"


# ---------------------------------------------------------------------------
# Team name normalisation.  Sportradar and ESPN disagree on punctuation,
# diacritics, and short-name forms ("Man City" vs "Manchester City").
# Strip everything to a comparable token bag so the match function can
# tolerate the noise.
# ---------------------------------------------------------------------------

_STOPWORDS: frozenset[str] = frozenset({
    "fc", "cf", "afc", "sc", "ac", "club", "futbol", "football",
    "the", "de", "la", "le", "el", "calcio", "fk", "fbk",
})


def _normalize_name(name: str | None) -> set[str]:
    if not name:
        return set()
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(c for c in nfkd if not unicodedata.combining(c))
    cleaned = re.sub(r"[^a-zA-Z0-9 ]+", " ", ascii_only).lower()
    tokens = {t for t in cleaned.split() if t and len(t) > 1}
    distinctive = tokens - _STOPWORDS
    return distinctive or tokens  # if everything is a stopword keep them all


def _name_score(a: str | None, b: str | None) -> float:
    """Symmetric Jaccard score over normalised token sets."""
    sa = _normalize_name(a)
    sb = _normalize_name(b)
    if not sa or not sb:
        return 0.0
    inter = sa & sb
    union = sa | sb
    return len(inter) / len(union)


_MIN_NAME_SCORE: float = 0.34  # jaccard ≥ ⅓ — same dominant token survives.


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def _parse_iso8601(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _competitor_name(c: dict[str, Any]) -> str | None:
    team = c.get("team") if isinstance(c, dict) else None
    if not isinstance(team, dict):
        return None
    for k in ("displayName", "shortDisplayName", "name", "location"):
        v = team.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _competitor_score(c: dict[str, Any]) -> int | None:
    if not isinstance(c, dict):
        return None
    raw = c.get("score")
    try:
        if raw is None or raw == "":
            return None
        return int(raw)
    except (TypeError, ValueError):
        return None


def _status_state(comp: dict[str, Any]) -> str:
    s = comp.get("status") if isinstance(comp, dict) else None
    if isinstance(s, dict):
        t = s.get("type")
        if isinstance(t, dict):
            v = t.get("state") or t.get("name") or t.get("description")
            if isinstance(v, str) and v.strip():
                return v.strip().lower()
    return ""


def _parse_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """Convert one ESPN event into a settlement summary dict (or None)."""
    if not isinstance(event, dict):
        return None
    comps = event.get("competitions") or []
    comp = comps[0] if isinstance(comps, list) and comps else {}
    competitors = comp.get("competitors") if isinstance(comp, dict) else None
    if not isinstance(competitors, list):
        return None
    home_name: str | None = None
    away_name: str | None = None
    home_score: int | None = None
    away_score: int | None = None
    for c in competitors:
        if not isinstance(c, dict):
            continue
        side = (c.get("homeAway") or "").strip().lower()
        if side == "home":
            home_name = _competitor_name(c)
            home_score = _competitor_score(c)
        elif side == "away":
            away_name = _competitor_name(c)
            away_score = _competitor_score(c)
    if not home_name or not away_name:
        return None
    state = _status_state(comp)
    is_final = state in _FINAL_STATES
    if not is_final or home_score is None or away_score is None:
        return {
            "home_team": home_name,
            "away_team": away_name,
            "home_score": home_score,
            "away_score": away_score,
            "status": state or "scheduled",
            "is_final": False,
            "scheduled_at": _parse_iso8601(event.get("date") or comp.get("date")),
        }
    if home_score > away_score:
        result = "home"
    elif home_score < away_score:
        result = "away"
    else:
        result = "draw"
    return {
        "home_team": home_name,
        "away_team": away_name,
        "home_score": int(home_score),
        "away_score": int(away_score),
        "status": state,
        "is_final": True,
        "result": result,
        "scheduled_at": _parse_iso8601(event.get("date") or comp.get("date")),
    }


# ---------------------------------------------------------------------------
# HTTP fetch — small surface so tests can patch it directly.
# ---------------------------------------------------------------------------

async def _fetch_scoreboard(
    league: str, when: datetime | None
) -> dict[str, Any] | None:
    url = _scoreboard_url(league, when)
    if url is None:
        return None
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(
                _HTTP_TIMEOUT_SECS, connect=3.0, pool=5.0,
            ),
            headers={
                "User-Agent": "axiom-predict/1.0 (+settlement-fallback)",
                "Accept": "application/json",
            },
        ) as client:
            resp = await client.get(url)
        if resp.status_code != 200:
            log.info(
                "espn_soccer scoreboard %s -> HTTP %s", url, resp.status_code
            )
            return None
        body = resp.json()
    except Exception as e:
        log.info("espn_soccer fetch failed for %s: %s", league, e)
        return None
    return body if isinstance(body, dict) else None


def _best_match(
    events: list[dict[str, Any]],
    *,
    home_team: str,
    away_team: str,
    scheduled_at: datetime | None,
) -> dict[str, Any] | None:
    """Pick the best fuzzy match from a parsed scoreboard.

    Score = (home_jaccard + away_jaccard) / 2; require both halves to
    clear ``_MIN_NAME_SCORE`` so a single shared token (e.g. "Real")
    can't pair "Real Madrid" with "Real Sociedad".  When multiple
    fixtures clear the bar (rare, but Manchester derbies do exist),
    prefer the one closest to the requested kickoff.
    """
    candidates: list[tuple[float, float, dict[str, Any]]] = []
    for ev in events:
        parsed = _parse_event(ev)
        if parsed is None:
            continue
        h_score = _name_score(home_team, parsed.get("home_team"))
        a_score = _name_score(away_team, parsed.get("away_team"))
        if h_score < _MIN_NAME_SCORE or a_score < _MIN_NAME_SCORE:
            continue
        avg = (h_score + a_score) / 2
        # Distance to scheduled kickoff in seconds (smaller = better).
        kickoff = parsed.get("scheduled_at")
        if scheduled_at and isinstance(kickoff, datetime):
            delta = abs((kickoff - scheduled_at).total_seconds())
        else:
            delta = float("inf")
        candidates.append((-avg, delta, parsed))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], x[1]))
    return candidates[0][2]


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

async def fetch_match_summary(
    *,
    league: str,
    home_team: str,
    away_team: str,
    scheduled_at: datetime | None = None,
) -> dict[str, Any] | None:
    """Fetch the ESPN summary for a soccer match.

    Returns a dict mirroring the Sportradar shape::

        {
          "result":       "home" | "draw" | "away",
          "home_team":    str,
          "away_team":    str,
          "home_score":   int,
          "away_score":   int,
          "status":       str,
          "source":       "espn",
        }

    when ESPN reports the match as final.  Returns ``None`` when:

      * the league is not in :data:`_ESPN_SOCCER_LEAGUES`,
      * the operator has set ``AXIOM_ESPN_SOCCER_DISABLED=1``,
      * the upstream fetch failed,
      * no event in the requested date matches both team names, or
      * the matched event is not yet final.
    """
    if not is_enabled():
        return None
    if not league or league.upper() not in _ESPN_SOCCER_LEAGUES:
        return None
    if not home_team or not away_team:
        return None
    body = await _fetch_scoreboard(league, scheduled_at)
    # Soccer matches sometimes shift across the day boundary (timezones,
    # extra time, abandoned/restarted).  Also probe ±1 day so the
    # secondary doesn't false-negative on edge-of-day kickoffs.
    pools: list[dict[str, Any]] = []
    if body:
        pools.append(body)
    if scheduled_at is not None:
        for offset in (-1, 1):
            extra_when = scheduled_at + timedelta(days=offset)
            extra = await _fetch_scoreboard(league, extra_when)
            if extra:
                pools.append(extra)
    seen_ids: set[str] = set()
    events: list[dict[str, Any]] = []
    for body_i in pools:
        for ev in (body_i.get("events") or []):
            if not isinstance(ev, dict):
                continue
            eid = str(ev.get("id") or ev.get("uid") or "")
            if eid and eid in seen_ids:
                continue
            seen_ids.add(eid)
            events.append(ev)
    if not events:
        return None
    match = _best_match(
        events,
        home_team=home_team,
        away_team=away_team,
        scheduled_at=scheduled_at,
    )
    if match is None or not match.get("is_final"):
        return None
    return {
        "result": match["result"],
        "home_team": match["home_team"],
        "away_team": match["away_team"],
        "home_score": match["home_score"],
        "away_score": match["away_score"],
        "status": match["status"],
        "source": "espn",
    }
