"""Regression tests for the persist_run datetime coercion fix.

``BacktestResult.started_at`` and ``completed_at`` are typed as ``str``
on the dataclass and assigned ISO-format strings throughout the
scheduler.  asyncpg refuses to bind a Python ``str`` to a
``timestamptz`` parameter — it raises
``expected a datetime.date or datetime.datetime instance, got 'str'`` —
so persist_run must coerce them to tz-aware datetimes before binding.

Before this fix, every scheduler tick (one per supported pair x
timeframe) logged a ``persist_run failed`` warning and dropped the row
on the floor, so downstream drift-monitoring queries were silently
starved.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest

from app.crypto import backtest_scheduler as bts
from app.crypto.backtest import BacktestResult


# ---------------------------------------------------------------------------
# _coerce_dt unit tests
# ---------------------------------------------------------------------------


def test_coerce_dt_accepts_iso_string() -> None:
    iso = "2026-04-26T01:54:28.281775+00:00"
    out = bts._coerce_dt(iso)
    assert isinstance(out, datetime)
    assert out.tzinfo is not None
    assert out.year == 2026 and out.month == 4 and out.day == 26


def test_coerce_dt_accepts_iso_string_with_z_suffix() -> None:
    out = bts._coerce_dt("2026-04-26T01:54:28Z")
    assert isinstance(out, datetime)
    assert out.tzinfo is not None
    assert out.utcoffset() == timezone.utc.utcoffset(out)


def test_coerce_dt_accepts_naive_datetime_and_adds_utc() -> None:
    naive = datetime(2026, 4, 26, 1, 54, 28)
    out = bts._coerce_dt(naive)
    assert isinstance(out, datetime)
    assert out.tzinfo is not None


def test_coerce_dt_accepts_aware_datetime_unchanged() -> None:
    aware = datetime(2026, 4, 26, 1, 54, 28, tzinfo=timezone.utc)
    out = bts._coerce_dt(aware)
    assert out == aware


def test_coerce_dt_accepts_epoch_float() -> None:
    out = bts._coerce_dt(1775192440.5235903)
    assert isinstance(out, datetime)
    assert out.tzinfo is not None


def test_coerce_dt_rejects_empty_and_garbage() -> None:
    assert bts._coerce_dt(None) is None
    assert bts._coerce_dt("") is None
    assert bts._coerce_dt("   ") is None
    assert bts._coerce_dt("not-a-date") is None
    assert bts._coerce_dt(object()) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# persist_run integration: capture the bind values to prove they're datetimes
# ---------------------------------------------------------------------------


def test_persist_run_binds_datetime_not_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """Capture the args passed to db.fetchval and assert that the
    started_at / completed_at parameters arrive as datetime instances."""
    captured: dict[str, Any] = {}

    async def _fake_fetchval(query: str, *args: Any) -> Any:
        captured["query"] = query
        captured["args"] = args
        return 12345

    monkeypatch.setattr(bts.db, "fetchval", _fake_fetchval)

    result = BacktestResult(
        symbol="BTC",
        timeframe="1h",
        n_evaluated=100,
        accuracy=0.55,
        brier_score=0.22,
        log_loss=0.65,
        reliability=[],
        # Both as ISO strings — exactly what the scheduler emits today.
        started_at="2026-04-26T01:54:28.281775+00:00",
        completed_at="2026-04-26T01:54:33.123456+00:00",
    )

    row_id = asyncio.run(bts.persist_run(result, days=7, lookback=30))
    assert row_id == 12345

    args = captured["args"]
    # Args are positional after the query: ($1..$11).  $10 = started_at, $11 = completed_at.
    started_at_arg = args[9]
    completed_at_arg = args[10]
    assert isinstance(started_at_arg, datetime), (
        f"started_at must be datetime for asyncpg, got {type(started_at_arg).__name__}"
    )
    assert isinstance(completed_at_arg, datetime), (
        f"completed_at must be datetime for asyncpg, got {type(completed_at_arg).__name__}"
    )
    # Both should be tz-aware.
    assert started_at_arg.tzinfo is not None
    assert completed_at_arg.tzinfo is not None


def test_persist_run_falls_back_to_now_for_empty_started_at(monkeypatch: pytest.MonkeyPatch) -> None:
    """When started_at is the dataclass default (empty string), persist_run
    must still bind a datetime (today) so the SQL bind doesn't blow up."""
    captured: dict[str, Any] = {}

    async def _fake_fetchval(query: str, *args: Any) -> Any:
        captured["args"] = args
        return 1

    monkeypatch.setattr(bts.db, "fetchval", _fake_fetchval)

    result = BacktestResult(
        symbol="ETH",
        timeframe="5m",
        n_evaluated=42,
        accuracy=0.50,
        brier_score=0.25,
        log_loss=0.70,
        reliability=[],
        # Empty defaults — what we'd see if the scheduler forgot to set them.
        started_at="",
        completed_at="",
    )
    asyncio.run(bts.persist_run(result, days=1, lookback=10))
    args = captured["args"]
    assert isinstance(args[9], datetime)
    assert isinstance(args[10], datetime)
