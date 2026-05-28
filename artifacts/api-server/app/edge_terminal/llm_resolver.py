"""LLM-backed fallback resolver for Kalshi ticker lookup.

The deterministic ``market_resolver`` scores candidate Kalshi markets
on team-token overlap + numeric-line agreement and falls through if no
candidate clears ``_MIN_SCORE``. That misses real matches whenever:

* The scraper writes a team name we don't have in ``_TEAM_ALIASES``
  (NCAA basketball, foreign soccer leagues, niche tennis events).
* Kalshi's title uses a venue/nickname instead of the city
  ("Lightning" vs "Tampa Bay").
* The series prefix mapping is correct but the line number rounds
  differently than the heuristic expects.

When the token resolver fails we still have a *list of candidates from
the right Kalshi series prefix*. This module asks an LLM to pick the
best ticker from that list (or ``"none"`` if none match), and caches
the answer in ``edge_terminal_llm_resolver_cache`` so repeat polls
don't re-pay for the same signal.

Public API
----------
``await llm_pick(signal, candidates) -> tuple[str | None, str]``
    Returns ``(ticker_or_None, reason)``. ``ticker_or_None`` is
    ``None`` if the LLM declined or if no API key is configured.
    ``reason`` is a short human-readable string for the dashboard.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from typing import Any

from .. import db
from ..config import get_settings


log = logging.getLogger(__name__)


# Cheap + fast — good enough for ticker disambiguation. Operator can
# override at the env layer without a redeploy.
_MODEL = os.environ.get("EDGE_TERMINAL_LLM_RESOLVER_MODEL", "gpt-4o-mini")
# Hard cap on how many candidates we paste into the prompt. Above ~15
# the model starts hallucinating tickers it didn't see.
_MAX_CANDIDATES = 15
# Hard timeout (s) — the resolver runs inside the poller's per-signal
# budget so we must not block forever.
_TIMEOUT_S = 8.0


_TICKER_RE = re.compile(r"\bKX[A-Z0-9_\-]+\b")


def _signal_key(signal: dict[str, Any]) -> str:
    """Stable hash of (vertical, matchup, selection, pick_type)."""
    vertical = (signal.get("vertical") or "").lower().strip()
    matchup = (signal.get("matchup") or "").lower().strip()
    selection = (signal.get("selection") or "").lower().strip()
    pick_type = (signal.get("pick_type") or "moneyline").lower().strip()
    blob = f"{vertical}|{matchup}|{selection}|{pick_type}".encode("utf-8")
    return hashlib.sha1(blob).hexdigest()


async def _cached(key: str) -> tuple[str | None, str] | None:
    try:
        row = await db.fetchrow(
            "SELECT ticker, reason FROM edge_terminal_llm_resolver_cache "
            "WHERE signal_key=$1",
            key,
        )
    except Exception as e:
        log.info("llm_resolver cache read failed: %s", e)
        return None
    if row is None:
        return None
    return (row["ticker"], f"LLM cache: {row['reason']}")


async def _store(
    key: str,
    *,
    ticker: str | None,
    model: str,
    score: int | None,
    reason: str,
) -> None:
    try:
        await db.execute(
            """
            INSERT INTO edge_terminal_llm_resolver_cache
                (signal_key, ticker, model, score, reason, resolved_at)
            VALUES ($1, $2, $3, $4, $5, now())
            ON CONFLICT (signal_key) DO UPDATE
              SET ticker = EXCLUDED.ticker,
                  model  = EXCLUDED.model,
                  score  = EXCLUDED.score,
                  reason = EXCLUDED.reason,
                  resolved_at = EXCLUDED.resolved_at
            """,
            key, ticker, model, score, reason,
        )
    except Exception as e:
        log.info("llm_resolver cache write failed: %s", e)


def _build_prompt(signal: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    """Compact instruction; LLM must respond strict JSON."""
    sport = (signal.get("vertical") or "?").upper()
    pick_type = (signal.get("pick_type") or "moneyline").lower()
    matchup = signal.get("matchup") or "?"
    selection = signal.get("selection") or "?"
    side = (signal.get("side") or "YES").upper()
    line = signal.get("market_price")
    lines = [
        "You map a sports/betting pick to a Kalshi market ticker.",
        "",
        f"SPORT: {sport}",
        f"PICK_TYPE: {pick_type}",
        f"MATCHUP: {matchup}",
        f"SELECTION: {selection}",
        f"SIDE: {side}",
    ]
    if isinstance(line, (int, float)):
        lines.append(f"MARKET_PRICE: {line}")
    lines += [
        "",
        "CANDIDATES (Kalshi markets in the right series):",
    ]
    for i, c in enumerate(candidates[:_MAX_CANDIDATES], start=1):
        score = c.get("score")
        title = (c.get("title") or "").replace("\n", " ").strip()[:160]
        lines.append(
            f"  {i}. ticker={c.get('ticker')!r}  score={score}  title={title!r}"
        )
    lines += [
        "",
        "Pick the ONE ticker that best represents the PICK above. If none",
        "of the candidates match (different teams, wrong line, wrong",
        "side, totally unrelated), return ticker=null.",
        "",
        "Respond with STRICT JSON, no prose, no fences:",
        '  {"ticker": "<KX...>" | null, "confidence": 1-5, "reason": "<short>"}',
    ]
    return "\n".join(lines)


def _extract_json(text: str) -> dict[str, Any] | None:
    """LLMs sometimes wrap JSON in fences or chatter — be lenient."""
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = re.sub(r"^json\s*", "", text, flags=re.IGNORECASE)
    # Find the outermost {...} block.
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def _enabled() -> bool:
    raw = os.environ.get("EDGE_TERMINAL_LLM_RESOLVER_ENABLED", "1").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    return bool(get_settings().openai_api_key)


async def _call_openai(prompt: str) -> str | None:
    """Single completion call. Returns raw text or None on any failure."""
    try:
        # Reuse the same lazy client wiring as the swarm.
        from ..swarm.runner import _get_openai  # type: ignore

        client = _get_openai()
    except Exception as e:
        log.info("llm_resolver: openai client unavailable: %s", e)
        return None

    try:
        # ``responses.create`` would be nicer but chat.completions is what
        # the rest of the codebase already depends on, so keep one path.
        import asyncio

        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a strict ticker matcher. "
                            "Reply with JSON only."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                max_tokens=120,
            ),
            timeout=_TIMEOUT_S,
        )
    except Exception as e:
        log.info("llm_resolver: openai call failed: %s", e)
        return None
    try:
        return resp.choices[0].message.content  # type: ignore[index]
    except Exception:
        return None


async def llm_pick(
    signal: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> tuple[str | None, str]:
    """Disambiguate a Kalshi ticker via LLM, with persistent caching.

    ``candidates`` is the full scored list from the deterministic
    resolver (even rows with score=0). We send the top
    ``_MAX_CANDIDATES`` to the LLM and validate the returned ticker
    exists in the candidate set — protects against hallucination.
    """
    if not _enabled():
        return (None, "llm fallback disabled (no OPENAI_API_KEY or opted out)")
    if not candidates:
        return (None, "no candidates to disambiguate")

    key = _signal_key(signal)
    cached = await _cached(key)
    if cached is not None:
        return cached

    # Sort by score desc so the LLM sees the strongest leads first.
    cands_sorted = sorted(
        candidates,
        key=lambda c: -int(c.get("score") or 0),
    )[:_MAX_CANDIDATES]
    valid_tickers = {str(c.get("ticker")) for c in cands_sorted if c.get("ticker")}

    prompt = _build_prompt(signal, cands_sorted)
    raw = await _call_openai(prompt)
    if raw is None:
        return (None, "llm call failed or timed out")

    parsed = _extract_json(raw)
    if parsed is None:
        log.info("llm_resolver: could not parse JSON from %r", raw[:200])
        await _store(
            key, ticker=None, model=_MODEL,
            score=None, reason="unparseable LLM response",
        )
        return (None, "llm response not JSON")

    chosen = parsed.get("ticker")
    confidence = parsed.get("confidence")
    reason = (parsed.get("reason") or "")[:200]
    if chosen in (None, "", "null"):
        await _store(
            key, ticker=None, model=_MODEL,
            score=int(confidence) if isinstance(confidence, (int, float)) else None,
            reason=f"declined: {reason}",
        )
        return (None, f"llm declined: {reason}")

    chosen_s = str(chosen).strip()
    # Hard guard against hallucination — only accept tickers from the
    # candidate set we showed it.
    if chosen_s not in valid_tickers:
        log.info(
            "llm_resolver: hallucinated ticker %r not in candidates",
            chosen_s,
        )
        await _store(
            key, ticker=None, model=_MODEL,
            score=int(confidence) if isinstance(confidence, (int, float)) else None,
            reason=f"hallucinated ticker {chosen_s!r}",
        )
        return (None, f"llm picked unknown ticker {chosen_s!r}")

    await _store(
        key, ticker=chosen_s, model=_MODEL,
        score=int(confidence) if isinstance(confidence, (int, float)) else None,
        reason=reason or "matched",
    )
    return (chosen_s, f"llm matched (confidence={confidence}): {reason}")
