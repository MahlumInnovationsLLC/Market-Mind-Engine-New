"""Tests for the champion-challenger A/B calibrator framework (task #755).

Covers the moving parts of the A/B framework end-to-end against an
in-memory ``calibration_snapshots`` mock so we can exercise the full
load → refit → promotion loop without a live Postgres:

* Migration 0044 added ``role`` / ``brier_in_sample`` /
  ``brier_out_of_sample`` to the snapshots row — the fake DB schema
  carries them and the persist path writes them.
* ``apply()`` always reads ``role='champion'`` even when both roles
  are loaded (production trade decisions never see the challenger).
* ``apply_both()`` returns the champion + challenger probabilities so
  the predictor can stitch both into ``raw.calibration``.
* ``load_all()`` partitions the latest snapshot per (league, role)
  into the nested-dict state, skipping ``retired_champion``.
* ``refit_all()``:
    - cold start writes only the champion row;
    - warm path writes both rows with temporal-OOS Brier on each.
* Auto-promotion fires only when the challenger's OOS Brier is
  strictly lower than the champion's in BOTH of the most recent two
  cycles (covers the three scenarios: winner / loser / tie).
* ``dashboard.get_calibrator_ab`` returns the per-league summary with
  the last N snapshots for the audit timeline.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from app import calibrators, dashboard, db as _db


# ---------------------------------------------------------------------------
# In-memory calibration_snapshots mock
# ---------------------------------------------------------------------------


class _AbFakeDb:
    """Minimal fake of the calibration_snapshots row store.

    Implements the four query shapes the A/B path depends on:

      * ``DISTINCT ON (sub_category, role)`` — hydrator load_all().
      * ``ORDER BY created_at DESC ... brier_out_of_sample IS NOT NULL``
        — auto-promotion read.
      * ``WITH retired AS (UPDATE ...) UPDATE`` — promotion swap.
      * ``ORDER BY sub_category, created_at DESC`` (full history)
        — dashboard.get_calibrator_ab().
    """

    def __init__(self) -> None:
        self.snapshots: list[dict[str, Any]] = []
        self._next_id = 1
        # Monotonic clock so tests can pin "this snap is newer than
        # that one" without sleeping or fighting time.time() jitter.
        self._clock = float(int(time.time()))

    def _tick(self) -> float:
        self._clock += 1.0
        return self._clock

    def add_snapshot(
        self, *, sub_category: str, role: str = "champion",
        calibrator_state: dict[str, Any] | None = None,
        sample_count: int = 100,
        brier_in_sample: float | None = None,
        brier_out_of_sample: float | None = None,
        created_at: float | None = None,
    ) -> int:
        new_id = self._next_id
        self._next_id += 1
        st = calibrator_state if calibrator_state is not None else {
            "type": "isotonic", "x": [0.0, 1.0], "y": [0.0, 1.0],
        }
        self.snapshots.append({
            "id": new_id,
            "sub_category": sub_category,
            "role": role,
            "calibrator_state": json.dumps(st),
            "sample_count": int(sample_count),
            "brier_in_sample": brier_in_sample,
            "brier_out_of_sample": brier_out_of_sample,
            "created_at": created_at if created_at is not None else self._tick(),
        })
        return new_id

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        q = " ".join(query.split())
        # Auto-promotion read.
        if (
            "FROM calibration_snapshots" in q
            and "brier_out_of_sample IS NOT NULL" in q
            and "ORDER BY created_at DESC" in q
            and "LIMIT 8" in q
        ):
            sub = args[0]
            history = sorted(
                (r for r in self.snapshots if r["sub_category"] == sub),
                key=lambda x: x["created_at"], reverse=True,
            )
            out: list[dict[str, Any]] = []
            for r in history:
                if r.get("brier_out_of_sample") is None:
                    continue
                out.append({
                    "id": r["id"],
                    "role": r.get("role", "champion"),
                    "brier_out_of_sample": r["brier_out_of_sample"],
                    "created_at": r["created_at"],
                })
            return out[:8]

        # Loader DISTINCT ON path.
        if "FROM calibration_snapshots" in q and "DISTINCT ON" in q:
            by_key: dict[tuple[str, str], dict[str, Any]] = {}
            for r in self.snapshots:
                sub = r["sub_category"]
                if not sub.startswith("per_league:"):
                    continue
                role = r.get("role", "champion")
                if role not in ("champion", "challenger"):
                    continue
                key = (sub, role)
                cur = by_key.get(key)
                if cur is None or r["created_at"] > cur["created_at"]:
                    by_key[key] = r
            out2: list[dict[str, Any]] = []
            for r in sorted(by_key.values(), key=lambda x: (x["sub_category"], x["role"])):
                out2.append({
                    "sub_category": r["sub_category"],
                    "role": r["role"],
                    "calibrator_state": r["calibrator_state"],
                    "sample_count": r["sample_count"],
                    "fit_at": float(r["created_at"]),
                })
            return out2

        # Dashboard read — full history per sub_category.
        if "FROM calibration_snapshots" in q and "ORDER BY sub_category" in q:
            out3: list[dict[str, Any]] = []
            ordered = sorted(
                self.snapshots,
                key=lambda x: (x["sub_category"], -x["created_at"]),
            )
            for r in ordered:
                out3.append({
                    "id": r["id"],
                    "sub_category": r["sub_category"],
                    "role": r.get("role", "champion"),
                    "sample_count": r["sample_count"],
                    "brier_in_sample": r.get("brier_in_sample"),
                    "brier_out_of_sample": r.get("brier_out_of_sample"),
                    "fit_at": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ",
                        time.gmtime(r["created_at"]),
                    ),
                    "created_at": r["created_at"],
                })
            return out3

        if "FROM trades t" in q and "trade_postmortems" in q:
            return []
        return []

    async def fetchval(self, query: str, *args: Any):
        q = " ".join(query.split())
        if "INSERT INTO calibration_snapshots" in q and "RETURNING id" in q:
            new_id = self._next_id
            self._next_id += 1
            sub_cat = args[0]
            n = int(args[4])
            state_json = args[5]
            role = args[9] if len(args) > 9 else "champion"
            brier_in = args[10] if len(args) > 10 else None
            brier_oos = args[11] if len(args) > 11 else None
            self.snapshots.append({
                "id": new_id,
                "sub_category": sub_cat,
                "calibrator_state": state_json,
                "sample_count": n,
                "created_at": self._tick(),
                "role": role,
                "brier_in_sample": brier_in,
                "brier_out_of_sample": brier_oos,
            })
            return new_id
        return None

    async def execute(self, query: str, *args: Any) -> str:
        q = " ".join(query.split())
        # Promotion swap: retire all champions for the sub_category,
        # then promote the row whose id matches.
        if "WITH retired" in q and "role = 'retired_champion'" in q:
            sub = args[0]
            new_champ_id = args[1]
            for r in self.snapshots:
                if (
                    r["sub_category"] == sub
                    and r.get("role", "champion") == "champion"
                ):
                    r["role"] = "retired_champion"
            for r in self.snapshots:
                if r["id"] == new_champ_id:
                    r["role"] = "champion"
            return "UPDATE 1"
        return ""


@pytest.fixture
def fake_db(monkeypatch):
    fake = _AbFakeDb()
    monkeypatch.setattr(_db, "fetch", fake.fetch)
    monkeypatch.setattr(_db, "fetchval", fake.fetchval)
    monkeypatch.setattr(_db, "execute", fake.execute)
    calibrators.invalidate()
    return fake


# ---------------------------------------------------------------------------
# apply() / apply_both()
# ---------------------------------------------------------------------------


def _make_cal(
    league: str, kind: str = "isotonic", *, role: str = calibrators.ROLE_CHAMPION,
    state: dict[str, Any] | None = None, n: int = 100,
) -> calibrators._Calibrator:
    return calibrators._Calibrator(
        league=league,
        type=kind,
        state=state or {"x": [0.0, 0.5, 1.0], "y": [0.1, 0.4, 0.9]},
        fit_at=time.time(),
        sample_count=n,
        role=role,
    )


def test_apply_uses_champion_only_even_when_challenger_loaded():
    """Sanity: production trade decisions must never see the challenger."""
    calibrators.invalidate()
    state = calibrators.get_state()
    state.calibrators["NBA"] = {
        calibrators.ROLE_CHAMPION: _make_cal(
            "NBA", state={"x": [0.0, 1.0], "y": [0.0, 1.0]},  # identity
        ),
        calibrators.ROLE_CHALLENGER: _make_cal(
            "NBA", state={"x": [0.0, 1.0], "y": [0.5, 0.5]},  # squashes to 0.5
        ),
    }
    out, info = calibrators.apply(0.7, "NBA")
    assert info["applied"] is True
    assert info["role"] == calibrators.ROLE_CHAMPION
    # Identity champion → 0.7 in, 0.7 out.  If the challenger leaked in
    # we'd see 0.5 instead.
    assert out == pytest.approx(0.7, abs=1e-6)


def test_apply_both_returns_both_probabilities():
    calibrators.invalidate()
    state = calibrators.get_state()
    state.calibrators["NBA"] = {
        calibrators.ROLE_CHAMPION: _make_cal(
            "NBA", state={"x": [0.0, 1.0], "y": [0.0, 1.0]},
        ),
        calibrators.ROLE_CHALLENGER: _make_cal(
            "NBA", state={"x": [0.0, 1.0], "y": [0.5, 0.5]},
        ),
    }
    out, info = calibrators.apply_both(0.7, "NBA")
    assert out == pytest.approx(0.7)  # champion-driven
    assert info["p_champion"] == pytest.approx(0.7)
    assert info["p_challenger"] == pytest.approx(0.5)
    assert info["challenger_applied"] is True


def test_apply_both_handles_missing_challenger():
    """When no challenger is loaded the predictor still gets clean info."""
    calibrators.invalidate()
    state = calibrators.get_state()
    state.calibrators["NBA"] = {
        calibrators.ROLE_CHAMPION: _make_cal(
            "NBA", state={"x": [0.0, 1.0], "y": [0.0, 1.0]},
        ),
    }
    out, info = calibrators.apply_both(0.6, "NBA")
    assert out == pytest.approx(0.6)
    assert info["p_champion"] == pytest.approx(0.6)
    assert info["p_challenger"] is None
    assert info["challenger_applied"] is False


def test_apply_both_bypasses_when_no_calibrator():
    calibrators.invalidate()
    out, info = calibrators.apply_both(0.55, "NBA")
    assert out == pytest.approx(0.55)
    assert info["applied"] is False
    assert info["p_challenger"] is None
    # ``p_champion`` must always equal the value the predictor uses for
    # trading — on bypass that's the raw probability.  Returning None
    # here would force every dashboard / log consumer to special-case
    # cold-start leagues.
    assert info["p_champion"] == pytest.approx(0.55)


def test_apply_both_p_champion_mirrors_returned_probability():
    """Whether or not a calibrator is loaded, ``info['p_champion']``
    matches the first element of the return tuple — the contract the
    predictor and dashboard depend on.
    """
    calibrators.invalidate()
    state = calibrators.get_state()
    state.calibrators["NBA"] = {
        calibrators.ROLE_CHAMPION: _make_cal(
            "NBA", state={"x": [0.0, 1.0], "y": [0.5, 0.5]},  # squash
        ),
    }
    out, info = calibrators.apply_both(0.7, "NBA")
    assert out == pytest.approx(0.5)
    assert info["p_champion"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# load_all() partitions by role
# ---------------------------------------------------------------------------


def test_load_all_partitions_champion_and_challenger(fake_db):
    fake_db.add_snapshot(
        sub_category="per_league:NBA", role="champion",
        calibrator_state={"type": "isotonic", "x": [0.0, 1.0], "y": [0.0, 1.0]},
        sample_count=200,
    )
    fake_db.add_snapshot(
        sub_category="per_league:NBA", role="challenger",
        calibrator_state={"type": "platt", "a": 1.5, "b": -0.2},
        sample_count=180,
    )
    state = asyncio.run(calibrators.load_all())
    assert state.champion("NBA") is not None
    assert state.champion("NBA").type == "isotonic"
    assert state.challenger("NBA") is not None
    assert state.challenger("NBA").type == "platt"


def test_load_all_skips_retired_champion(fake_db):
    fake_db.add_snapshot(
        sub_category="per_league:NBA", role="retired_champion",
        calibrator_state={"type": "isotonic", "x": [0.0, 1.0], "y": [0.5, 0.5]},
    )
    fake_db.add_snapshot(
        sub_category="per_league:NBA", role="champion",
        calibrator_state={"type": "isotonic", "x": [0.0, 1.0], "y": [0.0, 1.0]},
    )
    state = asyncio.run(calibrators.load_all())
    cal = state.champion("NBA")
    assert cal is not None
    # The retired_champion has y=[0.5,0.5] (squash); the active champion
    # is the identity.  If the loader picked up retired, apply(0.7) would
    # return 0.5 — the active one returns 0.7.
    out, _ = calibrators.apply(0.7, "NBA")
    assert out == pytest.approx(0.7)


# ---------------------------------------------------------------------------
# Temporal split
# ---------------------------------------------------------------------------


def test_temporal_split_holds_out_trailing_slice():
    probs = [0.5] * 50
    ys = [1] * 50
    sources = ["live"] * 50
    ts = list(range(50))
    is_p, is_y, _is_s, oos_p, oos_y, _oos_s = calibrators._temporal_split(
        probs, ys, sources, ts,
    )
    # 10% of 50 = 5 = OOS_HOLDOUT_MIN floor; trailing 5 land in OOS.
    assert len(oos_p) == 5
    assert len(is_p) == 45
    assert len(oos_y) == 5


def test_temporal_split_resorts_unordered_input():
    """Hand-rolled buckets may insert rows in any ts order — the split
    must re-sort so the OOS slice is genuinely the chronological tail.
    """
    # ts=[5,1,4,2,3] → after sort: [1,2,3,4,5]; trailing 5 = whole set
    # so we use a 12-row bucket to keep both slices populated.
    probs = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 0.05, 0.95]
    ys = [0, 0, 1, 1, 0, 1, 0, 1, 1, 1, 0, 1]
    sources = ["live"] * 12
    ts = [11, 1, 10, 2, 9, 3, 8, 4, 7, 5, 6, 0]  # not monotonic
    is_p, is_y, _is_s, oos_p, oos_y, _oos_s = calibrators._temporal_split(
        probs, ys, sources, ts,
    )
    assert len(oos_p) == 5  # OOS_HOLDOUT_MIN
    # The five highest ts values are 7, 8, 9, 10, 11 → indices 8,6,4,2,0
    # → probs 0.9, 0.7, 0.5, 0.3, 0.1 in chronological order.
    assert oos_p == [0.9, 0.7, 0.5, 0.3, 0.1]
    assert len(is_p) == 7


# ---------------------------------------------------------------------------
# refit_all — cold start vs warm path
# ---------------------------------------------------------------------------


def _seed_training_rows(grouped_by_league: dict[str, list[tuple[float, int, float]]]):
    """Returns a `_fetch_training_data`-shaped result dict the test can
    monkeypatch in place of the real DB-walking implementation.
    """
    out: dict[str, dict[str, list]] = {}
    for lg, rows in grouped_by_league.items():
        bucket = {"probs": [], "ys": [], "sources": [], "ts": []}
        for p, y, ts in rows:
            bucket["probs"].append(float(p))
            bucket["ys"].append(int(y))
            bucket["sources"].append("live")
            bucket["ts"].append(float(ts))
        out[lg] = bucket
    return out


def _stub_training(monkeypatch, grouped):
    async def _fake_fetch_training_data():
        return grouped
    monkeypatch.setattr(
        calibrators, "_fetch_training_data", _fake_fetch_training_data,
    )


def test_refit_cold_start_writes_only_champion(fake_db, monkeypatch):
    """No prior champion in memory → first refit writes a single row
    with role='champion' and skips the challenger entirely (no A/B
    target yet to compete against).
    """
    grouped = _seed_training_rows({
        "NBA": [(0.7, i % 2, float(i)) for i in range(80)],
    })
    _stub_training(monkeypatch, grouped)
    summary = asyncio.run(calibrators.refit_all())
    nba = next(r for r in summary["leagues"] if r["league"] == "NBA")
    assert nba["snapshot_id"] is not None
    assert nba["challenger_snapshot_id"] is None
    assert nba["promoted"] is False
    rows = [r for r in fake_db.snapshots if r["sub_category"] == "per_league:NBA"]
    assert len(rows) == 1
    assert rows[0]["role"] == "champion"


def test_refit_skips_challenger_when_champion_insert_fails(fake_db, monkeypatch):
    """If champion persistence fails the challenger row must NOT be
    written and promotion must NOT run — otherwise the most-recent two
    pairs the promotion gate inspects can be mis-paired and a
    challenger could promote against a champion that was never
    recorded.
    """
    state = calibrators.get_state()
    state.calibrators["NBA"] = {
        calibrators.ROLE_CHAMPION: _make_cal(
            "NBA", state={"x": [0.0, 1.0], "y": [0.0, 1.0]},
        ),
    }
    grouped = _seed_training_rows({
        "NBA": [(0.7 if i % 2 else 0.3, i % 2, float(i)) for i in range(80)],
    })
    _stub_training(monkeypatch, grouped)

    real_persist = calibrators._persist_calibrator
    calls: list[str] = []

    async def _flaky_persist(*args, **kwargs):
        role = kwargs.get("role", "champion")
        calls.append(role)
        if role == "champion":
            return None  # simulate a transient INSERT failure
        return await real_persist(*args, **kwargs)

    monkeypatch.setattr(calibrators, "_persist_calibrator", _flaky_persist)

    promoted_calls: list[str] = []

    async def _spy_promote(league):
        promoted_calls.append(league)
        return False, "should-not-be-called"

    monkeypatch.setattr(calibrators, "_maybe_promote_challenger", _spy_promote)

    summary = asyncio.run(calibrators.refit_all())
    nba = next(r for r in summary["leagues"] if r["league"] == "NBA")
    assert nba["snapshot_id"] is None
    assert nba["challenger_snapshot_id"] is None
    assert nba["promoted"] is False
    # _persist_calibrator was called for champion and refused; never for
    # challenger.  Promotion gate must also be skipped.
    assert calls == ["champion"]
    assert promoted_calls == []


def test_refit_warm_path_writes_champion_and_challenger(fake_db, monkeypatch):
    """A prior champion in memory → refit writes BOTH rows with in/oos
    Brier populated.
    """
    state = calibrators.get_state()
    state.calibrators["NBA"] = {
        calibrators.ROLE_CHAMPION: _make_cal(
            "NBA", state={"x": [0.0, 1.0], "y": [0.0, 1.0]},
        ),
    }
    grouped = _seed_training_rows({
        "NBA": [(0.7 if i % 2 else 0.3, i % 2, float(i)) for i in range(80)],
    })
    _stub_training(monkeypatch, grouped)
    summary = asyncio.run(calibrators.refit_all())
    nba = next(r for r in summary["leagues"] if r["league"] == "NBA")
    assert nba["snapshot_id"] is not None
    assert nba["challenger_snapshot_id"] is not None
    assert nba["champion_brier_in_sample"] is not None
    assert nba["champion_brier_out_of_sample"] is not None
    assert nba["challenger_brier_in_sample"] is not None
    assert nba["challenger_brier_out_of_sample"] is not None
    rows = [r for r in fake_db.snapshots if r["sub_category"] == "per_league:NBA"]
    roles = sorted(r["role"] for r in rows)
    assert roles == ["challenger", "champion"]
    for r in rows:
        assert r["brier_in_sample"] is not None
        assert r["brier_out_of_sample"] is not None


# ---------------------------------------------------------------------------
# Auto-promotion — three scenarios
# ---------------------------------------------------------------------------


def test_promotion_fires_on_two_consecutive_challenger_wins(fake_db):
    """Cycle 1 + cycle 2: challenger oos is strictly better → swap fires."""
    sub = "per_league:NBA"
    fake_db.add_snapshot(
        sub_category=sub, role="champion",
        brier_in_sample=0.20, brier_out_of_sample=0.22,
    )
    fake_db.add_snapshot(
        sub_category=sub, role="challenger",
        brier_in_sample=0.18, brier_out_of_sample=0.20,
    )
    fake_db.add_snapshot(
        sub_category=sub, role="champion",
        brier_in_sample=0.20, brier_out_of_sample=0.21,
    )
    challenger_id = fake_db.add_snapshot(
        sub_category=sub, role="challenger",
        brier_in_sample=0.17, brier_out_of_sample=0.19,
    )
    promoted, reason = asyncio.run(
        calibrators._maybe_promote_challenger("NBA"),
    )
    assert promoted is True
    assert "challenger won" in (reason or "").lower()
    promoted_row = next(r for r in fake_db.snapshots if r["id"] == challenger_id)
    assert promoted_row["role"] == "champion"
    # Every prior champion is now retired.
    retired = [
        r for r in fake_db.snapshots
        if r["sub_category"] == sub and r["role"] == "retired_champion"
    ]
    assert len(retired) == 2


def test_promotion_does_not_fire_when_challenger_loses(fake_db):
    """Even with two cycles, if either challenger is worse → no swap."""
    sub = "per_league:NBA"
    fake_db.add_snapshot(
        sub_category=sub, role="champion",
        brier_in_sample=0.20, brier_out_of_sample=0.20,
    )
    fake_db.add_snapshot(
        sub_category=sub, role="challenger",
        brier_in_sample=0.18, brier_out_of_sample=0.25,  # worse oos
    )
    fake_db.add_snapshot(
        sub_category=sub, role="champion",
        brier_in_sample=0.20, brier_out_of_sample=0.21,
    )
    fake_db.add_snapshot(
        sub_category=sub, role="challenger",
        brier_in_sample=0.17, brier_out_of_sample=0.19,  # better oos
    )
    promoted, reason = asyncio.run(
        calibrators._maybe_promote_challenger("NBA"),
    )
    assert promoted is False
    assert "not better" in (reason or "")
    # Champion stays champion.
    champs = [
        r for r in fake_db.snapshots
        if r["sub_category"] == sub and r["role"] == "champion"
    ]
    assert len(champs) == 2


def test_promotion_does_not_fire_on_tie(fake_db):
    """Tied OOS Brier counts as a non-win → no swap."""
    sub = "per_league:NBA"
    fake_db.add_snapshot(
        sub_category=sub, role="champion",
        brier_in_sample=0.20, brier_out_of_sample=0.20,
    )
    fake_db.add_snapshot(
        sub_category=sub, role="challenger",
        brier_in_sample=0.20, brier_out_of_sample=0.20,  # tie
    )
    fake_db.add_snapshot(
        sub_category=sub, role="champion",
        brier_in_sample=0.20, brier_out_of_sample=0.20,
    )
    fake_db.add_snapshot(
        sub_category=sub, role="challenger",
        brier_in_sample=0.20, brier_out_of_sample=0.20,  # tie
    )
    promoted, reason = asyncio.run(
        calibrators._maybe_promote_challenger("NBA"),
    )
    assert promoted is False
    assert "not better" in (reason or "")


def test_promotion_skips_when_history_too_short(fake_db):
    """Only one challenger snapshot → not enough evidence → no swap."""
    sub = "per_league:NBA"
    fake_db.add_snapshot(
        sub_category=sub, role="champion",
        brier_in_sample=0.20, brier_out_of_sample=0.22,
    )
    fake_db.add_snapshot(
        sub_category=sub, role="challenger",
        brier_in_sample=0.10, brier_out_of_sample=0.12,
    )
    promoted, reason = asyncio.run(
        calibrators._maybe_promote_challenger("NBA"),
    )
    assert promoted is False
    assert "insufficient history" in (reason or "")


# ---------------------------------------------------------------------------
# Dashboard tile shape
# ---------------------------------------------------------------------------


def test_dashboard_get_calibrator_ab_returns_history(fake_db):
    sub = "per_league:NBA"
    fake_db.add_snapshot(
        sub_category=sub, role="champion",
        sample_count=120,
        brier_in_sample=0.20, brier_out_of_sample=0.22,
    )
    fake_db.add_snapshot(
        sub_category=sub, role="challenger",
        sample_count=108,
        brier_in_sample=0.19, brier_out_of_sample=0.20,
    )
    fake_db.add_snapshot(
        sub_category=sub, role="champion",
        sample_count=125,
        brier_in_sample=0.21, brier_out_of_sample=0.21,
    )
    fake_db.add_snapshot(
        sub_category=sub, role="challenger",
        sample_count=112,
        brier_in_sample=0.18, brier_out_of_sample=0.19,
    )
    payload = asyncio.run(dashboard.get_calibrator_ab(history_limit=5))
    nba = next(it for it in payload["items"] if it["league"] == "NBA")
    assert nba["champion"] is not None
    assert nba["challenger"] is not None
    # Latest of each role wins.
    assert nba["champion"]["brier_out_of_sample"] == pytest.approx(0.21)
    assert nba["challenger"]["brier_out_of_sample"] == pytest.approx(0.19)
    # All four snapshots fit under the history_limit=5 cap.
    assert len(nba["snapshots"]) == 4
    assert payload["champion_count"] >= 1
    assert payload["challenger_count"] >= 1


def test_dashboard_get_calibrator_ab_flags_recent_promotion(fake_db):
    """``last_promotion_at`` surfaces when a champion row directly follows
    a retired_champion row in the time-ordered history.
    """
    sub = "per_league:NBA"
    fake_db.add_snapshot(
        sub_category=sub, role="retired_champion",
        sample_count=100,
        brier_in_sample=0.22, brier_out_of_sample=0.23,
    )
    fake_db.add_snapshot(
        sub_category=sub, role="champion",
        sample_count=110,
        brier_in_sample=0.18, brier_out_of_sample=0.19,
    )
    payload = asyncio.run(dashboard.get_calibrator_ab(history_limit=5))
    nba = next(it for it in payload["items"] if it["league"] == "NBA")
    assert nba["last_promotion_at"] is not None
