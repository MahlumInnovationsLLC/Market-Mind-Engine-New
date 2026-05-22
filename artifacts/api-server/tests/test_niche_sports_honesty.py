"""Tests for the niche-sports honesty-report collector (task #831)."""
from __future__ import annotations

import asyncio
from typing import Any

from app.notifications import daily_honesty_report as dhr


def _run(coro):
    return asyncio.run(coro)


class _FakeDB:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        return self.rows


def _row(league: str, p: float, y: int) -> dict[str, Any]:
    return {"league": league, "predicted_prob": p, "actual_outcome": y}


def test_collector_returns_items_above_min_samples(monkeypatch):
    # 10 perfect ATP picks → brier 0, ece 0, hit_rate 1.0
    rows: list[dict[str, Any]] = []
    rows += [_row("ATP", 1.0, 1) for _ in range(5)]
    rows += [_row("ATP", 0.0, 0) for _ in range(5)]
    monkeypatch.setattr(dhr, "db", _FakeDB(rows))
    out = _run(dhr.collect_niche_sports_performance(min_samples=5))
    assert out["error"] is None
    items = {it["league"]: it for it in out["items"]}
    assert "ATP" in items
    atp = items["ATP"]
    assert atp["n"] == 10
    assert atp["brier"] == 0.0
    assert atp["hit_rate"] == 1.0
    assert atp["ece"] == 0.0
    # the four other niche leagues land under insufficient_volume
    iv = {it["league"] for it in out["insufficient_volume"]}
    assert iv == {"WTA", "F1", "PGA", "UFC"}


def test_collector_skips_unknown_leagues(monkeypatch):
    rows = [_row("NBA", 0.5, 1) for _ in range(50)]
    monkeypatch.setattr(dhr, "db", _FakeDB(rows))
    out = _run(dhr.collect_niche_sports_performance())
    assert out["items"] == []
    # all 5 niche leagues marked as insufficient
    assert len(out["insufficient_volume"]) == 5


def test_collector_handles_db_error(monkeypatch):
    class _Broken:
        async def fetch(self, *_a, **_kw):
            raise RuntimeError("db gone")

    monkeypatch.setattr(dhr, "db", _Broken())
    out = _run(dhr.collect_niche_sports_performance())
    assert out["error"] is not None
    assert "RuntimeError" in out["error"]


def test_collector_handles_null_prob_and_outcome(monkeypatch):
    rows = [
        {"league": "F1", "predicted_prob": None, "actual_outcome": 1},
        {"league": "F1", "predicted_prob": 0.6, "actual_outcome": None},
    ]
    monkeypatch.setattr(dhr, "db", _FakeDB(rows))
    out = _run(dhr.collect_niche_sports_performance(min_samples=1))
    # both rows dropped; F1 lands in insufficient_volume with n=0
    f1 = next(it for it in out["insufficient_volume"] if it["league"] == "F1")
    assert f1["n"] == 0


def test_render_text_includes_section_10():
    payload = {
        "date_utc": "2026-05-22",
        "niche_sports": {
            "days": 30,
            "min_samples": 5,
            "items": [
                {"league": "ATP", "n": 10, "brier": 0.18,
                 "ece": 0.04, "hit_rate": 0.7},
                {"league": "F1", "n": 8, "brier": 0.35,
                 "ece": 0.10, "hit_rate": 0.4},
            ],
            "insufficient_volume": [
                {"league": "UFC", "n": 2, "min_samples": 5},
            ],
            "error": None,
        },
    }
    _, body = dhr.render_text(payload)
    assert "10. Niche sports performance" in body
    assert "ATP" in body and "F1" in body
    # ATP brier 0.18 ≤ 0.260 tennis bar → green
    assert "[GREEN]  ATP" in body
    # F1 brier 0.35 > 0.300 bar → yellow
    assert "[YELLOW]   F1" in body
    assert "UFC" in body and "insufficient volume" in body


def test_render_html_includes_niche_section():
    payload = {
        "date_utc": "2026-05-22",
        "niche_sports": {
            "days": 30,
            "min_samples": 5,
            "items": [
                {"league": "WTA", "n": 12, "brier": 0.22,
                 "ece": 0.05, "hit_rate": 0.6},
            ],
            "insufficient_volume": [],
            "error": None,
        },
    }
    html = dhr.render_html(payload)
    assert "Niche sports performance" in html
    assert "WTA" in html


def test_render_text_handles_collector_error():
    payload = {
        "date_utc": "2026-05-22",
        "niche_sports": {"days": 30, "items": [], "insufficient_volume": [],
                         "error": "RuntimeError: db gone"},
    }
    _, body = dhr.render_text(payload)
    assert "ERROR: RuntimeError: db gone" in body
