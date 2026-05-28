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
import contextvars
import dataclasses
import json
import logging
import re
import time
import uuid
from typing import Any

from .. import db
from ..config import get_settings
from .personas import (
    GATEKEEPER,
    MODERATOR,
    PERSONAS,
    Persona,
    active_gatekeeper,
    active_llm_mode_state,
    active_moderator,
    estimate_cost,
    fallback_for,
    normalize_aggression_mode,
    resolve_active_personas,
)
from .profiles import resolve_for_vertical
from .streams import llm_log_hub
from .toggles import disabled_persona_ids
from .weights import get_persona_weight


log = logging.getLogger(__name__)


# Per-debate correlation id, propagated via contextvars so helpers
# (`_call_persona`, `_stream_openai`, `_stream_anthropic`) automatically
# tag their published events with the originating debate.  This lets the
# /api/ws/llm-logs subscriber group token deltas, persona verdicts, and
# moderator/gatekeeper outcomes by debate even when several debates run
# concurrently in the same process.
_current_debate_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "swarm_debate_id", default=None
)


# Per-process counters for gatekeeper fail-closed events.  Surfaced by
# /api/health/snapshot so operators can see when the gatekeeper has
# been forcing PASS due to upstream LLM trouble vs. legitimate vetoes.
# Keys: "call_error" (provider exception), "malformed_json" (provider
# responded but JSON contract violated).
_GATEKEEPER_FAILURES: dict[str, int] = {}


def gatekeeper_failure_counters() -> dict[str, int]:
    """Snapshot of fail-closed gatekeeper counters (read-only copy)."""
    return dict(_GATEKEEPER_FAILURES)


def reset_gatekeeper_failure_counters() -> None:
    """Test helper — clears the in-process gatekeeper counters."""
    _GATEKEEPER_FAILURES.clear()


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


async def _publish(event: dict[str, Any]) -> None:
    """Inject the active debate_id + timestamp and forward to the hub."""
    did = _current_debate_id.get()
    enriched: dict[str, Any] = dict(event)
    if did and "debate_id" not in enriched:
        enriched["debate_id"] = did
    enriched.setdefault("ts", _now_iso())
    await llm_log_hub.publish(enriched)


# ---------------------------------------------------------------------------
# Lazy SDK clients (instantiated on first use so module import never fails
# when keys are missing — the endpoint surfaces a clean 503 instead).
# ---------------------------------------------------------------------------
_openai_client: Any = None
_anthropic_client: Any = None

# Both SDKs retry transient failures (connection errors, 408/409/429/5xx) with
# exponential backoff internally; the default is 2 retries but we pin it
# explicitly so swarm cycles survive a brief upstream blip instead of dropping
# a persona's contribution.  Streaming requests retry on the initial connect.
_LLM_MAX_RETRIES = 2


def _get_openai():
    global _openai_client
    if _openai_client is None:
        from openai import AsyncOpenAI  # type: ignore

        key = get_settings().openai_api_key
        if not key:
            raise RuntimeError("OPENAI_API_KEY not configured")
        _openai_client = AsyncOpenAI(api_key=key, max_retries=_LLM_MAX_RETRIES)
    return _openai_client


def _get_anthropic():
    global _anthropic_client
    if _anthropic_client is None:
        from anthropic import AsyncAnthropic  # type: ignore

        key = get_settings().anthropic_api_key
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY not configured")
        _anthropic_client = AsyncAnthropic(
            api_key=key, max_retries=_LLM_MAX_RETRIES
        )
    return _anthropic_client


_google_client: Any = None


def _get_google():
    """Lazy-init Google GenAI client.

    Used only by the gatekeeper-model selector when the operator chooses
    a Gemini model.  We import inside the function so projects that
    never select Google don't pay for the SDK install / import cost.
    """
    global _google_client
    if _google_client is None:
        from google import genai  # type: ignore

        key = get_settings().google_api_key
        if not key:
            raise RuntimeError("GEMINI_API_KEY not configured")
        _google_client = genai.Client(api_key=key)
    return _google_client


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
    persona_id: str, model_id: str, system: str, user: str, temperature: float
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
            temperature=temperature,
            max_tokens=600,
            stream=True,
            stream_options={"include_usage": True},
        )
        async for event in stream:
            if event.choices:
                delta = event.choices[0].delta.content or ""
                if delta:
                    chunks.append(delta)
                    await _publish(
                        {"type": "persona_delta", "persona": persona_id, "text": delta}
                    )
            if getattr(event, "usage", None):
                in_tok = int(getattr(event.usage, "prompt_tokens", 0) or 0)
                out_tok = int(getattr(event.usage, "completion_tokens", 0) or 0)
    finally:
        pass
    return "".join(chunks), in_tok, out_tok


async def _stream_anthropic(
    persona_id: str, model_id: str, system: str, user: str, temperature: float
) -> tuple[str, int, int]:
    client = _get_anthropic()
    chunks: list[str] = []
    in_tok = out_tok = 0
    async with client.messages.stream(
        model=model_id,
        system=system,
        max_tokens=600,
        temperature=temperature,
        messages=[{"role": "user", "content": user}],
    ) as stream:
        async for delta in stream.text_stream:
            if delta:
                chunks.append(delta)
                await _publish(
                    {"type": "persona_delta", "persona": persona_id, "text": delta}
                )
        final = await stream.get_final_message()
        if final.usage:
            in_tok = int(final.usage.input_tokens or 0)
            out_tok = int(final.usage.output_tokens or 0)
    return "".join(chunks), in_tok, out_tok


async def _stream_google(
    persona_id: str, model_id: str, system: str, user: str, temperature: float
) -> tuple[str, int, int]:
    """Streaming dispatch for Google Gemini.

    Mirrors the OpenAI / Anthropic helpers: emits ``persona_delta`` hub
    events as tokens arrive and returns ``(text, input_tokens,
    output_tokens)`` for the cost ledger.  Uses the new ``google-genai``
    SDK (the legacy ``google-generativeai`` package is not installed).
    """
    client = _get_google()
    chunks: list[str] = []
    in_tok = out_tok = 0
    # ``generate_content_stream`` is sync-iterable; wrap it through
    # ``asyncio.to_thread`` so we don't block the event loop on each
    # chunk.  google-genai v1.x exposes the streaming method on
    # ``client.models`` rather than on a model handle.
    import asyncio as _asyncio

    def _open():
        return client.models.generate_content_stream(
            model=model_id,
            contents=user,
            config={
                "system_instruction": system,
                "temperature": temperature,
                "max_output_tokens": 600,
            },
        )

    stream = await _asyncio.to_thread(_open)
    final_chunk: Any = None
    while True:
        try:
            chunk = await _asyncio.to_thread(next, stream, None)
        except StopIteration:
            break
        if chunk is None:
            break
        final_chunk = chunk
        text = getattr(chunk, "text", "") or ""
        if text:
            chunks.append(text)
            await _publish(
                {"type": "persona_delta", "persona": persona_id, "text": text}
            )
    if final_chunk is not None:
        usage = getattr(final_chunk, "usage_metadata", None)
        if usage is not None:
            in_tok = int(getattr(usage, "prompt_token_count", 0) or 0)
            out_tok = int(getattr(usage, "candidates_token_count", 0) or 0)
    return "".join(chunks), in_tok, out_tok


class OllamaProviderError(Exception):
    """Normalised Ollama transport / HTTP error.

    Carries an optional ``status_code`` so ``_is_retryable_provider_error``
    routes 429 / 5xx the same way OpenAI / Anthropic SDK errors are routed
    (i.e. triggers the cross-provider fallback chain).
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


async def _stream_ollama(
    persona_id: str, model_id: str, system: str, user: str, temperature: float
) -> tuple[str, int, int]:
    """Streaming dispatch for a local (or tunneled remote) Ollama server.

    POSTs ``/api/chat`` with ``stream=true`` and consumes the NDJSON
    response (one JSON object per line).  Each chunk has a ``message.content``
    delta; the terminal chunk carries ``done=true`` plus
    ``prompt_eval_count`` / ``eval_count`` for the (synthetic, $0) cost
    ledger.  Mirrors the OpenAI / Anthropic / Google helpers so the
    ``persona_delta`` event stream stays uniform across providers.
    """
    import httpx

    settings = get_settings()
    base = (settings.ollama_base_url or "http://localhost:11434").rstrip("/")
    url = f"{base}/api/chat"
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if settings.ollama_api_key:
        headers["Authorization"] = f"Bearer {settings.ollama_api_key}"
    payload: dict[str, Any] = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": True,
        "options": {
            "temperature": float(temperature),
            "num_predict": 600,
        },
    }
    chunks: list[str] = []
    in_tok = out_tok = 0
    timeout = httpx.Timeout(
        float(settings.ollama_request_timeout_s or 120.0), connect=10.0
    )
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST", url, json=payload, headers=headers
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", errors="replace")
                    raise OllamaProviderError(
                        f"ollama HTTP {resp.status_code}: {body[:500]}",
                        status_code=resp.status_code,
                    )
                async for line in resp.aiter_lines():
                    if not line or not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(obj, dict) and "error" in obj:
                        raise OllamaProviderError(
                            f"ollama error: {obj.get('error')!r}"
                        )
                    msg = (obj or {}).get("message") or {}
                    delta = msg.get("content") or ""
                    if delta:
                        chunks.append(delta)
                        await _publish(
                            {
                                "type": "persona_delta",
                                "persona": persona_id,
                                "text": delta,
                            }
                        )
                    if obj.get("done"):
                        in_tok = int(obj.get("prompt_eval_count") or 0)
                        out_tok = int(obj.get("eval_count") or 0)
    except httpx.HTTPError as e:
        # Normalise transport-level failures so the fallback path picks
        # them up via the duck-typed exception-name check.
        raise OllamaProviderError(f"ollama transport error: {e}") from e
    return "".join(chunks), in_tok, out_tok


def _is_retryable_provider_error(exc: BaseException) -> bool:
    """True for rate-limit (HTTP 429) or 5xx upstream failures.

    Uses duck-typing on the SDK exception classes so we don't pay an
    import cost when both libraries aren't installed in tests, and so a
    future SDK refactor doesn't silently disable the fallback.

    For Ollama specifically we are deliberately *narrow*: a missing /
    misconfigured model, bad auth, or malformed request returns a 4xx and
    must surface to the operator instead of silently burning hosted-LLM
    credit via the cross-provider fallback.  Only transport failures
    (daemon down, timeout — modeled as ``status_code is None``) and
    429 / 5xx are routed to fallback.
    """
    name = type(exc).__name__
    if name in (
        # OpenAI / Anthropic SDK exception classes — these are constructed
        # by the SDKs themselves only when the upstream is genuinely
        # unavailable, so the class name alone is a reliable signal.
        "RateLimitError", "APITimeoutError", "APIConnectionError",
    ):
        return True
    if name == "OllamaProviderError":
        status = getattr(exc, "status_code", None)
        # Transport-level failure (daemon down, connect/read timeout) —
        # _stream_ollama wraps httpx.HTTPError without a status_code.
        if status is None:
            return True
        if isinstance(status, int) and (status == 429 or 500 <= status < 600):
            return True
        # 4xx config / auth / model-not-found: surface, do NOT fallback.
        return False
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and (status == 429 or 500 <= status < 600):
        return True
    return False


async def _invoke_provider(
    *,
    persona_id: str,
    provider: str,
    model_id: str,
    system: str,
    user: str,
    temperature: float,
) -> tuple[str, int, int]:
    """Single dispatch point so callers (persona + fallback) share one path."""
    if provider == "openai":
        return await _stream_openai(persona_id, model_id, system, user, temperature)
    if provider == "anthropic":
        return await _stream_anthropic(persona_id, model_id, system, user, temperature)
    if provider == "google":
        return await _stream_google(persona_id, model_id, system, user, temperature)
    if provider == "ollama":
        return await _stream_ollama(persona_id, model_id, system, user, temperature)
    raise RuntimeError(f"unknown provider {provider!r}")


async def _call_persona(
    persona: Persona, user_prompt: str
) -> dict[str, Any]:
    """Run one persona end-to-end and return a verdict dict.

    On a retryable provider failure (rate-limit or 5xx) we re-issue the
    prompt against the persona's configured fallback so a single provider
    outage cannot silence a persona.  The verdict reports `provider` /
    `model` of the call that ACTUALLY produced the answer plus a
    `fallback_used` flag for telemetry.
    """
    started = time.perf_counter()
    await _publish(
        {
            "type": "persona_start",
            "persona": persona.id,
            "display": persona.display,
            "provider": persona.provider,
            "model": persona.model_id,
        }
    )
    error: str | None = None
    text = ""
    in_tok = out_tok = 0
    used_provider = persona.provider
    used_model = persona.model_id
    fallback_used = False
    fallback_error: str | None = None

    try:
        text, in_tok, out_tok = await _invoke_provider(
            persona_id=persona.id,
            provider=persona.provider,
            model_id=persona.model_id,
            system=persona.system_prompt,
            user=user_prompt,
            temperature=persona.temperature,
        )
    except Exception as e:  # noqa: BLE001 - provider error handled below
        primary_error = f"{type(e).__name__}: {e}"
        retryable = _is_retryable_provider_error(e)
        fb = fallback_for(persona) if retryable else None
        if fb is not None:
            fb_provider, fb_model = fb
            log.warning(
                "persona %s primary %s/%s failed (%s); falling back to %s/%s",
                persona.id, persona.provider, persona.model_id,
                primary_error, fb_provider, fb_model,
            )
            await _publish(
                {
                    "type": "persona_fallback",
                    "persona": persona.id,
                    "from": {"provider": persona.provider, "model": persona.model_id},
                    "to": {"provider": fb_provider, "model": fb_model},
                    "reason": primary_error,
                }
            )
            try:
                text, in_tok, out_tok = await _invoke_provider(
                    persona_id=persona.id,
                    provider=fb_provider,
                    model_id=fb_model,
                    system=persona.system_prompt,
                    user=user_prompt,
                    temperature=persona.temperature,
                )
                used_provider = fb_provider
                used_model = fb_model
                fallback_used = True
                fallback_error = primary_error
            except Exception as e2:  # noqa: BLE001
                error = (
                    f"primary={primary_error}; "
                    f"fallback={type(e2).__name__}: {e2}"
                )
                log.warning("persona %s fallback also failed: %s", persona.id, error)
        else:
            error = primary_error
            log.warning("persona %s failed: %s", persona.id, error)

    latency_ms = int((time.perf_counter() - started) * 1000)
    parsed = _extract_json(text) or {}
    verdict = {
        "persona": persona.id,
        "provider": used_provider,
        "model": used_model,
        "probability": _coerce_prob(parsed.get("probability")),
        "confidence": _coerce_prob(parsed.get("confidence")),
        "lean": _coerce_lean(parsed.get("lean")),
        "reasoning": (parsed.get("reasoning") or "").strip()[:1000] or text[-500:],
        "raw_text": text,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        # Local Ollama personas run on operator hardware so the cost ledger
        # records $0 — keeps the cost-vs-edge dashboards honest about which
        # spend is hosted-LLM vs. on-prem inference.
        "cost": (
            0.0
            if used_provider == "ollama"
            else estimate_cost(used_model, in_tok, out_tok)
        ),
        "latency_ms": latency_ms,
        "error": error,
        "primary_provider": persona.provider,
        "primary_model": persona.model_id,
        "fallback_used": fallback_used,
        "fallback_reason": fallback_error,
    }
    await _publish(
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

    Wraps `_run_debate_inner` with a try/finally so the per-debate
    contextvar is always reset, even if persistence or an LLM call
    raises mid-debate.
    """
    stream_debate_id = uuid.uuid4().hex
    token = _current_debate_id.set(stream_debate_id)
    try:
        return await _run_debate_inner(candidate, stream_debate_id)
    finally:
        _current_debate_id.reset(token)


async def _resolve_gatekeeper_persona() -> Persona:
    """Resolve the gatekeeper persona that will actually be called for
    this debate, applying — in this order — the LLM-mode rewrite (so a
    Mac Studio operator with `AXIOM_LLM_MODE=local` gets an Ollama-
    backed gatekeeper) and the operator-curated runtime override (so a
    `cloud`/`hybrid` deployment can pin a specific gatekeeper model
    from the admin UI).

    `local` mode IGNORES the runtime override entirely: `local` is a
    hard guarantee that no persona — including the gatekeeper — can
    silently route to OpenAI/Anthropic just because a stale override
    row was left over from a previous mode. Without this guard, a
    leftover `("openai","gpt-4o-mini")` override would burn hosted-LLM
    credit on every debate even though the operator asked for an all-
    local swarm. Lookup failures are non-fatal — we never let a
    settings-table hiccup silence the gatekeeper.
    """
    gk_persona = active_gatekeeper()
    try:
        from .. import runtime_settings as _rs

        override = await _rs.get_gatekeeper_model_override()
    except Exception as e:  # noqa: BLE001
        log.debug("gatekeeper override lookup failed; using default: %s", e)
        override = None
    llm_mode = active_llm_mode_state().get("mode", "cloud")
    if override is not None and llm_mode == "local":
        log.info(
            "ignoring gatekeeper override %s/%s under AXIOM_LLM_MODE=local",
            override[0],
            override[1],
        )
        override = None
    if override is not None:
        ov_provider, ov_model = override
        gk_persona = dataclasses.replace(
            gk_persona,
            provider=ov_provider,
            model_id=ov_model,
            # Disable cross-provider fallback for the operator-selected
            # model so the verdict reflects the chosen provider — the
            # outer try/except in the caller already fail-closes on
            # any error.
            fallback_provider="",
            fallback_model_id="",
        )
    return gk_persona


async def _run_debate_inner(
    candidate: dict[str, Any], stream_debate_id: str
) -> dict[str, Any]:
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

    # Resolve vertical persona profile from registry so debates carry the
    # profile tag forward.  Per-profile prompt overrides land in #194;
    # for now we attach it to the debate metadata + start event so the
    # operator UI / weight aggregator can branch on it without breaking
    # legacy "default"-profile callers.
    from .. import verticals as _verticals
    candidate_vertical = (
        str(candidate.get("vertical") or "").strip().lower() or None
    )
    _vspec = _verticals.get_spec(candidate_vertical) if candidate_vertical else None
    persona_profile = _vspec.persona_profile if _vspec is not None else "default"

    # Resolve the *active* persona tuple for this debate by combining the
    # vertical's profile with any operator deny-list from
    # ``swarm_persona_toggles``.  When a vertical has no toggle rows, the
    # full profile runs.  When every persona in the profile has been
    # disabled, ``resolve_for_vertical`` falls back to the full profile
    # so the swarm never silently degrades to a single LLM call.
    disabled_ids = await disabled_persona_ids(candidate_vertical)
    resolved = resolve_for_vertical(
        persona_profile, disabled_persona_ids=disabled_ids
    )
    # Mac Studio cutover (task #735): apply the active LLM-mode rewrite
    # so a `local` / `hybrid` operator gets ollama-backed personas
    # transparently — `cloud` mode is identity so existing replay /
    # Brier history continues to apply byte-for-byte.
    active_personas: tuple[Persona, ...] = resolve_active_personas(resolved.active)

    # `stream_debate_id` is set into `_current_debate_id` by the public
    # `run_debate` wrapper above, which also handles reset() in finally.
    kalshi_market_id = candidate.get("kalshi_market_id")

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

    # Per-league prompt augment (e.g. NFL snap counts / OL-DL injuries /
    # weather guidance for injury_roster + matchup_historian).  Empty
    # string for leagues without a configured augment.
    #
    # For NFL specifically we go further than the prose augment and
    # actually fetch:
    #   * outdoor stadium weather (Open-Meteo, keyless)
    #   * OL/DL availability (sportradar_injuries cache)
    #   * top skill-position snap %  (nfl_snap_counts cache)
    # so the personas reason on real numbers instead of just being told
    # to think about them.  All sub-fetches are best-effort: a failure
    # in any of them collapses to "no data" for that sub-block, never
    # blocks the debate.
    # ``game_time`` (a.k.a. scheduled_start) is carried onto the
    # candidate by the orchestrator (see cycle.py).  Passing it through
    # to the per-league builders lets ``fetch_weather`` consult the V1
    # archive for historical replays instead of calling a live HTTP
    # feed (or returning an empty block, for MLB / golf).
    from datetime import datetime as _dt
    _game_time = candidate.get("game_time")
    _observed_at = _game_time if isinstance(_game_time, _dt) else None

    nfl_game_context: dict[str, Any] = {}
    mlb_game_context: dict[str, Any] = {}
    golf_game_context: dict[str, Any] = {}
    league_upper = (league or "").strip().upper()
    if league_upper == "NFL":
        try:
            from ..sports import build_nfl_game_context as _build_nfl_ctx
            nfl_game_context = await _build_nfl_ctx(
                candidate.get("home_team"),
                candidate.get("away_team"),
                observed_at=_observed_at,
            )
        except Exception as e:  # noqa: BLE001 — best-effort
            log.debug("NFL game_context build failed (best-effort): %s", e)
            nfl_game_context = {}
    elif league_upper == "MLB":
        try:
            from ..sports import build_mlb_game_context as _build_mlb_ctx
            mlb_game_context = await _build_mlb_ctx(
                candidate.get("home_team"),
                candidate.get("away_team"),
                observed_at=_observed_at,
            )
        except Exception as e:  # noqa: BLE001 — best-effort
            log.debug("MLB game_context build failed (best-effort): %s", e)
            mlb_game_context = {}
    elif league_upper in ("PGA", "GOLF", "LIV"):
        try:
            from ..sports import build_golf_game_context as _build_golf_ctx
            # Golf uses ``venue`` (or ``tournament``) rather than home_team.
            _venue = (
                candidate.get("venue")
                or candidate.get("tournament")
                or matchup
            )
            golf_game_context = await _build_golf_ctx(
                _venue, observed_at=_observed_at,
            )
        except Exception as e:  # noqa: BLE001 — best-effort
            log.debug("golf game_context build failed (best-effort): %s", e)
            golf_game_context = {}

    # Soccer game_context: pulls the cached sportradar_rosters +
    # sportradar_injuries (missing-players feed, task #211) so the
    # lineup_rotation persona reasons on real squad availability
    # instead of just being told to "think about" rotation.  Best-
    # effort like the NFL block — a build failure collapses to {}.
    soccer_game_context: dict[str, Any] = {}
    try:
        from ..sports import is_soccer_league as _is_soccer
        _is_soccer_match = _is_soccer(league)
    except Exception:
        _is_soccer_match = False
    if _is_soccer_match:
        try:
            from ..sports import build_soccer_game_context as _build_soccer_ctx
            soccer_game_context = await _build_soccer_ctx(
                league,
                candidate.get("home_team"),
                candidate.get("away_team"),
            )
        except Exception as e:  # noqa: BLE001 — best-effort
            log.debug("soccer game_context build failed (best-effort): %s", e)
            soccer_game_context = {}

    try:
        from ..sports import persona_context_augment as _persona_aug
        league_aug = _persona_aug(
            league,
            game_context=(
                nfl_game_context
                or mlb_game_context
                or golf_game_context
                or soccer_game_context
                or None
            ),
        )
    except Exception:
        league_aug = ""
    if league_aug:
        user_prompt = user_prompt + "\n\n" + league_aug

    # Stash the structured context onto the candidate so
    # `_persist_debate` writes it into `swarm_debates.game_context` and
    # downstream systems (settlement, dashboard, replay UI) can join
    # against the exact numbers the personas saw.
    if nfl_game_context:
        candidate = dict(candidate)
        candidate["nfl_context"] = nfl_game_context
    if soccer_game_context:
        candidate = dict(candidate)
        candidate["soccer_context"] = soccer_game_context
    if mlb_game_context:
        candidate = dict(candidate)
        candidate["mlb_context"] = mlb_game_context
    if golf_game_context:
        candidate = dict(candidate)
        candidate["golf_context"] = golf_game_context

    await _publish(
        {
            "type": "debate_start",
            "game_id": game_id,
            "matchup": matchup,
            "league": league,
            "market_type": market_type,
            "side": side,
            "current_price": current_price,
            "aggression_mode": aggression_mode,
            # Mirror the cycle-stream candidate identifier so the UI can
            # link each debate back to its candidate row.
            "market_id": kalshi_market_id,
            "kalshi_market_id": kalshi_market_id,
            "vertical": candidate_vertical,
            "persona_profile": persona_profile,
            "personas": (
                [p.id for p in active_personas]
                + [MODERATOR.id, GATEKEEPER.id]
            ),
            "disabled_personas": list(resolved.disabled_ids),
            "ts": _now_iso(),
        }
    )

    # 1) Profile personas in parallel.
    persona_tasks = [_call_persona(p, user_prompt) for p in active_personas]
    verdicts = await asyncio.gather(*persona_tasks)

    # 2) Resolve weights from swarm_analyst_metrics scoped to this vertical.
    for v in verdicts:
        v["weight"] = await get_persona_weight(
            v["persona"], league, market_type, vertical=candidate_vertical
        )

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
    # Mac Studio cutover: pull the moderator through the LLM-mode resolver
    # so a `local`/`hybrid` operator routes the moderator at the same
    # provider as the personas instead of silently staying on the cloud
    # baseline.
    mod_persona = active_moderator()
    mod_verdict = await _call_persona(mod_persona, mod_input)
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

    await _publish(
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
    # 4) Gatekeeper veto.  This is the last line of defence in front of
    # the order pipe — if it cannot return a definitive `approved=true`
    # verdict (provider outage, malformed JSON, both fallbacks
    # exhausted) the entire trade is forced to PASS.  We never let a
    # gatekeeper failure silently fall back to the moderator's
    # recommendation, because the gatekeeper is what enforces the 12
    # risk gates the moderator does not see.
    gk_failed_closed = False
    gk_failure_reason: str | None = None
    # Resolve operator-tunable gatekeeper model override (admin UI knob).
    # `runtime_settings.get_gatekeeper_model_override()` returns None when
    # no override is set OR when the stored value is no longer in the
    # catalogue, so we fall back to the persona default in that case.
    # Failures are non-fatal — we never let a settings-table hiccup
    # silence the gatekeeper.
    # Mac Studio cutover: start from the LLM-mode-resolved gatekeeper so
    # `local`/`hybrid` operators get an Ollama-backed gatekeeper by
    # default. In `hybrid` mode the runtime_settings override below still
    # wins (operator-curated knob) so a hybrid deployment can keep cloud-
    # backed gatekeeper guardrails even while the rest of the swarm is
    # local. In `local` mode we IGNORE the override entirely — `local`
    # is a hard guarantee that no persona (including gatekeeper) reaches
    # OpenAI/Anthropic, regardless of stale operator settings. Without
    # this, an old override pointing at a hosted model would silently
    # burn cloud credit on every debate and contradict the env contract.
    gk_persona = await _resolve_gatekeeper_persona()
    try:
        gk_verdict = await _call_persona(gk_persona, gk_input)
    except Exception as e:  # noqa: BLE001 — fail-closed by design
        log.warning("gatekeeper call failed; forcing PASS: %s", e)
        _GATEKEEPER_FAILURES["call_error"] = _GATEKEEPER_FAILURES.get("call_error", 0) + 1
        gk_failed_closed = True
        gk_failure_reason = f"gatekeeper_call_error: {type(e).__name__}: {e}"
        gk_verdict = {
            "persona": GATEKEEPER.id,
            "probability": None,
            "confidence": None,
            "lean": None,
            "reasoning": gk_failure_reason,
            "raw_text": "",
            "weight": 1.0,
            "cost": 0.0,
            "latency_ms": 0,
            "model": None,
            "provider": None,
        }
        gk_parsed: dict[str, Any] = {}
    else:
        gk_parsed = _extract_json(gk_verdict["raw_text"]) or {}
        # Strict schema check.  An approval is only honoured when the
        # gatekeeper response is a dict containing a real boolean
        # `approved` key AND a `recommended_action` in the closed enum.
        # Anything else (empty parse, wrong types, missing keys,
        # unknown action) is treated as a malformed contract and fails
        # closed.  This pins the contract the architect flagged: a
        # syntactically-valid `{"approved": true}` with no action MUST
        # NOT slip through.
        valid_actions = ("BUY_YES", "BUY_NO", "PASS")
        gk_action_raw = gk_parsed.get("recommended_action")
        gk_action = str(gk_action_raw).upper() if isinstance(gk_action_raw, str) else ""
        schema_ok = (
            isinstance(gk_parsed, dict)
            and isinstance(gk_parsed.get("approved"), bool)
            and gk_action in valid_actions
        )
        if not schema_ok:
            _GATEKEEPER_FAILURES["malformed_json"] = (
                _GATEKEEPER_FAILURES.get("malformed_json", 0) + 1
            )
            gk_failed_closed = True
            gk_failure_reason = (
                "gatekeeper_malformed_schema: "
                f"approved={type(gk_parsed.get('approved')).__name__} "
                f"action={gk_action_raw!r}"
            )

    approved = (
        not gk_failed_closed and bool(gk_parsed.get("approved"))
    )
    if gk_failed_closed or not approved:
        final_action = "PASS"
    else:
        final_action = str(gk_parsed.get("recommended_action") or "PASS").upper()
        if final_action not in ("BUY_YES", "BUY_NO", "PASS"):
            final_action = "PASS"

    await _publish(
        {
            "type": "gatekeeper_done",
            "approved": approved,
            "recommended_action": final_action,
            "veto_reason": gk_parsed.get("veto_reason") or gk_failure_reason,
            "failed_closed": gk_failed_closed,
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

    await _publish(
        {
            "type": "debate_complete",
            # Keep the streaming UUID as the canonical correlation id so
            # subscribers grouping prior events still match this one;
            # surface the persisted DB id alongside it for cross-reference.
            "debate_id": stream_debate_id,
            "persisted_debate_id": debate_id,
            "game_id": game_id,
            "matchup": matchup,
            "league": league,
            "market_id": kalshi_market_id,
            "approved": approved,
            "recommended_action": final_action,
            "consensus_probability": consensus,
            "agreement_rate": agreement,
            "confidence_band_low": band_low,
            "confidence_band_high": band_high,
            "cost_estimate": total_cost,
            "latency_ms": total_latency_ms,
        }
    )

    return {
        "debate_id": debate_id,
        # Streaming UUID — same id every per-debate websocket event on
        # /api/ws/llm-logs carries.  Surfaced so the orchestrator can
        # correlate post-debate events (e.g. the per-entrant Plackett-
        # Luce sketch published from cycle.py) back to the live
        # transcript card without round-tripping through the persisted
        # DB id (task #613).
        "stream_debate_id": stream_debate_id,
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
                "primary_provider": v.get("primary_provider"),
                "primary_model": v.get("primary_model"),
                "fallback_used": v.get("fallback_used", False),
                "fallback_reason": v.get("fallback_reason"),
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
    # Include any optional linkage keys passed by the orchestrator so
    # downstream systems (settlement, dashboards) can join debates back
    # to the originating market without a fragile game_id heuristic.
    game_context_json = json.dumps(
        {
            "side": candidate.get("side"),
            "current_price": candidate.get("current_price"),
            "context": candidate.get("context"),
            "aggression_mode": aggression_mode,
            "kalshi_market_id": candidate.get("kalshi_market_id"),
            "kalshi_price": candidate.get("kalshi_price"),
            "vertical": candidate.get("vertical"),
            # Persist team names so the cached-debate rehydration
            # path (`cycle._cached_debates`) can drive the Elo
            # enrichment even when the `games` join misses (task #130).
            "home_team": candidate.get("home_team"),
            "away_team": candidate.get("away_team"),
            # Structured NFL conditions (weather + player_status) the
            # personas actually saw, persisted alongside the prompt
            # candidate so settlement / replay can audit the inputs.
            "nfl_context": candidate.get("nfl_context"),
            # Structured soccer conditions (lineup_status pulled from
            # cached sportradar_rosters + missing-players feeds) the
            # personas actually saw — same audit contract as nfl_context
            # so settlement / replay can join debates back to the exact
            # squad-availability numbers that drove the EV adjustment.
            "soccer_context": candidate.get("soccer_context"),
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
                    "primary_provider": v.get("primary_provider"),
                    "primary_model": v.get("primary_model"),
                    "fallback_used": v.get("fallback_used", False),
                    "fallback_reason": v.get("fallback_reason"),
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
