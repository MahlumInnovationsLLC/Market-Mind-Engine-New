"""Tests for ``dashboard.get_soccer_per_leg_brier`` (Task #902).

Section 9 of the daily honesty digest already breaks per-league Brier
out by HOME / DRAW / AWAY for soccer leagues (task #901), but operators
only see that once a day.  The soccer forensics console wraps the same
``collect_pre_vs_in_game_brier`` helper so the rolling per-leg breakdown
is visible between digests — and so the numbers reconcile with Section 9
by construction.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app import dashboard
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


def _samples(league: str, is_live: bool, p: float, y: int, n: int,
             leg: str | None = None) -> list[dict[str, Any]]:
    meta = {"leg_orientation": leg} if leg else None
    return [
        {"league": league, "is_live": is_live, "predicted_prob": p,
         "actual_outcome": y, "metadata": meta}
        for _ in range(n)
    ]


def test_filters_to_soccer_leagues_and_keeps_legs(monkeypatch):
    rows: list[dict[str, Any]] = []
    # Non-soccer league should be dropped.
    rows += _samples("NBA", False, 1.0, 1, 50)
    rows += _samples("NBA", True, 0.9, 1, 50)
    # EPL with per-leg breakdown that mirrors Section 9.
    rows += _samples("EPL", False, 1.0, 1, 15, leg="HOME")
    rows += _samples("EPL", True, 0.9, 1, 15, leg="HOME")
    rows += _samples("EPL", False, 1.0, 1, 15, leg="DRAW")
    rows += _samples("EPL", True, 0.8, 1, 15, leg="DRAW")
    rows += _samples("EPL", False, 1.0, 1, 15, leg="AWAY")
    rows += _samples("EPL", True, 0.7, 1, 15, leg="AWAY")
    monkeypatch.setattr(dhr, "db", _FakeDB(rows))

    out = _run(dashboard.get_soccer_per_leg_brier(days=30, min_samples=10))

    assert out["error"] is None
    assert out["days"] == 30
    assert out["min_samples_per_side"] == 10
    assert [it["league"] for it in out["items"]] == ["EPL"]
    legs = {leg["leg"]: leg for leg in out["items"][0].get("legs") or []}
    assert set(legs) == {"HOME", "DRAW", "AWAY"}
    assert legs["HOME"]["abs_delta"] == pytest.approx(0.01, abs=1e-6)
    assert legs["DRAW"]["abs_delta"] == pytest.approx(0.04, abs=1e-6)
    assert legs["AWAY"]["abs_delta"] == pytest.approx(0.09, abs=1e-6)
    # Soccer league enumeration is exposed for the UI header pill.
    assert "EPL" in out["leagues"]
    # fetched_at is an ISO-ish UTC stamp.
    assert out["fetched_at"].endswith("Z")


def test_reconciles_with_section_9(monkeypatch):
    # Same fixture should produce the same per-league items + legs
    # whether collected through Section 9 or through the dashboard
    # wrapper — that's the whole point of the wrapper.
    rows: list[dict[str, Any]] = []
    rows += _samples("EPL", False, 1.0, 1, 20, leg="HOME")
    rows += _samples("EPL", True, 0.85, 1, 20, leg="HOME")
    rows += _samples("EPL", False, 1.0, 1, 20, leg="DRAW")
    rows += _samples("EPL", True, 0.75, 1, 20, leg="DRAW")
    rows += _samples("EPL", False, 1.0, 1, 20, leg="AWAY")
    rows += _samples("EPL", True, 0.65, 1, 20, leg="AWAY")
    monkeypatch.setattr(dhr, "db", _FakeDB(rows))

    section9 = _run(dhr.collect_pre_vs_in_game_brier(
        days=30, min_samples=10,
    ))
    console = _run(dashboard.get_soccer_per_leg_brier(
        days=30, min_samples=10,
    ))

    s9_epl = next(it for it in section9["items"] if it["league"] == "EPL")
    cn_epl = next(it for it in console["items"] if it["league"] == "EPL")
    # Strip ordering differences: compare league-level numbers and the
    # per-leg sub-rows keyed by leg.
    for field in ("n_pre", "n_in", "brier_pre", "brier_in", "abs_delta"):
        assert s9_epl[field] == cn_epl[field], field
    s9_legs = {row["leg"]: row for row in s9_epl.get("legs") or []}
    cn_legs = {row["leg"]: row for row in cn_epl.get("legs") or []}
    assert set(s9_legs) == set(cn_legs)
    for leg, s9_row in s9_legs.items():
        for field in ("n_pre", "n_in", "brier_pre", "brier_in", "abs_delta"):
            assert s9_row[field] == cn_legs[leg][field], (leg, field)


def test_too_small_is_filtered_to_soccer(monkeypatch):
    # NHL has live rows below the bar — must NOT show up on the soccer
    # console even though Section 9 would list it.
    rows = (
        _samples("NHL", False, 0.5, 1, 100)
        + _samples("NHL", True, 0.5, 1, 3)
        + _samples("EPL", False, 0.5, 1, 100)
        + _samples("EPL", True, 0.5, 1, 3)
    )
    monkeypatch.setattr(dhr, "db", _FakeDB(rows))

    out = _run(dashboard.get_soccer_per_leg_brier(days=30, min_samples=10))

    assert out["items"] == []
    leagues_too_small = {t["league"] for t in out["samples_too_small"]}
    assert "EPL" in leagues_too_small
    assert "NHL" not in leagues_too_small


def test_propagates_section_9_error(monkeypatch):
    class _BrokenDB:
        async def fetch(self, *a, **kw):
            raise RuntimeError("boom")

    monkeypatch.setattr(dhr, "db", _BrokenDB())
    out = _run(dashboard.get_soccer_per_leg_brier(days=30, min_samples=10))
    assert out["items"] == []
    assert out["error"] and "boom" in out["error"]
