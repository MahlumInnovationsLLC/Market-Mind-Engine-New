"""Live-Games chatbot.

Single-shot Q&A over the read-model assembled by
``dashboard.get_live_game_context``.  Uses the existing Anthropic client
wrapper from ``app.swarm.runner`` so we don't duplicate SDK init / key
handling — the same ANTHROPIC_API_KEY that powers the swarm pipeline
powers this endpoint.

The endpoint is intentionally *non-streaming* (one ``messages.create``
call) and returns a small JSON blob the frontend can render directly.
Streaming is a follow-up if the operator wants the typewriter UX.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from . import dashboard
from .config import get_settings


log = logging.getLogger(__name__)


# Operator-tunable model id.  Defaults to the same Sonnet pin the rest of
# the swarm leans on so cost / behaviour stays predictable.
DEFAULT_MODEL = "claude-sonnet-4-5-20250929"
MAX_QUESTION_LEN = 800
MAX_ANSWER_TOKENS = 600


_SYSTEM_PROMPT = """You are Axiom-Predict's live-game analyst chatbot.

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


def _format_context(ctx: dict[str, Any]) -> str:
    """Render the context bundle as a compact JSON block the model can
    quote from cleanly.  Keep it deterministic so prompt caching works."""
    return json.dumps(ctx, default=str, sort_keys=True, separators=(",", ":"))


async def _call_anthropic(system: str, user: str, *, model: str) -> str:
    """Single-shot Anthropic call.  Imported lazily so a missing key only
    surfaces when the endpoint is actually hit, not on module import."""
    # Reuse the singleton client wired by the swarm runner — same SDK
    # instance, same retry config, same key.
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


async def answer_question(
    *,
    market_id: str,
    question: str,
    model: str | None = None,
) -> dict[str, Any]:
    """Build the context, call the model, return the response shape the
    frontend renders.  Raises ``ValueError`` for malformed input and
    ``RuntimeError`` when the LLM key isn't configured.
    """
    market_id = (market_id or "").strip()
    question = (question or "").strip()
    if not market_id:
        raise ValueError("market_id_required")
    if not question:
        raise ValueError("question_required")
    if len(question) > MAX_QUESTION_LEN:
        raise ValueError("question_too_long")

    settings = get_settings()
    if not settings.anthropic_api_key:
        # Surface a clean operator-facing message instead of a stack trace.
        raise RuntimeError("anthropic_key_not_configured")

    ctx = await dashboard.get_live_game_context(market_id)
    has_data = bool(ctx["timeline"] or ctx["swarm_debate"] or ctx["ticks"])

    user_block = (
        f"CONTEXT (JSON):\n{_format_context(ctx)}\n\n"
        f"QUESTION: {question}\n\n"
        "Answer using only the context above.  If the context is empty or "
        "doesn't cover the question, say so explicitly."
    )

    chosen_model = (model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    try:
        answer = await _call_anthropic(_SYSTEM_PROMPT, user_block, model=chosen_model)
    except Exception as e:
        log.warning("live_games_chat anthropic call failed for %s: %s", market_id, e)
        raise

    return {
        "market_id": market_id,
        "answer": answer,
        "model": chosen_model,
        "context_summary": {
            "has_data": has_data,
            "timeline_rows": len(ctx["timeline"]),
            "ticks_rows": len(ctx["ticks"]),
            "has_swarm_debate": ctx["swarm_debate"] is not None,
        },
    }
