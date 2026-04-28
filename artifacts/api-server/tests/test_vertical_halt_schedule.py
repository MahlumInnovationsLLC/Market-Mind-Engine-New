"""Task #713 — recurring per-vertical halt schedules + TTL sweeper.

Covers:
  * 5-field cron parser (basic, ranges, lists, steps, aliases, errors).
  * ``cron_matches`` Vixie OR semantics for restricted dom + dow.
  * ``set_vertical_halted(ttl_secs=...)`` writes ``expires_at`` into
    the JSON meta + a ``halted`` history row.
  * ``clear_vertical_halt`` writes a matching ``resumed`` /
    ``ttl_expired`` history row (with ``schedule_id`` plumbed back
    when the original halt was rule-driven).
  * ``list_expired_vertical_halts`` only surfaces past deadlines.
  * ``list_due_vertical_halt_schedules`` honours the dedupe window.
  * Schedule create validates cron + duration + non-empty reason.
  * The full ``vertical_halt_scheduler.tick_once`` round-trip:
    - a TTL halt is auto-resumed and the operator alert is dispatched,
    - a due cron rule fires, halts the vertical, marks ``last_fired_at``,
      and emits the scheduled-fired alert,
    - a rule pointing at an already-halted vertical is a no-op fire
      that still stamps ``last_fired_at`` (so the dedupe window holds).
"""
from __future__ import annotations

import asyncio
import itertools
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app import runtime_settings as runtime_settings_mod
from app import vertical_halt_scheduler as scheduler_mod
from app.notifications import alerts as alerts_mod


# ---------------------------------------------------------------------------
# Memory DB — covers system_settings, admin_audit_log,
# vertical_halt_schedules, and vertical_halt_history. Mirrors the
# layout used by tests/test_vertical_halt.py so the two suites stay
# consistent.
# ---------------------------------------------------------------------------


class _MemoryDb:
    def __init__(self) -> None:
        self.system_settings: dict[str, str] = {}
        self.audit: list[tuple[str, dict[str, Any]]] = []
        self._sched_id_seq = itertools.count(1)
        self.schedules: dict[int, dict[str, Any]] = {}
        self.history: list[dict[str, Any]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        if "FROM vertical_halt_schedules" in sql:
            rows = sorted(
                self.schedules.values(),
                key=lambda r: r["created_at"],
                reverse=True,
            )
            return [dict(r) for r in rows]
        if "FROM vertical_halt_history" in sql:
            rows = list(self.history)
            params = list(args[:-1])  # last arg is the LIMIT
            limit = args[-1] if args else 100
            if "vertical = $" in sql:
                want = params.pop(0)
                rows = [r for r in rows if r["vertical"] == want]
            if "effective_at >= $" in sql:
                cutoff = params.pop(0)
                rows = [r for r in rows if r["effective_at"] >= cutoff]
            rows = sorted(
                rows,
                key=lambda r: (r["effective_at"], r["id"]),
                reverse=True,
            )
            return [dict(r) for r in rows[:limit]]
        if "FROM system_settings" in sql and "LIKE" in sql:
            prefix = args[0].rstrip("%")
            return [
                {"key": k, "value": v}
                for k, v in self.system_settings.items()
                if k.startswith(prefix)
            ]
        if "FROM system_settings" in sql:
            return [
                {"key": k, "value": v}
                for k, v in self.system_settings.items()
                if k in args
            ]
        return []

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if "DELETE FROM vertical_halt_schedules" in sql and "count" in sql:
            sid = args[0]
            return 1 if self.schedules.pop(sid, None) is not None else 0
        return None

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        if "INSERT INTO vertical_halt_schedules" in sql:
            now = datetime.now(timezone.utc)
            sid = next(self._sched_id_seq)
            row = {
                "id": sid,
                "vertical": args[0],
                "cron_expr": args[1],
                "duration_secs": args[2],
                "reason": args[3],
                "enabled": True,
                "created_by_user_id": args[4],
                "created_by_email": args[5],
                "created_at": now,
                "updated_at": now,
                "last_fired_at": None,
            }
            self.schedules[sid] = row
            return dict(row)
        return None

    class _Conn:
        def __init__(self, parent: "_MemoryDb") -> None:
            self.parent = parent

        async def execute(self, sql: str, *args: Any) -> str:
            if "INSERT INTO system_settings" in sql:
                self.parent.system_settings[args[0]] = args[1]
            elif "DELETE FROM system_settings" in sql:
                self.parent.system_settings.pop(args[0], None)
            elif "INSERT INTO admin_audit_log" in sql:
                action = args[4]
                self.parent.audit.append((action, {
                    "actor_user_id": args[0],
                    "actor_email": args[1],
                    "before": json.loads(args[5]) if isinstance(args[5], str) else args[5],
                    "after": json.loads(args[6]) if isinstance(args[6], str) else args[6],
                }))
            elif "INSERT INTO vertical_halt_history" in sql:
                self.parent.history.append({
                    "id": len(self.parent.history) + 1,
                    "vertical": args[0],
                    "event": args[1],
                    "source": args[2],
                    "actor_user_id": args[3],
                    "actor_email": args[4],
                    "reason": args[5],
                    "expires_at": args[6],
                    "schedule_id": args[7],
                    "effective_at": datetime.now(timezone.utc),
                })
            return "OK"

        async def fetchrow(self_inner, sql: str, *args: Any) -> dict[str, Any] | None:
            if "UPDATE vertical_halt_schedules" in sql and "last_fired_at" in sql:
                # Conditional atomic claim: ts, sid, minute_start.
                ts, sid, minute_start = args[0], args[1], args[2]
                row = self_inner.parent.schedules.get(sid)
                if row is None:
                    return None
                last = row.get("last_fired_at")
                if last is not None and last >= minute_start:
                    return None
                row["last_fired_at"] = ts
                row["updated_at"] = ts
                return {"id": sid}
            return None

    def acquire(self):
        parent = self

        class _Ctx:
            async def __aenter__(self_inner):
                return _MemoryDb._Conn(parent)

            async def __aexit__(self_inner, *exc):
                return None

        return _Ctx()


@pytest.fixture
def mem_db(monkeypatch):
    db = _MemoryDb()
    monkeypatch.setattr(runtime_settings_mod, "db", db, raising=True)
    return db


def _vertical() -> str:
    from app import verticals as _verticals

    return _verticals.all_specs()[0].name


# ---------------------------------------------------------------------------
# Cron parser
# ---------------------------------------------------------------------------


def test_parse_cron_basic_star() -> None:
    minute, hour, dom, month, dow = runtime_settings_mod.parse_cron("* * * * *")
    assert len(minute) == 60
    assert len(hour) == 24
    assert len(dom) == 31
    assert len(month) == 12
    assert dow == set(range(0, 7))


def test_parse_cron_alias_daily() -> None:
    minute, hour, _dom, _month, _dow = runtime_settings_mod.parse_cron("@daily")
    assert minute == {0}
    assert hour == {0}


def test_parse_cron_step_and_range() -> None:
    minute, hour, _dom, _month, _dow = runtime_settings_mod.parse_cron(
        "*/15 9-17 * * *"
    )
    assert minute == {0, 15, 30, 45}
    assert hour == set(range(9, 18))


def test_parse_cron_dow_seven_normalises_to_zero() -> None:
    _m, _h, _d, _mo, dow = runtime_settings_mod.parse_cron("0 0 * * 7")
    assert dow == {0}


def test_parse_cron_invalid_field() -> None:
    with pytest.raises(ValueError):
        runtime_settings_mod.parse_cron("99 * * * *")


def test_parse_cron_too_few_fields() -> None:
    with pytest.raises(ValueError):
        runtime_settings_mod.parse_cron("0 0 *")


def test_cron_matches_minute_boundary() -> None:
    dt = datetime(2026, 4, 28, 12, 30, tzinfo=timezone.utc)
    assert runtime_settings_mod.cron_matches("30 12 * * *", dt)
    assert not runtime_settings_mod.cron_matches("31 12 * * *", dt)


def test_cron_matches_vixie_or_semantics() -> None:
    # 2026-04-01 is a Wednesday. Vixie cron's "OR" semantics: when both
    # dom and dow are restricted, EITHER match wins.
    dt_wed_apr1 = datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc)
    # Sunday OR the 1st of the month — Wed-the-1st matches via dom.
    assert runtime_settings_mod.cron_matches("0 0 1 * 0", dt_wed_apr1)
    # Restricted dom only — Wed-the-1st matches via dom.
    assert runtime_settings_mod.cron_matches("0 0 1 * *", dt_wed_apr1)
    # Restricted dow only (Sunday) — Wed-the-1st does NOT match.
    assert not runtime_settings_mod.cron_matches("0 0 * * 0", dt_wed_apr1)


# ---------------------------------------------------------------------------
# TTL plumbing on set / clear
# ---------------------------------------------------------------------------


def test_set_vertical_halted_with_ttl_records_expires_at_and_history(mem_db) -> None:
    v = _vertical()
    meta, changed = asyncio.run(
        runtime_settings_mod.set_vertical_halted(
            v,
            actor_user_id="u1",
            actor_email="u1@example.com",
            reason="weekend window",
            ttl_secs=3600,
        )
    )
    assert changed is True
    assert "expires_at" in meta
    expires_dt = datetime.fromisoformat(meta["expires_at"])
    assert expires_dt > datetime.now(timezone.utc)
    assert len(mem_db.history) == 1
    row = mem_db.history[0]
    assert row["event"] == "halted"
    assert row["source"] == "manual_admin"
    assert row["expires_at"] is not None
    assert row["schedule_id"] is None


def test_set_vertical_halted_rejects_negative_ttl(mem_db) -> None:
    v = _vertical()
    with pytest.raises(ValueError):
        asyncio.run(
            runtime_settings_mod.set_vertical_halted(
                v, actor_user_id="u1", actor_email=None, ttl_secs=0,
            )
        )


def test_set_vertical_halted_rejects_ttl_above_seven_days(mem_db) -> None:
    v = _vertical()
    with pytest.raises(ValueError):
        asyncio.run(
            runtime_settings_mod.set_vertical_halted(
                v, actor_user_id="u1", actor_email=None,
                ttl_secs=7 * 24 * 3600 + 1,
            )
        )


def test_set_vertical_halted_rejects_unknown_source(mem_db) -> None:
    v = _vertical()
    with pytest.raises(ValueError):
        asyncio.run(
            runtime_settings_mod.set_vertical_halted(
                v, actor_user_id="u1", actor_email=None, source="bogus",
            )
        )


def test_clear_vertical_halt_writes_resumed_history_row(mem_db) -> None:
    v = _vertical()
    asyncio.run(
        runtime_settings_mod.set_vertical_halted(
            v, actor_user_id="u1", actor_email=None, ttl_secs=60,
        )
    )
    prev, changed = asyncio.run(
        runtime_settings_mod.clear_vertical_halt(
            v, actor_user_id="u2", actor_email="u2@example.com",
        )
    )
    assert changed is True
    assert prev is not None
    events = [r["event"] for r in mem_db.history]
    assert events == ["halted", "resumed"]
    assert mem_db.history[1]["source"] == "manual_admin"


def test_clear_vertical_halt_ttl_expiry_event(mem_db) -> None:
    v = _vertical()
    asyncio.run(
        runtime_settings_mod.set_vertical_halted(
            v, actor_user_id="u1", actor_email=None, ttl_secs=60,
            source="scheduled_rule", schedule_id=42,
        )
    )
    asyncio.run(
        runtime_settings_mod.clear_vertical_halt(
            v, actor_user_id="system:ttl_sweeper", actor_email=None,
            source="ttl_expiry", event="ttl_expired",
        )
    )
    last = mem_db.history[-1]
    assert last["event"] == "ttl_expired"
    assert last["source"] == "ttl_expiry"
    # schedule_id from the halt meta is plumbed forward to the resume row
    assert last["schedule_id"] == 42


# ---------------------------------------------------------------------------
# Sweeper lookups
# ---------------------------------------------------------------------------


def test_list_expired_vertical_halts(mem_db) -> None:
    v = _vertical()
    past = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    mem_db.system_settings[f"risk.vertical_halted.{v}"] = json.dumps({
        "by_user_id": "u1", "at": past, "expires_at": past,
    })
    expired = asyncio.run(runtime_settings_mod.list_expired_vertical_halts())
    assert [e[0] for e in expired] == [v]


def test_list_expired_skips_permanent_halts(mem_db) -> None:
    v = _vertical()
    mem_db.system_settings[f"risk.vertical_halted.{v}"] = json.dumps({
        "by_user_id": "u1", "at": datetime.now(timezone.utc).isoformat(),
    })
    assert asyncio.run(runtime_settings_mod.list_expired_vertical_halts()) == []


def test_list_due_schedules_dedupes_recent_fires(mem_db) -> None:
    v = _vertical()
    now = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    asyncio.run(
        runtime_settings_mod.create_vertical_halt_schedule(
            vertical=v, cron_expr="* * * * *", duration_secs=60,
            reason="every minute", actor_user_id="u1", actor_email=None,
        )
    )
    due = asyncio.run(
        runtime_settings_mod.list_due_vertical_halt_schedules(now=now)
    )
    assert len(due) == 1
    asyncio.run(
        runtime_settings_mod.mark_schedule_fired(due[0]["id"], fired_at=now)
    )
    due_again = asyncio.run(
        runtime_settings_mod.list_due_vertical_halt_schedules(
            now=now + timedelta(seconds=30)
        )
    )
    assert due_again == []
    due_after = asyncio.run(
        runtime_settings_mod.list_due_vertical_halt_schedules(
            now=now + timedelta(seconds=120)
        )
    )
    assert len(due_after) == 1


# ---------------------------------------------------------------------------
# Schedule CRUD validation
# ---------------------------------------------------------------------------


def test_create_schedule_rejects_bad_cron(mem_db) -> None:
    v = _vertical()
    with pytest.raises(ValueError):
        asyncio.run(
            runtime_settings_mod.create_vertical_halt_schedule(
                vertical=v, cron_expr="bogus", duration_secs=60,
                reason="r", actor_user_id="u1", actor_email=None,
            )
        )


def test_create_schedule_requires_reason(mem_db) -> None:
    v = _vertical()
    with pytest.raises(ValueError):
        asyncio.run(
            runtime_settings_mod.create_vertical_halt_schedule(
                vertical=v, cron_expr="@daily", duration_secs=60,
                reason="   ", actor_user_id="u1", actor_email=None,
            )
        )


def test_create_schedule_rejects_bad_duration(mem_db) -> None:
    v = _vertical()
    with pytest.raises(ValueError):
        asyncio.run(
            runtime_settings_mod.create_vertical_halt_schedule(
                vertical=v, cron_expr="@daily", duration_secs=-1,
                reason="r", actor_user_id="u1", actor_email=None,
            )
        )


def test_delete_schedule_idempotent(mem_db) -> None:
    v = _vertical()
    row = asyncio.run(
        runtime_settings_mod.create_vertical_halt_schedule(
            vertical=v, cron_expr="@daily", duration_secs=60,
            reason="r", actor_user_id="u1", actor_email=None,
        )
    )
    assert asyncio.run(
        runtime_settings_mod.delete_vertical_halt_schedule(row["id"])
    )
    assert not asyncio.run(
        runtime_settings_mod.delete_vertical_halt_schedule(row["id"])
    )


# ---------------------------------------------------------------------------
# Scheduler tick_once round-trip
# ---------------------------------------------------------------------------


def test_tick_once_sweeps_ttl_and_fires_alert(mem_db, monkeypatch) -> None:
    v = _vertical()
    past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    mem_db.system_settings[f"risk.vertical_halted.{v}"] = json.dumps({
        "by_user_id": "u1", "at": past, "expires_at": past,
        "source": "manual_admin",
    })
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        alerts_mod, "vertical_halt_ttl_expired",
        lambda **kw: calls.append(("ttl", kw)) or {},
        raising=True,
    )
    monkeypatch.setattr(
        alerts_mod, "vertical_halt_scheduled_fired",
        lambda **kw: calls.append(("sched", kw)) or {},
        raising=True,
    )
    out = asyncio.run(scheduler_mod.tick_once())
    assert len(out["ttl_expired"]) == 1
    assert out["ttl_expired"][0]["vertical"] == v
    assert f"risk.vertical_halted.{v}" not in mem_db.system_settings
    events = [r["event"] for r in mem_db.history]
    assert "ttl_expired" in events
    assert any(c[0] == "ttl" for c in calls)


def test_tick_once_fires_due_schedule(mem_db, monkeypatch) -> None:
    v = _vertical()
    asyncio.run(
        runtime_settings_mod.create_vertical_halt_schedule(
            vertical=v, cron_expr="* * * * *", duration_secs=120,
            reason="every minute", actor_user_id="u1", actor_email=None,
        )
    )
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        alerts_mod, "vertical_halt_scheduled_fired",
        lambda **kw: calls.append(("sched", kw)) or {},
        raising=True,
    )
    monkeypatch.setattr(
        alerts_mod, "vertical_halt_ttl_expired",
        lambda **kw: calls.append(("ttl", kw)) or {},
        raising=True,
    )
    now = datetime.now(timezone.utc).replace(microsecond=0)
    out = asyncio.run(scheduler_mod.tick_once(now=now))
    assert len(out["scheduled_fired"]) == 1
    assert out["scheduled_fired"][0]["vertical"] == v
    assert out["scheduled_fired"][0]["changed"] is True
    raw = mem_db.system_settings[f"risk.vertical_halted.{v}"]
    meta = json.loads(raw)
    assert meta["expires_at"]
    assert meta["source"] == "scheduled_rule"
    assert meta["schedule_id"] is not None
    assert any(c[0] == "sched" for c in calls)
    rule = next(iter(mem_db.schedules.values()))
    assert rule["last_fired_at"] is not None


def test_tick_once_skip_when_already_halted(mem_db, monkeypatch) -> None:
    v = _vertical()
    asyncio.run(
        runtime_settings_mod.set_vertical_halted(
            v, actor_user_id="u1", actor_email=None, reason="manual",
        )
    )
    asyncio.run(
        runtime_settings_mod.create_vertical_halt_schedule(
            vertical=v, cron_expr="* * * * *", duration_secs=60,
            reason="every minute", actor_user_id="u1", actor_email=None,
        )
    )
    monkeypatch.setattr(
        alerts_mod, "vertical_halt_scheduled_fired",
        lambda **kw: {}, raising=True,
    )
    monkeypatch.setattr(
        alerts_mod, "vertical_halt_ttl_expired",
        lambda **kw: {}, raising=True,
    )
    out = asyncio.run(scheduler_mod.tick_once())
    assert out["scheduled_fired"][0]["changed"] is False
    rule = next(iter(mem_db.schedules.values()))
    assert rule["last_fired_at"] is not None
