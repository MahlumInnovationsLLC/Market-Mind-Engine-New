"""Unit tests for `dashboard.get_vertical_performance`.

Stubs the asyncpg-style `db` accessor so we can exercise the bucket math
and the empty-vertical fallback without a live Supabase connection.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app import dashboard


class _FakeDB:
    """Routes db.fetch / fetchrow / fetchval based on substring match."""

    def __init__(
        self,
        by_vertical: dict[str, dict[str, Any]],
        *,
        approved_debates_by_league: dict[str, int] | None = None,
    ) -> None:
        self._by_vertical = by_vertical
        self._approved_debates_by_league = approved_debates_by_league or {}

    async def fetchrow(self, query: str, *args: Any) -> Any:
        # Trade aggregate query — first arg is the vertical.
        if "FROM trades" in query and "calibration_snapshots" not in query:
            v = args[0]
            return self._by_vertical.get(v, {}).get("agg")
        return None

    async def fetchval(self, query: str, *args: Any) -> Any:
        if "calibration_snapshots" in query:
            v = args[0]
            return self._by_vertical.get(v, {}).get("brier")
        if "FROM positions" in query:
            v = args[0]
            return self._by_vertical.get(v, {}).get("open_exposure")
        if "FROM trades" in query and "executed_at >= (now() AT TIME ZONE 'UTC')::date" in query:
            v = args[0]
            return self._by_vertical.get(v, {}).get("pnl_today")
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "FROM swarm_debates" in query and "approved = TRUE" in query:
            return [
                {"league": lg, "n": n}
                for lg, n in self._approved_debates_by_league.items()
            ]
        if "bucketed" in query or "WITH settled" in query:
            v = args[0]
            return self._by_vertical.get(v, {}).get("bucket_rows", [])
        return []


def _run(coro):
    return asyncio.run(coro)


def test_returns_three_verticals_with_empty_buckets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dashboard, "db", _FakeDB({}))
    out = _run(dashboard.get_vertical_performance(7))

    assert out["days"] == 7
    # Strip ordering follows the registry's insertion order; legacy
    # three first, then any verticals later promoted to the strip
    # (task #196 promoted "soccer", task #215 promoted
    # "combat-individual").
    names = [v["vertical"] for v in out["verticals"]]
    assert names[:3] == ["sports", "weather", "crypto"]
    assert "soccer" in names
    assert "combat-individual" in names

    for v in out["verticals"]:
        # Empty agg → defaults preserved.
        assert v["trades"] == 0
        assert v["pnl"] == 0.0
        assert v["win_rate"] is None
        assert v["brier"] is None
        # Spec-anchored badges (task #450) default to numeric zeros so
        # the front-end can render them without nullable plumbing.
        assert v["open_exposure_usd"] == 0.0
        assert v["pnl_today"] == 0.0
        assert v["approved_debates_today"] == 0
        # Always 10 buckets emitted, even with no data, with continuous midpoints.
        assert len(v["calibration"]) == 10
        assert len(v["pit"]) == 10
        assert all(b["n"] == 0 for b in v["calibration"])
        assert all(b["count"] == 0 for b in v["pit"])
        # Bucket midpoints span [0.05, 0.95] in 0.1 steps.
        mids = [round(b["bucket"], 3) for b in v["calibration"]]
        assert mids == [0.05, 0.15, 0.25, 0.35, 0.45, 0.55, 0.65, 0.75, 0.85, 0.95]


def test_aggregates_and_buckets_populate(monkeypatch: pytest.MonkeyPatch) -> None:
    # Fake aggregate row plus two calibration buckets and one PIT bucket.
    fake = _FakeDB({
        "sports": {
            "agg": {"n": 10, "pnl": 12.5, "wins": 6, "losses": 4, "win_rate": 0.6},
            "brier": 0.21,
            # bin 2 (0.2-0.3) has 3 cal samples; bin 7 (0.7-0.8) has 1 cal
            # sample. PIT count of 4 lands in bin 7.
            "bucket_rows": [
                {"bin": 2, "cal_n": 3, "cal_p_sum": 0.75, "cal_y_sum": 1.0, "pit_n": 0},
                {"bin": 7, "cal_n": 1, "cal_p_sum": 0.72, "cal_y_sum": 1.0, "pit_n": 4},
            ],
        },
    })
    monkeypatch.setattr(dashboard, "db", fake)
    out = _run(dashboard.get_vertical_performance(30))

    sports = next(v for v in out["verticals"] if v["vertical"] == "sports")
    assert sports["trades"] == 10
    assert sports["pnl"] == pytest.approx(12.5)
    assert sports["wins"] == 6
    assert sports["losses"] == 4
    assert sports["win_rate"] == pytest.approx(0.6)
    assert sports["brier"] == pytest.approx(0.21)

    # Bin 2 calibration: predicted = 0.75/3 = 0.25, observed = 1/3.
    bin2 = sports["calibration"][2]
    assert bin2["n"] == 3
    assert bin2["predicted"] == pytest.approx(0.25, abs=1e-3)
    assert bin2["observed"] == pytest.approx(1 / 3, abs=1e-3)

    # Bin 7 calibration: predicted = 0.72, observed = 1.0.
    bin7 = sports["calibration"][7]
    assert bin7["n"] == 1
    assert bin7["predicted"] == pytest.approx(0.72, abs=1e-3)
    assert bin7["observed"] == pytest.approx(1.0)

    # Untouched bins remain empty (nullable predicted/observed, n=0).
    bin5 = sports["calibration"][5]
    assert bin5["n"] == 0
    assert bin5["predicted"] is None
    assert bin5["observed"] is None

    # PIT histogram: bin 7 should have count=4, every other bin 0.
    pit_counts = [b["count"] for b in sports["pit"]]
    assert pit_counts[7] == 4
    assert sum(pit_counts) == 4

    # Other verticals still emitted with empty defaults so the strip stays
    # 3-wide on the front-end.
    # Soccer was added to the dashboard strip in task #196; the
    # legacy three must still be present.
    names = {v["vertical"] for v in out["verticals"]}
    assert {"sports", "weather", "crypto"}.issubset(names)
    assert "soccer" in names
    assert "combat-individual" in names


def test_clamps_days_to_valid_range(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dashboard, "db", _FakeDB({}))
    assert _run(dashboard.get_vertical_performance(0))["days"] == 1
    assert _run(dashboard.get_vertical_performance(-50))["days"] == 1
    assert _run(dashboard.get_vertical_performance(99999))["days"] == 365


def test_strip_badges_open_exposure_pnl_today_and_debates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task #450 — every tile carries open exposure, today's P&L, and
    today's approved-debate count, with debates attributed to the right
    tile via league mapping."""
    fake = _FakeDB(
        {
            "weather": {"open_exposure": 1234.56, "pnl_today": -45.0},
            "crypto": {"open_exposure": 200.0, "pnl_today": 12.5},
            "sports": {"open_exposure": 0.0, "pnl_today": 0.0},
        },
        approved_debates_by_league={
            # Weather + crypto map to their own tile via spec.leagues.
            "WEATHER": 3,
            "CRYPTO": 2,
            # NFL has no dedicated strip vertical → falls into "sports".
            "NFL": 5,
            # NBA likewise.
            "NBA": 4,
            # Empty league string is dropped (cannot be attributed).
            "": 7,
        },
    )
    monkeypatch.setattr(dashboard, "db", fake)
    out = _run(dashboard.get_vertical_performance(7))

    by = {v["vertical"]: v for v in out["verticals"]}

    assert by["weather"]["open_exposure_usd"] == pytest.approx(1234.56)
    assert by["weather"]["pnl_today"] == pytest.approx(-45.0)
    assert by["weather"]["approved_debates_today"] == 3

    assert by["crypto"]["open_exposure_usd"] == pytest.approx(200.0)
    assert by["crypto"]["pnl_today"] == pytest.approx(12.5)
    assert by["crypto"]["approved_debates_today"] == 2

    # Sports is the catch-all: NFL (5) + NBA (4) = 9.  The empty-league
    # row and any league owned by another strip vertical (WEATHER /
    # CRYPTO / etc.) must NOT be double-counted here.
    assert by["sports"]["approved_debates_today"] == 9
    assert by["sports"]["open_exposure_usd"] == 0.0
    assert by["sports"]["pnl_today"] == 0.0

    # Verticals with no fake debates default to zero — no leakage from
    # the global "sports" bucket.
    assert by["soccer"]["approved_debates_today"] == 0
    assert by["combat-individual"]["approved_debates_today"] == 0
