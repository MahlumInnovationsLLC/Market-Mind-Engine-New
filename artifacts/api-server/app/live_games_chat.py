"""Live-Games chatbot.

Single-shot Q&A over the read-model assembled by
``dashboard.get_live_game_context``.  Dispatches to OpenAI, Anthropic
or Google Gemini based on the requested ``model`` id, reusing the lazy
SDK clients from ``app.swarm.runner`` so we never duplicate key handling
or pay the import cost for SDKs we don't use.

The endpoint is intentionally *non-streaming* (one request per call)
and returns a small JSON blob the frontend can render directly.
Streaming is a follow-up if the operator wants the typewriter UX.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from . import dashboard
from .config import get_settings


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Model catalog
# ---------------------------------------------------------------------------
#
# (provider, model_id, label).  Order is the order we surface in the
# frontend picker.  ``provider`` is the short key used to find the right
# API key in ``Settings`` and the right SDK in ``swarm.runner``.
#
# Only current-generation flagships — the swarm runner already
# validates these IDs against the live SDKs and the pricing table in
# ``app/swarm/personas.py``, so adding a new entry here is safe as
# long as it's tracked there too.

AVAILABLE_MODELS: tuple[tuple[str, str, str], ...] = (
    ("anthropic", "claude-sonnet-4-5-20250929", "Claude Sonnet 4.5"),
    ("openai",    "gpt-5",                      "GPT-5"),
    ("google",    "gemini-2.5-pro",             "Gemini 2.5 Pro"),
)

DEFAULT_MODEL = "claude-sonnet-4-5-20250929"
MAX_QUESTION_LEN = 800
MAX_ANSWER_TOKENS = 600


_LIVE_SYSTEM_PROMPT = """You are Axiom-Predict's live-game analyst chatbot.

You answer questions about a SINGLE in-progress sports market using ONLY
the JSON context block provided below.  The context contains:

  * timeline   — most recent live ticks for this market (newest first):
                 each entry has period, score, time_remaining, market_price,
                 updated_prob (model probability), pre_game_prob,
                 current_edge (model_prob - market_price).
  * swarm_debate — most recent 6-persona LLM debate verdict for this market
                 if one exists, including consensus_probability, agreement_rate,
                 approved (True = the swarm cleared the trade) and
                 recommended_action.
  * ticks      — most recent Kalshi cached price ticks (cents, 0..100).

Hard rules:
  1. NEVER invent scores, probabilities, prices, debate verdicts or any
     other number that isn't visibly present in the context block.
  2. If the user asks about something the context doesn't cover, say so
     plainly: "I don't have that data in this game's feed."
  3. When you give a price/probability, ALWAYS quote the source field
     (e.g. "model_prob = 0.79 (latest tick at 02:15Z)").
  4. Be concise: 2-4 short paragraphs max, plain text — no markdown
     headers, no bullet lists unless the question explicitly asks for one.
  5. NEVER provide gambling advice; describe what the model and the swarm
     think, do NOT instruct the user to bet.
"""


_UPCOMING_SYSTEM_PROMPT = """You are Axiom-Predict's pre-game analyst chatbot.

You answer questions about a SINGLE upcoming sports matchup using ONLY
the JSON context block provided below.  The context contains:

  * schedule   — league, home_team, away_team, scheduled (UTC), venue,
                 status, plus any structured payload from the schedule feed.
  * player_status — per-team injury list and snap-count usage when
                 available (NFL has the richest data; soccer leagues
                 carry missing-player notes; other leagues may have only
                 raw sportradar_injuries rows).
  * intangibles — recent intangibles_reports rows for this matchup with
                 their fair_prob, edge_pct and free-form payload notes.
  * recent_swarm_debate — most recent pre-game swarm debate verdict
                 for this matchup if one has run.
  * ticks      — most recent Kalshi cached price ticks for any matched
                 market (cents, 0..100), so you can describe the line
                 movement leading into the game.

Hard rules:
  1. NEVER invent injuries, news items, line movements, debate verdicts
     or any other number that isn't visibly present in the context.
  2. If the user asks about something the context doesn't cover (e.g.
     a specific player who isn't in the injury list), say so plainly:
     "I don't have that in the pre-game feed for this matchup."
  3. Quote source fields when citing numbers (e.g.
     "fair_prob = 0.62 (intangibles_reports at 18:42Z)").
  4. Distinguish current line from model fair value when both are
     present, and call out edge direction (model > market = YES edge).
  5. Be concise: 2-4 short paragraphs max, plain text.
  6. NEVER provide gambling advice; describe what the data shows, do
     NOT instruct the user to bet.
"""


# Sentinel prefix the frontend uses to flag an upcoming-game context
# (vs. a kalshi market_id which is the live-game default).  Format:
# ``sched:<league>:<game_id>``.
UPCOMING_KEY_PREFIX = "sched:"


# ---------------------------------------------------------------------------
# Catalog helpers
# ---------------------------------------------------------------------------


def _provider_for(model_id: str) -> str:
    """Return the provider key for a given model id, or empty string if
    we don't recognise it.  Falls back to a prefix match so any future
    ``gpt-*`` / ``claude-*`` / ``gemini-*`` model works without a code
    change to the catalog."""
    for provider, mid, _ in AVAILABLE_MODELS:
        if mid == model_id:
            return provider
    if model_id.startswith("gpt-") or model_id.startswith("o1-") or model_id.startswith("o3-"):
        return "openai"
    if model_id.startswith("claude-"):
        return "anthropic"
    if model_id.startswith("gemini-"):
        return "google"
    return ""


def _provider_key_present(provider: str) -> bool:
    settings = get_settings()
    if provider == "openai":
        return bool(settings.openai_api_key)
    if provider == "anthropic":
        return bool(settings.anthropic_api_key)
    if provider == "google":
        return bool(settings.google_api_key)
    return False


def list_models() -> list[dict[str, Any]]:
    """Return the catalog with an ``available`` flag per row, plus the
    default-model id (which is the first available row in catalog order
    so the frontend doesn't have to guess)."""
    rows: list[dict[str, Any]] = []
    for provider, mid, label in AVAILABLE_MODELS:
        rows.append({
            "provider": provider,
            "model_id": mid,
            "label": label,
            "available": _provider_key_present(provider),
        })
    return rows


def default_available_model() -> str | None:
    """First catalog entry whose provider key is configured."""
    for row in list_models():
        if row["available"]:
            return row["model_id"]
    return None


# ---------------------------------------------------------------------------
# Provider dispatch
# ---------------------------------------------------------------------------


def _format_context(ctx: dict[str, Any]) -> str:
    """Render the context bundle as a compact JSON block the model can
    quote from cleanly.  Keep it deterministic so prompt caching works."""
    return json.dumps(ctx, default=str, sort_keys=True, separators=(",", ":"))


async def _call_anthropic(system: str, user: str, *, model: str) -> str:
    from .swarm.runner import _get_anthropic  # noqa: PLC0415

    client = _get_anthropic()
    msg = await client.messages.create(
        model=model,
        system=system,
        max_tokens=MAX_ANSWER_TOKENS,
        temperature=0.2,
        messages=[{"role": "user", "content": user}],
    )
    chunks: list[str] = []
    for block in msg.content:
        text = getattr(block, "text", None)
        if text:
            chunks.append(text)
    return "".join(chunks).strip() or "(no response)"


async def _call_openai(system: str, user: str, *, model: str) -> str:
    from .swarm.runner import _get_openai  # noqa: PLC0415

    client = _get_openai()
    resp = await client.chat.completions.create(
        model=model,
        max_tokens=MAX_ANSWER_TOKENS,
        temperature=0.2,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    choice = resp.choices[0] if resp.choices else None
    text = (choice.message.content or "") if choice else ""
    return text.strip() or "(no response)"


async def _call_google(system: str, user: str, *, model: str) -> str:
    """Google GenAI ``generate_content`` is sync; wrap through to_thread.

    Mirrors the helper in ``swarm.runner``: the v1.x google-genai SDK
    exposes the model methods on ``client.models`` rather than on a
    handle, and accepts ``system_instruction`` via the ``config`` kwarg."""
    from .swarm.runner import _get_google  # noqa: PLC0415

    client = _get_google()

    def _generate() -> Any:
        return client.models.generate_content(
            model=model,
            contents=user,
            config={
                "system_instruction": system,
                "temperature": 0.2,
                "max_output_tokens": MAX_ANSWER_TOKENS,
            },
        )

    resp = await asyncio.to_thread(_generate)
    text = (getattr(resp, "text", "") or "").strip()
    return text or "(no response)"


async def _dispatch(provider: str, system: str, user: str, *, model: str) -> str:
    if provider == "anthropic":
        return await _call_anthropic(system, user, model=model)
    if provider == "openai":
        return await _call_openai(system, user, model=model)
    if provider == "google":
        return await _call_google(system, user, model=model)
    raise ValueError(f"unknown_provider:{provider}")


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


async def answer_question(
    *,
    market_id: str,
    question: str,
    model: str | None = None,
) -> dict[str, Any]:
    """Build the context, call the chosen model, return the response
    shape the frontend renders.  Raises ``ValueError`` for malformed
    input and ``RuntimeError`` when the chosen provider's key isn't
    configured."""
    market_id = (market_id or "").strip()
    question = (question or "").strip()
    if not market_id:
        raise ValueError("market_id_required")
    if not question:
        raise ValueError("question_required")
    if len(question) > MAX_QUESTION_LEN:
        raise ValueError("question_too_long")

    explicit_model = bool((model or "").strip())
    chosen_model = (model or "").strip() or DEFAULT_MODEL
    provider = _provider_for(chosen_model)
    if not provider:
        raise ValueError(f"unknown_model:{chosen_model}")

    if not _provider_key_present(provider):
        # Two paths here, deliberately:
        #   * Explicit pick — operator chose this model, so do NOT
        #     silently substitute.  Surface the misconfiguration so
        #     they can either pick a different model or wire the key.
        #   * Auto mode (model omitted) — fall back to any configured
        #     model so the chatbot stays usable out of the box.
        if explicit_model:
            raise RuntimeError(f"{provider}_key_not_configured")
        fallback = default_available_model()
        if fallback is None:
            raise RuntimeError(f"{provider}_key_not_configured")
        chosen_model = fallback
        provider = _provider_for(chosen_model)

    is_upcoming = market_id.startswith(UPCOMING_KEY_PREFIX)
    if is_upcoming:
        ctx = await dashboard.get_upcoming_game_context(market_id)
        has_data = bool(
            ctx.get("schedule")
            or ctx.get("player_status")
            or ctx.get("intangibles")
            or ctx.get("ticks")
            or ctx.get("recent_swarm_debate")
        )
        system_prompt = _UPCOMING_SYSTEM_PROMPT
        ctx_summary = {
            "kind": "upcoming",
            "has_data": has_data,
            "has_schedule": ctx.get("schedule") is not None,
            "intangibles_rows": len(ctx.get("intangibles") or []),
            "ticks_rows": len(ctx.get("ticks") or []),
            "has_swarm_debate": ctx.get("recent_swarm_debate") is not None,
            "has_player_status": ctx.get("player_status") is not None,
        }
    else:
        ctx = await dashboard.get_live_game_context(market_id)
        has_data = bool(ctx["timeline"] or ctx["swarm_debate"] or ctx["ticks"])
        system_prompt = _LIVE_SYSTEM_PROMPT
        ctx_summary = {
            "kind": "live",
            "has_data": has_data,
            "timeline_rows": len(ctx["timeline"]),
            "ticks_rows": len(ctx["ticks"]),
            "has_swarm_debate": ctx["swarm_debate"] is not None,
        }

    user_block = (
        f"CONTEXT (JSON):\n{_format_context(ctx)}\n\n"
        f"QUESTION: {question}\n\n"
        "Answer using only the context above.  If the context is empty or "
        "doesn't cover the question, say so explicitly."
    )

    try:
        answer = await _dispatch(provider, system_prompt, user_block, model=chosen_model)
    except Exception as e:
        log.warning(
            "live_games_chat dispatch failed for %s/%s: %s",
            provider, market_id, e,
        )
        raise

    return {
        "market_id": market_id,
        "answer": answer,
        "model": chosen_model,
        "provider": provider,
        "context_summary": ctx_summary,
    }
