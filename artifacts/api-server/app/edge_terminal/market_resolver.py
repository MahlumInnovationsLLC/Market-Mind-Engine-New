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


async def resolve(signal: dict[str, Any]) -> str | None:
    """Best-effort Kalshi ticker resolution.  ``None`` = skip."""
    direct = (signal.get("kalshi_ticker") or "").strip()
    if direct:
        return direct
    matchup = signal.get("matchup") or ""
    selection = signal.get("selection") or ""
    needle = f"{matchup} {selection}".strip()
    if not needle:
        return None
    tokens = _tokenise(needle)
    if not tokens:
        return None
    try:
        rows = await db.fetch(
            """
            SELECT ticker, title FROM markets
            WHERE title ILIKE $1
            ORDER BY updated_at DESC NULLS LAST
            LIMIT 25
            """,
            f"%{tokens[0]}%",
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
    # ("over", "yes", "win") doesn't latch onto an unrelated market.
    if best_score < 2:
        return None
    return best_ticker
