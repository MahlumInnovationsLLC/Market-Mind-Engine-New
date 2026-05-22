"""Tests for the NHL in-game live-prediction wiring (task #856).

Covers:
  * The NHL live-feature builder is null-safe and emits a fixed-width
    bounded vector.
  * ``_predict_live_for_market`` dispatches to the NHL builder + the
    canonical predictor chain and returns a calibrated YES probability
    for an NHL row; non-NHL rows still return None.
  * ``run_in_game_cycle("nhl")`` invokes the predictor for matched
    rows and stamps the new ``model_yes_prob`` via
    ``_persist_live_model_yes_prob`` (so the orchestrator's next
    sweep routes the updated value through a ``RiskContext`` with
    ``is_live=True`` and the live risk gates apply).
  * The per-sport flag still ships disabled — first invocation with
    a bare runtime_settings table returns ``enabled=False``.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.learning import in_game_loop as _loop
from app.learning import nhl_live_features as _nhlf


@pytest.fixture(autouse=True)
def _isolate_event_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _loop._reset_state_for_tests()
    yield
    loop.close()
    _loop._reset_state_for_tests()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# NHL feature builder
# ---------------------------------------------------------------------------


def test_nhl_builder_emits_fixed_width_vector():
    names, values = _nhlf.build_nhl_live(
        {
            "home_score": 2, "away_score": 1,
            "period": "2", "clock": "10:00",
            "model_yes_prob": 0.55, "kalshi_price": 0.60,
            "state_age_seconds": 12.0,
        }
    )
    assert tuple(names) == _nhlf.NHL_LIVE_FEATURE_NAMES
    assert len(values) == len(_nhlf.NHL_LIVE_FEATURE_NAMES)
    for v in values:
        assert isinstance(v, float)
        assert v == v  # not NaN
    # score_diff = 1, abs = 1
    assert values[0] == 1.0
    assert values[1] == 1.0
    # period 2
    assert values[2] == 2.0
    # regulation remaining: 1 full period (20:00) + 10:00 left of P2 = 30:00 of 60:00
    assert abs(values[3] - 0.5) < 1e-6
    # not OT / SO
    assert values[4] == 0.0
    assert values[5] == 0.0
    assert values[6] == 0.55
    assert values[7] == 0.60
    assert values[8] == 12.0


def test_nhl_builder_null_safe():
    # Empty candidate must not raise and must yield bounded defaults.
    names, values = _nhlf.build_nhl_live({})
    assert len(names) == len(values)
    for v in values:
        assert v == v
        assert v not in (float("inf"), float("-inf"))
    # Defaults: 0 score, period 1, full regulation remaining, 0.5 prob/price
    assert values[0] == 0.0
    assert values[2] == 1.0
    assert abs(values[3] - 1.0) < 1e-6
    assert values[6] == 0.5
    assert values[7] == 0.5


def test_nhl_builder_overtime_and_shootout_flags():
    _, ot = _nhlf.build_nhl_live({"period": "OT"})
    assert ot[2] == 4.0
    assert ot[3] == 0.0  # regulation done
    assert ot[4] == 1.0
    assert ot[5] == 0.0

    _, so = _nhlf.build_nhl_live({"period": "SO"})
    assert so[2] == 5.0
    assert so[3] == 0.0
    assert so[4] == 0.0
    assert so[5] == 1.0


def test_nhl_builder_clock_handles_garbage():
    # Garbled clock falls back to "full period remaining" so the
    # regulation_remaining_frac is the maximum for that period.
    _, vals = _nhlf.build_nhl_live({"period": "3", "clock": "garbage"})
    # period 3 with 20:00 still on the clock → 20:00 / 60:00 ≈ 0.333…
    assert abs(vals[3] - (20.0 / 60.0)) < 1e-6


# ---------------------------------------------------------------------------
# _predict_live_for_market dispatch
# ---------------------------------------------------------------------------


class _FakePrediction:
    def __init__(self, probability: float) -> None:
        self.probability = probability


class _FakePredictor:
    def __init__(self, prob: float) -> None:
        self.prob = prob
        self.calls: list[tuple[list[float], str | None]] = []

    async def predict(self, features, *, league=None):
        self.calls.append((list(features), league))
        return _FakePrediction(self.prob)


def test_predict_dispatches_to_nhl_builder_and_predictor(monkeypatch):
    fake_predictor = _FakePredictor(prob=0.72)
    import sys
    # Use monkeypatch.setitem so sys.modules is restored after the test —
    # otherwise downstream tests (task #881 added per-sport predict tests
    # that go through the real predictor module) see this fake instead.
    monkeypatch.setitem(sys.modules, "app.predictor", fake_predictor)
    # Also satisfy the relative import inside _predict_live_via_registry.
    import app
    monkeypatch.setattr(app, "predictor", fake_predictor, raising=False)

    row = {
        "market_id": "KX-NHL-A", "league": "NHL",
        "home_score": 3, "away_score": 1, "period": "3", "clock": "05:00",
        "model_yes_prob": 0.62, "kalshi_price": 0.70,
        "state_age_seconds": 8.0, "is_live": True,
    }
    prob = _run(_loop._predict_live_via_registry(row, sport="nhl"))
    assert prob == 0.72
    assert len(fake_predictor.calls) == 1
    feats, league = fake_predictor.calls[0]
    assert league == "NHL"
    assert len(feats) == len(_nhlf.NHL_LIVE_FEATURE_NAMES)


def test_predict_returns_none_for_unregistered_sport(monkeypatch):
    # NBA has no registered builder yet → falls through to None.
    row = {"market_id": "KX-NBA-A", "league": "NBA"}
    assert _run(_loop._predict_live_via_registry(row, sport="nba")) is None


def test_predict_swallows_predictor_errors(monkeypatch):
    class _Broken:
        async def predict(self, *_a, **_kw):
            raise RuntimeError("model not loaded")

    import app
    monkeypatch.setattr(app, "predictor", _Broken(), raising=False)
    row = {"market_id": "KX-NHL-Z", "league": "NHL"}
    # MUST NOT raise — best-effort by design so a sick model doesn't
    # stall the loop.
    assert _run(_loop._predict_live_via_registry(row, sport="nhl")) is None


def test_predict_clamps_out_of_range_probability(monkeypatch):
    import app
    monkeypatch.setattr(app, "predictor", _FakePredictor(1.5), raising=False)
    row = {"market_id": "KX-NHL-Q", "league": "NHL"}
    assert _run(_loop._predict_live_via_registry(row, sport="nhl")) is None


# ---------------------------------------------------------------------------
# run_in_game_cycle integration
# ---------------------------------------------------------------------------


class _FakeDB:
    def __init__(self, *, enabled: bool, rows: list[dict[str, Any]]) -> None:
        self.enabled = enabled
        self.rows = rows
        self.update_calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, query, *args):
        if "runtime_settings" in query and "live_loop_enabled_" in args[0]:
            return {"value": "true" if self.enabled else "false"}
        if "runtime_settings" in query and "live_loop_cadence_" in args[0]:
            return None
        return None

    async def fetch(self, query, *args):
        if "sportradar_live_state" in query:
            return self.rows
        return []

    async def execute(self, query, *args):
        if "UPDATE predictions" in query and "is_live = true" in query:
            self.update_calls.append(("tag", args))
            return "UPDATE 1"
        if "UPDATE predictions" in query and "model_yes_prob" in query:
            self.update_calls.append(("persist", args))
            return "UPDATE 1"
        return "OK"


def test_nhl_cycle_invokes_predictor_and_persists(monkeypatch):
    fake_predictor = _FakePredictor(prob=0.81)
    import app
    monkeypatch.setattr(app, "predictor", fake_predictor, raising=False)

    fake_db = _FakeDB(
        enabled=True,
        rows=[
            {
                "market_id": "KX-NHL-A", "league": "NHL",
                "game_id": "g1", "game_time": None, "is_live": False,
                "model_yes_prob": 0.60, "kalshi_price": 0.55,
                "status": "inprogress", "state_age_seconds": 5.0,
                "home_score": 2, "away_score": 2,
                "period": "3", "clock": "08:30",
            },
        ],
    )
    monkeypatch.setattr(_loop, "db", fake_db)

    r = _run(_loop.run_in_game_cycle("nhl"))
    assert r.enabled is True
    assert r.skipped_reason is None
    assert r.candidates_scanned == 1
    assert r.candidates_live == 1
    assert r.candidates_predicted == 1
    assert r.candidates_prob_updated == 1
    # Writes: one to tag is_live, then a persist from the generic
    # dispatcher (task #885 wired NHL on the same generic path NBA
    # uses) AND a persist from the registry path.  Both write the
    # same column; the registry one carries the meta-model 0.81.
    kinds = [k for k, _ in fake_db.update_calls]
    assert kinds.count("tag") == 1
    assert kinds.count("persist") >= 1
    persist_probs = [args[1] for k, args in fake_db.update_calls if k == "persist"]
    assert 0.81 in persist_probs
    persist_mids = {args[0] for k, args in fake_db.update_calls if k == "persist"}
    assert persist_mids == {"KX-NHL-A"}
    # Predictor saw the NHL league for calibration.
    assert fake_predictor.calls[0][1] == "NHL"


def test_disabled_by_default_for_nhl(monkeypatch):
    class _Empty:
        async def fetchrow(self, *a, **kw):
            return None

        async def fetch(self, *a, **kw):
            return []

        async def execute(self, *a, **kw):
            return "OK"

    monkeypatch.setattr(_loop, "db", _Empty())
    r = _run(_loop.run_in_game_cycle("nhl"))
    assert r.enabled is False
    assert r.skipped_reason is not None
    assert "live_loop_enabled_nhl" in r.skipped_reason


def test_nhl_cycle_continues_when_predictor_returns_none(monkeypatch):
    """A row whose predictor returns None still gets tagged is_live —
    the loop must keep working even if the model temporarily can't
    produce a calibrated probability."""
    class _Predictor:
        async def predict(self, *_a, **_kw):
            from app.predictor import PredictorUnavailable
            raise PredictorUnavailable("no model loaded")

    import app
    monkeypatch.setattr(app, "predictor", _Predictor(), raising=False)

    fake_db = _FakeDB(
        enabled=True,
        rows=[{
            "market_id": "KX-NHL-B", "league": "NHL",
            "game_id": "g2", "game_time": None, "is_live": False,
            "model_yes_prob": 0.60, "kalshi_price": 0.55,
            "status": "inprogress", "state_age_seconds": 5.0,
            "home_score": 1, "away_score": 0,
            "period": "1", "clock": "15:00",
        }],
    )
    monkeypatch.setattr(_loop, "db", fake_db)
    r = _run(_loop.run_in_game_cycle("nhl"))
    assert r.candidates_live == 1
    assert r.candidates_predicted == 0
    assert r.candidates_prob_updated == 0
    # Tag still happened so the dashboard tile + risk gates see the row as live.
    assert any(k == "tag" for k, _ in fake_db.update_calls)
    # The registry path didn't persist (meta-model predictor raised) — but
    # the generic dispatcher (task #885 — closed-form blend of pre-game
    # prior + market mid) is unaffected by the meta-model outage and still
    # writes its own model_yes_prob refresh.  That's the whole point of
    # carrying the generic path alongside the meta-model path.
    persist_mids = {args[0] for k, args in fake_db.update_calls if k == "persist"}
    assert persist_mids <= {"KX-NHL-B"}
