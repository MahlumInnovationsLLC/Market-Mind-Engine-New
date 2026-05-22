"""Tests for the honesty-report pre-vs-in-game Brier split (task #830).

Covers:
  * ``collect_pre_vs_in_game_brier`` splits rows by ``is_live`` per
    league and returns the absolute Brier delta.
  * Leagues with insufficient in-game samples land in
    ``samples_too_small`` (so the operator can see the loop produced
    data even before it crosses the min-samples bar).
  * ``render_text`` / ``render_html`` surface rows under a section 9
    header and tone the delta against the 0.02 ship criterion.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.notifications import daily_honesty_report as dhr


def _run(coro):
    return asyncio.run(coro)


class _FakeDB:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "online_learning_buffer" in query:
            return self.rows
        return []


def _samples(league: str, is_live: bool, p: float, y: int, n: int):
    return [
        {"league": league, "is_live": is_live, "predicted_prob": p,
         "actual_outcome": y}
        for _ in range(n)
    ]


def test_split_produces_items_per_league(monkeypatch):
    # NBA: 50 pre-game perfect (brier 0) + 50 in-game off
    # (predicted=0.4, actual=1.0 → brier=0.36 each → mean 0.36)
    rows: list[dict[str, Any]] = []
    rows += _samples("NBA", False, 1.0, 1, 25)
    rows += _samples("NBA", False, 0.0, 0, 25)
    rows += _samples("NBA", True, 0.4, 1, 50)
    monkeypatch.setattr(dhr, "db", _FakeDB(rows))

    out = _run(dhr.collect_pre_vs_in_game_brier(
        days=30, min_samples=10,
    ))
    assert out["error"] is None
    assert len(out["items"]) == 1
    it = out["items"][0]
    assert it["league"] == "NBA"
    assert it["n_pre"] == 50 and it["n_in"] == 50
    assert it["brier_pre"] == pytest.approx(0.0, abs=1e-6)
    assert it["brier_in"] == pytest.approx(0.36, abs=1e-6)
    assert it["abs_delta"] == pytest.approx(0.36, abs=1e-6)


def test_too_small_in_game_sample_is_surfaced(monkeypatch):
    rows = _samples("NHL", False, 0.5, 1, 100) + _samples(
        "NHL", True, 0.5, 1, 3
    )
    monkeypatch.setattr(dhr, "db", _FakeDB(rows))
    out = _run(dhr.collect_pre_vs_in_game_brier(
        days=30, min_samples=10,
    ))
    assert out["items"] == [], "NHL should not produce a delta row"
    assert any(
        it["league"] == "NHL" and it["n_in"] == 3
        for it in out["samples_too_small"]
    )


def test_pre_game_only_league_is_not_too_small(monkeypatch):
    # In-game loop ships disabled per-league — a pre-game-only
    # league must be silent, not flagged.
    rows = _samples("MLB", False, 0.5, 1, 100)
    monkeypatch.setattr(dhr, "db", _FakeDB(rows))
    out = _run(dhr.collect_pre_vs_in_game_brier(
        days=30, min_samples=10,
    ))
    assert out["items"] == []
    assert out["samples_too_small"] == []


def test_render_text_includes_section_9_header():
    payload = {
        "date_utc": "2026-05-22",
        "pre_vs_in_game_brier": {
            "days": 30, "min_samples_per_side": 50,
            "items": [
                {"league": "NBA", "n_pre": 100, "n_in": 100,
                 "brier_pre": 0.20, "brier_in": 0.205,
                 "abs_delta": 0.005},
                {"league": "NHL", "n_pre": 80, "n_in": 80,
                 "brier_pre": 0.22, "brier_in": 0.26,
                 "abs_delta": 0.04},
            ],
            "samples_too_small": [],
            "error": None,
        },
    }
    _subj, body = dhr.render_text(payload)
    assert "Pre-game vs in-game Brier split" in body
    assert "NBA" in body and "NHL" in body
    # 0.005 is within the 0.02 ship criterion → green.
    # 0.04 is outside → yellow.
    assert "[GREEN]" in body
    assert "[YELLOW]" in body


def test_render_html_includes_table_rows():
    payload = {
        "date_utc": "2026-05-22",
        "pre_vs_in_game_brier": {
            "days": 30, "min_samples_per_side": 50,
            "items": [
                {"league": "NBA", "n_pre": 100, "n_in": 100,
                 "brier_pre": 0.20, "brier_in": 0.21,
                 "abs_delta": 0.01},
            ],
            "samples_too_small": [],
            "error": None,
        },
    }
    html = dhr.render_html(payload)
    assert "Pre-game vs in-game Brier split" in html
    assert "NBA" in html
    assert "0.0100" in html or "0.01" in html
