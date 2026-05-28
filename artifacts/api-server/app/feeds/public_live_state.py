"""Public-API backstop for ``sportradar_live_state``.

When the operator has no Sportradar contract, every per-sport
``SPORTRADAR_<SPORT>_KEY`` is unset and the live-state table stays
empty — which silences the Live-Games-Now tile, the chatbot's
in-progress lookup, and every per-sport live-prediction loop that
reads ``sportradar_live_state``.

This feed fills that gap with three free public sources:

  * **ESPN** site-API scoreboards for every league we trade (NBA,
    NFL, MLB, NHL, WNBA, plus the seven soccer leagues already
    wired in ``espn_soccer.py``).  No API key.
  * **MLB Stats API** (``statsapi.mlb.com``) for MLB — first-party,
    no key, richer line score (inning, half, balls/strikes, outs).
  * **NHL Web API** (``api-web.nhle.com``) for NHL — first-party,
    no key, authoritative period / clock.

Per-league routing: the official endpoint (MLB / NHL) is preferred
when available; ESPN is used everywhere else and as the failover
when an official endpoint errors.  Game-ids are namespaced
``<source>:<league>:<event-id>`` so they cannot collide with native
Sportradar ids or with the schedule-only ``espn:`` rows the
``espn_scoreboard`` feed writes.

The status string is normalised to the same vocabulary
``app.feeds.sportradar._LIVE_STATUSES`` accepts (``inprogress`` /
``halftime`` / ``delayed`` / ``scheduled.live``) so the existing
``/api/live-games`` and Live-Now tile queries pick the rows up
without any other change.

Disabled via ``AXIOM_PUBLIC_LIVE_STATE_DISABLED=1``.  Cadence via
``AXIOM_PUBLIC_LIVE_STATE_CADENCE_SECS`` (default 45s — live scores
move fast and the upstreams are cached behind CDNs).
"""
from __future__ import annotations

import json as _json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Iterable

import httpx

from .. import db
from .registry import Feed, registry
from .scheduler import RateLimited


log = logging.getLogger("axiom.feeds.public_live_state")


# ---------------------------------------------------------------------------
# Provider config
# ---------------------------------------------------------------------------

# (sport_path, league_path) on https://site.api.espn.com/apis/site/v2/sports
_ESPN_LEAGUES: dict[str, tuple[str, str]] = {
    "NFL":    ("football",   "nfl"),
    "NBA":    ("basketball", "nba"),
    "WNBA":   ("basketball", "wnba"),
    "MLB":    ("baseball",   "mlb"),
    "NHL":    ("hockey",     "nhl"),
    "NCAAB":  ("basketball", "mens-college-basketball"),
    "NCAAF":  ("football",   "college-football"),
    "EPL":    ("soccer",     "eng.1"),
    "LALIGA": ("soccer",     "esp.1"),
    "SERIEA": ("soccer",     "ita.1"),
    "BUNDES": ("soccer",     "ger.1"),
    "LIGUE1": ("soccer",     "fra.1"),
    "MLS":    ("soccer",     "usa.1"),
    "UCL":    ("soccer",     "uefa.champions"),
}

_ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
_MLB_BASE = "https://statsapi.mlb.com/api/v1"
_NHL_BASE = "https://api-web.nhle.com/v1"
_HTTP_TIMEOUT_SECS = 8.0
_DEFAULT_CADENCE_SECS = 45.0

# Must align with app.feeds.sportradar._LIVE_STATUSES so the dashboard
# query (which filters on `lower(status) = ANY(_LIVE_STATUSES)`) picks
# rows up.  ESPN's coarse state enum is {pre, in, post}; the official
# MLB / NHL endpoints expose richer enums we collapse below.
_STATUS_INPROGRESS = "inprogress"
_STATUS_HALFTIME = "halftime"
_STATUS_DELAYED = "delayed"


def _is_enabled() -> bool:
    raw = (os.environ.get("AXIOM_PUBLIC_LIVE_STATE_DISABLED") or "").strip().lower()
    return raw not in {"1", "true", "yes", "on", "y"}


# Map each public-source league back to the Sportradar env var that, when
# set, indicates the operator is paying for the authoritative feed for
# that league.  We skip the public fallback for any such league so the
# dashboard doesn't double-count parallel rows (different game_id
# namespaces ⇒ no PK conflict, but they'd both appear in the Live-Now
# count).  Override the gate by setting
# ``AXIOM_PUBLIC_LIVE_STATE_FORCE_ALL=1`` (useful for testing).
_LEAGUE_TO_SPORTRADAR_KEY: dict[str, str] = {
    "NFL":    "SPORTRADAR_NFL_KEY",
    "NBA":    "SPORTRADAR_NBA_KEY",
    "WNBA":   "SPORTRADAR_WNBA_KEY",
    "MLB":    "SPORTRADAR_MLB_KEY",
    "NHL":    "SPORTRADAR_NHL_KEY",
    "NCAAB":  "SPORTRADAR_NCAAB_KEY",
    "NCAAF":  "SPORTRADAR_NCAAF_KEY",
    "EPL":    "SPORTRADAR_EPL_KEY",
    "LALIGA": "SPORTRADAR_LALIGA_KEY",
    "SERIEA": "SPORTRADAR_SERIEA_KEY",
    "BUNDES": "SPORTRADAR_BUNDES_KEY",
    "LIGUE1": "SPORTRADAR_LIGUE1_KEY",
    "MLS":    "SPORTRADAR_MLS_KEY",
    "UCL":    "SPORTRADAR_UCL_KEY",
}


def _force_all() -> bool:
    raw = (os.environ.get("AXIOM_PUBLIC_LIVE_STATE_FORCE_ALL") or "").strip().lower()
    return raw in {"1", "true", "yes", "on", "y"}


def _league_needs_fallback(league: str) -> bool:
    """True iff we should fill the public-API gap for ``league``.

    Returns False when the operator has a Sportradar key for the league
    (the authoritative feed will own those rows) and the force flag is
    off — preventing double-counted Live-Now entries.
    """
    if _force_all():
        return True
    var = _LEAGUE_TO_SPORTRADAR_KEY.get(league.upper())
    if not var:
        return True
    return not (os.environ.get(var) or "").strip()


def _cadence_seconds() -> float:
    raw = os.environ.get("AXIOM_PUBLIC_LIVE_STATE_CADENCE_SECS")
    try:
        v = float(raw) if raw is not None else _DEFAULT_CADENCE_SECS
    except (TypeError, ValueError):
        return _DEFAULT_CADENCE_SECS
    return max(15.0, v)


def _parse_iso8601(raw: Any) -> datetime | None:
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


def _coerce_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# ESPN parser  (one shape covers all 14 supported leagues)
# ---------------------------------------------------------------------------

_HALFTIME_TOKENS = ("halftime", "half-time", "half time", "end of 1st half")
_DELAYED_TOKENS = ("delay", "postpone", "suspend", "rain")


def _espn_status(comp_status: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """Return (normalised_status, period_str, clock_str) for one ESPN comp.

    ESPN's status block looks like::

        {"clock": 123.4, "displayClock": "5:23",
         "period": 2, "type": {"state": "in", "detail": "Halftime"}}

    We collapse to our live-status vocabulary.  ``state`` is the coarse
    enum (``pre`` / ``in`` / ``post``); a delayed / suspended /
    postponed game sometimes surfaces with ``state == "pre"`` (or even
    ``"post"``) and the actual signal lives in ``detail`` / ``name``,
    so we sniff those alongside the state to avoid silently dropping
    interrupted live games.  Halftime detection also handles ESPN's
    soccer short-form (``"HT"`` / ``"Half-Time"``).
    """
    t = comp_status.get("type") if isinstance(comp_status, dict) else None
    state = None
    detail_raw = ""
    short_raw = ""
    if isinstance(t, dict):
        state = (t.get("state") or "").lower() or None
        detail_raw = t.get("detail") or ""
        short_raw = t.get("shortDetail") or ""
    detail = (detail_raw or "").strip().lower()
    short = (short_raw or "").strip().lower()
    combined = f"{detail} {short}".strip()

    # Delay/postponement may carry any state.  Always honour the text.
    if any(tok in combined for tok in _DELAYED_TOKENS):
        period = comp_status.get("period") if isinstance(comp_status, dict) else None
        period_str = str(period) if period not in (None, "") else None
        return (_STATUS_DELAYED, period_str, None)

    if state != "in":
        return (None, None, None)

    # In-progress.  Soccer half-time uses "HT" as the short detail.
    status = _STATUS_INPROGRESS
    if any(tok in combined for tok in _HALFTIME_TOKENS) or short == "ht":
        status = _STATUS_HALFTIME

    period = comp_status.get("period") if isinstance(comp_status, dict) else None
    period_str = str(period) if period not in (None, "") else None
    clock = None
    if isinstance(comp_status, dict):
        clock = comp_status.get("displayClock") or comp_status.get("clock")
    clock_str = str(clock) if clock not in (None, "") else None
    return (status, period_str, clock_str)


def _espn_rows(league: str, body: dict[str, Any]) -> list[tuple]:
    events = body.get("events") if isinstance(body, dict) else None
    if not isinstance(events, list):
        return []
    out: list[tuple] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        eid = str(ev.get("id") or ev.get("uid") or "").strip()
        if not eid:
            continue
        comps = ev.get("competitions") or []
        comp = comps[0] if isinstance(comps, list) and comps else {}
        if not isinstance(comp, dict):
            continue
        cstatus = comp.get("status") if isinstance(comp, dict) else None
        if not isinstance(cstatus, dict):
            continue
        status, period, clock = _espn_status(cstatus)
        if status is None:
            continue  # not in-progress; skip — schedules feed covers pre-game
        # Score extraction — competitors[].score is a stringified int.
        home_score = away_score = None
        competitors = comp.get("competitors") if isinstance(comp, dict) else []
        if not isinstance(competitors, list):
            competitors = []
        for c in competitors:
            if not isinstance(c, dict):
                continue
            side = (c.get("homeAway") or "").lower()
            s = _coerce_int(c.get("score"))
            if side == "home":
                home_score = s
            elif side == "away":
                away_score = s
        scheduled = _parse_iso8601(ev.get("date") or comp.get("date"))
        payload = {
            "source": "espn",
            "espn_event_id": eid,
            "league": league,
            "name": ev.get("name"),
            "short_name": ev.get("shortName"),
            "status_detail": (
                cstatus.get("type", {}).get("detail")
                if isinstance(cstatus.get("type"), dict) else None
            ),
        }
        out.append((
            league.upper(),
            f"espn:{league.lower()}:{eid}",
            status,
            home_score,
            away_score,
            period,
            clock,
            scheduled,
            _json.dumps(payload, default=str),
        ))
    return out


# ---------------------------------------------------------------------------
# MLB Stats API parser  (preferred for MLB; ESPN is the fallback)
# ---------------------------------------------------------------------------

def _mlb_rows(body: dict[str, Any]) -> list[tuple]:
    """Build live rows from ``/schedule?sportId=1`` response.

    Status codes of interest:
        I = In Progress, W = Warmup (treat as scheduled, skip),
        D = Delayed, F = Final.
    """
    dates = body.get("dates") if isinstance(body, dict) else None
    if not isinstance(dates, list):
        return []
    out: list[tuple] = []
    for d in dates:
        games = (d or {}).get("games") if isinstance(d, dict) else None
        if not isinstance(games, list):
            continue
        for g in games:
            if not isinstance(g, dict):
                continue
            game_pk = g.get("gamePk")
            if not game_pk:
                continue
            status_obj = g.get("status") or {}
            abstract = (status_obj.get("abstractGameState") or "").lower()
            coded = (status_obj.get("codedGameState") or "").upper()
            # Honour the coded delay marker first — MLB sometimes leaves
            # ``abstractGameState`` at ``Live`` during a rain delay, and
            # we want those rows on the dashboard as ``delayed`` rather
            # than ``inprogress``.
            if coded == "D":
                status = _STATUS_DELAYED
            elif abstract == "live":
                status = _STATUS_INPROGRESS
            else:
                continue  # Preview / Warmup / Final — not live
            teams = g.get("teams") or {}
            home_score = _coerce_int(((teams.get("home") or {}).get("score")))
            away_score = _coerce_int(((teams.get("away") or {}).get("score")))
            linescore = g.get("linescore") or {}
            inning = linescore.get("currentInning")
            half = (linescore.get("inningHalf") or "").lower() or None  # 'top'/'bottom'
            period = str(inning) if inning not in (None, "") else None
            clock = None
            if half and period:
                clock = f"{half[:3].upper()} {period}"
            elif half:
                clock = half.upper()
            scheduled = _parse_iso8601(g.get("gameDate"))
            payload = {
                "source": "mlb_statsapi",
                "game_pk": game_pk,
                "status": status_obj,
                "linescore_summary": {
                    "inning": inning,
                    "half": half,
                    "balls": linescore.get("balls"),
                    "strikes": linescore.get("strikes"),
                    "outs": linescore.get("outs"),
                },
            }
            out.append((
                "MLB",
                f"mlb:{game_pk}",
                status,
                home_score,
                away_score,
                period,
                clock,
                scheduled,
                _json.dumps(payload, default=str),
            ))
    return out


# ---------------------------------------------------------------------------
# NHL Web API parser  (preferred for NHL; ESPN is the fallback)
# ---------------------------------------------------------------------------

def _nhl_rows(body: dict[str, Any]) -> list[tuple]:
    """Build live rows from ``/score/now`` response.

    Game states surfaced by the public NHL API: FUT, PRE, LIVE,
    CRIT (final minutes), OFF (final).
    """
    games = body.get("games") if isinstance(body, dict) else None
    if not isinstance(games, list):
        return []
    out: list[tuple] = []
    for g in games:
        if not isinstance(g, dict):
            continue
        gid = g.get("id")
        if not gid:
            continue
        state = (g.get("gameState") or "").upper()
        if state not in ("LIVE", "CRIT"):
            continue
        status = _STATUS_INPROGRESS
        home = g.get("homeTeam") or {}
        away = g.get("awayTeam") or {}
        home_score = _coerce_int(home.get("score"))
        away_score = _coerce_int(away.get("score"))
        period = g.get("periodDescriptor") or {}
        period_num = period.get("number") if isinstance(period, dict) else None
        period_type = (period.get("periodType") or "").upper() if isinstance(period, dict) else ""
        # Preserve REG/OT/SO so downstream NHL parsing can tell a 4th
        # regulation period (impossible) from overtime, and a 5th from
        # the shootout.  Examples: "1", "2", "3", "OT", "2OT", "SO".
        if not period_num:
            period_str = period_type or None
        elif period_type in ("", "REG"):
            period_str = str(period_num)
        elif period_type == "OT":
            period_str = "OT" if int(period_num) <= 4 else f"{int(period_num) - 3}OT"
        elif period_type == "SO":
            period_str = "SO"
        else:
            period_str = f"{period_num} {period_type}"
        clock_obj = g.get("clock") or {}
        clock = clock_obj.get("timeRemaining") if isinstance(clock_obj, dict) else None
        scheduled = _parse_iso8601(g.get("startTimeUTC"))
        payload = {
            "source": "nhl_web",
            "game_id": gid,
            "game_state": state,
            "period_descriptor": period,
            "clock": clock_obj,
        }
        out.append((
            "NHL",
            f"nhl:{gid}",
            status,
            home_score,
            away_score,
            period_str,
            str(clock) if clock else None,
            scheduled,
            _json.dumps(payload, default=str),
        ))
    return out


# ---------------------------------------------------------------------------
# HTTP fetchers
# ---------------------------------------------------------------------------

async def _http_get(client: httpx.AsyncClient, url: str, label: str) -> dict[str, Any] | None:
    try:
        resp = await client.get(url)
        if resp.status_code == 429:
            retry_after_raw = resp.headers.get("Retry-After")
            try:
                retry_after = float(retry_after_raw) if retry_after_raw else 60.0
            except (TypeError, ValueError):
                retry_after = 60.0
            raise RateLimited(label, retry_after=max(30.0, retry_after))
        resp.raise_for_status()
    except RateLimited:
        raise
    except httpx.HTTPError as e:
        log.info("public-live-state fetch failed [%s]: %s", label, e)
        return None
    try:
        body = resp.json()
    except ValueError as e:
        log.info("public-live-state JSON decode failed [%s]: %s", label, e)
        return None
    return body if isinstance(body, dict) else None


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

async def _upsert_rows(rows: Iterable[tuple]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    async with db.acquire() as conn, conn.transaction():
        await conn.executemany(
            """
            INSERT INTO sportradar_live_state
              (league, game_id, status, home_score, away_score,
               period, clock, scheduled, payload, fetched_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, NOW())
            ON CONFLICT (league, game_id) DO UPDATE SET
              status     = EXCLUDED.status,
              home_score = EXCLUDED.home_score,
              away_score = EXCLUDED.away_score,
              period     = EXCLUDED.period,
              clock      = EXCLUDED.clock,
              scheduled  = EXCLUDED.scheduled,
              payload    = EXCLUDED.payload,
              fetched_at = NOW()
            """,
            rows,
        )
    return len(rows)


# ---------------------------------------------------------------------------
# Poller
# ---------------------------------------------------------------------------

async def poll_public_live_state() -> int:
    """One poll across every supported league + provider."""
    if not _is_enabled():
        return 0

    timeout = httpx.Timeout(_HTTP_TIMEOUT_SECS, connect=3.0)
    headers = {
        "User-Agent": "axiom-predict/1.0 (+public-live-state)",
        "Accept": "application/json",
    }
    total = 0
    leagues_with_rows: list[str] = []
    sources_used: list[str] = []

    # ``follow_redirects=True`` is required for the NHL endpoint —
    # ``api-web.nhle.com/v1/score/now`` 307-redirects to a date-stamped
    # path (``/v1/score/<YYYY-MM-DD>``).  ESPN and MLB statsapi serve
    # 200s directly, so the flag is a no-op for them.
    # ``timeout`` here is a number-of-seconds the caller passed in for
    # per-request bounds; wrap it with explicit connect/read/write/pool
    # so a hung TCP handshake or stuck response body can't freeze the
    # event loop forever (task #922).
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(
            timeout if isinstance(timeout, (int, float)) else 15.0,
            connect=5.0, pool=5.0,
        ),
        headers=headers, follow_redirects=True,
    ) as client:
        # --- MLB official (preferred when Sportradar MLB key absent) --
        mlb_handled = False
        if _league_needs_fallback("MLB"):
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            mlb_url = (
                f"{_MLB_BASE}/schedule"
                f"?sportId=1&date={today}&hydrate=linescore,team"
            )
            try:
                mlb_body = await _http_get(client, mlb_url, "mlb_statsapi")
            except RateLimited as e:
                # Per-provider isolation — don't propagate to ESPN poll.
                log.info("public-live-state mlb_statsapi rate-limited: retry_after=%.1fs", e.retry_after)
                mlb_body = None
            if mlb_body is not None:
                mlb_rows = _mlb_rows(mlb_body)
                if mlb_rows:
                    try:
                        written = await _upsert_rows(mlb_rows)
                        total += written
                        leagues_with_rows.append("MLB")
                        sources_used.append("mlb_statsapi")
                    except Exception as e:
                        log.warning("public-live-state mlb upsert failed: %s", e)
                mlb_handled = True

        # --- NHL official (preferred when Sportradar NHL key absent) --
        nhl_handled = False
        if _league_needs_fallback("NHL"):
            try:
                nhl_body = await _http_get(client, f"{_NHL_BASE}/score/now", "nhl_web")
            except RateLimited as e:
                log.info("public-live-state nhl_web rate-limited: retry_after=%.1fs", e.retry_after)
                nhl_body = None
            if nhl_body is not None:
                nhl_rows = _nhl_rows(nhl_body)
                if nhl_rows:
                    try:
                        written = await _upsert_rows(nhl_rows)
                        total += written
                        leagues_with_rows.append("NHL")
                        sources_used.append("nhl_web")
                    except Exception as e:
                        log.warning("public-live-state nhl upsert failed: %s", e)
                nhl_handled = True

        # --- ESPN for everything else (and as MLB/NHL fallback) -------
        espn_used = False
        for league, (sport, slug) in _ESPN_LEAGUES.items():
            if not _league_needs_fallback(league):
                continue
            if league == "MLB" and mlb_handled:
                continue
            if league == "NHL" and nhl_handled:
                continue
            url = f"{_ESPN_BASE}/{sport}/{slug}/scoreboard"
            try:
                body = await _http_get(client, url, f"espn:{league}")
            except RateLimited as e:
                # One league's 429 must not abort the rest — log and skip.
                log.info(
                    "public-live-state espn[%s] rate-limited: retry_after=%.1fs",
                    league, e.retry_after,
                )
                continue
            if body is None:
                continue
            rows = _espn_rows(league, body)
            if not rows:
                continue
            try:
                written = await _upsert_rows(rows)
            except Exception as e:
                log.warning("public-live-state espn[%s] upsert failed: %s", league, e)
                continue
            if written:
                leagues_with_rows.append(league)
                total += written
                espn_used = True
        if espn_used:
            sources_used.append("espn")

    registry.set_metadata(
        "public_live_state",
        leagues_with_rows=leagues_with_rows,
        sources_used=sorted(set(sources_used)),
        last_total_rows=total,
    )
    return total


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_public_live_state_feed() -> None:
    """Register the public-API live-state backstop.

    Idempotent on hot reload.  Advertises a clear ``skipped_reason``
    when explicitly disabled so the operator console renders the row
    as ``enabled=false`` rather than silently dropping it.
    """
    name = "public_live_state"
    if registry.status_of(name):
        return
    enabled = _is_enabled()
    skipped = None if enabled else "AXIOM_PUBLIC_LIVE_STATE_DISABLED set"
    registry.register(Feed(
        name=name,
        cadence_seconds=_cadence_seconds(),
        poll=poll_public_live_state,
        enabled=enabled,
        skipped_reason=skipped,
    ))
    log.info(
        "public live-state feed registered (enabled=%s, cadence=%.1fs)",
        enabled, _cadence_seconds(),
    )
