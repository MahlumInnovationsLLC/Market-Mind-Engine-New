"""Regression tests for operator-tunable aggression mode (Task #100).

Covers:
* `_cached_debates` filters out debates whose stored mode differs from the
  active aggression mode (otherwise post-switch trades could come from
  stale moderator/gatekeeper verdicts biased for the prior mode).
* `/api/swarm/debate` defaults to the global active aggression mode when
  the request omits it, keeping direct debates consistent with cycles.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app import runtime_settings as runtime_settings_mod
from app.orchestrator import cycle as cycle_mod


def test_cached_debates_filters_out_stale_mode(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_fetch(sql: str, *params: Any) -> list[dict[str, Any]]:
        captured["sql"] = sql
        captured["params"] = params
        return []

    monkeypatch.setattr(cycle_mod.db, "fetch", fake_fetch)
    monkeypatch.setattr(cycle_mod, "_league_filter", lambda v: [])

    async def go():
        await cycle_mod._cached_debates(
            "nfl", limit=10, aggression_mode="aggressive"
        )

    asyncio.run(go())

    assert "aggression_mode" in captured["sql"], (
        "cached-debate query must filter by aggression_mode when supplied"
    )
    assert "aggressive" in captured["params"]


def test_cached_debates_no_mode_filter_when_unspecified(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_fetch(sql: str, *params: Any) -> list[dict[str, Any]]:
        captured["sql"] = sql
        captured["params"] = params
        return []

    monkeypatch.setattr(cycle_mod.db, "fetch", fake_fetch)
    monkeypatch.setattr(cycle_mod, "_league_filter", lambda v: [])

    async def go():
        await cycle_mod._cached_debates("nfl", limit=10)

    asyncio.run(go())

    assert "aggression_mode" not in captured["sql"], (
        "cached-debate query should not add mode filter when caller omits it"
    )


def test_swarm_debate_endpoint_defaults_to_active_mode(monkeypatch):
    """When the request omits aggression_mode, /api/swarm/debate must
    inject the global runtime mode rather than the literal default."""
    from app import main as app_main

    async def fake_get_mode() -> str:
        return "conservative"

    captured: dict[str, Any] = {}

    async def fake_run_debate(payload: dict[str, Any]) -> dict[str, Any]:
        captured["payload"] = payload
        return {"ok": True}

    class _Settings:
        openai_api_key = "x"
        anthropic_api_key = "y"

    monkeypatch.setattr(
        runtime_settings_mod, "get_aggression_mode", fake_get_mode
    )
    monkeypatch.setattr(
        app_main.runtime_settings_mod, "get_aggression_mode", fake_get_mode
    )
    monkeypatch.setattr(app_main, "run_debate", fake_run_debate)
    monkeypatch.setattr(app_main, "get_settings", lambda: _Settings())

    body = app_main.SwarmDebateRequest(
        game_id="g1", matchup="A vs B", league="NFL"
    )

    async def go():
        await app_main.swarm_debate(body=body, _=object())

    asyncio.run(go())

    assert captured["payload"]["aggression_mode"] == "conservative"


def test_swarm_debate_endpoint_respects_explicit_override(monkeypatch):
    from app import main as app_main

    async def fake_get_mode() -> str:
        return "conservative"

    captured: dict[str, Any] = {}

    async def fake_run_debate(payload: dict[str, Any]) -> dict[str, Any]:
        captured["payload"] = payload
        return {"ok": True}

    class _Settings:
        openai_api_key = "x"
        anthropic_api_key = "y"

    monkeypatch.setattr(
        app_main.runtime_settings_mod, "get_aggression_mode", fake_get_mode
    )
    monkeypatch.setattr(app_main, "run_debate", fake_run_debate)
    monkeypatch.setattr(app_main, "get_settings", lambda: _Settings())

    body = app_main.SwarmDebateRequest(
        game_id="g1",
        matchup="A vs B",
        league="NFL",
        aggression_mode="aggressive",
    )

    async def go():
        await app_main.swarm_debate(body=body, _=object())

    asyncio.run(go())

    assert captured["payload"]["aggression_mode"] == "aggressive"
