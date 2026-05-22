"""Tests for the niche-sport calibrator seed helper (task #831)."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from app.niche_sports import calibrator_scaffold as cs


class _FakeDB:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, query: str, *args: Any) -> None:
        self.calls.append((query, args))


def _run(coro):
    return asyncio.run(coro)


def test_seed_all_inserts_one_row_per_league(monkeypatch):
    fake = _FakeDB()
    monkeypatch.setattr(cs, "db", fake)
    out = _run(cs.seed_all())
    assert out["seeded"] == 5
    assert sorted(out["leagues"]) == ["ATP", "F1", "PGA", "UFC", "WTA"]
    assert out["errors"] == []
    # one execute per league
    assert len(fake.calls) == 5
    seen_subcats = set()
    for _, args in fake.calls:
        sub = args[0]
        seen_subcats.add(sub)
        # calibrator_state must be a true identity (two-point isotonic
        # over [0,1] → [0,1]).  Identity Platt with a=1,b=0 would
        # actually run sigmoid(p), NOT a pass-through, so the scaffold
        # uses isotonic — guard the choice with an explicit assertion.
        state = json.loads(args[1])
        assert state["type"] == "isotonic"
        assert state["x"] == [0.0, 1.0]
        assert state["y"] == [0.0, 1.0]
        assert state["seed"] is True
        assert state["task"] == 831
    assert seen_subcats == {
        "per_league:ATP", "per_league:WTA", "per_league:F1",
        "per_league:PGA", "per_league:UFC",
    }


def test_seed_all_restrict_leagues(monkeypatch):
    fake = _FakeDB()
    monkeypatch.setattr(cs, "db", fake)
    out = _run(cs.seed_all(leagues=("UFC",)))
    assert out["seeded"] == 1
    assert out["leagues"] == ["UFC"]


def test_seed_all_collects_errors_without_raising(monkeypatch):
    class _BrokenDB:
        async def execute(self, *_a, **_kw):
            raise RuntimeError("boom")

    monkeypatch.setattr(cs, "db", _BrokenDB())
    out = _run(cs.seed_all(leagues=("ATP", "WTA")))
    assert out["seeded"] == 0
    assert len(out["errors"]) == 2
    assert all("RuntimeError" in e["error"] for e in out["errors"])


def test_seed_targets_list_matches_documented_set():
    assert cs.SEED_LEAGUES == ("ATP", "WTA", "F1", "PGA", "UFC")


def test_identity_isotonic_seed_is_pass_through_under_apply():
    """The seed state, when run through ``calibrators._apply_isotonic``,
    must return the input probability unchanged for every p in [0, 1].

    This guards against accidentally swapping to "identity Platt"
    (a=1, b=0), which would actually run sigmoid(p) instead of p.
    """
    from app import calibrators

    state = cs._identity_isotonic_state()
    for p in (0.0, 0.05, 0.27, 0.5, 0.73, 0.95, 1.0):
        out = calibrators._apply_isotonic(p, state)
        assert abs(out - p) < 1e-9, f"identity broken at p={p}: out={out}"
