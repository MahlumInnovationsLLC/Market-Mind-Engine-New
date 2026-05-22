"""Tests for dashboard.get_niche_sports_tile (task #831)."""
from __future__ import annotations

import asyncio
from typing import Any

from app import dashboard


def _run(coro):
    return asyncio.run(coro)


class _FakeDB:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        return self.rows


def test_tile_empty_when_no_rows(monkeypatch):
    monkeypatch.setattr(dashboard, "db", _FakeDB([]))
    out = _run(dashboard.get_niche_sports_tile())
    assert out["error"] is None
    # one item per niche league, all zeroed
    assert len(out["items"]) == 5
    leagues = [it["league"] for it in out["items"]]
    assert leagues == ["ATP", "WTA", "F1", "PGA", "UFC"]
    for it in out["items"]:
        assert it["n_settled"] == 0
        assert it["n_picks_total"] == 0
        assert it["brier"] is None
        assert it["hit_rate"] is None


def test_tile_populated_with_metrics(monkeypatch):
    rows = [
        {"league": "ATP", "n_picks_total": 20, "n_settled": 15,
         "brier": 0.18, "hit_rate": 0.7},
        {"league": "UFC", "n_picks_total": 5, "n_settled": 5,
         "brier": 0.24, "hit_rate": 0.6},
    ]
    monkeypatch.setattr(dashboard, "db", _FakeDB(rows))
    out = _run(dashboard.get_niche_sports_tile())
    by_lg = {it["league"]: it for it in out["items"]}
    assert by_lg["ATP"]["n_settled"] == 15
    assert by_lg["ATP"]["n_picks_total"] == 20
    assert by_lg["ATP"]["brier"] == 0.18
    assert by_lg["ATP"]["hit_rate"] == 0.7
    assert by_lg["UFC"]["n_settled"] == 5
    # WTA/F1/PGA had no rows → defaulted
    assert by_lg["WTA"]["brier"] is None
    assert by_lg["F1"]["n_settled"] == 0
    assert by_lg["PGA"]["n_picks_total"] == 0


def test_tile_handles_db_error(monkeypatch):
    class _Broken:
        async def fetch(self, *_a, **_kw):
            raise RuntimeError("db gone")

    monkeypatch.setattr(dashboard, "db", _Broken())
    out = _run(dashboard.get_niche_sports_tile())
    assert out["error"] is not None
    assert "RuntimeError" in out["error"]
    assert out["items"] == []


def test_tile_includes_days_and_generated_at(monkeypatch):
    monkeypatch.setattr(dashboard, "db", _FakeDB([]))
    out = _run(dashboard.get_niche_sports_tile(days=7))
    assert out["days"] == 7
    assert out["generated_at"]
