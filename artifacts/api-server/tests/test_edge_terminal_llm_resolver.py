"""Tests for the LLM-backed fallback resolver."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.edge_terminal import llm_resolver


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _reset_cache_calls(monkeypatch):
    store: dict[str, dict[str, Any]] = {}

    async def _fake_cached(key: str):
        if key in store:
            r = store[key]
            return (r["ticker"], f"LLM cache: {r['reason']}")
        return None

    async def _fake_store(key, *, ticker, model, score, reason):
        store[key] = {"ticker": ticker, "model": model, "score": score, "reason": reason}

    monkeypatch.setattr(llm_resolver, "_cached", _fake_cached)
    monkeypatch.setattr(llm_resolver, "_store", _fake_store)
    yield store


def test_signal_key_is_stable_and_normalised():
    a = llm_resolver._signal_key({
        "vertical": "MLB", "matchup": "CIN @ NYM",
        "selection": "Mets", "pick_type": "Moneyline",
    })
    b = llm_resolver._signal_key({
        "vertical": "mlb", "matchup": "cin @ nym",
        "selection": "mets", "pick_type": "moneyline",
    })
    assert a == b
    c = llm_resolver._signal_key({
        "vertical": "mlb", "matchup": "cin @ nym",
        "selection": "reds", "pick_type": "moneyline",
    })
    assert a != c


def test_extract_json_strips_fences_and_chatter():
    assert llm_resolver._extract_json(
        '```json\n{"ticker": "KX-A", "confidence": 4}\n```'
    ) == {"ticker": "KX-A", "confidence": 4}
    assert llm_resolver._extract_json(
        'sure! {"ticker": null, "reason": "x"}'
    ) == {"ticker": None, "reason": "x"}
    assert llm_resolver._extract_json("nope") is None


def test_disabled_when_no_openai_key(monkeypatch):
    monkeypatch.setattr(llm_resolver, "_enabled", lambda: False)
    out, reason = _run(llm_resolver.llm_pick(
        {"vertical": "mlb", "matchup": "a @ b", "selection": "a"},
        [{"ticker": "KX-A", "title": "A vs B", "score": 1}],
    ))
    assert out is None
    assert "disabled" in reason.lower()


def test_picks_valid_ticker_from_candidates(monkeypatch):
    monkeypatch.setattr(llm_resolver, "_enabled", lambda: True)

    async def _fake_call(prompt: str):
        # Sanity: prompt must enumerate the ticker we're going to pick.
        assert "KX-MLB-CIN-NYM" in prompt
        return '{"ticker": "KX-MLB-CIN-NYM", "confidence": 5, "reason": "Mets win"}'

    monkeypatch.setattr(llm_resolver, "_call_openai", _fake_call)
    out, reason = _run(llm_resolver.llm_pick(
        {"vertical": "mlb", "matchup": "CIN @ NYM",
         "selection": "Mets", "pick_type": "moneyline"},
        [
            {"ticker": "KX-MLB-CIN-NYM", "title": "CIN vs NYM", "score": 4},
            {"ticker": "KX-MLB-OTHER", "title": "ATL vs LAD", "score": 1},
        ],
    ))
    assert out == "KX-MLB-CIN-NYM"
    assert "llm matched" in reason.lower()


def test_rejects_hallucinated_ticker(monkeypatch):
    """LLM picks a ticker we never showed it → must be discarded."""
    monkeypatch.setattr(llm_resolver, "_enabled", lambda: True)

    async def _fake_call(prompt: str):
        return '{"ticker": "KX-MADE-UP-12345", "confidence": 5, "reason": "x"}'

    monkeypatch.setattr(llm_resolver, "_call_openai", _fake_call)
    out, reason = _run(llm_resolver.llm_pick(
        {"vertical": "mlb", "matchup": "CIN @ NYM", "selection": "Mets"},
        [{"ticker": "KX-MLB-CIN-NYM", "title": "CIN vs NYM", "score": 4}],
    ))
    assert out is None
    assert "unknown" in reason.lower() or "hallucin" in reason.lower()


def test_decline_means_no_ticker(monkeypatch):
    monkeypatch.setattr(llm_resolver, "_enabled", lambda: True)

    async def _fake_call(prompt: str):
        return '{"ticker": null, "confidence": 1, "reason": "no team match"}'

    monkeypatch.setattr(llm_resolver, "_call_openai", _fake_call)
    out, reason = _run(llm_resolver.llm_pick(
        {"vertical": "tennis", "matchup": "Sinner vs Alcaraz",
         "selection": "Sinner"},
        [{"ticker": "KX-ATP-X", "title": "Djokovic vs Nadal", "score": 1}],
    ))
    assert out is None
    assert "declined" in reason.lower()


def test_cache_hit_skips_llm_call(monkeypatch, _reset_cache_calls):
    """Second call with same signal must NOT invoke the LLM."""
    monkeypatch.setattr(llm_resolver, "_enabled", lambda: True)

    call_count = {"n": 0}

    async def _fake_call(prompt: str):
        call_count["n"] += 1
        return '{"ticker": "KX-A", "confidence": 4, "reason": "ok"}'

    monkeypatch.setattr(llm_resolver, "_call_openai", _fake_call)
    sig = {"vertical": "mlb", "matchup": "A @ B", "selection": "A"}
    cands = [{"ticker": "KX-A", "title": "A vs B", "score": 3}]

    _run(llm_resolver.llm_pick(sig, cands))
    _run(llm_resolver.llm_pick(sig, cands))  # cache hit
    assert call_count["n"] == 1
