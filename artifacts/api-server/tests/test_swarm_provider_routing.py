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
