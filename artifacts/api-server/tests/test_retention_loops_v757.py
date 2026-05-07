"""Tests for the task #757 retention loops + registry.

Strategy:
  * Exercise one archive-then-delete loop (``predictions``) and one
    delete-only loop (``pipeline_events``) end-to-end with fakes for
    ``app.db`` / the archive uploader so the test never opens a
    network or DB connection.
  * Exercise the cross-loop registry (``retention_loops_v757``) to
    confirm the inventory and lifecycle hooks behave correctly when
    the env knobs flip the loops on or off.
  * Exercise the dashboard wrapper end-to-end with a stubbed loop to
    verify the derived ``health`` and ``counts`` fields.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import pytest

from app import (
    archive_helper,
    db as _db,
    pipeline_events_retention,
    predictions_retention,
    retention_loops_v757,
)


# ---------------------------------------------------------------------------
# Defaults / env knobs
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch: pytest.MonkeyPatch):
    for prefix in ("AXIOM_PREDICTIONS_", "AXIOM_PIPELINE_EVENTS_"):
        for suffix in (
            "RETENTION_ENABLED",
            "RETENTION_DAYS",
            "CLEANUP_INTERVAL_SECONDS",
        ):
            monkeypatch.delenv(prefix + suffix, raising=False)
    predictions_retention._reset_state_for_tests()
    pipeline_events_retention._reset_state_for_tests()
    yield
    predictions_retention._reset_state_for_tests()
    pipeline_events_retention._reset_state_for_tests()


def test_archive_loop_defaults_disabled_and_ninety_days():
    state = predictions_retention.state()
    assert state["enabled"] is False
    assert state["retention_days"] == 90
    assert state["flavor"] == "archive"
    assert state["table"] == "predictions"
    # 6 hours is the default sweep cadence for archive loops.
    assert state["interval_seconds"] == 6 * 3600


def test_delete_only_loop_defaults_disabled_and_thirty_days():
    state = pipeline_events_retention.state()
    assert state["enabled"] is False
    assert state["retention_days"] == 30
    assert state["flavor"] == "delete-only"
    assert state["table"] == "pipeline_events"


def test_env_can_enable_and_override_horizon(monkeypatch):
    monkeypatch.setenv("AXIOM_PREDICTIONS_RETENTION_ENABLED", "1")
    monkeypatch.setenv("AXIOM_PREDICTIONS_RETENTION_DAYS", "45")
    state = predictions_retention.state()
    assert state["enabled"] is True
    assert state["retention_days"] == 45


def test_retention_days_floor_protects_against_zero(monkeypatch):
    monkeypatch.setenv("AXIOM_PREDICTIONS_RETENTION_DAYS", "0")
    assert predictions_retention.state()["retention_days"] == 1
    monkeypatch.setenv("AXIOM_PREDICTIONS_RETENTION_DAYS", "-3")
    assert predictions_retention.state()["retention_days"] == 1


def test_interval_seconds_floor_against_busy_loop(monkeypatch):
    monkeypatch.setenv(
        "AXIOM_PIPELINE_EVENTS_CLEANUP_INTERVAL_SECONDS", "5"
    )
    # Helper floors at 300s so a typo can't busy-loop the DB.
    assert pipeline_events_retention.state()["interval_seconds"] == 300


# ---------------------------------------------------------------------------
# delete-only cleanup_once
# ---------------------------------------------------------------------------

def test_delete_only_cleanup_once_emits_age_bounded_delete(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_execute(query: str, *args: Any) -> str:
        captured["query"] = query
        captured["args"] = args
        return "DELETE 7"

    monkeypatch.setattr(_db, "execute", fake_execute)

    deleted = asyncio.run(pipeline_events_retention.cleanup_once())
    assert deleted == 7

    q = captured["query"]
    assert "DELETE FROM pipeline_events" in q
    assert "created_at <" in q
    assert "INTERVAL '1 day'" in q
    assert captured["args"] == (30,)
    s = pipeline_events_retention.state()
    assert s["last_deleted"] == 7
    assert s["total_deleted"] == 7
    assert s["last_error"] is None


def test_delete_only_cleanup_once_swallows_db_error(monkeypatch):
    async def boom(*_a, **_kw):
        raise RuntimeError("db blip")

    monkeypatch.setattr(_db, "execute", boom)
    deleted = asyncio.run(pipeline_events_retention.cleanup_once())
    assert deleted == 0
    s = pipeline_events_retention.state()
    assert "db blip" in (s["last_error"] or "")


# ---------------------------------------------------------------------------
# archive cleanup_once
# ---------------------------------------------------------------------------

def test_archive_cleanup_once_uploads_then_deletes(monkeypatch):
    """Archive loop must call the helper, persist the chunk, and
    update its archive-specific counters in ``state()``."""
    chunks: list[bytes] = []
    deletes: list[Any] = []

    async def fake_archive_table(**kwargs):
        # Verify the loop hands the helper the expected wiring.
        assert kwargs["table"] == "predictions"
        assert kwargs["ts_column"] == "created_at"
        assert kwargs["pk_columns"] == ("id",)
        chunks.append(b"<would-be-jsonl-gz>")
        deletes.append(kwargs["cutoff"])
        return archive_helper.ArchiveRunSummary(
            table="predictions",
            rows=42,
            chunks=2,
            bytes_compressed=1234,
            bytes_uncompressed=5678,
            object_keys=[
                "archives/predictions/dt=2026-04-22/part-0000.jsonl.gz",
                "archives/predictions/dt=2026-04-22/part-0001.jsonl.gz",
            ],
            last_object_key=(
                "archives/predictions/dt=2026-04-22/part-0001.jsonl.gz"
            ),
            cutoff=kwargs["cutoff"].isoformat(),
        )

    monkeypatch.setattr(
        "app.retention_base.archive_helper.archive_table",
        fake_archive_table,
    )

    moved = asyncio.run(predictions_retention.cleanup_once())
    assert moved == 42
    assert chunks  # archive helper was invoked

    s = predictions_retention.state()
    assert s["last_deleted"] == 42
    assert s["last_archived_rows"] == 42
    assert s["total_archived_rows"] == 42
    assert s["last_bytes_compressed"] == 1234
    assert s["last_object_key"].endswith("part-0001.jsonl.gz")
    assert s["last_error"] is None


def test_archive_cleanup_once_surfaces_helper_error(monkeypatch):
    async def fake_archive_table(**kwargs):
        return archive_helper.ArchiveRunSummary(
            table=kwargs["table"],
            rows=0,
            error="StorageError: bucket down",
        )

    monkeypatch.setattr(
        "app.retention_base.archive_helper.archive_table",
        fake_archive_table,
    )

    moved = asyncio.run(predictions_retention.cleanup_once())
    assert moved == 0
    assert "bucket down" in (predictions_retention.state()["last_error"] or "")


# ---------------------------------------------------------------------------
# start() respects the disabled-by-default contract
# ---------------------------------------------------------------------------

def test_start_short_circuits_when_disabled(monkeypatch):
    async def boom(*_a, **_kw):
        raise AssertionError("DB must not be touched while disabled")

    monkeypatch.setattr(_db, "execute", boom)

    async def go():
        await predictions_retention.start()
        assert predictions_retention.state()["running"] is False
        await predictions_retention.stop()

    asyncio.run(go())


def test_start_runs_loop_when_enabled(monkeypatch):
    monkeypatch.setenv("AXIOM_PIPELINE_EVENTS_RETENTION_ENABLED", "1")
    monkeypatch.setenv(
        "AXIOM_PIPELINE_EVENTS_CLEANUP_INTERVAL_SECONDS", "300"
    )

    calls: list[int] = []

    async def fake_execute(*_a, **_kw):
        calls.append(1)
        return "DELETE 0"

    monkeypatch.setattr(_db, "execute", fake_execute)

    async def go():
        await pipeline_events_retention.start()
        for _ in range(50):
            if calls:
                break
            await asyncio.sleep(0.01)
        assert calls
        assert pipeline_events_retention.state()["running"] is True
        await pipeline_events_retention.stop()
        assert pipeline_events_retention.state()["running"] is False

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Registry inventory + lifecycle helpers
# ---------------------------------------------------------------------------

def test_registry_lists_all_22_loops_with_expected_split():
    assert len(retention_loops_v757.ARCHIVE_MODULES) == 7
    assert len(retention_loops_v757.DELETE_ONLY_MODULES) == 15
    assert len(retention_loops_v757.ALL_MODULES) == 22

    archive_tables = {m.state()["table"] for m in retention_loops_v757.ARCHIVE_MODULES}
    assert archive_tables == {
        "predictions",
        "market_price_ticks",
        "autoresearch_features",
        "tech_evidence",
        "line_snapshots",
        "sportradar_tournament_replays",
        "precip_emos_observations",
    }

    delete_tables = {m.state()["table"] for m in retention_loops_v757.DELETE_ONLY_MODULES}
    assert "pipeline_events" in delete_tables
    assert "rc_events" in delete_tables
    assert "stripe_events" in delete_tables


def test_states_returns_one_row_per_loop():
    rows = retention_loops_v757.states()
    assert len(rows) == 22
    flavors = {r["flavor"] for r in rows}
    assert flavors == {"archive", "delete-only"}


def test_start_all_does_not_raise_when_loops_disabled():
    # All loops default disabled, so start_all() should be a no-op
    # that doesn't touch the DB.
    asyncio.run(retention_loops_v757.start_all())
    asyncio.run(retention_loops_v757.stop_all())


def test_start_all_swallows_individual_loop_failure(monkeypatch):
    """One bad loop must not take down the rest."""
    boom_called: list[bool] = []

    class _Boom:
        def state(self):
            return {"name": "boom", "flavor": "archive", "table": "x"}

        async def start(self):
            boom_called.append(True)
            raise RuntimeError("simulated start error")

        async def stop(self):
            pass

    monkeypatch.setattr(
        retention_loops_v757,
        "ALL_MODULES",
        retention_loops_v757.ALL_MODULES + (_Boom(),),
    )
    asyncio.run(retention_loops_v757.start_all())
    assert boom_called == [True]


# ---------------------------------------------------------------------------
# Dashboard wrapper
# ---------------------------------------------------------------------------

def test_dashboard_supabase_growth_returns_per_loop_health(monkeypatch):
    from app import dashboard

    payload = asyncio.run(dashboard.get_supabase_growth_retention_status())
    assert "archive" in payload and "delete_only" in payload
    assert len(payload["archive"]) == 7
    assert len(payload["delete_only"]) == 15
    assert payload["total"] == 22

    # Every loop is currently disabled in the test env, so the
    # aggregate must reflect that.
    assert payload["counts"]["disabled"] == 22
    assert payload["counts"]["ok"] == 0
    assert payload["counts"]["error"] == 0
    assert payload["counts"]["stale"] == 0

    for row in payload["archive"]:
        assert row["health"] == "disabled"
        assert "table" in row
        assert "retention_days" in row
        assert row["seconds_since_last_tick"] is None
        # Archive rows must always carry a last_object_url field — null
        # when no chunk has been uploaded yet — so the dashboard can
        # always know whether to render the "view in Supabase Storage"
        # link without re-checking the bucket config.
        assert "last_object_url" in row
        assert row["last_object_url"] is None


def test_dashboard_supabase_growth_builds_object_url_from_settings(monkeypatch):
    """When an archive loop has uploaded a chunk, the dashboard must
    glue the configured Supabase Storage base URL + bucket + the loop's
    ``last_object_key`` into a clickable URL the operator can open."""
    from app import config as _config
    from app import dashboard

    real_settings = _config.get_settings()

    class _StubSettings:
        supabase_storage_url = "https://example.supabase.co/storage/v1"
        supabase_bucket = "axiom-archive"

        def __getattr__(self, item: str) -> Any:
            return getattr(real_settings, item)

    monkeypatch.setattr(_config, "get_settings", lambda: _StubSettings())

    class _FakeArchiveModule:
        __name__ = "fake_archive"

        @staticmethod
        def state() -> dict[str, Any]:
            return {
                "name": "fake_archive",
                "flavor": "archive",
                "table": "fake_table",
                "ts_column": "created_at",
                "running": False,
                "enabled": False,
                "started_at": None,
                "last_tick_at": None,
                "last_error": None,
                "last_deleted": 0,
                "total_deleted": 0,
                "retention_days": 30,
                "interval_seconds": 21600,
                "pk_columns": ["id"],
                "chunk_size": 1000,
                "max_rows_per_run": 50000,
                "last_archived_rows": 0,
                "total_archived_rows": 123,
                "last_bytes_compressed": 0,
                "total_bytes_compressed": 4567,
                "last_object_key": "fake_table/2026/05/07/chunk-001.jsonl.gz",
            }

    monkeypatch.setattr(
        dashboard, "__name__", dashboard.__name__,
    )  # no-op to keep monkeypatch import alive
    from app import retention_loops_v757 as _rlv757

    monkeypatch.setattr(
        _rlv757, "ARCHIVE_MODULES", (_FakeArchiveModule(),)
    )
    monkeypatch.setattr(_rlv757, "DELETE_ONLY_MODULES", ())

    payload = asyncio.run(dashboard.get_supabase_growth_retention_status())
    assert len(payload["archive"]) == 1
    row = payload["archive"][0]
    assert row["last_object_key"] == "fake_table/2026/05/07/chunk-001.jsonl.gz"
    assert (
        row["last_object_url"]
        == "https://example.supabase.co/storage/v1/object/axiom-archive/"
        "fake_table/2026/05/07/chunk-001.jsonl.gz"
    )
