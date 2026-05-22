"""Resolve an Edge Terminal pick to a Kalshi market ticker.

The scraper can emit ``kalshi_ticker`` directly when the Edge Terminal
DOM exposes it; that path returns immediately.  Otherwise we fall back
to a Kalshi-side title match — same path the rest of the codebase
already uses via :func:`app.kalshi.market_titles.resolve_titles`,
but in reverse (title-substring → ticker) by scanning the cached
``markets`` table.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from .. import db


log = logging.getLogger(__name__)


_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _tokenise(s: str | None) -> list[str]:
    if not s:
        return []
    return [t.lower() for t in _TOKEN_RE.findall(s) if len(t) >= 2]


async def _ticker_exists(ticker: str) -> bool:
    """Cheap check that ``ticker`` is in the cached ``markets`` table.

    Returns ``True`` on a DB error so a transient query failure
    doesn't cause us to silently drop an otherwise-valid signal —
    the downstream lifecycle / dedupe gate is the authoritative
    rejector for stale tickers.
    """
    try:
        v = await db.fetchval(
            "SELECT 1 FROM markets WHERE ticker = $1 LIMIT 1",
            ticker,
        )
    except Exception as e:  # pragma: no cover — defensive
        log.warning("ticker existence check failed for %r: %s", ticker, e)
        return True
    return v is not None


async def resolve(signal: dict[str, Any]) -> str | None:
    """Best-effort Kalshi ticker resolution.  ``None`` = skip."""
    direct = (signal.get("kalshi_ticker") or "").strip()
    if direct:
        # Validate the scraper-supplied ticker against the cached
        # markets table — a stale / unknown ticker would otherwise
        # blow up paper.place_order and bury the signal under a
        # 'failed' status that the daily dedupe key prevents from
        # ever being re-attempted.
        if await _ticker_exists(direct):
            return direct
        log.info(
            "edge_terminal: scraped ticker %r not found in markets cache; "
            "falling through to title match",
            direct,
        )
    matchup = signal.get("matchup") or ""
    selection = signal.get("selection") or ""
    needle = f"{matchup} {selection}".strip()
    if not needle:
        return None
    tokens = _tokenise(needle)
    if not tokens:
        return None
    # Anchor the ILIKE on the LONGEST unique token rather than the
    # first one.  The first token is often a short stopword that
    # passed the 2-char minimum ("vs", "at", "on", "to") and would
    # otherwise dump 25 unrelated markets into the scorer.
    unique_tokens = sorted(set(tokens), key=lambda t: (-len(t), t))
    anchor = unique_tokens[0]
    try:
        rows = await db.fetch(
            """
            SELECT ticker, title FROM markets
            WHERE title ILIKE $1
            ORDER BY updated_at DESC NULLS LAST
            LIMIT 25
            """,
            f"%{anchor}%",
        )
    except Exception as e:
        log.warning("kalshi market lookup failed for %r: %s", needle, e)
        return None
    if not rows:
        return None
    # Score by overlap of *unique* significant tokens so repeated team
    # names on the signal side can't inflate the match against a title
    # containing just one of those words.
    sig_tokens = set(tokens)
    best_ticker: str | None = None
    best_score = 0
    for r in rows:
        title = r.get("title") if isinstance(r, dict) else r["title"]
        ticker = r.get("ticker") if isinstance(r, dict) else r["ticker"]
        title_tokens = set(_tokenise(title))
        score = len(sig_tokens & title_tokens)
        if score > best_score:
            best_score = score
            best_ticker = str(ticker)
    # Require at least 2 token overlaps so a single common word
    # ("over", "yes", "win") doesn't latch onto an unrelated market —
    # but adapt to verticals whose picks naturally produce <2 unique
    # tokens (crypto: "BTC", forex: "EUR") where requiring 2 would
    # always skip.
    threshold = 2 if len(sig_tokens) >= 2 else 1
    if best_score < threshold:
        return None
    return best_ticker
