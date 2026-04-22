"""Election polling pollers (task #201).

Pollers that fill the `election_polls` cache used by the elections
predictor.  Five sources are wired:

1. **FiveThirtyEight RSS** — public RSS feed (538 still publishes
   topical posts including poll snapshots).  Heuristic title parser
   pulls candidate / scope / support%.
2. **Cook Political Report RSS** — public RSS for the race-rating
   updates.  The race-rating string is converted to a coarse
   support % (Toss-up=50, Lean R=46, Likely R=42 …) and an
   appropriate scope.
3. **Real Clear Politics polling RSS** — public RSS feed of newly
   added polls; we extract sample size + topline.
4. **Polymarket cross-reference** — uses `app.polymarket.client`
   to pull current election market prices and write them into
   `election_polls` as `kind=polymarket_xref` rows so the model +
   arb detector see them.  No-op when Polymarket creds are missing.
5. **OpenSecrets fundraising snapshot** — placeholder (the official
   bulk dump requires an API key + an annual download).  The poller
   exists so a future upgrade can land a real fetch without changing
   the registration wiring.

Every poller is *idempotent*: it upserts into `election_polls`
keyed by `(source, signal_id)` so overlapping polls are harmless.

Public surface:
    register_election_polls_feeds() — wired into `app.main` lifespan.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from .. import db
from .registry import Feed, registry


log = logging.getLogger("axiom.feeds.election_polls")


# Canonical poll "kind" tags.
KIND_POLL = "poll"
KIND_RATING = "rating"
KIND_POLYMARKET_XREF = "polymarket_xref"
KIND_FUNDRAISING = "fundraising"


# RSS sources (per task spec).
RSS_SOURCES: tuple[tuple[str, str, str], ...] = (
    ("fivethirtyeight_rss", "https://fivethirtyeight.com/politics/feed/",
     KIND_POLL),
    ("cook_political_rss",  "https://www.cookpolitical.com/rss.xml",
     KIND_RATING),
    ("rcp_polling_rss",     "https://www.realclearpolitics.com/index.xml",
     KIND_POLL),
)


# Cadence: 1 hour for RSS pollers, 5 minutes for the Polymarket cross-
# reference (prices move fast; arb panel needs fresh data).
_CADENCE_RSS = 3600.0
_CADENCE_POLYMARKET = 300.0
_CADENCE_PLACEHOLDER = 24 * 3600.0


# Cook race-rating → coarse support % for the *favoured* side.
_COOK_RATING_TO_PCT: dict[str, float] = {
    "solid d":   58.0,
    "likely d":  54.0,
    "lean d":    52.0,
    "tilt d":    51.0,
    "toss-up":   50.0,
    "tossup":    50.0,
    "tilt r":    49.0,
    "lean r":    48.0,
    "likely r":  46.0,
    "solid r":   42.0,
}


# --- Persistence -------------------------------------------------------------


async def upsert_polls(rows: Iterable[dict[str, Any]]) -> int:
    """Idempotent upsert into `election_polls`.  Returns rows touched."""
    n = 0
    for r in rows:
        try:
            await db.execute(
                """
                INSERT INTO election_polls
                  (source, signal_id, scope, candidate, kind, observed_at,
                   sample_size, support_pct, weight, payload)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)
                ON CONFLICT (source, signal_id) DO UPDATE
                  SET scope = EXCLUDED.scope,
                      candidate = EXCLUDED.candidate,
                      kind = EXCLUDED.kind,
                      observed_at = EXCLUDED.observed_at,
                      sample_size = EXCLUDED.sample_size,
                      support_pct = EXCLUDED.support_pct,
                      weight = EXCLUDED.weight,
                      payload = EXCLUDED.payload
                """,
                r["source"], r["signal_id"], r["scope"], r["candidate"],
                r["kind"], r["observed_at"],
                r.get("sample_size"), r.get("support_pct"),
                float(r.get("weight") or 1.0),
                json.dumps(r.get("payload") or {}),
            )
            n += 1
        except Exception as e:
            log.warning("election_polls upsert failed (%s/%s): %s",
                        r.get("source"), r.get("signal_id"), e)
    return n


# --- HTTP fetch helpers ------------------------------------------------------


async def _http_get_text(url: str, *, headers: dict[str, str] | None = None,
                        timeout: float = 10.0) -> str | None:
    try:
        import httpx  # type: ignore
    except ImportError:  # pragma: no cover
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url, headers=headers or {})
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


# Keyword routing: title fragment -> (scope, candidate).  Coarse but
# good enough to bootstrap; production install will refine via an
# explicit pollster -> scope mapping table.
_SCOPE_KEYWORDS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bpresident(ial)?\b", re.I),               "US_PRES_2028"),
    (re.compile(r"\bsenate\b.*\bohio\b|\bohio\b.*\bsenate\b", re.I), "US_SEN_OH_2026"),
    (re.compile(r"\bsenate\b.*\bpenn(s|sylvan|ya)\b|\bpennsylvania\b.*\bsenate\b", re.I), "US_SEN_PA_2026"),
    (re.compile(r"\bgovernor\b.*\bcalifornia\b|\bcalifornia\b.*\bgovernor\b", re.I), "US_GOV_CA_2026"),
    (re.compile(r"\bhouse\b.*\bnew york\b", re.I),           "US_HOUSE_NY_2026"),
    (re.compile(r"\bfrance\b|\bfrench\b", re.I),             "FRPRES_2027"),
    (re.compile(r"\buk\b.*\b(general election|prime minister|pm)\b", re.I), "UKPM_2029"),
)


_SUPPORT_RE = re.compile(r"(?P<cand>[A-Z][a-zA-Z]{2,})\s+\((?P<party>[DRIL])\)\s+(?P<pct>\d{1,2}(?:\.\d)?)")


def _route_title_to_scope(title: str) -> str | None:
    for pat, scope in _SCOPE_KEYWORDS:
        if pat.search(title):
            return scope
    return None


def _extract_support(text: str) -> tuple[str, float] | None:
    """Pull a (candidate, support%) tuple from a title/description.

    Looks for "Candidate (D) 47" / "Candidate (R) 45" patterns common
    in 538 / RCP polling write-ups.  Returns the *first* match (the
    poller upserts one row per signal_id; multi-candidate polls land
    multiple signal_ids that the model averages downstream).
    """
    m = _SUPPORT_RE.search(text)
    if not m:
        return None
    return m.group("cand").upper(), float(m.group("pct"))


def _extract_sample_size(text: str) -> int | None:
    m = re.search(r"(?:n=|n\s*=\s*|sample of\s+)(\d{2,5})", text, re.IGNORECASE)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


async def _fetch_rss(source: str, url: str, default_kind: str) -> list[dict[str, Any]]:
    body = await _http_get_text(url)
    if not body:
        return []
    out: list[dict[str, Any]] = []
    for item_match in _RSS_ITEM_RE.finditer(body):
        item = item_match.group(1)
        fields: dict[str, str] = {}
        for fm in _RSS_FIELD_RE.finditer(item):
            fields[fm.group(1).lower()] = _strip_cdata(fm.group(2))
        title = fields.get("title", "")
        link = fields.get("link", "")
        guid = fields.get("guid") or link or title
        if not guid:
            continue
        observed = _parse_rss_pubdate(fields.get("pubdate", ""))
        scope = _route_title_to_scope(title + " " + fields.get("description", ""))
        if not scope:
            continue
        if default_kind == KIND_RATING:
            cand, pct = _cook_rating_for(title)
            sample = None
        else:
            extracted = _extract_support(title + " " + fields.get("description", ""))
            if extracted is None:
                continue
            cand, pct = extracted
            sample = _extract_sample_size(title + " " + fields.get("description", ""))
        out.append({
            "source": source,
            "signal_id": guid[:300],
            "scope": scope,
            "candidate": cand,
            "kind": default_kind,
            "observed_at": observed,
            "sample_size": sample,
            "support_pct": pct,
            "weight": 1.0,
            "payload": {
                "title": title[:500],
                "url": link,
            },
        })
    return out


def _cook_rating_for(title: str) -> tuple[str, float]:
    """Map a Cook race-rating headline to a (party, pct) pair.

    Returns the *favoured* party label (DEM / REP / TOSSUP) and its
    coarse implied support %.  Default ("TOSSUP", 50.0) when no
    rating phrase is present.
    """
    t = title.lower()
    for phrase, pct in _COOK_RATING_TO_PCT.items():
        if phrase in t:
            if " r" in phrase or phrase.endswith(" r"):
                return "REP", pct
            if " d" in phrase or phrase.endswith(" d"):
                return "DEM", pct
            return "TOSSUP", pct
    return "TOSSUP", 50.0


async def poll_election_rss() -> int:
    """Poll all wired election RSS feeds.

    Failure on any source is logged-and-skipped so a flaky upstream
    doesn't take the others down with it.
    """
    rows: list[dict[str, Any]] = []
    for source, url, default_kind in RSS_SOURCES:
        try:
            rows.extend(await _fetch_rss(source, url, default_kind))
        except Exception as e:
            log.debug("rss source %s fetch failed: %s", source, e)
    return await upsert_polls(rows)


# --- Polymarket cross-reference ----------------------------------------------


async def poll_polymarket_xref() -> int:
    """Pull current Polymarket election market prices into `election_polls`.

    Best-effort: returns 0 if the Polymarket client is unavailable
    or if no election markets are currently exposed by the CLOB.
    """
    try:
        from ..polymarket import client as poly_client_mod
    except Exception as e:
        log.debug("polymarket client import failed: %s", e)
        return 0
    rows: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    try:
        async with poly_client_mod.PolymarketClient() as poly:
            try:
                # `list_markets` only accepts `(limit, next_cursor)` and
                # returns a CLOB envelope `{ data: [...], next_cursor, ... }`.
                # We filter for election-flavoured markets client-side via
                # the title/group hints so we don't depend on a server-side
                # `category` filter that the CLOB API doesn't expose.
                payload = await poly.list_markets(limit=100)
            except Exception as e:
                log.debug("polymarket list_markets failed: %s", e)
                return 0
    except Exception as e:
        log.debug("polymarket client construct failed: %s", e)
        return 0
    # CLOB list endpoint returns a paginated envelope; markets land
    # under `data`.  Some test/mocked clients return a bare list — be
    # tolerant of both.
    if isinstance(payload, dict):
        markets = payload.get("data") or payload.get("markets") or []
    elif isinstance(payload, list):
        markets = payload
    else:
        markets = []
    if not markets:
        return 0
    for m in markets:
        try:
            scope = (m.get("scope") or m.get("group") or "").upper().strip()
            if not scope:
                # Fall back to a best-effort title-keyword route.
                scope = _route_title_to_scope(m.get("question", "") or "") or ""
            if not scope:
                continue
            outcomes = m.get("outcomes") or []
            for oc in outcomes:
                cand = (oc.get("name") or oc.get("outcome") or "").upper()
                price = oc.get("price")
                cid = m.get("condition_id") or m.get("id") or ""
                token = oc.get("token_id") or oc.get("id") or cand
                if not (cand and price is not None and cid):
                    continue
                try:
                    pct = float(price) * 100.0
                except Exception:
                    continue
                rows.append({
                    "source": "polymarket_xref",
                    "signal_id": f"{cid}/{token}",
                    "scope": scope,
                    "candidate": cand,
                    "kind": KIND_POLYMARKET_XREF,
                    "observed_at": now,
                    "sample_size": None,
                    "support_pct": pct,
                    "weight": 1.0,
                    "payload": {
                        "condition_id": cid,
                        "token_id": token,
                        "question": m.get("question"),
                    },
                })
        except Exception as e:
            log.debug("polymarket xref parse failed: %s", e)
    return await upsert_polls(rows)


# --- Placeholder pollers -----------------------------------------------------


async def poll_opensecrets_fundraising() -> int:
    """Placeholder for the OpenSecrets fundraising poller.

    OpenSecrets' bulk dump requires an API key + an annual download
    cycle; landing a real implementation requires ops to provision
    the credential.  Until then this no-ops so the registration
    doesn't spam the scheduler with errors.
    """
    if not os.environ.get("OPENSECRETS_API_KEY"):
        return 0
    return 0


# --- Feed registration -------------------------------------------------------


def register_election_polls_feeds() -> None:
    """Register every election poll poller with the global feed registry.

    Idempotent: if any feed is already registered (e.g. the lifespan
    fired twice during a hot reload), the duplicate is silently
    skipped so we don't crash the application boot.
    """
    candidates: list[tuple[str, float, Any]] = [
        ("election_polls.rss",                  _CADENCE_RSS, poll_election_rss),
        ("election_polls.polymarket_xref",      _CADENCE_POLYMARKET, poll_polymarket_xref),
        ("election_polls.opensecrets",          _CADENCE_PLACEHOLDER, poll_opensecrets_fundraising),
    ]
    registered = 0
    for name, cadence, poll in candidates:
        try:
            registry.register(Feed(name=name, cadence_seconds=cadence, poll=poll))
            registered += 1
        except ValueError:
            continue
    log.info("election polls feeds registered: %d", registered)
