"""Economic-calendar pollers for the events-econ vertical (task #202).

Pollers fill the `econ_releases` cache used by the events-econ
predictor.  Three sources are wired:

1. **Trading-Economics public RSS** — public RSS for the daily macro
   release calendar (consensus / previous, no actual until print).
2. **BLS press-release RSS** — public RSS for CPI/PPI/NFP/JOBLESS
   release announcements; carries `actual` once the print lands.
3. **FRED back-fill** — pulls a 2-year history of CPI/PPI/GDP/NFP/etc
   actuals from the FRED public API (no key required for limited
   read traffic) so the rolling forecast-error variance has a
   stable history to anchor on.

Every poller is *idempotent*: it upserts into `econ_releases` keyed
by `(source, signal_id)` so overlapping pulls are harmless.

Public surface:
    register_econ_calendar_feeds() — wired into `app.main` lifespan.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .. import db
from .registry import Feed, registry


log = logging.getLogger("axiom.feeds.econ_calendar")


# Cadence: 30 minutes for the daily calendar, 5 minutes around release
# windows for actuals, 24 hours for the FRED back-fill.
_CADENCE_CALENDAR = 30 * 60.0
_CADENCE_ACTUALS = 5 * 60.0
_CADENCE_BACKFILL = 24 * 3600.0


# Trading-Economics public calendar RSS (no key).  We tolerate a
# missing feed (e.g. CI without network) — the FRED back-fill still
# populates history, and the live cycle can degrade to swarm-only.
_TRADING_ECONOMICS_RSS = "https://tradingeconomics.com/calendar.rss"
_BLS_RELEASES_RSS = "https://www.bls.gov/feed/news_release/empsit.rss"


# Mapping from release-name keywords -> canonical release_type.  Order
# matters: longer / more specific first to avoid CPI matching against
# a "core CPI" headline.
_RELEASE_KEYWORDS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(non-?farm payrolls?|nfp|employment situation)\b", re.I), "NFP"),
    (re.compile(r"\b(initial jobless claims|jobless claims)\b", re.I), "JOBLESS"),
    (re.compile(r"\b(retail sales)\b", re.I), "RETAIL"),
    (re.compile(r"\b(personal consumption expenditures|pce)\b", re.I), "PCE"),
    (re.compile(r"\b(gross domestic product|gdp)\b", re.I), "GDP"),
    (re.compile(r"\b(consumer price index|\bcpi\b)\b", re.I), "CPI"),
    (re.compile(r"\b(producer price index|\bppi\b)\b", re.I), "PPI"),
    (re.compile(r"\b(fomc|federal funds rate|fed funds rate|interest rate decision)\b", re.I), "FOMC"),
    (re.compile(r"\b(ism manufacturing|ism services)\b", re.I), "ISM"),
    (re.compile(r"\b(consumer confidence|university of michigan)\b", re.I), "CONFIDENCE"),
    (re.compile(r"\b(housing starts|new home sales|existing home sales)\b", re.I), "HOUSING"),
)


# FRED series -> canonical release_type for the historical back-fill.
_FRED_SERIES: tuple[tuple[str, str], ...] = (
    ("CPIAUCSL",   "CPI"),
    ("PPIACO",     "PPI"),
    ("GDP",        "GDP"),
    ("PAYEMS",     "NFP"),
    ("ICSA",       "JOBLESS"),
    ("RSAFS",      "RETAIL"),
    ("PCE",        "PCE"),
    ("FEDFUNDS",   "FOMC"),
)


# --- Persistence -------------------------------------------------------------


async def upsert_releases(rows: Iterable[dict[str, Any]]) -> int:
    """Idempotent upsert into `econ_releases`.  Returns rows touched."""
    n = 0
    for r in rows:
        try:
            await db.execute(
                """
                INSERT INTO econ_releases
                  (source, signal_id, release_type, release_at,
                   consensus, previous, actual, revised_previous, payload)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
                ON CONFLICT (source, signal_id) DO UPDATE
                  SET release_type = EXCLUDED.release_type,
                      release_at = EXCLUDED.release_at,
                      consensus = COALESCE(EXCLUDED.consensus, econ_releases.consensus),
                      previous = COALESCE(EXCLUDED.previous, econ_releases.previous),
                      actual = COALESCE(EXCLUDED.actual, econ_releases.actual),
                      revised_previous = COALESCE(
                        EXCLUDED.revised_previous, econ_releases.revised_previous
                      ),
                      payload = EXCLUDED.payload
                """,
                r["source"], r["signal_id"], r["release_type"], r["release_at"],
                r.get("consensus"), r.get("previous"),
                r.get("actual"), r.get("revised_previous"),
                json.dumps(r.get("payload") or {}),
            )
            n += 1
        except Exception as e:
            log.warning("econ_releases upsert failed (%s/%s): %s",
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
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


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


def _route_release_type(text: str) -> str | None:
    for pat, rtype in _RELEASE_KEYWORDS:
        if pat.search(text):
            return rtype
    return None


def _extract_consensus_actual(text: str) -> tuple[float | None, float | None, float | None]:
    """Pull (actual, consensus, previous) from a calendar item description.

    Best-effort regex extraction tolerant of Trading-Economics' free-form
    text format.  Returns (None, None, None) when none of the labels
    appear — caller proceeds with whatever it has.
    """
    def _find(label: str) -> float | None:
        m = re.search(label + r"\s*[:=]?\s*(-?\d+(?:\.\d+)?)", text, re.IGNORECASE)
        if not m:
            return None
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return _find(r"actual"), _find(r"(?:consensus|forecast)"), _find(r"previous")


async def _fetch_calendar_rss() -> list[dict[str, Any]]:
    body = await _http_get_text(_TRADING_ECONOMICS_RSS)
    if not body:
        return []
    out: list[dict[str, Any]] = []
    for item_match in _RSS_ITEM_RE.finditer(body):
        item = item_match.group(1)
        fields: dict[str, str] = {}
        for fm in _RSS_FIELD_RE.finditer(item):
            fields[fm.group(1).lower()] = _strip_cdata(fm.group(2))
        title = fields.get("title", "")
        description = fields.get("description", "")
        guid = fields.get("guid") or fields.get("link") or title
        if not guid:
            continue
        rtype = _route_release_type(title + " " + description)
        if not rtype:
            continue
        observed = _parse_rss_pubdate(fields.get("pubdate", ""))
        actual, consensus, previous = _extract_consensus_actual(title + " " + description)
        out.append({
            "source": "trading_economics_rss",
            "signal_id": guid[:300],
            "release_type": rtype,
            "release_at": observed,
            "consensus": consensus,
            "previous": previous,
            "actual": actual,
            "revised_previous": None,
            "payload": {
                "title": title[:500],
                "description": description[:1000],
            },
        })
    return out


async def poll_calendar() -> int:
    """Pull the upcoming release calendar (consensus, previous)."""
    try:
        rows = await _fetch_calendar_rss()
    except Exception as e:
        log.debug("calendar rss fetch failed: %s", e)
        rows = []
    return await upsert_releases(rows)


async def poll_actuals() -> int:
    """Pull released actuals from BLS press-release RSS.

    Best-effort: the BLS RSS is small and only carries the latest
    employment situation print, so the fast cadence is cheap.  Other
    actuals back-fill via FRED on the slower cadence.
    """
    body = await _http_get_text(_BLS_RELEASES_RSS)
    if not body:
        return 0
    out: list[dict[str, Any]] = []
    for item_match in _RSS_ITEM_RE.finditer(body):
        item = item_match.group(1)
        fields: dict[str, str] = {}
        for fm in _RSS_FIELD_RE.finditer(item):
            fields[fm.group(1).lower()] = _strip_cdata(fm.group(2))
        title = fields.get("title", "")
        description = fields.get("description", "")
        guid = fields.get("guid") or fields.get("link") or title
        if not guid:
            continue
        rtype = _route_release_type(title + " " + description)
        if not rtype:
            continue
        observed = _parse_rss_pubdate(fields.get("pubdate", ""))
        actual, consensus, previous = _extract_consensus_actual(title + " " + description)
        out.append({
            "source": "bls_releases_rss",
            "signal_id": guid[:300],
            "release_type": rtype,
            "release_at": observed,
            "consensus": consensus,
            "previous": previous,
            "actual": actual,
            "revised_previous": None,
            "payload": {
                "title": title[:500],
                "description": description[:1000],
            },
        })
    return await upsert_releases(out)


async def poll_fred_backfill() -> int:
    """2-year FRED back-fill of historical actuals for variance estimation.

    Best-effort.  Uses the public CSV endpoint (no key required for
    light traffic).  Each settled row lands as `(source='fred', ...)`
    keyed by `(series_id, observation_date)` so re-pulling is
    idempotent.

    Each row carries both the print's `actual` AND the prior print's
    value as `previous` (a random-walk / naive consensus benchmark).
    The model's variance estimator falls back to (previous, actual)
    pairs when an explicit (consensus, actual) pair isn't available,
    so the FRED back-fill alone gives the rolling forecast-error
    sigma a 2-year settled-history anchor.
    """
    rows: list[dict[str, Any]] = []
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=730)
    for series_id, rtype in _FRED_SERIES:
        url = (
            "https://fred.stlouisfed.org/graph/fredgraph.csv"
            f"?id={series_id}"
            f"&cosd={start.strftime('%Y-%m-%d')}"
            f"&coed={end.strftime('%Y-%m-%d')}"
        )
        body = await _http_get_text(url, timeout=15.0)
        if not body:
            continue
        lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
        if len(lines) <= 1:
            continue
        # CSV header: DATE,VALUE  (or DATE,<series_id>)
        # Pre-pass: parse all (date, value) rows so we can carry the
        # prior print's value through as `previous` (the naive
        # random-walk consensus).
        parsed: list[tuple[datetime, float]] = []
        for raw_line in lines[1:]:
            try:
                date_s, value_s = raw_line.split(",", 1)
            except ValueError:
                continue
            value_s = value_s.strip()
            if value_s in {".", ""}:
                continue
            try:
                value = float(value_s)
            except ValueError:
                continue
            try:
                release_at = datetime.strptime(date_s.strip(), "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                continue
            parsed.append((release_at, value))
        # Stable order ascending so previous-row carry works.
        parsed.sort(key=lambda t: t[0])
        prev_value: float | None = None
        for release_at, value in parsed:
            rows.append({
                "source": "fred",
                "signal_id": f"{series_id}/{release_at.strftime('%Y-%m-%d')}",
                "release_type": rtype,
                "release_at": release_at,
                # Naive random-walk consensus = prior print's value.
                # Lets the variance estimator anchor on (consensus, actual)
                # pairs for the full 2yr back-fill window even though
                # FRED itself doesn't publish forecast consensus.
                "consensus": prev_value,
                "previous": prev_value,
                "actual": value,
                "revised_previous": None,
                "payload": {"series_id": series_id, "naive_consensus": True},
            })
            prev_value = value
    return await upsert_releases(rows)


# --- Feed registration -------------------------------------------------------


def register_econ_calendar_feeds() -> None:
    """Register every economic-calendar poller with the global feed registry.

    Idempotent: if any feed is already registered (e.g. lifespan fired
    twice during a hot reload), the duplicate is silently skipped so
    we don't crash boot.
    """
    candidates: list[tuple[str, float, Any]] = [
        ("econ_calendar.calendar", _CADENCE_CALENDAR, poll_calendar),
        ("econ_calendar.actuals",  _CADENCE_ACTUALS,  poll_actuals),
        ("econ_calendar.fred",     _CADENCE_BACKFILL, poll_fred_backfill),
    ]
    registered = 0
    for name, cadence, poll in candidates:
        try:
            registry.register(Feed(name=name, cadence_seconds=cadence, poll=poll))
            registered += 1
        except ValueError:
            continue
    log.info("econ calendar feeds registered: %d", registered)
