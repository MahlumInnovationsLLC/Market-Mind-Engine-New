"""Regression tests for the per-vertical dashboard overview (Task #204).

The overview is heavily DB-bound; we stub `app.db` so the unit suite can
exercise the shape and visibility-merge logic without a live Supabase
connection.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app import dashboard, runtime_settings as runtime_settings_mod


class _StubDb:
    """Minimal asyncpg-shaped stub with deterministic, query-routed
    responses. Only the methods exercised by the overview helpers are
    implemented."""

    def __init__(self, *, trades_per_vertical: dict[str, dict[str, Any]] | None = None,
                 brier: dict[str, float] | None = None,
                 daily: dict[str, list[float]] | None = None,
                 open_pos: dict[str, int] | None = None,
                 realized: dict[str, float] | None = None,
                 cycles: dict[str, dict[str, Any]] | None = None,
                 visibility_rows: list[dict[str, Any]] | None = None) -> None:
        self.trades_per_vertical = trades_per_vertical or {}
        self.brier = brier or {}
        self.daily = daily or {}
        self.open_pos = open_pos or {}
        self.realized = realized or {}
        self.cycles = cycles or {}
        self.visibility_rows = visibility_rows or []
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        v = args[0] if args else None
        if "FROM trades" in sql and "AS realized" in sql:
            r = self.realized.get(v)
            return {"realized": r} if r is not None else {"realized": None}
        if "FROM trades" in sql and "AS n" in sql:
            agg = self.trades_per_vertical.get(v)
            if not agg:
                return {"n": 0, "pnl": 0.0, "wins": 0, "losses": 0, "win_rate": None, "avg_edge": None}
            return {
                "n": agg.get("n", 0),
                "pnl": agg.get("pnl", 0.0),
                "wins": agg.get("wins", 0),
                "losses": agg.get("losses", 0),
                "win_rate": agg.get("win_rate"),
                "avg_edge": agg.get("avg_edge"),
            }
        if "FROM trading_cycles" in sql:
            return self.cycles.get(v)
        return None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        v = args[0] if args else None
        if "FROM calibration_snapshots" in sql:
            return self.brier.get(v)
        if "FROM positions" in sql:
            return self.open_pos.get(v, 0)
        return None

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        # Daily P&L grouping
        if "date_trunc('day'" in sql and "FROM trades" in sql:
            v = args[0] if args else None
            return [{"d": i, "daily_pnl": p} for i, p in enumerate(self.daily.get(v, []))]
        # System-settings visibility read
        if "FROM system_settings" in sql:
            return list(self.visibility_rows)
        return []

    class _Conn:
        def __init__(self, parent: "_StubDb") -> None:
            self.parent = parent
        async def execute(self, sql: str, *args: Any) -> str:
            self.parent.executed.append((sql, args))
            return "OK"
        async def fetchrow(self, sql: str, *args: Any):
            return await self.parent.fetchrow(sql, *args)

    def acquire(self):
        parent = self
        class _Ctx:
            async def __aenter__(self_inner): return _StubDb._Conn(parent)
            async def __aexit__(self_inner, *exc): return None
        return _Ctx()


@pytest.fixture
def stub_db(monkeypatch):
    """Install a fresh stub DB on both modules and reset the cached
    schema-bootstrap flag so each test gets clean state."""
    stub = _StubDb()
    monkeypatch.setattr(dashboard, "db", stub, raising=True)
    monkeypatch.setattr(runtime_settings_mod, "db", stub, raising=True)
    runtime_settings_mod._system_settings_ready = True
    return stub


def test_overview_emits_one_entry_per_registered_vertical(stub_db):
    from app import verticals as _verticals

    spec_names = [s.name for s in _verticals.all_specs()]
    assert spec_names, "registry should expose at least one vertical"

    out = asyncio.run(dashboard.get_verticals_overview(days=7))

    assert out["days"] == 7
    assert {e["vertical"] for e in out["verticals"]} == set(spec_names)
    assert out["visibility"] == {"hidden": [], "pinned": []}
    # Empty DB → no halts surfaced on the public overview.
    assert out["halts"] == {}
    # Every entry must carry the contract keys, even when DB returned nothing.
    for e in out["verticals"]:
        for k in ("trades", "wins", "losses", "pnl", "open_positions",
                  "hidden", "pinned", "leagues", "enabled", "halted", "halt"):
            assert k in e
        # Empty DB → safe defaults
        assert e["trades"] == 0
        assert e["pnl"] == 0.0
        assert e["open_positions"] == 0
        assert e["sharpe"] is None
        assert e["brier"] is None
        assert e["last_cycle"] is None
        assert e["halted"] is False
        assert e["halt"] is None


def test_overview_surfaces_halts_to_non_admin_consumers(stub_db):
    """The dashboard endpoint must include halt metadata so non-admin
    operators can see which verticals are currently sat out without
    needing /system access (Task #302)."""
    from app import verticals as _verticals

    target = _verticals.all_specs()[0].name
    halt_meta = {
        "by_user_id": "user_abc",
        "by_email": "ops@example.com",
        "at": "2026-04-22T12:34:56Z",
        "reason": "primetime drift",
    }
    stub_db.visibility_rows = [
        {
            "key": f"{runtime_settings_mod.VERTICAL_HALT_KEY_PREFIX}{target}",
            "value": json.dumps(halt_meta),
        },
    ]

    out = asyncio.run(dashboard.get_verticals_overview(days=7))
    assert target in out["halts"]
    assert out["halts"][target]["by_email"] == "ops@example.com"

    halted_entry = next(e for e in out["verticals"] if e["vertical"] == target)
    assert halted_entry["halted"] is True
    assert halted_entry["halt"] == halt_meta

    # Other entries stay non-halted.
    other = next(e for e in out["verticals"] if e["vertical"] != target)
    assert other["halted"] is False
    assert other["halt"] is None


def test_overview_populates_metrics_from_stubbed_db(stub_db, monkeypatch):
    from app import verticals as _verticals

    target = _verticals.all_specs()[0].name
    stub_db.trades_per_vertical = {
        target: {"n": 12, "pnl": 348.5, "wins": 8, "losses": 4,
                 "win_rate": 0.6667, "avg_edge": 0.05},
    }
    stub_db.brier = {target: 0.214}
    stub_db.daily = {target: [10.0, -5.0, 22.0, 8.0]}
    stub_db.open_pos = {target: 3}
    stub_db.realized = {target: 0.038}
    stub_db.cycles = {target: {
        "id": 99, "picks_evaluated": 17, "trades_placed": 4,
        "errors": None, "started_at": "2026-04-22T10:00:00Z",
        "completed_at": "2026-04-22T10:00:14Z",
    }}

    out = asyncio.run(dashboard.get_verticals_overview(days=7))
    entry = next(e for e in out["verticals"] if e["vertical"] == target)

    assert entry["trades"] == 12
    assert entry["pnl"] == pytest.approx(348.5)
    assert entry["wins"] == 8
    assert entry["losses"] == 4
    assert entry["win_rate"] == pytest.approx(0.6667)
    assert entry["edge_predicted_bps"] == pytest.approx(500.0)
    assert entry["edge_realized_bps"] == pytest.approx(380.0)
    assert entry["brier"] == pytest.approx(0.214)
    assert entry["open_positions"] == 3
    assert entry["sharpe"] is not None and entry["sharpe"] != 0
    assert entry["last_cycle"]["id"] == 99


def test_overview_merges_visibility_and_pins_first(stub_db):
    from app import verticals as _verticals

    names = [s.name for s in _verticals.all_specs()]
    # Pin the LAST registered vertical so a passing test means the pin
    # actually moved it, not just registry order coincidence.
    pinned_target = names[-1]
    hidden_target = names[0]

    stub_db.visibility_rows = [
        {"key": runtime_settings_mod.VERTICAL_VISIBILITY_HIDDEN_KEY,
         "value": json.dumps([hidden_target])},
        {"key": runtime_settings_mod.VERTICAL_VISIBILITY_PINNED_KEY,
         "value": json.dumps([pinned_target])},
    ]

    out = asyncio.run(dashboard.get_verticals_overview(days=7))
    assert out["verticals"][0]["vertical"] == pinned_target
    assert out["verticals"][0]["pinned"] is True

    hidden_entry = next(e for e in out["verticals"] if e["vertical"] == hidden_target)
    assert hidden_entry["hidden"] is True
    assert out["visibility"]["hidden"] == [hidden_target]
    assert out["visibility"]["pinned"] == [pinned_target]


def test_overview_partial_failure_does_not_blank_row(stub_db):
    """A failing sub-query for one field should leave the rest of the
    entry populated rather than erroring out the whole overview."""
    from app import verticals as _verticals

    target = _verticals.all_specs()[0].name

    async def bad_fetchval(*a, **kw):
        raise RuntimeError("simulated brier failure")

    # Trade aggregates still succeed:
    stub_db.trades_per_vertical = {target: {"n": 5, "pnl": 11.0, "wins": 3, "losses": 2,
                                             "win_rate": 0.6, "avg_edge": None}}
    stub_db.fetchval = bad_fetchval  # type: ignore[assignment]

    out = asyncio.run(dashboard.get_verticals_overview(days=7))
    entry = next(e for e in out["verticals"] if e["vertical"] == target)
    assert entry["trades"] == 5
    assert entry["pnl"] == pytest.approx(11.0)
    assert entry["brier"] is None  # failed gracefully
    assert entry["open_positions"] == 0  # also failed gracefully
