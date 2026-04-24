"""Tests for the per-provider LLM fallback-rate dashboard endpoint.

The dashboard panel reuses the same query/window/threshold/min-sample
knobs as the ``llm-fallback-spike`` alert in
``app.swarm.fallback_monitor`` so the panel and the alert never disagree.
We stub the underlying ``fetch_provider_stats`` helper so these tests
exercise pure shaping logic without touching a real database.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import dashboard, main as main_module
from app.auth import AuthedUser, require_approved_user
from app.main import app
from app.swarm import fallback_monitor as _monitor


def _stat(provider: str, total: int, fallback: int,
          reasons: list[str] | None = None) -> dict[str, Any]:
    rate = (fallback / total) if total else 0.0
    return {
        "provider": provider,
        "total": total,
        "fallback": fallback,
        "rate": rate,
        "reasons": list(reasons or []),
    }


@pytest.fixture
def stub_fetch(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"rows": [], "calls": []}

    async def _fake(window_minutes: int) -> list[dict[str, Any]]:
        state["calls"].append(window_minutes)
        return list(state["rows"])

    monkeypatch.setattr(_monitor, "fetch_provider_stats", _fake)
    return state


def test_helper_returns_window_threshold_and_items(
    stub_fetch: dict[str, Any]
) -> None:
    stub_fetch["rows"] = [
        _stat("openai", total=20, fallback=8, reasons=["RateLimitError"]),
        _stat("anthropic", total=15, fallback=1),
    ]
    out = asyncio.run(dashboard.get_provider_fallback_rates())
    # Helper must consult the alert window, not invent one.
    assert stub_fetch["calls"] == [out["window_minutes"]]
    assert out["window_minutes"] > 0
    assert out["threshold_pct"] > 0
    assert out["min_samples"] >= 1
    assert out["alert_enabled"] in (True, False)
    items = out["items"]
    assert {r["provider"] for r in items} == {"openai", "anthropic"}
    openai = next(r for r in items if r["provider"] == "openai")
    assert openai["total"] == 20
    assert openai["fallback"] == 8
    assert openai["rate"] == pytest.approx(0.4)
    assert openai["reasons"] == ["RateLimitError"]


def test_helper_marks_would_alert_when_over_threshold(
    stub_fetch: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pin the alert knobs so the test owns the math.
    from app import config as _config

    _config.get_settings.cache_clear()
    monkeypatch.setenv("AXIOM_LLM_FALLBACK_ALERT_THRESHOLD_PCT", "30")
    monkeypatch.setenv("AXIOM_LLM_FALLBACK_ALERT_MIN_SAMPLES", "5")
    try:
        stub_fetch["rows"] = [
            _stat("openai", total=10, fallback=6),       # 60% > 30%, n>=5
            _stat("anthropic", total=10, fallback=1),    # 10% < 30%
            _stat("xai", total=2, fallback=2),           # 100% but n<5
        ]
        out = asyncio.run(dashboard.get_provider_fallback_rates())
        flags = {r["provider"]: r["would_alert"] for r in out["items"]}
        assert flags == {"openai": True, "anthropic": False, "xai": False}
    finally:
        monkeypatch.delenv("AXIOM_LLM_FALLBACK_ALERT_THRESHOLD_PCT", raising=False)
        monkeypatch.delenv("AXIOM_LLM_FALLBACK_ALERT_MIN_SAMPLES", raising=False)
        _config.get_settings.cache_clear()


def test_helper_swallows_query_errors(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom(window_minutes: int) -> list[dict[str, Any]]:
        raise RuntimeError("db down")

    monkeypatch.setattr(_monitor, "fetch_provider_stats", _boom)
    out = asyncio.run(dashboard.get_provider_fallback_rates())
    # Failure surfaces as an empty list, not a 500 — the rest of the
    # operator dashboard must keep rendering.
    assert out["items"] == []
    assert out["window_minutes"] > 0


def test_endpoint_returns_helper_payload(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {
        "window_minutes": 15,
        "threshold_pct": 25.0,
        "min_samples": 8,
        "alert_enabled": True,
        "items": [
            {
                "provider": "openai",
                "total": 12,
                "fallback": 4,
                "rate": 0.3333,
                "reasons": ["RateLimitError"],
                "would_alert": True,
            }
        ],
    }

    async def _fake() -> dict[str, Any]:
        return payload

    monkeypatch.setattr(
        main_module.dashboard, "get_provider_fallback_rates", _fake
    )

    async def _fake_user() -> AuthedUser:
        return AuthedUser(user_id="op_1", session_id="sess_t", claims={})

    app.dependency_overrides[require_approved_user] = _fake_user
    try:
        client = TestClient(app)
        r = client.get("/api/dashboard/llm-fallback-rates")
    finally:
        app.dependency_overrides.pop(require_approved_user, None)
    assert r.status_code == 200, r.text
    assert r.json() == payload


def test_fetch_provider_stats_sorts_by_rate_desc(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """The helper sorts so the worst offender is first — operators
    eyeballing the panel should see a creeping provider before they
    have to scan the whole list."""
    from app import db as _db

    async def _fake_fetch(query: str, *args: Any) -> list[dict[str, Any]]:
        return [
            {"provider": "anthropic", "total": 10, "fallback": 1, "reasons": []},
            {"provider": "openai", "total": 20, "fallback": 12, "reasons": []},
            {"provider": "xai", "total": 5, "fallback": 3, "reasons": []},
        ]

    monkeypatch.setattr(_db, "fetch", _fake_fetch)
    rows = asyncio.run(_monitor.fetch_provider_stats(15))
    providers = [r["provider"] for r in rows]
    # 60% openai, 60% xai (same rate, openai has more samples → first),
    # 10% anthropic last.
    assert providers == ["openai", "xai", "anthropic"]
