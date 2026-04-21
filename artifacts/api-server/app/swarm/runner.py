"""Swarm orchestrator: runs the 6 personas in parallel against OpenAI +
Anthropic, then the moderator round, then the gatekeeper veto.  Persists
to `swarm_debates` (one row) + `llm_predictions` (one row per LLM call,
including moderator + gatekeeper) and live-streams events on the
`llm_log_hub`.

Direct OpenAI + Anthropic SDKs are used per task spec — NOT Replit AI
Integrations.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

from .. import db
from ..config import get_settings
from .personas import (
    GATEKEEPER,
    MODERATOR,
    PERSONAS,
    Persona,
    estimate_cost,
    normalize_aggression_mode,
)
from .streams import llm_log_hub
from .weights import get_persona_weight


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lazy SDK clients (instantiated on first use so module import never fails
# when keys are missing — the endpoint surfaces a clean 503 instead).
# ---------------------------------------------------------------------------
_openai_client: Any = None
_anthropic_client: Any = None


def _get_openai():
    global _openai_client
    if _openai_client is None:
        from openai import AsyncOpenAI  # type: ignore

        key = get_settings().openai_api_key
        if not key:
            raise RuntimeError("OPENAI_API_KEY not configured")
        _openai_client = AsyncOpenAI(api_key=key)
    return _openai_client


def _get_anthropic():
    global _anthropic_client
    if _anthropic_client is None:
        from anthropic import AsyncAnthropic  # type: ignore

        key = get_settings().anthropic_api_key
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY not configured")
        _anthropic_client = AsyncAnthropic(api_key=key)
    return _anthropic_client


# ---------------------------------------------------------------------------
# JSON extraction (LLMs sometimes wrap output in fences or chatter)
# ---------------------------------------------------------------------------
_JSON_OBJ = re.compile(r"\{[^{}]*\}", re.DOTALL)


def _extract_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    try:
        return json.loads(text)
    except Exception:
        pass
    # Fall back to the LAST balanced {...} we can find.
    matches = list(re.finditer(r"\{.*\}", text, re.DOTALL))
    for m in reversed(matches):
        try:
            return json.loads(m.group(0))
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Per-provider streaming helpers.  Each yields token deltas via the hub
# and returns (full_text, input_tokens, output_tokens).
# ---------------------------------------------------------------------------
async def _stream_openai(
    persona_id: str, model_id: str, system: str, user: str
) -> tuple[str, int, int]:
    client = _get_openai()
    chunks: list[str] = []
    in_tok = out_tok = 0
    try:
        stream = await client.chat.completions.create(
            model=model_id,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.4,
            max_tokens=600,
            stream=True,
            stream_options={"include_usage": True},
        )
        async for event in stream:
            if event.choices:
                delta = event.choices[0].delta.content or ""
                if delta:
                    chunks.append(delta)
                    await llm_log_hub.publish(
                        {"type": "persona_delta", "persona": persona_id, "text": delta}
                    )
            if getattr(event, "usage", None):
                in_tok = int(getattr(event.usage, "prompt_tokens", 0) or 0)
                out_tok = int(getattr(event.usage, "completion_tokens", 0) or 0)
    finally:
        pass
    return "".join(chunks), in_tok, out_tok


async def _stream_anthropic(
    persona_id: str, model_id: str, system: str, user: str
) -> tuple[str, int, int]:
    client = _get_anthropic()
    chunks: list[str] = []
    in_tok = out_tok = 0
    async with client.messages.stream(
        model=model_id,
        system=system,
        max_tokens=600,
        temperature=0.4,
        messages=[{"role": "user", "content": user}],
    ) as stream:
        async for delta in stream.text_stream:
            if delta:
                chunks.append(delta)
                await llm_log_hub.publish(
                    {"type": "persona_delta", "persona": persona_id, "text": delta}
                )
        final = await stream.get_final_message()
        if final.usage:
            in_tok = int(final.usage.input_tokens or 0)
            out_tok = int(final.usage.output_tokens or 0)
    return "".join(chunks), in_tok, out_tok


async def _call_persona(
    persona: Persona, user_prompt: str
) -> dict[str, Any]:
    """Run one persona end-to-end and return a verdict dict."""
    started = time.perf_counter()
    await llm_log_hub.publish(
        {"type": "persona_start", "persona": persona.id, "model": persona.model_id}
    )
    error: str | None = None
    text = ""
    in_tok = out_tok = 0
    try:
        if persona.provider == "openai":
            text, in_tok, out_tok = await _stream_openai(
                persona.id, persona.model_id, persona.system_prompt, user_prompt
            )
        else:
            text, in_tok, out_tok = await _stream_anthropic(
                persona.id, persona.model_id, persona.system_prompt, user_prompt
            )
    except Exception as e:  # provider error — record but don't crash the swarm
        error = f"{type(e).__name__}: {e}"
        log.warning("persona %s failed: %s", persona.id, error)

    latency_ms = int((time.perf_counter() - started) * 1000)
    parsed = _extract_json(text) or {}
    verdict = {
        "persona": persona.id,
        "provider": persona.provider,
        "model": persona.model_id,
        "probability": _coerce_prob(parsed.get("probability")),
        "confidence": _coerce_prob(parsed.get("confidence")),
        "lean": _coerce_lean(parsed.get("lean")),
        "reasoning": (parsed.get("reasoning") or "").strip()[:1000] or text[-500:],
        "raw_text": text,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "cost": estimate_cost(persona.model_id, in_tok, out_tok),
        "latency_ms": latency_ms,
        "error": error,
    }
    await llm_log_hub.publish(
        {
            "type": "persona_done",
            "persona": persona.id,
            "verdict": {
                "probability": verdict["probability"],
                "confidence": verdict["confidence"],
                "lean": verdict["lean"],
                "reasoning": verdict["reasoning"],
                "latency_ms": latency_ms,
                "error": error,
            },
        }
    )
    return verdict


def _coerce_prob(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return max(0.0, min(1.0, f))


def _coerce_lean(v: Any) -> str:
    s = str(v or "").strip().upper()
    if s in ("YES", "BUY_YES"):
        return "YES"
    if s in ("NO", "BUY_NO"):
        return "NO"
    return "PASS"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
async def run_debate(candidate: dict[str, Any]) -> dict[str, Any]:
    """Run a full swarm debate against the candidate trade.

    `candidate` keys:
      - game_id (str, required)
      - matchup (str)
      - league (str, e.g. "MLB")
      - market_type (str, default "moneyline")
      - side (str, e.g. "home"/"away"/"YES"/"NO")
      - current_price (float in [0,1], the market YES probability)
      - context (str, optional free-text market/game context)
    Returns the persisted swarm_debates row as a dict, plus the full
    transcript under `transcript`.
    """
    started = time.perf_counter()
    game_id = str(candidate.get("game_id") or "").strip()
    if not game_id:
        raise ValueError("candidate.game_id is required")
    matchup = str(candidate.get("matchup") or "").strip() or None
    league = str(candidate.get("league") or "").strip() or None
    market_type = (str(candidate.get("market_type") or "moneyline").strip() or "moneyline")
    side = str(candidate.get("side") or "YES").strip()
    current_price = _coerce_prob(candidate.get("current_price"))
    ctx = str(candidate.get("context") or "").strip()
    aggression_mode = normalize_aggression_mode(candidate.get("aggression_mode"))

    user_prompt = (
        f"CANDIDATE TRADE\n"
        f"  game_id         : {game_id}\n"
        f"  matchup         : {matchup or '(unknown)'}\n"
        f"  league          : {league or '(unknown)'}\n"
        f"  market_type     : {market_type}\n"
        f"  side            : {side}\n"
        f"  current YES price: "
        f"{'%.4f' % current_price if current_price is not None else '(unknown)'}\n"
        f"  aggression_mode : {aggression_mode}\n"
        f"\nCONTEXT\n{ctx or '(no additional context provided)'}"
    )

    await llm_log_hub.publish(
        {
            "type": "debate_start",
            "game_id": game_id,
            "matchup": matchup,
            "league": league,
            "market_type": market_type,
            "aggression_mode": aggression_mode,
        }
    )

    # 1) Six personas in parallel.
    persona_tasks = [_call_persona(p, user_prompt) for p in PERSONAS]
    verdicts = await asyncio.gather(*persona_tasks)

    # 2) Resolve weights from swarm_analyst_metrics.
    for v in verdicts:
        v["weight"] = await get_persona_weight(v["persona"], league, market_type)

    # 3) Moderator synthesizes weighted consensus.
    mod_input = (
        user_prompt
        + f"\n\nAGGRESSION_MODE: {aggression_mode}\n"
        + "PERSONA VERDICTS (with Brier-derived weights):\n"
        + json.dumps(
            [
                {
                    "persona": v["persona"],
                    "weight": round(v["weight"], 4),
                    "probability": v["probability"],
                    "confidence": v["confidence"],
                    "lean": v["lean"],
                    "reasoning": v["reasoning"],
                }
                for v in verdicts
            ],
            indent=2,
        )
    )
    mod_verdict = await _call_persona(MODERATOR, mod_input)
    mod_parsed = _extract_json(mod_verdict["raw_text"]) or {}
    consensus = _coerce_prob(mod_parsed.get("consensus_probability"))
    band_low = _coerce_prob(mod_parsed.get("confidence_band_low"))
    band_high = _coerce_prob(mod_parsed.get("confidence_band_high"))
    agreement = _coerce_prob(mod_parsed.get("agreement_rate"))
    mod_action = str(mod_parsed.get("recommended_action") or "PASS").upper()

    # Fallbacks if moderator returned malformed JSON: compute a
    # weighted mean over personas that returned a probability.
    if consensus is None:
        ws = [(v["weight"], v["probability"]) for v in verdicts if v["probability"] is not None]
        if ws:
            tot_w = sum(w for w, _ in ws)
            consensus = sum(w * p for w, p in ws) / tot_w if tot_w > 0 else None
    if agreement is None and consensus is not None:
        target_lean = "YES" if consensus >= 0.5 else "NO"
        agreement = sum(1 for v in verdicts if v["lean"] == target_lean) / len(verdicts)

    await llm_log_hub.publish(
        {
            "type": "moderator_done",
            "consensus_probability": consensus,
            "agreement_rate": agreement,
            "recommended_action": mod_action,
        }
    )

    # 4) Gatekeeper veto.
    gk_input = (
        user_prompt
        + f"\n\nAGGRESSION_MODE: {aggression_mode}\n"
        + "MODERATOR SYNTHESIS:\n"
        + json.dumps(
            {
                "consensus_probability": consensus,
                "confidence_band_low": band_low,
                "confidence_band_high": band_high,
                "agreement_rate": agreement,
                "recommended_action": mod_action,
                "synthesis": mod_parsed.get("synthesis"),
            },
            indent=2,
        )
        + "\n\nPERSONA LEANS: "
        + json.dumps({v["persona"]: v["lean"] for v in verdicts})
    )
    gk_verdict = await _call_persona(GATEKEEPER, gk_input)
    gk_parsed = _extract_json(gk_verdict["raw_text"]) or {}
    approved = bool(gk_parsed.get("approved"))
    final_action = str(gk_parsed.get("recommended_action") or mod_action or "PASS").upper()
    if final_action not in ("BUY_YES", "BUY_NO", "PASS"):
        final_action = "PASS"
    if not approved:
        final_action = "PASS"

    await llm_log_hub.publish(
        {
            "type": "gatekeeper_done",
            "approved": approved,
            "recommended_action": final_action,
            "veto_reason": gk_parsed.get("veto_reason"),
        }
    )

    # 5) Aggregate cost + latency, persist.
    total_cost = sum(v["cost"] for v in verdicts) + mod_verdict["cost"] + gk_verdict["cost"]
    total_latency_ms = int((time.perf_counter() - started) * 1000)

    debate_id = await _persist_debate(
        game_id=game_id,
        matchup=matchup,
        league=league,
        market_type=market_type,
        verdicts=verdicts,
        moderator=mod_verdict,
        moderator_parsed=mod_parsed,
        gatekeeper=gk_verdict,
        gatekeeper_parsed=gk_parsed,
        consensus=consensus,
        band_low=band_low,
        band_high=band_high,
        agreement=agreement,
        approved=approved,
        recommended_action=final_action,
        total_cost=total_cost,
        total_latency_ms=total_latency_ms,
        candidate=candidate,
        aggression_mode=aggression_mode,
    )

    await llm_log_hub.publish(
        {
            "type": "debate_complete",
            "debate_id": debate_id,
            "approved": approved,
            "recommended_action": final_action,
            "consensus_probability": consensus,
            "cost_estimate": total_cost,
            "latency_ms": total_latency_ms,
        }
    )

    return {
        "debate_id": debate_id,
        "game_id": game_id,
        "matchup": matchup,
        "league": league,
        "market_type": market_type,
        "aggression_mode": aggression_mode,
        "consensus_probability": consensus,
        "confidence_band_low": band_low,
        "confidence_band_high": band_high,
        "agreement_rate": agreement,
        "approved": approved,
        "recommended_action": final_action,
        "cost_estimate": total_cost,
        "latency_ms": total_latency_ms,
        "transcript": {
            "personas": [_strip_for_transcript(v) for v in verdicts],
            "moderator": _strip_for_transcript(mod_verdict),
            "gatekeeper": _strip_for_transcript(gk_verdict),
        },
    }


def _strip_for_transcript(v: dict[str, Any]) -> dict[str, Any]:
    return {k: val for k, val in v.items() if k != "raw_text"}


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
async def _persist_debate(
    *,
    game_id: str,
    matchup: str | None,
    league: str | None,
    market_type: str,
    verdicts: list[dict[str, Any]],
    moderator: dict[str, Any],
    moderator_parsed: dict[str, Any],
    gatekeeper: dict[str, Any],
    gatekeeper_parsed: dict[str, Any],
    consensus: float | None,
    band_low: float | None,
    band_high: float | None,
    agreement: float | None,
    approved: bool,
    recommended_action: str,
    total_cost: float,
    total_latency_ms: int,
    candidate: dict[str, Any],
    aggression_mode: str,
) -> int:
    analyst_verdicts_json = json.dumps(
        [
            {
                "persona": v["persona"],
                "provider": v["provider"],
                "model": v["model"],
                "probability": v["probability"],
                "confidence": v["confidence"],
                "lean": v["lean"],
                "weight": v["weight"],
                "reasoning": v["reasoning"],
                "latency_ms": v["latency_ms"],
                "tokens": {"input": v["input_tokens"], "output": v["output_tokens"]},
                "cost": v["cost"],
                "error": v["error"],
            }
            for v in verdicts
        ]
    )
    moderator_synthesis_json = json.dumps(
        {
            "model": moderator["model"],
            "consensus_probability": consensus,
            "confidence_band_low": band_low,
            "confidence_band_high": band_high,
            "agreement_rate": agreement,
            "recommended_action": (moderator_parsed.get("recommended_action") or "PASS"),
            "synthesis": moderator_parsed.get("synthesis"),
            "latency_ms": moderator["latency_ms"],
            "cost": moderator["cost"],
            "gatekeeper": {
                "model": gatekeeper["model"],
                "approved": approved,
                "recommended_action": recommended_action,
                "veto_reason": gatekeeper_parsed.get("veto_reason"),
                "latency_ms": gatekeeper["latency_ms"],
                "cost": gatekeeper["cost"],
            },
        }
    )
    game_context_json = json.dumps(
        {
            "side": candidate.get("side"),
            "current_price": candidate.get("current_price"),
            "context": candidate.get("context"),
            "aggression_mode": aggression_mode,
        }
    )

    # llm_predictions rows are built first (with debate_id placeholders
    # patched after the parent insert) so that the entire two-statement
    # write executes inside a single asyncpg transaction below.
    rows: list[dict[str, Any]] = []
    for v in verdicts:
        rows.append(
            {
                "provider": v["provider"],
                "model_id": v["model"],
                "category": "swarm_persona",
                "league": league,
                "game_id": game_id,
                "matchup": matchup,
                "prediction_type": market_type,
                "predicted_value": v["probability"],
                "confidence": v["confidence"],
                "latency_ms": v["latency_ms"],
                "tokens_used": (v["input_tokens"] or 0) + (v["output_tokens"] or 0),
                "cost_estimate": v["cost"],
                "reasoning": v["reasoning"],
                "metadata": {
                    "persona": v["persona"],
                    "lean": v["lean"],
                    "weight": v["weight"],
                    "input_tokens": v["input_tokens"],
                    "output_tokens": v["output_tokens"],
                    "error": v["error"],
                },
            }
        )
    rows.append(
        {
            "provider": moderator["provider"],
            "model_id": moderator["model"],
            "category": "swarm_moderator",
            "league": league,
            "game_id": game_id,
            "matchup": matchup,
            "prediction_type": market_type,
            "predicted_value": consensus,
            "confidence": agreement,
            "latency_ms": moderator["latency_ms"],
            "tokens_used": (moderator["input_tokens"] or 0) + (moderator["output_tokens"] or 0),
            "cost_estimate": moderator["cost"],
            "reasoning": (moderator_parsed.get("synthesis") or "")[:1000],
            "metadata": {
                "recommended_action": moderator_parsed.get("recommended_action"),
                "confidence_band_low": band_low,
                "confidence_band_high": band_high,
            },
        }
    )
    rows.append(
        {
            "provider": gatekeeper["provider"],
            "model_id": gatekeeper["model"],
            "category": "swarm_gatekeeper",
            "league": league,
            "game_id": game_id,
            "matchup": matchup,
            "prediction_type": market_type,
            "predicted_value": consensus,
            "confidence": None,
            "latency_ms": gatekeeper["latency_ms"],
            "tokens_used": (gatekeeper["input_tokens"] or 0) + (gatekeeper["output_tokens"] or 0),
            "cost_estimate": gatekeeper["cost"],
            "reasoning": (gatekeeper_parsed.get("veto_reason") or "")[:1000],
            "metadata": {
                "approved": approved,
                "recommended_action": recommended_action,
            },
        }
    )

    # Persistence is contractual AND atomic: every successful debate
    # writes 1 swarm_debates + exactly 8 llm_predictions rows in a
    # single asyncpg transaction.  A children-insert failure rolls back
    # the parent row so we never leave an orphaned swarm_debates row.
    # Any failure raises and the endpoint returns 502 instead of silent
    # success.
    try:
        async with db.acquire() as conn:
            async with conn.transaction():
                debate_id_raw = await conn.fetchval(
                    """
                    INSERT INTO swarm_debates (
                        game_id, matchup, league, market_type,
                        analyst_verdicts, moderator_synthesis,
                        consensus_probability, confidence_band_low, confidence_band_high,
                        agreement_rate, approved, recommended_action,
                        cost_estimate, latency_ms, game_context
                    ) VALUES (
                        $1, $2, $3, $4,
                        $5::jsonb, $6::jsonb,
                        $7, $8, $9,
                        $10, $11, $12,
                        $13, $14, $15::jsonb
                    )
                    RETURNING id
                    """,
                    game_id,
                    matchup,
                    league,
                    market_type,
                    analyst_verdicts_json,
                    moderator_synthesis_json,
                    consensus,
                    band_low,
                    band_high,
                    agreement,
                    approved,
                    recommended_action,
                    float(total_cost),
                    float(total_latency_ms),
                    game_context_json,
                )
                if debate_id_raw is None:
                    raise RuntimeError("swarm_debates insert returned no id")
                debate_id = int(debate_id_raw)
                # Stamp the freshly-allocated debate id into each child row's
                # metadata so the children are queryable by parent id.
                for r in rows:
                    r["metadata"]["debate_id"] = debate_id
                await conn.executemany(
                    """
                    INSERT INTO llm_predictions (
                        provider, model_id, category, league, game_id, matchup,
                        prediction_type, predicted_value, confidence,
                        latency_ms, tokens_used, cost_estimate, reasoning, metadata
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6,
                        $7, $8, $9,
                        $10, $11, $12, $13, $14::jsonb
                    )
                    """,
                    [
                        (
                            r["provider"],
                            r["model_id"],
                            r["category"],
                            r["league"],
                            r["game_id"],
                            r["matchup"],
                            r["prediction_type"],
                            r["predicted_value"],
                            r["confidence"],
                            r["latency_ms"],
                            r["tokens_used"],
                            r["cost_estimate"],
                            r["reasoning"],
                            json.dumps(r["metadata"]),
                        )
                        for r in rows
                    ],
                )
    except Exception as e:
        log.error("swarm persistence failed (transaction rolled back): %s", e)
        raise RuntimeError(f"swarm persistence failed: {e}") from e

    return debate_id
