"""Entertainment-signal pollers for the entertainment vertical (task #203).

Pollers fill the `entertainment_signals` cache used by the
entertainment predictor.  Four sources are wired:

1. **Box Office Mojo / The-Numbers RSS** — public RSS for opening
   weekend / weekly box office (carries `value` once the weekend
   actuals land).
2. **Rotten Tomatoes RSS** — public reviews RSS — surfaces critic-
   score updates per title.
3. **Spotify weekly charts CSV** — public weekly Spotify chart export
   (Top 200, by territory).  We pull global Top 200 only.
4. **Awards calendar (manual + Variety RSS)** — upcoming Oscars / Emmys
   / Grammys ceremony dates and per-category nominees.  Nominees carry
   a precursor-derived prior probability on `value`.

Every poller is *idempotent*: it upserts into `entertainment_signals`
keyed by `(source, signal_id)`.

Public surface:
    register_entertainment_signal_feeds() — wired into `app.main` lifespan.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .. import db
from .registry import Feed, registry


log = logging.getLogger("axiom.feeds.entertainment_signals")


# Cadence: 60min for box office (weekly cycle), 30min for RT, 6h for
# Spotify (weekly chart drop), 24h for awards calendar updates.
_CADENCE_BOX_OFFICE = 60 * 60.0
_CADENCE_RT = 30 * 60.0
_CADENCE_SPOTIFY = 6 * 3600.0
_CADENCE_AWARDS = 24 * 3600.0


# Public RSS feeds.  Best-effort: missing network downgrades to no-op
# without crashing the lifespan.
_BOX_OFFICE_MOJO_RSS = "https://www.boxofficemojo.com/rss/weekend.xml"
_RT_RSS = "https://www.rottentomatoes.com/news/rss/"
_VARIETY_AWARDS_RSS = "https://variety.com/v/awards/feed/"
_SPOTIFY_CHARTS_CSV = (
    "https://charts.spotify.com/charts/view/regional-global-weekly/latest/download"
)


# --- Persistence -------------------------------------------------------------


async def upsert_signals(rows: Iterable[dict[str, Any]]) -> int:
    """Idempotent upsert into `entertainment_signals`.  Returns rows touched."""
    n = 0
    for r in rows:
        try:
            await db.execute(
                """
                INSERT INTO entertainment_signals
                  (source, signal_id, signal_type, title, event_at,
                   value, settled, outcome, payload)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
                ON CONFLICT (source, signal_id) DO UPDATE
                  SET signal_type = EXCLUDED.signal_type,
                      title = EXCLUDED.title,
                      event_at = EXCLUDED.event_at,
                      value = COALESCE(EXCLUDED.value, entertainment_signals.value),
                      settled = entertainment_signals.settled OR EXCLUDED.settled,
                      outcome = COALESCE(EXCLUDED.outcome, entertainment_signals.outcome),
                      payload = EXCLUDED.payload
                """,
                r["source"], r["signal_id"], r["signal_type"], r["title"],
                r["event_at"], r.get("value"),
                bool(r.get("settled", False)), r.get("outcome"),
                json.dumps(r.get("payload") or {}),
            )
            n += 1
        except Exception as e:
            log.warning("entertainment_signals upsert failed (%s/%s): %s",
                        r.get("source"), r.get("signal_id"), e)
    return n


# --- HTTP fetch helpers ------------------------------------------------------


async def _http_get_text(url: str, *, timeout: float = 10.0) -> str | None:
    try:
        import httpx  # type: ignore
    except ImportError:  # pragma: no cover
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url, headers={"User-Agent": "axiom-predict/1.0"})
            if r.status_code >= 400:
                log.debug("GET %s -> HTTP %s", url, r.status_code)
                return None
            return r.text
    except Exception as e:
        log.debug("GET %s failed: %s", url, e)
        return None


# --- RSS parsing -------------------------------------------------------------


_RSS_ITEM_RE = re.compile(r"<item>(.*?)</item>", re.DOTALL | re.IGNORECASE)
_RSS_FIELD_RE = re.compile(
    r"<(title|link|guid|pubDate|description)[^>]*>(.*?)</\1>",
    re.DOTALL | re.IGNORECASE,
)
_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.DOTALL)


def _strip_cdata(s: str) -> str:
    m = _CDATA_RE.search(s)
    return (m.group(1) if m else s).strip()


def _parse_rss_pubdate(raw: str) -> datetime:
    try:
        from email.utils import parsedate_to_datetime
        if raw:
            dt = parsedate_to_datetime(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
    except Exception:
        pass
    return datetime.now(timezone.utc)


# Box-office gross extractor: $123.4 million / $123.4M / $123,400,000.
_GROSS_DOLLAR_M = re.compile(
    r"\$\s*(\d+(?:\.\d+)?)\s*(million|m\b)",
    re.IGNORECASE,
)
_GROSS_DOLLAR_RAW = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)")


def _extract_box_office_gross(text: str) -> float | None:
    """Return opening-weekend gross in USD millions, or None."""
    m = _GROSS_DOLLAR_M.search(text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    m = _GROSS_DOLLAR_RAW.search(text)
    if m:
        try:
            raw = float(m.group(1).replace(",", ""))
            return raw / 1_000_000.0 if raw > 1_000_000 else raw
        except ValueError:
            return None
    return None


_RT_SCORE_RE = re.compile(r"(\d{1,3})\s*%\s*(?:tomatometer|fresh|critic)", re.IGNORECASE)


def _extract_rt_score(text: str) -> float | None:
    m = _RT_SCORE_RE.search(text)
    if not m:
        return None
    try:
        v = float(m.group(1))
        return v if 0.0 <= v <= 100.0 else None
    except ValueError:
        return None


def _title_from_rss(title_field: str) -> str:
    """Extract a clean subject title from an RSS <title>, dropping suffixes."""
    s = re.sub(r"\(\d{4}\)", "", title_field).strip()
    s = re.sub(r"\s*[-:|]\s*.*$", "", s).strip()
    return s[:200] or title_field[:200]


# --- Pollers ------------------------------------------------------------------


async def poll_box_office() -> int:
    body = await _http_get_text(_BOX_OFFICE_MOJO_RSS)
    if not body:
        return 0
    out: list[dict[str, Any]] = []
    for item_match in _RSS_ITEM_RE.finditer(body):
        item = item_match.group(1)
        fields: dict[str, str] = {}
        for fm in _RSS_FIELD_RE.finditer(item):
            fields[fm.group(1).lower()] = _strip_cdata(fm.group(2))
        title_raw = fields.get("title", "")
        description = fields.get("description", "")
        guid = fields.get("guid") or fields.get("link") or title_raw
        if not guid:
            continue
        observed = _parse_rss_pubdate(fields.get("pubdate", ""))
        gross = _extract_box_office_gross(title_raw + " " + description)
        title = _title_from_rss(title_raw)
        out.append({
            "source": "box_office_mojo_rss",
            "signal_id": guid[:300],
            "signal_type": "BOX",
            "title": title,
            "event_at": observed,
            "value": gross,
            # Carries `value` ⇒ weekend already settled.
            "settled": gross is not None,
            "outcome": "released" if gross is not None else None,
            "payload": {
                "title": title_raw[:500],
                "description": description[:1000],
            },
        })
    return await upsert_signals(out)


async def poll_rotten_tomatoes() -> int:
    body = await _http_get_text(_RT_RSS)
    if not body:
        return 0
    out: list[dict[str, Any]] = []
    for item_match in _RSS_ITEM_RE.finditer(body):
        item = item_match.group(1)
        fields: dict[str, str] = {}
        for fm in _RSS_FIELD_RE.finditer(item):
            fields[fm.group(1).lower()] = _strip_cdata(fm.group(2))
        title_raw = fields.get("title", "")
        description = fields.get("description", "")
        guid = fields.get("guid") or fields.get("link") or title_raw
        if not guid:
            continue
        observed = _parse_rss_pubdate(fields.get("pubdate", ""))
        score = _extract_rt_score(title_raw + " " + description)
        title = _title_from_rss(title_raw)
        out.append({
            "source": "rotten_tomatoes_rss",
            "signal_id": guid[:300],
            "signal_type": "RT",
            "title": title,
            "event_at": observed,
            "value": score,
            "settled": score is not None,
            "outcome": "scored" if score is not None else None,
            "payload": {
                "title": title_raw[:500],
                "description": description[:1000],
            },
        })
    return await upsert_signals(out)


async def poll_spotify_charts() -> int:
    """Pull the global weekly Spotify Top 200 chart and emit per-track rows.

    Each chart row becomes a settled signal with value = rank (1..200)
    so the charts head can compute a TOP-K probability empirically.
    """
    body = await _http_get_text(_SPOTIFY_CHARTS_CSV, timeout=15.0)
    if not body:
        return 0
    rows = body.splitlines()
    if len(rows) <= 1:
        return 0
    out: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    # CSV header line varies — look up "rank" / "track" / "artist".
    header = [h.strip().lower() for h in rows[0].split(",")]
    try:
        i_rank = header.index("rank")
        i_track = header.index("track name") if "track name" in header else header.index("track")
        i_artist = header.index("artist") if "artist" in header else header.index("artists")
    except ValueError:
        return 0
    for raw in rows[1:201]:
        parts = [p.strip().strip('"') for p in raw.split(",")]
        if len(parts) <= max(i_rank, i_track, i_artist):
            continue
        try:
            rank = int(parts[i_rank])
        except ValueError:
            continue
        track = parts[i_track][:200]
        artist = parts[i_artist][:200]
        if not track or not artist:
            continue
        wk_id = now.strftime("%G-W%V")  # ISO year-week
        out.append({
            "source": "spotify_charts_csv",
            "signal_id": f"{wk_id}/{rank}/{artist}/{track}"[:300],
            "signal_type": "SPOTIFY",
            "title": f"{artist} - {track}",
            "event_at": now,
            "value": float(rank),
            "settled": True,
            "outcome": "charted",
            "payload": {"artist": artist, "track": track,
                        "rank": rank, "week": wk_id},
        })
    return await upsert_signals(out)


_AWARD_KEYWORDS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(oscar|academy award|academy[\u2010-]award)s?\b", re.I), "OSCAR"),
    (re.compile(r"\b(primetime emmy|emmy)s?\b", re.I), "EMMY"),
    (re.compile(r"\b(grammy)s?\b", re.I), "GRAMMY"),
)


async def poll_awards_calendar() -> int:
    """Pull awards-cycle headlines + emit per-nominee placeholder rows.

    Each headline that mentions a frontrunner (e.g. "Oppenheimer leads
    Oscar nominations") becomes an *unsettled* row carrying a 0.5
    placeholder prior so the awards head has a candidate to anchor on
    before the precursor-derived prior arrives via a later poll cycle.
    """
    body = await _http_get_text(_VARIETY_AWARDS_RSS)
    if not body:
        return 0
    out: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for item_match in _RSS_ITEM_RE.finditer(body):
        item = item_match.group(1)
        fields: dict[str, str] = {}
        for fm in _RSS_FIELD_RE.finditer(item):
            fields[fm.group(1).lower()] = _strip_cdata(fm.group(2))
        title_raw = fields.get("title", "")
        description = fields.get("description", "")
        guid = fields.get("guid") or fields.get("link") or title_raw
        if not guid:
            continue
        award_type: str | None = None
        for pat, atype in _AWARD_KEYWORDS:
            if pat.search(title_raw + " " + description):
                award_type = atype
                break
        if award_type is None:
            continue
        observed = _parse_rss_pubdate(fields.get("pubdate", ""))
        # Nominee title is best-effort: use the cleaned RSS title.
        title = _title_from_rss(title_raw)
        out.append({
            "source": "variety_awards_rss",
            "signal_id": guid[:300],
            "signal_type": award_type,
            "title": title,
            "event_at": observed if observed > now - timedelta(days=180) else now,
            "value": 0.5,  # placeholder prior, refined by precursor pollers
            "settled": False,
            "outcome": None,
            "payload": {
                "title": title_raw[:500],
                "description": description[:1000],
                "award_type": award_type,
            },
        })
    return await upsert_signals(out)


# --- Feed registration -------------------------------------------------------


def register_entertainment_signal_feeds() -> None:
    """Register every entertainment-signal poller with the global feed registry.

    Idempotent: duplicate registrations are silently skipped so we
    don't crash boot during a hot reload.
    """
    candidates: list[tuple[str, float, Any]] = [
        ("entertainment_signals.box_office", _CADENCE_BOX_OFFICE, poll_box_office),
        ("entertainment_signals.rotten_tomatoes", _CADENCE_RT, poll_rotten_tomatoes),
        ("entertainment_signals.spotify", _CADENCE_SPOTIFY, poll_spotify_charts),
        ("entertainment_signals.awards", _CADENCE_AWARDS, poll_awards_calendar),
    ]
    registered = 0
    for name, cadence, poll in candidates:
        try:
            registry.register(Feed(name=name, cadence_seconds=cadence, poll=poll))
            registered += 1
        except ValueError:
            continue
    log.info("entertainment signal feeds registered: %d", registered)
