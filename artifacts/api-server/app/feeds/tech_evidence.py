"""Tech / AI evidence pollers (task #199).

The tech-AI vertical needs a steady stream of release / benchmark /
regulatory signals to ground its Bayesian probability model.  This
module owns the public-API pollers that fill the `tech_evidence`
cache table.

Six sources are wired:

1. **GitHub releases** — a curated list of orgs/repos (huggingface,
   openai, anthropics, meta-llama).  Public REST API, no key required;
   a `GITHUB_TOKEN` is honored when present so production gets the
   higher rate limit.
2. **Hugging Face trending** — `/api/models?sort=downloads&direction=-1`
   public endpoint, no auth required.
3. **ArXiv recent submissions** — Atom feed for `cs.LG` and `cs.CL`
   categories, no auth required.
4. **Industry-analyst RSS** — SemiAnalysis + The Information public RSS
   feeds, parsed for headline/title and routed onto a symbol class via
   keyword heuristics.
5. **MLPerf rounds** — lists the public `mlcommons` GitHub org and
   detects new `*_results_v*` benchmark-round repos (inference,
   training, tiny, mobile, storage, client tracks).  No auth required;
   honors `GITHUB_TOKEN` for the higher rate limit.
6. **Earnings calendar** — Yahoo Finance `quoteSummary` for the next
   scheduled NVDA / TSMC (TSM ADR) earnings date, with a SEC EDGAR
   8-K Atom-feed backstop for historical earnings press releases so
   the table is never empty when Yahoo throttles us.

Every poller is *idempotent*: it upserts into `tech_evidence` keyed by
`(source, signal_id)` so overlapping polls are harmless.

Public surface:
    register_tech_evidence_feeds() — wired into `app.main` lifespan.
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


log = logging.getLogger("axiom.feeds.tech_evidence")


# Canonical evidence "kind" tags used by the Bayesian model.
KIND_RELEASE = "release"
KIND_TRENDING = "trending"
KIND_ARXIV = "arxiv"
KIND_BENCHMARK = "benchmark"
KIND_EARNINGS = "earnings"


# Repo -> symbol class mapping for GitHub release polling.  Kept tight
# so a noisy upstream commit doesn't contaminate signals for unrelated
# markets.
GITHUB_REPOS: tuple[tuple[str, str], ...] = (
    ("openai/openai-python",       "GPT"),
    ("openai/whisper",             "GPT"),
    ("anthropics/anthropic-sdk-python", "CLAUDE"),
    ("meta-llama/llama3",          "LLAMA"),
    ("meta-llama/llama-models",    "LLAMA"),
    ("huggingface/transformers",   "LLM"),
    ("huggingface/diffusers",      "LLM"),
    ("nvidia/megatron-lm",         "NVDA"),
    ("nvidia/cuda-samples",        "NVDA"),
    ("mlcommons/inference",        "MLPERF"),
)


# ArXiv categories (per task spec): cs.LG and cs.CL.
ARXIV_CATEGORIES: tuple[str, ...] = ("cs.LG", "cs.CL")


# Cadence: 30 minutes for release/trending, 1 hour for ArXiv + RSS, 6
# hours for the placeholder pollers (the resolver only needs them to
# refresh after upstream updates).
_CADENCE_RELEASE = 1800.0
_CADENCE_TRENDING = 1800.0
_CADENCE_ARXIV = 3600.0
_CADENCE_RSS = 3600.0
_CADENCE_PLACEHOLDER = 6 * 3600.0


# Industry-analyst RSS feeds (per task spec).  Both are public RSS;
# SemiAnalysis publishes free + subscriber posts (we only need
# headlines), and The Information's RSS exposes article titles even
# behind their paywall.
RSS_SOURCES: tuple[tuple[str, str, str], ...] = (
    ("semianalysis_rss",  "https://www.semianalysis.com/feed",     "NVDA"),
    ("theinformation_rss","https://www.theinformation.com/feed",   "LLM"),
)


# --- Persistence -------------------------------------------------------------


async def upsert_evidence(rows: Iterable[dict[str, Any]]) -> int:
    """Idempotent upsert into `tech_evidence`.  Returns rows touched."""
    n = 0
    for r in rows:
        try:
            await db.execute(
                """
                INSERT INTO tech_evidence
                  (source, signal_id, symbol, kind, observed_at, weight, payload)
                VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
                ON CONFLICT (source, signal_id) DO UPDATE
                  SET symbol = EXCLUDED.symbol,
                      kind = EXCLUDED.kind,
                      observed_at = EXCLUDED.observed_at,
                      weight = EXCLUDED.weight,
                      payload = EXCLUDED.payload
                """,
                r["source"], r["signal_id"], r["symbol"], r["kind"],
                r["observed_at"], float(r.get("weight") or 0.0),
                json.dumps(r.get("payload") or {}),
            )
            n += 1
        except Exception as e:
            log.warning("tech_evidence upsert failed (%s/%s): %s",
                        r.get("source"), r.get("signal_id"), e)
    return n


# --- HTTP fetch helpers ------------------------------------------------------


async def _http_get_json(url: str, *, headers: dict[str, str] | None = None,
                        timeout: float = 10.0) -> Any:
    """Minimal async JSON GET with httpx; returns None on failure."""
    try:
        import httpx  # type: ignore
    except ImportError:  # pragma: no cover
        log.debug("httpx not installed; tech evidence fetch skipped")
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url, headers=headers or {})
            if r.status_code >= 400:
                log.debug("GET %s -> HTTP %s", url, r.status_code)
                return None
            return r.json()
    except Exception as e:
        log.debug("GET %s failed: %s", url, e)
        return None


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
                return None
            return r.text
    except Exception as e:
        log.debug("GET %s failed: %s", url, e)
        return None


# --- GitHub releases ---------------------------------------------------------


def _github_headers() -> dict[str, str]:
    h = {"Accept": "application/vnd.github+json"}
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


async def _fetch_github_releases(repo: str, symbol: str) -> list[dict[str, Any]]:
    url = f"https://api.github.com/repos/{repo}/releases?per_page=10"
    payload = await _http_get_json(url, headers=_github_headers())
    if not isinstance(payload, list):
        return []
    out: list[dict[str, Any]] = []
    for item in payload:
        try:
            tag = str(item.get("tag_name") or item.get("name") or "")
            published = item.get("published_at") or item.get("created_at")
            if not (tag and published):
                continue
            ts = datetime.fromisoformat(published.replace("Z", "+00:00"))
            out.append({
                "source": "github_releases",
                "signal_id": f"{repo}@{tag}",
                "symbol": symbol,
                "kind": KIND_RELEASE,
                "observed_at": ts,
                "weight": 1.0,
                "payload": {
                    "repo": repo,
                    "tag": tag,
                    "html_url": item.get("html_url"),
                    "prerelease": bool(item.get("prerelease")),
                },
            })
        except Exception as e:
            log.debug("github release parse failed: %s", e)
    return out


async def poll_github_releases() -> int:
    rows: list[dict[str, Any]] = []
    for repo, symbol in GITHUB_REPOS:
        rows.extend(await _fetch_github_releases(repo, symbol))
    return await upsert_evidence(rows)


# --- Hugging Face trending ---------------------------------------------------


_HF_AUTHOR_TO_SYMBOL: dict[str, str] = {
    "meta-llama":  "LLAMA",
    "openai":      "GPT",
    "openai-community": "GPT",
    "anthropic":   "CLAUDE",
    "nvidia":      "NVDA",
    "mistralai":   "LLM",
    "google":      "LLM",
    "microsoft":   "LLM",
}


def _hf_symbol_for(model_id: str) -> str:
    """Best-effort author->symbol mapping; defaults to LLM."""
    author = (model_id.split("/", 1)[0] if "/" in model_id else "").lower()
    return _HF_AUTHOR_TO_SYMBOL.get(author, "LLM")


async def poll_hf_trending() -> int:
    url = "https://huggingface.co/api/models?sort=downloads&direction=-1&limit=30"
    payload = await _http_get_json(url)
    if not isinstance(payload, list):
        return 0
    rows: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for item in payload:
        try:
            mid = str(item.get("modelId") or item.get("id") or "")
            if not mid:
                continue
            downloads = int(item.get("downloads") or 0)
            likes = int(item.get("likes") or 0)
            # Weight scales with rank; trending high-volume drops carry
            # more signal than long-tail community uploads.
            weight = min(1.0, (downloads + likes * 100) / 1_000_000)
            rows.append({
                "source": "hf_trending",
                "signal_id": mid,
                "symbol": _hf_symbol_for(mid),
                "kind": KIND_TRENDING,
                "observed_at": now,
                "weight": weight,
                "payload": {
                    "model_id": mid,
                    "downloads": downloads,
                    "likes": likes,
                    "tags": item.get("tags") or [],
                },
            })
        except Exception as e:
            log.debug("hf trending parse failed: %s", e)
    return await upsert_evidence(rows)


# --- ArXiv -------------------------------------------------------------------


_ARXIV_ENTRY_RE = re.compile(
    r"<entry>(.*?)</entry>", re.DOTALL | re.IGNORECASE
)
_ARXIV_FIELD_RE = re.compile(
    r"<(id|title|published|summary)>(.*?)</\1>", re.DOTALL | re.IGNORECASE
)


def _arxiv_symbol_for(text: str) -> str:
    """Crude keyword-based routing to the symbol class."""
    t = text.lower()
    if any(k in t for k in ("gpt-", "openai", "chatgpt")):
        return "GPT"
    if "claude" in t or "anthropic" in t:
        return "CLAUDE"
    if "llama" in t or "meta-llama" in t:
        return "LLAMA"
    if "nvidia" in t or "h100" in t or "h200" in t or "blackwell" in t:
        return "NVDA"
    if "tsmc" in t or "wafer" in t or "fab" in t:
        return "TSMC"
    if "mlperf" in t or "benchmark" in t:
        return "MLPERF"
    return "LLM"


async def poll_arxiv() -> int:
    rows: list[dict[str, Any]] = []
    for cat in ARXIV_CATEGORIES:
        url = (
            "http://export.arxiv.org/api/query?"
            f"search_query=cat:{cat}&sortBy=submittedDate&sortOrder=descending&max_results=25"
        )
        body = await _http_get_text(url)
        if not body:
            continue
        for entry_match in _ARXIV_ENTRY_RE.finditer(body):
            entry = entry_match.group(1)
            fields: dict[str, str] = {}
            for fm in _ARXIV_FIELD_RE.finditer(entry):
                fields[fm.group(1).lower()] = fm.group(2).strip()
            arxiv_id = fields.get("id", "")
            title = fields.get("title", "")
            pub = fields.get("published", "")
            if not (arxiv_id and pub):
                continue
            try:
                ts = datetime.fromisoformat(pub.replace("Z", "+00:00"))
            except Exception:
                continue
            rows.append({
                "source": f"arxiv_{cat}",
                "signal_id": arxiv_id,
                "symbol": _arxiv_symbol_for(title + " " + fields.get("summary", "")),
                "kind": KIND_ARXIV,
                "observed_at": ts,
                "weight": 0.3,
                "payload": {
                    "title": title[:500],
                    "category": cat,
                    "url": arxiv_id,
                },
            })
    return await upsert_evidence(rows)


# --- Industry-analyst RSS pollers --------------------------------------------


_RSS_ITEM_RE = re.compile(r"<item>(.*?)</item>", re.DOTALL | re.IGNORECASE)
_RSS_FIELD_RE = re.compile(
    r"<(title|link|guid|pubDate|description)[^>]*>(.*?)</\1>",
    re.DOTALL | re.IGNORECASE,
)
_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.DOTALL)


def _strip_cdata(s: str) -> str:
    m = _CDATA_RE.search(s)
    return (m.group(1) if m else s).strip()


def _rss_symbol_for(text: str, default_symbol: str) -> str:
    """Re-route an RSS item to a more specific symbol class when the
    title gives a strong tell — otherwise keep the source's default."""
    t = text.lower()
    if "tsmc" in t or "wafer" in t or "fab " in t:
        return "TSMC"
    if "nvda" in t or "nvidia" in t or "blackwell" in t or "h200" in t or "h100" in t:
        return "NVDA"
    if "claude" in t or "anthropic" in t:
        return "CLAUDE"
    if "llama" in t or "meta-llama" in t:
        return "LLAMA"
    if "openai" in t or "gpt-" in t or "chatgpt" in t:
        return "GPT"
    if "ipo" in t:
        return "IPO"
    if "mlperf" in t or "benchmark" in t:
        return "MLPERF"
    return default_symbol


async def _fetch_rss(source: str, url: str, default_symbol: str) -> list[dict[str, Any]]:
    body = await _http_get_text(url)
    if not body:
        return []
    out: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
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
        # pubDate is RFC-822-ish; fall back to "now" when we can't parse.
        pub_raw = fields.get("pubdate", "")
        observed = now
        try:
            from email.utils import parsedate_to_datetime
            if pub_raw:
                observed = parsedate_to_datetime(pub_raw)
                if observed.tzinfo is None:
                    observed = observed.replace(tzinfo=timezone.utc)
        except Exception:
            observed = now
        out.append({
            "source": source,
            "signal_id": guid[:300],
            "symbol": _rss_symbol_for(title, default_symbol),
            "kind": KIND_TRENDING,
            "observed_at": observed,
            "weight": 0.4,
            "payload": {"title": title[:500], "url": link},
        })
    return out


async def poll_industry_rss() -> int:
    """Poll the SemiAnalysis + The Information RSS feeds.

    Failure on either source is logged-and-skipped so a flaky upstream
    doesn't take the other source down with it.
    """
    rows: list[dict[str, Any]] = []
    for source, url, default_symbol in RSS_SOURCES:
        try:
            rows.extend(await _fetch_rss(source, url, default_symbol))
        except Exception as e:
            log.debug("rss source %s fetch failed: %s", source, e)
    return await upsert_evidence(rows)


# --- MLPerf benchmark rounds -------------------------------------------------


# MLCommons publishes each MLPerf round as a public results repository
# under github.com/mlcommons (e.g. inference_results_v4.1,
# training_results_v4.0).  Listing the org's repos and filtering to
# the `<track>_results_v<...>` pattern is a reliable, no-auth signal
# that a new round has dropped — the (heavy, JS-rendered) public
# results page would otherwise need a headless browser to scrape.
_MLPERF_REPO_RE = re.compile(
    r"^(?P<track>inference|training|tiny|mobile|storage|client)_results_v[\w.\-]+$",
    re.IGNORECASE,
)


async def poll_mlperf() -> int:
    """Detect new MLPerf benchmark rounds via the public mlcommons GitHub org.

    Each `<track>_results_v<version>` repo represents one published
    round (inference, training, tiny, mobile, storage, client).  Rows
    are tagged `kind="benchmark"` against the `MLPERF` symbol class.
    """
    url = (
        "https://api.github.com/orgs/mlcommons/repos"
        "?per_page=100&type=public&sort=updated"
    )
    payload = await _http_get_json(url, headers=_github_headers())
    if not isinstance(payload, list):
        return 0
    rows: list[dict[str, Any]] = []
    for item in payload:
        try:
            name = str(item.get("name") or "")
            m = _MLPERF_REPO_RE.match(name)
            if not m:
                continue
            published = (
                item.get("pushed_at")
                or item.get("updated_at")
                or item.get("created_at")
            )
            if not published:
                continue
            ts = datetime.fromisoformat(str(published).replace("Z", "+00:00"))
            track = m.group("track").lower()
            rows.append({
                "source": "mlperf_results",
                "signal_id": name,
                "symbol": "MLPERF",
                "kind": KIND_BENCHMARK,
                "observed_at": ts,
                "weight": 1.0,
                "payload": {
                    "track": track,
                    "repo": item.get("full_name") or f"mlcommons/{name}",
                    "html_url": item.get("html_url"),
                    "description": (item.get("description") or "")[:300],
                },
            })
        except Exception as e:
            log.debug("mlperf parse failed: %s", e)
    return await upsert_evidence(rows)


# --- Earnings calendar (NVDA / TSMC) -----------------------------------------


# Yahoo Finance quoteSummary endpoint exposes the next scheduled
# earnings date for a ticker.  TSMC's US ADR trades as TSM.
_EARNINGS_TICKERS: tuple[tuple[str, str], ...] = (
    ("NVDA", "NVDA"),
    ("TSM",  "TSMC"),
)


# SEC EDGAR Atom feed of recent 8-K filings (earnings press releases
# arrive on item 2.02 8-Ks).  Used as a historical backstop so the
# table is never empty when Yahoo throttles us.
_EDGAR_CIKS: tuple[tuple[str, str], ...] = (
    ("0001045810", "NVDA"),   # NVIDIA Corp
    ("0001046179", "TSMC"),   # Taiwan Semiconductor Mfg Co Ltd
)


_EDGAR_ENTRY_RE = re.compile(r"<entry>(.*?)</entry>", re.DOTALL | re.IGNORECASE)
_EDGAR_FIELD_RE = re.compile(
    r"<(id|title|updated|summary)>(.*?)</\1>", re.DOTALL | re.IGNORECASE
)


# Earnings press releases are 8-Ks reporting under Item 2.02 ("Results
# of Operations and Financial Condition").  Filtering on these markers
# keeps the EDGAR backstop from polluting the cache with unrelated
# 8-Ks (officer departures, M&A, regulation FD disclosures, etc.).
_EDGAR_EARNINGS_TOKENS: tuple[str, ...] = (
    "results of operations",
    "item 2.02",
    "earnings",
    "financial results",
)


def _is_earnings_8k(title: str, summary: str) -> bool:
    blob = f"{title}\n{summary}".lower()
    return any(tok in blob for tok in _EDGAR_EARNINGS_TOKENS)


def _yahoo_headers() -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (compatible; AxiomEvidenceBot/1.0; "
            "+https://axiom.app/bot)"
        ),
        "Accept": "application/json",
    }


def _edgar_headers() -> dict[str, str]:
    # SEC requires a contact-bearing User-Agent.  Operators can override
    # via SEC_EDGAR_UA to satisfy their own fair-access policy.
    return {
        "User-Agent": os.environ.get(
            "SEC_EDGAR_UA",
            "Axiom Evidence Bot evidence@axiom.app",
        ),
        "Accept": "application/atom+xml",
    }


async def _fetch_yahoo_earnings(yticker: str, symbol: str) -> list[dict[str, Any]]:
    url = (
        f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{yticker}"
        "?modules=calendarEvents,earnings"
    )
    payload = await _http_get_json(url, headers=_yahoo_headers())
    if not isinstance(payload, dict):
        return []
    try:
        result = (payload.get("quoteSummary") or {}).get("result") or []
        if not result:
            return []
        block = result[0] or {}
        events = (block.get("calendarEvents") or {}).get("earnings") or {}
        dates = events.get("earningsDate") or []
    except Exception as e:
        log.debug("yahoo earnings shape unexpected for %s: %s", yticker, e)
        return []
    out: list[dict[str, Any]] = []
    for entry in dates:
        raw = entry.get("raw") if isinstance(entry, dict) else entry
        if raw is None:
            continue
        try:
            ts = datetime.fromtimestamp(int(raw), tz=timezone.utc)
        except (TypeError, ValueError):
            continue
        out.append({
            "source": "yahoo_earnings",
            "signal_id": f"{yticker}@{ts.date().isoformat()}",
            "symbol": symbol,
            "kind": KIND_EARNINGS,
            "observed_at": ts,
            "weight": 1.0,
            "payload": {
                "yahoo_ticker": yticker,
                "earnings_date": ts.isoformat(),
                "fmt": entry.get("fmt") if isinstance(entry, dict) else None,
            },
        })
    return out


async def _fetch_edgar_8k(cik: str, symbol: str) -> list[dict[str, Any]]:
    url = (
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
        f"&CIK={cik}&type=8-K&dateb=&owner=include&count=10&output=atom"
    )
    body = await _http_get_text(url, headers=_edgar_headers())
    if not body:
        return []
    out: list[dict[str, Any]] = []
    for entry_match in _EDGAR_ENTRY_RE.finditer(body):
        entry = entry_match.group(1)
        fields: dict[str, str] = {}
        for fm in _EDGAR_FIELD_RE.finditer(entry):
            fields[fm.group(1).lower()] = fm.group(2).strip()
        eid = fields.get("id", "")
        title = fields.get("title", "")
        summary = fields.get("summary", "")
        updated = fields.get("updated", "")
        if not (eid and updated):
            continue
        # Skip non-earnings 8-Ks (officer changes, M&A, Reg-FD, etc.).
        if not _is_earnings_8k(title, summary):
            continue
        try:
            ts = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        except Exception:
            continue
        out.append({
            "source": "edgar_8k",
            "signal_id": eid[:300],
            "symbol": symbol,
            "kind": KIND_EARNINGS,
            "observed_at": ts,
            # Past-filing 8-Ks are weaker signal than a confirmed
            # forward-looking earnings date, so they get a lower weight.
            "weight": 0.6,
            "payload": {
                "cik": cik,
                "title": title[:300],
                "url": eid,
            },
        })
    return out


async def poll_earnings_calendar() -> int:
    """Poll NVDA / TSMC earnings dates from Yahoo Finance with an EDGAR backstop.

    Both sources are tried so a single flaky upstream doesn't leave
    the cache empty.  All rows are tagged `kind="earnings"`.
    """
    rows: list[dict[str, Any]] = []
    for yticker, symbol in _EARNINGS_TICKERS:
        try:
            rows.extend(await _fetch_yahoo_earnings(yticker, symbol))
        except Exception as e:
            log.debug("yahoo earnings fetch failed for %s: %s", yticker, e)
    for cik, symbol in _EDGAR_CIKS:
        try:
            rows.extend(await _fetch_edgar_8k(cik, symbol))
        except Exception as e:
            log.debug("edgar fetch failed for %s: %s", cik, e)
    return await upsert_evidence(rows)


# --- Feed registration -------------------------------------------------------


def register_tech_evidence_feeds() -> None:
    """Register every tech-AI evidence poller with the global feed registry.

    Idempotent: if any feed is already registered (e.g. the lifespan
    fired twice during a hot reload), the duplicate is silently
    skipped so we don't crash the application boot.
    """
    candidates: list[tuple[str, float, Any]] = [
        ("tech_evidence.github_releases", _CADENCE_RELEASE, poll_github_releases),
        ("tech_evidence.hf_trending",     _CADENCE_TRENDING, poll_hf_trending),
        ("tech_evidence.arxiv",           _CADENCE_ARXIV,    poll_arxiv),
        ("tech_evidence.industry_rss",    _CADENCE_RSS,      poll_industry_rss),
        ("tech_evidence.mlperf",          _CADENCE_PLACEHOLDER, poll_mlperf),
        ("tech_evidence.earnings",        _CADENCE_PLACEHOLDER, poll_earnings_calendar),
    ]
    registered = 0
    for name, cadence, poll in candidates:
        try:
            registry.register(Feed(name=name, cadence_seconds=cadence, poll=poll))
            registered += 1
        except ValueError:
            # Already registered — fine.
            continue
    log.info("tech evidence feeds registered: %d", registered)
