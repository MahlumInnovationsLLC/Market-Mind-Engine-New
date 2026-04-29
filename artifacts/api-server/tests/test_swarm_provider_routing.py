"""Provider routing + cross-provider fallback tests for the swarm runner.

These tests exercise `_call_persona` with the OpenAI/Anthropic streaming
helpers monkey-patched, so we never hit a real provider.  They cover:

  - Each persona uses the configured (provider, model_id, temperature).
  - On a rate-limit / 5xx primary failure the call automatically retries
    against the cross-provider fallback and the verdict reflects the
    provider/model that ACTUALLY produced the answer.
  - Telemetry-relevant fields (provider, model, cost, latency_ms,
    fallback_used, primary_provider) are populated on the verdict.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.swarm import personas, runner


def _ok_response(text: str = '{"probability":0.55,"confidence":0.6,"lean":"YES","reasoning":"r"}'):
    async def _impl(persona_id, model_id, system, user, temperature):  # noqa: ARG001
        # Token counts so estimate_cost has something to work with.
        return text, 100, 50

    return _impl


class _FakeRateLimit(Exception):
    """Mimics the OpenAI/Anthropic SDK RateLimitError class name + status."""

    def __init__(self) -> None:
        super().__init__("rate limited")
        self.status_code = 429


# Manually pin the class name so `_is_retryable_provider_error` matches.
_FakeRateLimit.__name__ = "RateLimitError"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _isolate_event_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield
    loop.close()


def test_persona_uses_configured_provider_model_and_temperature(monkeypatch):
    captured: dict = {}

    async def fake_openai(persona_id, model_id, system, user, temperature):
        captured["openai"] = (model_id, temperature)
        return '{"probability":0.5,"confidence":0.5,"lean":"YES","reasoning":"x"}', 10, 5

    async def fake_anthropic(persona_id, model_id, system, user, temperature):
        captured["anthropic"] = (model_id, temperature)
        return '{"probability":0.5,"confidence":0.5,"lean":"NO","reasoning":"y"}', 10, 5

    monkeypatch.setattr(runner, "_stream_openai", fake_openai)
    monkeypatch.setattr(runner, "_stream_anthropic", fake_anthropic)

    sharp = next(p for p in personas.PERSONAS if p.id == "sharp_edge")
    injury = next(p for p in personas.PERSONAS if p.id == "injury_roster")

    v1 = _run(runner._call_persona(sharp, "prompt"))
    v2 = _run(runner._call_persona(injury, "prompt"))

    assert captured["openai"] == (sharp.model_id, sharp.temperature)
    assert captured["anthropic"] == (injury.model_id, injury.temperature)
    assert v1["provider"] == "openai" and v1["model"] == sharp.model_id
    assert v2["provider"] == "anthropic" and v2["model"] == injury.model_id
    assert v1["fallback_used"] is False and v2["fallback_used"] is False
    assert v1["cost"] > 0 and v1["latency_ms"] >= 0


def test_fallback_on_rate_limit_switches_provider(monkeypatch):
    """OpenAI persona that 429s should fall back to the Anthropic model."""
    calls: list[tuple[str, str]] = []

    async def fake_openai(persona_id, model_id, system, user, temperature):
        calls.append(("openai", model_id))
        raise _FakeRateLimit()

    async def fake_anthropic(persona_id, model_id, system, user, temperature):
        calls.append(("anthropic", model_id))
        return '{"probability":0.7,"confidence":0.8,"lean":"YES","reasoning":"fb"}', 20, 10

    monkeypatch.setattr(runner, "_stream_openai", fake_openai)
    monkeypatch.setattr(runner, "_stream_anthropic", fake_anthropic)

    sharp = next(p for p in personas.PERSONAS if p.id == "sharp_edge")
    fb = personas.fallback_for(sharp)
    assert fb is not None
    fb_provider, fb_model = fb

    verdict = _run(runner._call_persona(sharp, "prompt"))

    assert calls[0] == ("openai", sharp.model_id)
    assert calls[1] == (fb_provider, fb_model)
    assert verdict["fallback_used"] is True
    assert verdict["provider"] == fb_provider
    assert verdict["model"] == fb_model
    assert verdict["primary_provider"] == sharp.provider
    assert verdict["primary_model"] == sharp.model_id
    assert verdict["error"] is None  # fallback succeeded
    assert verdict["probability"] == 0.7
    # Cost should be priced at the fallback model, not the primary.
    expected = personas.estimate_cost(fb_model, 20, 10)
    assert verdict["cost"] == expected


def test_non_retryable_error_does_not_trigger_fallback(monkeypatch):
    """A plain ValueError must NOT cause a cross-provider retry."""
    calls: list[str] = []

    async def fake_openai(persona_id, model_id, system, user, temperature):
        calls.append("openai")
        raise ValueError("bad request")

    async def fake_anthropic(persona_id, model_id, system, user, temperature):
        calls.append("anthropic")
        return "ignored", 0, 0

    monkeypatch.setattr(runner, "_stream_openai", fake_openai)
    monkeypatch.setattr(runner, "_stream_anthropic", fake_anthropic)

    sharp = next(p for p in personas.PERSONAS if p.id == "sharp_edge")
    verdict = _run(runner._call_persona(sharp, "prompt"))

    assert calls == ["openai"]
    assert verdict["fallback_used"] is False
    assert verdict["error"] and "ValueError" in verdict["error"]
    assert verdict["provider"] == sharp.provider  # still reports primary


def test_persona_defaults_split_three_three_across_providers():
    by_provider: dict[str, int] = {}
    for p in personas.PERSONAS:
        by_provider[p.provider] = by_provider.get(p.provider, 0) + 1
    assert by_provider == {"openai": 3, "anthropic": 3}


# ---------------------------------------------------------------------------
# Ollama provider (task #730) — local/remote LLM box.  These tests stub
# ``httpx.AsyncClient`` via ``MockTransport`` so the actual NDJSON parser
# in ``_stream_ollama`` runs end-to-end without touching the network.
# ---------------------------------------------------------------------------


def _ollama_ndjson_chunks(content: str, prompt_tokens: int = 11, eval_tokens: int = 13) -> bytes:
    """Build a minimal Ollama-shaped NDJSON stream.

    Three streamed message deltas + a terminal ``done=true`` chunk that
    carries ``prompt_eval_count`` / ``eval_count`` so the cost ledger has
    something to assert against.  Splitting the content into 3 deltas
    lets the ``persona_delta`` aggregator accumulate the same way it
    would in production.
    """
    third = max(1, len(content) // 3)
    a, b, c = content[:third], content[third:2 * third], content[2 * third:]
    lines = [
        json.dumps({"message": {"role": "assistant", "content": a}, "done": False}),
        json.dumps({"message": {"role": "assistant", "content": b}, "done": False}),
        json.dumps({"message": {"role": "assistant", "content": c}, "done": False}),
        json.dumps(
            {
                "message": {"role": "assistant", "content": ""},
                "done": True,
                "prompt_eval_count": prompt_tokens,
                "eval_count": eval_tokens,
            }
        ),
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _patch_httpx_with_mock(monkeypatch, handler):
    """Swap ``httpx.AsyncClient`` for a thin wrapper that injects a
    ``MockTransport`` so ``_stream_ollama`` (which constructs its own
    ``AsyncClient`` internally) can be exercised end-to-end without real
    network IO.  Returns the captured request list for assertions.
    """
    import httpx

    captured: list[dict] = []

    async def _async_handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            {
                "method": request.method,
                "url": str(request.url),
                "headers": dict(request.headers),
                "json": json.loads(request.content.decode("utf-8") or "null"),
            }
        )
        return handler(request)

    transport = httpx.MockTransport(_async_handler)
    real_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _factory)
    return captured


def test_stream_ollama_parses_ndjson_and_returns_token_counts(monkeypatch):
    """Direct call into the NDJSON parser: deltas concatenate, terminal
    chunk supplies token counts, no network IO."""
    body = (
        '{"probability":0.62,"confidence":0.7,"lean":"YES","reasoning":"local"}'
    )

    def _handler(req):
        import httpx
        return httpx.Response(200, content=_ollama_ndjson_chunks(body, 100, 50))

    captured = _patch_httpx_with_mock(monkeypatch, _handler)

    text, in_tok, out_tok = _run(
        runner._stream_ollama(
            "sharp_edge", "llama3.1:8b", "sys", "user", 0.3
        )
    )

    assert text == body
    assert in_tok == 100
    assert out_tok == 50
    assert len(captured) == 1
    req = captured[0]
    assert req["method"] == "POST"
    assert req["url"].endswith("/api/chat")
    payload = req["json"]
    assert payload["model"] == "llama3.1:8b"
    assert payload["stream"] is True
    assert payload["options"]["temperature"] == 0.3
    assert payload["messages"][0] == {"role": "system", "content": "sys"}
    assert payload["messages"][1] == {"role": "user", "content": "user"}


def test_invoke_provider_routes_ollama(monkeypatch):
    """``_invoke_provider(provider='ollama', ...)`` must dispatch to
    ``_stream_ollama`` and not silently route to OpenAI/Anthropic."""
    seen: dict = {}

    async def fake_ollama(persona_id, model_id, system, user, temperature):
        seen["called"] = (persona_id, model_id, temperature)
        return "ok", 1, 2

    async def fake_openai(*a, **kw):
        seen["openai"] = True
        return "openai", 0, 0

    monkeypatch.setattr(runner, "_stream_ollama", fake_ollama)
    monkeypatch.setattr(runner, "_stream_openai", fake_openai)

    text, in_tok, out_tok = _run(
        runner._invoke_provider(
            persona_id="px",
            provider="ollama",
            model_id="llama3.1:8b",
            system="s",
            user="u",
            temperature=0.4,
        )
    )

    assert text == "ok" and (in_tok, out_tok) == (1, 2)
    assert seen["called"] == ("px", "llama3.1:8b", 0.4)
    assert "openai" not in seen


def test_persona_with_ollama_provider_records_zero_cost(monkeypatch):
    """A persona configured with provider='ollama' must price at $0 even
    when token counts are non-zero (local inference)."""
    body = (
        '{"probability":0.55,"confidence":0.6,"lean":"YES","reasoning":"local"}'
    )

    def _handler(req):
        import httpx
        return httpx.Response(200, content=_ollama_ndjson_chunks(body, 200, 80))

    _patch_httpx_with_mock(monkeypatch, _handler)

    local_persona = personas.Persona(
        id="local_test",
        display="Local Test",
        provider="ollama",
        model_id="llama3.1:8b",
        system_prompt="be concise",
        temperature=0.3,
        # Disable fallback to keep this test focused on the success path.
        fallback_provider="",
        fallback_model_id="",
    )

    verdict = _run(runner._call_persona(local_persona, "prompt"))

    assert verdict["provider"] == "ollama"
    assert verdict["model"] == "llama3.1:8b"
    assert verdict["fallback_used"] is False
    assert verdict["error"] is None
    assert verdict["probability"] == 0.55
    assert verdict["input_tokens"] == 200
    assert verdict["output_tokens"] == 80
    # Local inference → cost ledger records exactly $0 even with tokens.
    assert verdict["cost"] == 0.0


def test_ollama_5xx_falls_back_to_openai(monkeypatch):
    """A 503 from the Ollama box must trigger the cross-provider fallback
    so a downed local node never silences a persona."""

    def _handler(req):
        import httpx
        return httpx.Response(503, content=b'{"error":"model loading"}')

    _patch_httpx_with_mock(monkeypatch, _handler)

    openai_calls: list[tuple[str, str]] = []

    async def fake_openai(persona_id, model_id, system, user, temperature):
        openai_calls.append(("openai", model_id))
        return (
            '{"probability":0.71,"confidence":0.8,"lean":"YES","reasoning":"fb"}',
            30, 15,
        )

    monkeypatch.setattr(runner, "_stream_openai", fake_openai)

    local_persona = personas.Persona(
        id="local_test_fb",
        display="Local Test FB",
        provider="ollama",
        model_id="llama3.1:8b",
        system_prompt="be concise",
        temperature=0.3,
    )

    verdict = _run(runner._call_persona(local_persona, "prompt"))

    fb = personas.fallback_for(local_persona)
    assert fb == ("openai", "gpt-4o-mini")
    assert openai_calls == [("openai", "gpt-4o-mini")]
    assert verdict["fallback_used"] is True
    assert verdict["provider"] == "openai"
    assert verdict["model"] == "gpt-4o-mini"
    assert verdict["primary_provider"] == "ollama"
    assert verdict["primary_model"] == "llama3.1:8b"
    assert verdict["error"] is None  # fallback succeeded
    assert verdict["probability"] == 0.71
    # Cost is the OpenAI fallback's price, not $0 — the fallback was
    # NOT served from local hardware.
    assert verdict["cost"] == personas.estimate_cost("gpt-4o-mini", 30, 15)
    assert verdict["cost"] > 0


def test_is_retryable_recognises_ollama_provider_error():
    # 5xx and 429 → retry / cross-provider fallback.
    assert (
        runner._is_retryable_provider_error(
            runner.OllamaProviderError("boom", status_code=503)
        )
        is True
    )
    assert (
        runner._is_retryable_provider_error(
            runner.OllamaProviderError("rate", status_code=429)
        )
        is True
    )
    # Transport stall (no status_code) → retry.
    assert (
        runner._is_retryable_provider_error(
            runner.OllamaProviderError("transport stalled")
        )
        is True
    )
    # Non-retryable: a plain ValueError still must NOT trigger fallback.
    assert runner._is_retryable_provider_error(ValueError("nope")) is False


# ---------------------------------------------------------------------------
# Mac Studio cutover (task #735) — `AXIOM_LLM_MODE` rewrites the active
# persona set to point at Ollama (`local` / `hybrid`) or leaves it
# untouched (`cloud`).  These tests exercise the resolver directly and
# end-to-end through `_call_persona`, including a tripwire that
# `_stream_openai` / `_stream_anthropic` are NEVER invoked in `local`
# mode (a downed local box must surface as an error, not silently spend
# hosted-LLM credit).
# ---------------------------------------------------------------------------


def _stub_settings_for_mode(monkeypatch, mode: str) -> None:
    """Patch `personas.get_settings` (re-imported lazily inside the
    resolver) to return a stub with the requested mode + a known
    Ollama model triple, without touching the rest of the Settings
    surface.  Uses SimpleNamespace so we don't have to construct the
    full frozen dataclass.
    """
    from types import SimpleNamespace

    from app import config as app_config

    stub = SimpleNamespace(
        axiom_llm_mode=mode,
        ollama_model_fast="qwen-test:14b",
        ollama_model_reasoning="llama-test:70b",
        ollama_model_backup="deepseek-test:16b",
    )
    monkeypatch.setattr(app_config, "get_settings", lambda: stub)


def test_resolve_active_personas_cloud_mode_is_identity(monkeypatch):
    _stub_settings_for_mode(monkeypatch, "cloud")
    out = personas.resolve_active_personas(personas.PERSONAS)
    # Identity (same tuple) so existing replay / Brier paths are
    # byte-identical to today.
    assert out is personas.PERSONAS
    # And the moderator / gatekeeper getters are unchanged too.
    assert personas.active_moderator() is personas.MODERATOR
    assert personas.active_gatekeeper() is personas.GATEKEEPER


def test_resolve_active_personas_local_mode_rewrites_to_ollama(monkeypatch):
    _stub_settings_for_mode(monkeypatch, "local")

    out = personas.resolve_active_personas(personas.PERSONAS)
    assert len(out) == len(personas.PERSONAS)

    # Every persona — including the moderator + gatekeeper — must now
    # point at Ollama.  Light personas land on the fast model, the
    # injury / risk trio land on the reasoning model.
    expected_fast = {"sharp_edge", "momentum_form", "matchup_historian", "public_contrarian"}
    expected_reasoning = {"injury_roster", "risk_guardian"}
    for p in out:
        assert p.provider == "ollama", f"{p.id} stayed on {p.provider}"
        if p.id in expected_reasoning:
            assert p.model_id == "llama-test:70b"
        elif p.id in expected_fast:
            assert p.model_id == "qwen-test:14b"

    # Moderator → fast slot.
    mod = personas.active_moderator()
    assert mod.provider == "ollama"
    assert mod.model_id == "qwen-test:14b"

    # Gatekeeper → reasoning slot.
    gk = personas.active_gatekeeper()
    assert gk.provider == "ollama"
    assert gk.model_id == "llama-test:70b"

    # Local-mode fallback is the same-provider Ollama backup model — no
    # hosted-LLM model in the chain.  ``fallback_for`` must round-trip
    # to the configured backup, NOT the cross-provider default.
    for p in out:
        fb = personas.fallback_for(p)
        assert fb == ("ollama", "deepseek-test:16b"), (
            f"{p.id} fallback escaped local mode: {fb!r}"
        )

    # Backwards-compat tripwire: the underlying static PERSONAS tuple
    # is untouched so other callers (replay, weights) still see the
    # cloud baseline.
    assert personas.PERSONAS[0].provider == "openai"


def test_resolve_active_personas_hybrid_keeps_cross_provider_fallback(monkeypatch):
    _stub_settings_for_mode(monkeypatch, "hybrid")

    out = personas.resolve_active_personas(personas.PERSONAS)
    assert all(p.provider == "ollama" for p in out)

    # Hybrid leaves fallback_provider/model_id unset → resolver picks
    # up the default `_FALLBACK_BY_PROVIDER["ollama"]` chain so a
    # downed local node still produces a verdict via the cheapest
    # hosted model.
    for p in out:
        assert p.fallback_provider is None
        assert p.fallback_model_id is None
        fb = personas.fallback_for(p)
        assert fb == ("openai", "gpt-4o-mini"), (
            f"{p.id} hybrid fallback unexpectedly: {fb!r}"
        )


def test_local_mode_never_invokes_cloud_sdk_paths(monkeypatch):
    """End-to-end: in `local` mode every persona resolves to ollama
    and a primary failure on the local box must NOT trigger the
    OpenAI/Anthropic fallback (which would silently burn hosted-LLM
    credit on what is really a local-hardware outage).
    """
    _stub_settings_for_mode(monkeypatch, "local")

    body = '{"probability":0.6,"confidence":0.7,"lean":"YES","reasoning":"local"}'

    def _ok_handler(req):
        import httpx
        return httpx.Response(200, content=_ollama_ndjson_chunks(body, 30, 15))

    captured = _patch_httpx_with_mock(monkeypatch, _ok_handler)

    # Tripwire: any cloud SDK call fails the test.
    async def explode_openai(*a, **kw):
        raise AssertionError(
            "local mode must NEVER call _stream_openai"
        )

    async def explode_anthropic(*a, **kw):
        raise AssertionError(
            "local mode must NEVER call _stream_anthropic"
        )

    monkeypatch.setattr(runner, "_stream_openai", explode_openai)
    monkeypatch.setattr(runner, "_stream_anthropic", explode_anthropic)

    # Run every default persona + the moderator + the gatekeeper
    # through `_call_persona` after the local-mode rewrite.  All must
    # succeed against the mocked Ollama transport, none must touch the
    # cloud SDKs.
    active = personas.resolve_active_personas(personas.PERSONAS)
    for p in list(active) + [personas.active_moderator(), personas.active_gatekeeper()]:
        verdict = _run(runner._call_persona(p, "prompt"))
        assert verdict["provider"] == "ollama"
        assert verdict["fallback_used"] is False
        assert verdict["error"] is None
        # Local inference is free.
        assert verdict["cost"] == 0.0

    # Sanity: the mocked Ollama endpoint received one request per
    # persona (8 total — 6 personas + moderator + gatekeeper).
    assert len(captured) == 8
    assert all(req["url"].endswith("/api/chat") for req in captured)


def test_local_mode_5xx_does_not_fall_back_to_cloud(monkeypatch):
    """Same tripwire on the failure path: a 503 from the local Ollama
    box in `local` mode must surface to the operator instead of
    silently retrying against OpenAI.  In contrast `hybrid` mode keeps
    the cross-provider fallback (covered by
    `test_ollama_5xx_falls_back_to_openai`)."""
    _stub_settings_for_mode(monkeypatch, "local")

    def _503_handler(req):
        import httpx
        return httpx.Response(503, content=b'{"error":"model loading"}')

    _patch_httpx_with_mock(monkeypatch, _503_handler)

    async def explode_openai(*a, **kw):
        raise AssertionError(
            "local mode must NEVER fall back to _stream_openai on Ollama 5xx"
        )

    async def explode_anthropic(*a, **kw):
        raise AssertionError(
            "local mode must NEVER fall back to _stream_anthropic on Ollama 5xx"
        )

    monkeypatch.setattr(runner, "_stream_openai", explode_openai)
    monkeypatch.setattr(runner, "_stream_anthropic", explode_anthropic)

    sharp = personas.resolve_active_personas(personas.PERSONAS)[0]
    assert sharp.provider == "ollama"
    fb = personas.fallback_for(sharp)
    # Same-provider Ollama backup, NOT the cross-provider default.
    assert fb == ("ollama", "deepseek-test:16b")

    # Both primary and backup will 503 against the same mocked
    # transport — verdict must surface the failure, fallback_used is
    # True (local→local), and no cloud SDK was ever touched.
    verdict = _run(runner._call_persona(sharp, "prompt"))
    assert verdict["provider"] == "ollama"
    # Both attempts failed (primary + same-provider backup) → error
    # must surface to the operator.
    assert verdict["error"] is not None
    assert "fallback" in verdict["error"]
    assert verdict["cost"] == 0.0


def test_active_llm_mode_state_returns_resolved_triple(monkeypatch):
    _stub_settings_for_mode(monkeypatch, "local")
    state = personas.active_llm_mode_state()
    assert state == {
        "mode": "local",
        "ollama_model_fast": "qwen-test:14b",
        "ollama_model_reasoning": "llama-test:70b",
        "ollama_model_backup": "deepseek-test:16b",
    }


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 422])
def test_ollama_4xx_does_not_trigger_fallback(status_code):
    """A 4xx from Ollama (bad model, missing auth, malformed request)
    must surface to the operator instead of silently triggering the
    cross-provider fallback and burning hosted-LLM credit."""

    err = runner.OllamaProviderError(
        f"ollama HTTP {status_code}: model not found",
        status_code=status_code,
    )
    assert runner._is_retryable_provider_error(err) is False


def test_ollama_404_surfaces_instead_of_falling_back(monkeypatch):
    """End-to-end: a 404 from Ollama (typically ``model not found``)
    must surface in the verdict as a primary-provider error WITHOUT
    invoking the OpenAI fallback chain — otherwise a typo in the
    operator's local model name silently burns hosted-LLM credit."""

    def _handler(req):
        import httpx

        return httpx.Response(
            404, content=b'{"error":"model \\"phi9001\\" not found"}'
        )

    _patch_httpx_with_mock(monkeypatch, _handler)

    # Trip-wire: any call to the OpenAI fallback must fail the test.
    async def explode_openai(*args, **kwargs):
        raise AssertionError(
            "fallback to OpenAI must NOT trigger on Ollama 4xx"
        )

    monkeypatch.setattr(runner, "_stream_openai", explode_openai)

    local_persona = personas.Persona(
        id="local_test_404",
        display="Local Test 404",
        provider="ollama",
        model_id="phi9001",
        system_prompt="be concise",
        temperature=0.3,
    )

    verdict = _run(runner._call_persona(local_persona, "prompt"))

    # Primary provider stays Ollama, fallback was *not* used, error is
    # surfaced so the operator sees the misconfiguration.
    assert verdict["provider"] == "ollama"
    assert verdict["model"] == "phi9001"
    assert verdict["fallback_used"] is False
    assert verdict["error"] is not None
    assert "404" in str(verdict["error"]) or "not found" in str(
        verdict["error"]
    ).lower()
    # No hosted-LLM credit was burned.
    assert verdict["cost"] == 0
