"""Gatekeeper fail-closed tests for swarm.runner.

The gatekeeper is the last veto in front of the order pipe.  When the
gatekeeper LLM call cannot return a definitive `approved=true` verdict
(provider outage, malformed JSON, both fallbacks exhausted), the trade
MUST be forced to PASS — never silently fall through to whatever the
moderator suggested.  These tests pin that contract.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.swarm import personas, runner


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        asyncio.set_event_loop(None)
        loop.close()


_PERSONA_VERDICT_TEMPLATE = {
    "probability": 0.62,
    "confidence": 0.7,
    "lean": "YES",
    "reasoning": "stub",
    "raw_text": "{}",
    "weight": 1.0,
    "cost": 0.0,
    "latency_ms": 1,
    "model": "stub",
    "provider": "openai",
    "fallback_used": False,
}


_MODERATOR_RAW = json.dumps(
    {
        "consensus_probability": 0.62,
        "confidence_band_low": 0.55,
        "confidence_band_high": 0.7,
        "agreement_rate": 0.83,
        "recommended_action": "BUY_YES",
        "synthesis": "moderator says go",
    }
)


def _stub_persona_call(gatekeeper_behaviour):
    """Build a `_call_persona` replacement.

    `gatekeeper_behaviour(prompt) -> (raw_text | Exception)` is called
    only for the gatekeeper persona; every other persona returns the
    stock verdict / moderator JSON.
    """

    async def _impl(persona, user_prompt):
        if persona.id == personas.MODERATOR.id:
            return {**_PERSONA_VERDICT_TEMPLATE, "persona": persona.id, "raw_text": _MODERATOR_RAW}
        if persona.id == personas.GATEKEEPER.id:
            outcome = gatekeeper_behaviour(user_prompt)
            if isinstance(outcome, BaseException):
                raise outcome
            return {**_PERSONA_VERDICT_TEMPLATE, "persona": persona.id, "raw_text": outcome}
        return {**_PERSONA_VERDICT_TEMPLATE, "persona": persona.id}

    return _impl


@pytest.fixture(autouse=True)
def _patch_runner(monkeypatch):
    runner.reset_gatekeeper_failure_counters()

    async def _no_publish(_event):
        return None

    async def _stub_persist(**kwargs):
        return "stub-debate-id"

    async def _no_weight(*args, **kwargs):
        return 1.0

    monkeypatch.setattr(runner, "_publish", _no_publish)
    monkeypatch.setattr(runner, "_persist_debate", _stub_persist)
    # Skip DB-backed weight resolution (every persona gets a flat 1.0).
    from app.swarm import weights as _weights
    monkeypatch.setattr(_weights, "get_persona_weight", _no_weight)
    yield
    runner.reset_gatekeeper_failure_counters()


def _candidate() -> dict:
    return {
        "game_id": "GAME_X",
        "matchup": "A @ B",
        "league": "NBA",
        "market_type": "moneyline",
        "side": "YES",
        "current_price": 0.55,
        "context": "test",
    }


def test_gatekeeper_approved_false_forces_pass(monkeypatch):
    """When the gatekeeper explicitly vetoes (approved=false), the
    moderator's BUY_YES is overridden to PASS."""
    raw = json.dumps({"approved": False, "veto_reason": "risk gate 7"})
    monkeypatch.setattr(runner, "_call_persona", _stub_persona_call(lambda _: raw))
    out = _run(runner.run_debate(_candidate()))
    assert out["approved"] is False
    assert out["recommended_action"] == "PASS"


def test_gatekeeper_call_exception_forces_pass(monkeypatch):
    """When the gatekeeper LLM call raises (both providers down), the
    debate completes successfully with PASS rather than crashing."""
    monkeypatch.setattr(
        runner,
        "_call_persona",
        _stub_persona_call(lambda _: RuntimeError("openai+anthropic both 503")),
    )
    out = _run(runner.run_debate(_candidate()))
    assert out["approved"] is False
    assert out["recommended_action"] == "PASS"
    counters = runner.gatekeeper_failure_counters()
    assert counters.get("call_error", 0) == 1


def test_gatekeeper_malformed_json_forces_pass(monkeypatch):
    """When the gatekeeper returns garbage that can't be parsed, the
    debate fails-closed to PASS and bumps the malformed_json counter."""
    monkeypatch.setattr(
        runner,
        "_call_persona",
        _stub_persona_call(lambda _: "absolutely not json at all"),
    )
    out = _run(runner.run_debate(_candidate()))
    assert out["approved"] is False
    assert out["recommended_action"] == "PASS"
    counters = runner.gatekeeper_failure_counters()
    assert counters.get("malformed_json", 0) == 1


def test_gatekeeper_approved_true_without_action_forces_pass(monkeypatch):
    """A syntactically-valid `{"approved": true}` with NO recommended
    action (or an unknown action) is a contract violation: it must
    fail-closed instead of silently inheriting the moderator's BUY_YES.
    Pins the strict-schema check the architect flagged."""
    raw = json.dumps({"approved": True})
    monkeypatch.setattr(runner, "_call_persona", _stub_persona_call(lambda _: raw))
    out = _run(runner.run_debate(_candidate()))
    assert out["approved"] is False
    assert out["recommended_action"] == "PASS"
    counters = runner.gatekeeper_failure_counters()
    assert counters.get("malformed_json", 0) == 1


def test_gatekeeper_wrong_type_for_approved_forces_pass(monkeypatch):
    """`approved` must be a real bool — a truthy string like
    "true" or 1 must NOT be treated as approval."""
    raw = json.dumps({"approved": "true", "recommended_action": "BUY_YES"})
    monkeypatch.setattr(runner, "_call_persona", _stub_persona_call(lambda _: raw))
    out = _run(runner.run_debate(_candidate()))
    assert out["approved"] is False
    assert out["recommended_action"] == "PASS"
    counters = runner.gatekeeper_failure_counters()
    assert counters.get("malformed_json", 0) == 1


def test_gatekeeper_unknown_action_forces_pass(monkeypatch):
    """Even if `approved=true`, an action outside the closed enum
    (e.g. "BUY_MAYBE") must fail-closed."""
    raw = json.dumps({"approved": True, "recommended_action": "BUY_MAYBE"})
    monkeypatch.setattr(runner, "_call_persona", _stub_persona_call(lambda _: raw))
    out = _run(runner.run_debate(_candidate()))
    assert out["approved"] is False
    assert out["recommended_action"] == "PASS"


def test_gatekeeper_approved_true_passes_through_buy(monkeypatch):
    """Sanity: when the gatekeeper approves and recommends BUY_YES,
    that action survives so we know the fail-closed path isn't always
    triggering."""
    raw = json.dumps({"approved": True, "recommended_action": "BUY_YES"})
    monkeypatch.setattr(runner, "_call_persona", _stub_persona_call(lambda _: raw))
    out = _run(runner.run_debate(_candidate()))
    assert out["approved"] is True
    assert out["recommended_action"] == "BUY_YES"
    counters = runner.gatekeeper_failure_counters()
    assert counters.get("call_error", 0) == 0
    assert counters.get("malformed_json", 0) == 0
