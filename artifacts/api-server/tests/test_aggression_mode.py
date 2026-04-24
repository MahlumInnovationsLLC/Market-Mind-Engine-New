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


# ---------------------------------------------------------------------------
# Task #136: reason note + scheduled auto-revert
# ---------------------------------------------------------------------------


def _stub_db(monkeypatch, store: dict[str, Any], audit: list[dict[str, Any]]):
    """Tiny in-memory stand-in for the asyncpg connection used by
    runtime_settings.  We mock the bits we hit (`db.acquire`, `db.fetch*`)
    so the unit tests don't need a live Postgres."""

    class _Conn:
        async def execute(self, sql: str, *params: Any) -> None:
            sql_l = sql.strip().lower()
            if sql_l.startswith("create table"):
                return
            if sql_l.startswith("insert into runtime_settings"):
                # Two shapes:
                #   ('aggression_mode', $1, now(), $2) → params=(value, by)
                #   ($1, $2, now(), $3)                → params=(key, value, by)
                if "'aggression_mode'" in sql_l:
                    value, _by = params[0], params[1]
                    store["aggression_mode"] = {"value": value, "updated_by": _by}
                else:
                    key, value, _by = params[0], params[1], params[2]
                    store[key] = {"value": value, "updated_by": _by}
                return
            if sql_l.startswith("delete from runtime_settings"):
                store.pop(params[0], None)
                return
            if sql_l.startswith("insert into admin_audit_log"):
                audit.append(
                    {
                        "actor_user_id": params[0],
                        "actor_email": params[1],
                        "action": params[4],
                        "before_value": params[5],
                        "after_value": params[6],
                    }
                )
                return

    class _Acquire:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *_: Any) -> None:
            return None

    async def fake_fetchval(sql: str, *params: Any):
        sql_l = sql.strip().lower()
        if "where key = $1" in sql_l and params:
            row = store.get(params[0])
            return row["value"] if row else None
        if "where key = 'aggression_mode'" in sql_l:
            row = store.get("aggression_mode")
            return row["value"] if row else None
        if "where key = 'trading_halted'" in sql_l:
            row = store.get("trading_halted")
            return row["value"] if row else None
        return None

    async def fake_fetch(sql: str, *params: Any):
        sql_l = sql.strip().lower()
        if "from admin_audit_log" in sql_l:
            from datetime import datetime, timezone

            rows = [
                {
                    "actor_user_id": a["actor_user_id"],
                    "actor_email": a["actor_email"],
                    "before_value": a["before_value"],
                    "after_value": a["after_value"],
                    "created_at": datetime.now(timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                }
                for a in audit
                if a["action"] == "set_aggression_mode"
            ]
            limit = params[0] if params else 5
            return list(reversed(rows))[:limit]
        return []

    async def fake_fetchrow(sql: str, *params: Any):
        return None

    monkeypatch.setattr(runtime_settings_mod.db, "acquire", lambda: _Acquire())
    monkeypatch.setattr(runtime_settings_mod.db, "fetchval", fake_fetchval)
    monkeypatch.setattr(runtime_settings_mod.db, "fetch", fake_fetch)
    monkeypatch.setattr(runtime_settings_mod.db, "fetchrow", fake_fetchrow)


def test_set_aggression_mode_records_reason_in_audit_log(monkeypatch):
    store: dict[str, Any] = {}
    audit: list[dict[str, Any]] = []
    _stub_db(monkeypatch, store, audit)

    async def go():
        await runtime_settings_mod.set_aggression_mode(
            "aggressive",
            actor_user_id="user-1",
            actor_email="op@example.com",
            reason="NFL primetime — go big",
        )

    asyncio.run(go())

    set_rows = [a for a in audit if a["action"] == "set_aggression_mode"]
    assert len(set_rows) == 1
    import json

    after = json.loads(set_rows[0]["after_value"])
    assert after["mode"] == "aggressive"
    assert after["reason"] == "NFL primetime — go big"
    assert "pending_revert" not in after


def test_set_aggression_mode_schedules_revert(monkeypatch):
    store: dict[str, Any] = {}
    audit: list[dict[str, Any]] = []
    _stub_db(monkeypatch, store, audit)

    from datetime import datetime, timedelta, timezone

    future = (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat()

    async def go():
        await runtime_settings_mod.set_aggression_mode(
            "conservative",
            actor_user_id="user-1",
            actor_email="op@example.com",
            reason="quiet hours",
            revert_at=future,
            revert_to_mode="balanced",
        )
        return await runtime_settings_mod.get_pending_revert()

    pending = asyncio.run(go())
    assert pending is not None
    assert pending["target"] == "balanced"
    assert pending["from_mode"] == "conservative"
    assert pending["reason"] == "quiet hours"


def test_set_aggression_mode_rejects_past_revert(monkeypatch):
    store: dict[str, Any] = {}
    audit: list[dict[str, Any]] = []
    _stub_db(monkeypatch, store, audit)

    from datetime import datetime, timedelta, timezone

    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()

    async def go():
        await runtime_settings_mod.set_aggression_mode(
            "aggressive",
            actor_user_id="user-1",
            actor_email=None,
            revert_at=past,
        )

    import pytest

    with pytest.raises(ValueError):
        asyncio.run(go())


def test_apply_due_revert_flips_mode(monkeypatch):
    store: dict[str, Any] = {}
    audit: list[dict[str, Any]] = []
    _stub_db(monkeypatch, store, audit)

    from datetime import datetime, timedelta, timezone
    import json

    # Pre-seed an aggressive mode + a revert that's already due.
    store["aggression_mode"] = {"value": "aggressive", "updated_by": "user-1"}
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    store[runtime_settings_mod.AGGRESSION_REVERT_KEY] = {
        "value": json.dumps(
            {
                "at": past,
                "target": "balanced",
                "from_mode": "aggressive",
                "by_user_id": "user-1",
                "by_email": "op@example.com",
                "reason": "primetime over",
            }
        ),
        "updated_by": "user-1",
    }

    async def go():
        applied = await runtime_settings_mod.apply_due_revert()
        return applied, await runtime_settings_mod.get_aggression_mode()

    applied, mode = asyncio.run(go())
    assert applied is not None
    assert mode == "balanced"
    # Revert row should be gone.
    assert runtime_settings_mod.AGGRESSION_REVERT_KEY not in store


def test_get_aggression_mode_history_returns_recent(monkeypatch):
    store: dict[str, Any] = {}
    audit: list[dict[str, Any]] = []
    _stub_db(monkeypatch, store, audit)

    async def go():
        await runtime_settings_mod.set_aggression_mode(
            "aggressive", actor_user_id="u", actor_email="a@x", reason="r1"
        )
        await runtime_settings_mod.set_aggression_mode(
            "balanced", actor_user_id="u", actor_email="a@x", reason="r2"
        )
        return await runtime_settings_mod.get_aggression_mode_history(limit=5)

    rows = asyncio.run(go())
    assert len(rows) == 2
    # Most recent first (after our reversed audit-log return path).
    assert rows[0]["to_mode"] == "balanced"
    assert rows[0]["reason"] == "r2"
    assert rows[1]["to_mode"] == "aggressive"
    assert rows[1]["reason"] == "r1"
