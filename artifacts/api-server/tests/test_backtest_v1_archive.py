"""Tests for the V1 backtest harness."""
from __future__ import annotations

import asyncio
import json
import math
import time
from pathlib import Path

import pytest

from app import calibrators
from scripts import backtest_v1_archive as bt


FIXTURE = Path(__file__).parent / "fixtures" / "v1_backtest_synthetic.json"


@pytest.fixture(autouse=True)
def _clean_calibrator_state():
    calibrators.invalidate()
    yield
    calibrators.invalidate()


# --- pure-function metrics + side alignment ---------------------------------


@pytest.mark.parametrize("side,expected", [
    ("yes", 0.7), ("YES", 0.7), ("buy", 0.7),
    ("live_yes", 0.7), ("buy yes", 0.7), ("Buy Yes", 0.7),
    ("no", 0.3), ("NO", 0.3), ("sell", 0.3),
    ("live_no", 0.3), ("buy no", 0.3), ("  Sell  ", 0.3),
])
def test_side_align_canonical_aliases(side, expected):
    assert bt._side_align(0.7, side) == pytest.approx(expected)


@pytest.mark.parametrize("side", ["pass", None, ""])
def test_side_align_drops_unknown(side):
    assert bt._side_align(0.7, side) is None


def test_side_align_clamps_out_of_range():
    assert bt._side_align(1.4, "yes") == 1.0
    assert bt._side_align(-0.2, "yes") == 0.0
    assert bt._side_align(1.4, "no") == 0.0


def test_brier_log_loss_auc_basics():
    probs = [0.99, 0.99, 0.01, 0.01]
    ys = [1, 1, 0, 0]
    assert bt._brier(probs, ys) == pytest.approx(0.0001, abs=1e-4)
    assert bt._log_loss(probs, ys) < 0.05
    assert bt._auc(probs, ys) == 1.0
    assert bt._brier([], []) is None
    assert bt._log_loss([], []) is None
    assert bt._auc([], []) is None
    # Single-class outcomes => AUC undefined, Brier defined.
    assert bt._auc([0.4, 0.6], [1, 1]) is None
    assert bt._brier([0.4, 0.6], [1, 1]) == pytest.approx((0.36 + 0.16) / 2)


def test_log_loss_clamps_degenerate_zero_one():
    """V1 sometimes emitted exactly 0/1 — log_loss must not return -inf."""
    val = bt._log_loss([0.0, 1.0], [0, 1])
    assert val is not None and val < 1e-6
    val = bt._log_loss([0.0, 1.0], [1, 0])
    assert val is not None and 25 < val < 35


# --- normalization ----------------------------------------------------------


def test_normalize_rows_drops_unknown_side_and_missing():
    rows = [
        {"league": "NBA", "predicted_prob": 0.6, "actual_outcome": 1, "side": "yes"},
        {"league": "NBA", "predicted_prob": 0.6, "actual_outcome": 1, "side": "pass"},
        {"league": "NBA", "predicted_prob": None, "actual_outcome": 1, "side": "yes"},
        {"league": "NBA", "predicted_prob": 0.6, "actual_outcome": None, "side": "yes"},
        {"league": "", "predicted_prob": 0.6, "actual_outcome": 1, "side": "yes"},
        # analysis_json fallback for side, both dict and string forms.
        {"league": "NFL", "predicted_prob": 0.7, "actual_outcome": 0,
         "analysis_json": {"side": "no"}},
        {"league": "NFL", "predicted_prob": 0.7, "actual_outcome": 0,
         "analysis_json": '{"side":"buy"}'},
    ]
    grouped = bt._normalize_rows(rows)
    assert set(grouped) == {"NBA", "NFL"}
    assert len(grouped["NBA"]["probs"]) == 1
    assert grouped["NFL"]["probs"] == pytest.approx([0.3, 0.7])
    assert grouped["NFL"]["ys"] == [0, 0]


def test_normalize_rows_respects_league_filter():
    rows = [
        {"league": "NBA", "predicted_prob": 0.6, "actual_outcome": 1, "side": "yes"},
        {"league": "NFL", "predicted_prob": 0.6, "actual_outcome": 1, "side": "yes"},
    ]
    assert list(bt._normalize_rows(rows, leagues_filter={"NBA"})) == ["NBA"]


def test_normalize_rows_drops_malformed_analysis_json():
    """Malformed analysis_json string must not crash; row falls through to drop."""
    rows = [
        {"league": "NBA", "predicted_prob": 0.6, "actual_outcome": 1,
         "analysis_json": "{not valid json"},
        {"league": "NBA", "predicted_prob": 0.6, "actual_outcome": 1,
         "side": "yes", "analysis_json": "{still garbage"},
    ]
    grouped = bt._normalize_rows(rows)
    assert "NBA" in grouped
    assert grouped["NBA"]["probs"] == pytest.approx([0.6])


def test_normalize_into_matches_normalize_rows():
    rows = [
        {"league": "NBA", "predicted_prob": 0.7, "actual_outcome": 1,
         "analysis_json": {"side": "yes"}},
        {"league": "nba", "predicted_prob": 0.3, "actual_outcome": 0,
         "analysis_json": {"side": "no"}},
        {"league": "NFL", "predicted_prob": 0.55, "actual_outcome": 1,
         "analysis_json": '{"side": "yes"}'},
        {"league": "MLB", "predicted_prob": None, "actual_outcome": 1,
         "analysis_json": {"side": "yes"}},
    ]
    one_shot = bt._normalize_rows(rows, leagues_filter=None)
    streamed: dict[str, dict[str, list]] = {}
    appended = bt._normalize_into(streamed, rows, leagues_filter=None)
    assert appended == sum(len(v["probs"]) for v in one_shot.values())
    assert streamed == one_shot


# --- score_grouped ----------------------------------------------------------


def test_score_grouped_noop_calibrator_yields_zero_delta():
    """Cold-install behaviour: no calibrator loaded => raw == calibrated."""
    rows = json.loads(FIXTURE.read_text())
    scored = bt.score_grouped(bt._normalize_rows(rows))
    assert set(scored["per_league"]) == {"NBA", "NFL"}
    for m in scored["per_league"].values():
        assert m["calibrator_applied_count"] == 0
        assert m["brier_v1_raw"] == pytest.approx(m["brier_calibrated"])
        assert m["brier_delta"] == pytest.approx(0.0)
        assert m["delta_vs_v1"] == m["brier_delta"]
        assert m["sample_count"] == 5
        assert m["verdict"] == "parity"
    assert scored["summary"]["row_count"] == 10
    assert sorted(scored["summary"]["leagues_parity"]) == ["NBA", "NFL"]
    assert sorted(scored["summary"]["leagues_within_tolerance"]) == ["NBA", "NFL"]


def test_score_grouped_calibrator_changes_delta():
    calibrators._state.calibrators["NBA"] = calibrators._Calibrator(
        league="NBA", type="isotonic",
        state={"x": [0.0, 1.0], "y": [0.4, 0.6]},
        fit_at=time.time(), sample_count=999,
    )
    rows = json.loads(FIXTURE.read_text())
    scored = bt.score_grouped(bt._normalize_rows(rows))
    nba = scored["per_league"]["NBA"]
    nfl = scored["per_league"]["NFL"]
    assert nba["calibrator_applied_count"] == nba["sample_count"]
    assert nba["calibrator_type"] == "isotonic"
    assert nba["brier_calibrated"] != pytest.approx(nba["brier_v1_raw"])
    assert nfl["calibrator_applied_count"] == 0
    assert nfl["brier_delta"] == pytest.approx(0.0)


def test_verdict_boundary_classification():
    """|delta| == tolerance => parity (inclusive); above => improved/regressed."""
    tol = bt._BRIER_TOLERANCE
    L_PARITY, L_IMPROVE, L_REGRESS = "UFC", "ATP", "F1"
    for lg in (L_PARITY, L_IMPROVE, L_REGRESS):
        assert lg in calibrators.SUPPORTED_LEAGUES, lg
    grouped = {
        L_PARITY:  {"probs": [0.5, 0.5], "ys": [0, 1]},   # raw 0.25
        L_REGRESS: {"probs": [0.0, 1.0], "ys": [0, 1]},   # raw 0.0
        L_IMPROVE: {"probs": [1.0, 0.0], "ys": [0, 1]},   # raw 1.0
    }
    # Solve q so calibrated Brier on parity league = raw + tol exactly.
    q = (1 - math.sqrt(1 - 4 * 0.23)) / 2
    assert ((q - 0) ** 2 + (q - 1) ** 2) / 2 == pytest.approx(0.25 + tol, abs=1e-9)
    calibrators._state.calibrators[L_PARITY] = calibrators._Calibrator(
        league=L_PARITY, type="isotonic",
        state={"x": [0.0, 1.0], "y": [q, q]},
        fit_at=time.time(), sample_count=999,
    )
    calibrators._state.calibrators[L_IMPROVE] = calibrators._Calibrator(
        league=L_IMPROVE, type="isotonic",
        state={"x": [0.0, 1.0], "y": [1.0, 0.0]},
        fit_at=time.time(), sample_count=999,
    )
    calibrators._state.calibrators[L_REGRESS] = calibrators._Calibrator(
        league=L_REGRESS, type="isotonic",
        state={"x": [0.0, 1.0], "y": [0.5, 0.5]},
        fit_at=time.time(), sample_count=999,
    )
    scored = bt.score_grouped(grouped)
    assert scored["per_league"][L_PARITY]["verdict"] == "parity"
    assert scored["per_league"][L_IMPROVE]["verdict"] == "improved"
    assert scored["per_league"][L_REGRESS]["verdict"] == "regressed"
    assert sorted(scored["summary"]["leagues_outside_tolerance"]) == sorted(
        [L_IMPROVE, L_REGRESS]
    )


def test_score_grouped_apply_calibrator_false_skips_calibration():
    """apply_calibrator=False bypasses calibrators.apply() entirely.

    Regression guard: an earlier full-stack draft re-applied the
    calibrator on top of probabilities predict() had already calibrated.
    """
    apply_calls: list[tuple[float, str | None]] = []

    def _spy_apply(p, league):
        apply_calls.append((p, league))
        return (1.0 - p, {"applied": True, "type": "spy"})

    grouped = {"NBA": {"probs": [0.7, 0.3, 0.6], "ys": [1, 0, 1]}}
    original = bt.calibrators.apply
    bt.calibrators.apply = _spy_apply  # type: ignore[assignment]
    try:
        scored = bt.score_grouped(grouped, apply_calibrator=False)
    finally:
        bt.calibrators.apply = original  # type: ignore[assignment]
    assert apply_calls == []
    nba = scored["per_league"]["NBA"]
    assert nba["calibrator_applied_count"] == 0
    assert nba["calibrator_type"] is None
    assert nba["brier_v1_raw"] == pytest.approx(nba["brier_calibrated"])
    assert nba["brier_delta"] == pytest.approx(0.0, abs=1e-12)
    assert nba["delta_vs_v1"] == pytest.approx(0.0, abs=1e-12)


# --- run() end-to-end -------------------------------------------------------


def test_run_end_to_end_against_fixture():
    rows = json.loads(FIXTURE.read_text())
    report = asyncio.run(bt.run(
        rows=rows, limit=None, leagues_filter=None, load_calibrators=False,
    ))
    for key in (
        "started_at", "completed_at", "source", "row_input_count",
        "row_kept_count", "leagues_filter", "limit", "per_league",
        "summary", "parity_gate_status",
    ):
        assert key in report, f"missing top-level key {key}"
    assert report["source"] == "in-memory"
    assert report["row_input_count"] == 10
    assert report["row_kept_count"] == 10
    for league, m in report["per_league"].items():
        for key in (
            "sample_count", "calibrator_applied_count", "calibrator_type",
            "brier_v1_raw", "brier_calibrated", "brier_delta", "delta_vs_v1",
            "log_loss_v1_raw", "log_loss_calibrated",
            "auc_v1_raw", "auc_calibrated",
            "within_brier_tolerance", "verdict",
        ):
            assert key in m, f"{league}: missing {key}"
    s = report["summary"]
    assert s["league_count"] == 2
    assert s["row_count"] == 10
    assert s["brier_tolerance"] == bt._BRIER_TOLERANCE


def test_run_with_league_filter():
    rows = json.loads(FIXTURE.read_text())
    report = asyncio.run(bt.run(
        rows=rows, limit=None, leagues_filter={"NBA"}, load_calibrators=False,
    ))
    assert list(report["per_league"]) == ["NBA"]
    assert report["row_kept_count"] == 5
    assert report["leagues_filter"] == ["NBA"]


def test_run_streams_in_batches_and_reports_batch_count():
    """Batching is observationally invariant; batch_count surfaces chunk count."""
    rows = [
        {"league": "NBA", "predicted_prob": 0.6, "actual_outcome": 1, "side": "yes"},
        {"league": "NBA", "predicted_prob": 0.4, "actual_outcome": 0, "side": "yes"},
        {"league": "NFL", "predicted_prob": 0.55, "actual_outcome": 1, "side": "yes"},
        {"league": "MLB", "predicted_prob": 0.5, "actual_outcome": 0, "side": "yes"},
        {"league": "MLB", "predicted_prob": 0.7, "actual_outcome": 1, "side": "yes"},
    ]
    big = asyncio.run(bt.run(rows=rows, limit=None, leagues_filter=None,
                             load_calibrators=False, batch_size=1000))
    small = asyncio.run(bt.run(rows=rows, limit=None, leagues_filter=None,
                               load_calibrators=False, batch_size=2))
    assert big["batch_count"] == 1
    assert small["batch_count"] == 3  # ceil(5/2)
    assert big["row_kept_count"] == small["row_kept_count"] == 5
    for lg in big["per_league"]:
        assert big["per_league"][lg]["brier_v1_raw"] == pytest.approx(
            small["per_league"][lg]["brier_v1_raw"], abs=1e-12
        )


# --- feature extraction + surrogate -----------------------------------------


def test_extract_feature_vector_handles_archive_shapes():
    assert bt._extract_feature_vector({"analysis_json": {"side": "yes"}}) is None
    assert bt._extract_feature_vector({"analysis_json": '{"side":"yes"}'}) is None
    assert bt._extract_feature_vector({"analysis_json": "not-json"}) is None
    assert bt._extract_feature_vector({"league": "NBA"}) is None
    assert bt._extract_feature_vector(
        {"analysis_json": {"feature_vector": [0.1, 0.2, 0.3]}}
    ) == [0.1, 0.2, 0.3]
    assert bt._extract_feature_vector(
        {"analysis_json": {"features": [1, 2, 3]}}
    ) == [1.0, 2.0, 3.0]
    assert bt._extract_feature_vector(
        {"analysis_json": {"feature_vector": ["not", "a", "number"]}}
    ) is None


def test_archive_to_candidate_maps_known_fields_and_returns_kelly_override():
    """Pin the V1-archive -> live-candidate mapping table."""
    row = {
        "league": "nba", "predicted_prob": 0.62, "market_type": "moneyline",
        "analysis_json": {
            "side": "yes", "market_implied_prob": 0.55,
            "signal_strength": 0.18, "edge_at_entry": 0.07,
            "kelly_used": 0.025,
        },
    }
    candidate, kelly_override = bt._archive_to_candidate(row)
    assert candidate["consensus_probability"] == pytest.approx(0.62)
    assert candidate["market_prob"] == pytest.approx(0.55)
    assert candidate["signal_strength"] == pytest.approx(0.18)
    assert candidate["edge"] == pytest.approx(0.07)
    assert candidate["side"] == "yes"
    assert candidate["league"] == "NBA"
    assert candidate["market_type"] == "moneyline"
    assert kelly_override == pytest.approx(0.025)
    # None-valued mappings dropped so build_vector falls back to defaults.
    sparse_candidate, sparse_kelly = bt._archive_to_candidate(
        {"league": "NBA", "predicted_prob": 0.5, "analysis_json": {"side": "no"}}
    )
    assert "market_prob" not in sparse_candidate
    assert "signal_strength" not in sparse_candidate
    assert sparse_kelly is None


@pytest.mark.parametrize("raw_side,expected", [
    ("yes", "yes"), ("YES", "yes"), ("buy", "yes"),
    ("live_yes", "yes"), ("buy yes", "yes"), ("Buy Yes", "yes"),
    ("no", "no"), ("NO", "no"), ("sell", "no"),
    ("live_no", "no"), ("buy no", "no"), ("  Sell  ", "no"),
])
def test_archive_to_candidate_canonicalizes_side_aliases(raw_side, expected):
    """V1 archive uses several side aliases; surrogate must canonicalize before
    handing to build_vector — _resolve_side() only treats literal 'NO' as no."""
    candidate, _kelly = bt._archive_to_candidate(
        {"league": "NBA", "predicted_prob": 0.55,
         "analysis_json": {"side": raw_side}}
    )
    assert candidate["side"] == expected


@pytest.mark.parametrize("row", [
    {"league": "NBA", "analysis_json": {"side": "yes"}},                       # no prob
    {"league": "NBA", "predicted_prob": 0.5, "analysis_json": {}},             # no side
    {"league": "NBA", "predicted_prob": "n/a", "analysis_json": {"side": "yes"}},  # bad prob
    {"league": "NBA", "predicted_prob": 0.55, "analysis_json": {"side": "maybe"}}, # unknown side
    {"league": "NBA", "predicted_prob": 0.55, "analysis_json": {"side": ""}},
])
def test_archive_to_candidate_rejects_invalid_rows(row):
    assert bt._archive_to_candidate(row) is None


def test_build_surrogate_vector_against_real_build_vector():
    """Direct integration with app.features.build_vector — side alias routes correctly."""
    from app.features.builders import _SPECS

    vec = bt._build_surrogate_vector({
        "league": "NBA", "predicted_prob": 0.62, "market_type": "moneyline",
        "analysis_json": {"side": "sell", "market_implied_prob": 0.55,
                          "edge_at_entry": 0.07, "signal_strength": 0.18},
    })
    assert vec is not None
    assert len(vec) == len(_SPECS)
    side_idx = next(i for i, s in enumerate(_SPECS) if s.name == "side_val")
    assert vec[side_idx] == 0.0  # 'sell' -> 'no'

    vec_yes = bt._build_surrogate_vector(
        {"league": "NBA", "predicted_prob": 0.62,
         "analysis_json": {"side": "buy yes"}}
    )
    assert vec_yes is not None
    assert vec_yes[side_idx] == 1.0


# --- full-stack inference ---------------------------------------------------


class _StubPrediction:
    def __init__(self, p: float) -> None:
        self.probability = p


def test_full_stack_attempt_skips_when_features_missing():
    """V1 archive rows have no features => 0 predicted, all skipped."""
    rows = [
        {"league": "NBA", "predicted_prob": 0.6, "actual_outcome": 1,
         "analysis_json": {"side": "yes"}},
        {"league": "NFL", "predicted_prob": 0.4, "actual_outcome": 0,
         "analysis_json": {"side": "no"}},
        {"league": "MLB", "predicted_prob": 0.55, "actual_outcome": 1,
         "analysis_json": {"side": "buy"}},
    ]

    async def _never_called(*args, **kwargs):  # pragma: no cover
        raise AssertionError("predictor must not be called when features absent")

    grouped, summary = asyncio.run(
        bt._attempt_full_stack_inference(rows, leagues_filter=None,
                                         predict_fn=_never_called)
    )
    assert grouped == {}
    assert summary["rows_attempted"] == 0
    assert summary["rows_predicted"] == 0
    assert summary["rows_skipped"] == 3
    assert summary["skip_reasons"]["feature_vector_unavailable"] == 3


def test_full_stack_attempt_invokes_predictor_when_features_present():
    """End-to-end: predictor stub feeds score_grouped; skips counted by reason."""
    seen_calls: list[tuple[list[float], str]] = []

    async def _stub_predict(features, *, league):
        seen_calls.append((list(features), league))
        m = sum(features) / max(1, len(features))
        return _StubPrediction(max(0.0, min(1.0, m)))

    rows = [
        {"league": "NBA", "actual_outcome": 1,
         "analysis_json": {"feature_vector": [0.7, 0.8, 0.9]}},
        {"league": "NBA", "actual_outcome": 0,
         "analysis_json": {"feature_vector": [0.1, 0.2, 0.3]}},
        {"league": "NFL", "actual_outcome": 1,
         "analysis_json": {"feature_vector": [0.6, 0.6, 0.6]}},
        {"league": "MLB", "actual_outcome": 1, "analysis_json": {"side": "yes"}},
        {"league": "NHL", "actual_outcome": "tied",
         "analysis_json": {"feature_vector": [0.5]}},
    ]
    grouped, summary = asyncio.run(
        bt._attempt_full_stack_inference(rows, leagues_filter=None,
                                         predict_fn=_stub_predict)
    )
    assert len(seen_calls) == 3
    assert summary["rows_attempted"] == 3
    assert summary["rows_predicted"] == 3
    assert summary["rows_skipped"] == 2
    assert summary["skip_reasons"]["feature_vector_unavailable"] == 1
    assert summary["skip_reasons"]["non_binary_outcome"] == 1
    scored = bt.score_grouped(grouped)
    assert set(scored["per_league"]) == {"NBA", "NFL"}


def test_full_stack_attempt_uses_surrogate_when_features_missing():
    """surrogate_features=True routes feature-less rows through the builder."""
    seen_calls: list[tuple[list[float], str]] = []

    async def _stub_predict(features, *, league):
        seen_calls.append((list(features), league))
        m = sum(features) / max(1, len(features))
        return _StubPrediction(max(0.0, min(1.0, m)))

    def _stub_builder(row):
        return [float(row.get("predicted_prob") or 0.5)] * 32

    rows = [
        {"league": "NBA", "actual_outcome": 1, "predicted_prob": 0.7,
         "analysis_json": {"side": "yes"}},
        {"league": "NBA", "actual_outcome": 0, "predicted_prob": 0.3,
         "analysis_json": {"side": "yes"}},
        {"league": "NFL", "actual_outcome": 1,
         "analysis_json": {"feature_vector": [0.6, 0.6, 0.6]}},
    ]
    grouped, summary = asyncio.run(
        bt._attempt_full_stack_inference(
            rows, leagues_filter=None, predict_fn=_stub_predict,
            surrogate_features=True, feature_builder=_stub_builder,
        )
    )
    assert len(seen_calls) == 3
    assert summary["rows_predicted"] == 3
    assert summary["rows_skipped"] == 0
    assert summary["feature_source"]["archived"] == 1
    assert summary["feature_source"]["surrogate"] == 2
    scored = bt.score_grouped(grouped)
    assert scored["per_league"]["NBA"]["sample_count"] == 2
    assert scored["per_league"]["NFL"]["sample_count"] == 1


def test_run_full_stack_emits_triage_and_surrogate_counts():
    """Top-level run() with full_stack=True surfaces triage + feature_source."""
    async def _stub_predict(features, *, league):
        return _StubPrediction(0.55)

    def _stub_builder(row):
        return [0.55] * 32

    rows = [
        {"league": "NBA", "actual_outcome": 1, "predicted_prob": 0.6,
         "analysis_json": {"side": "yes"}},
        {"league": "NBA", "actual_outcome": 0, "predicted_prob": 0.4,
         "analysis_json": {"side": "yes"}},
    ]
    report = asyncio.run(
        bt.run(rows=rows, limit=None, leagues_filter=None,
               load_calibrators=False, full_stack=True,
               predict_fn=_stub_predict, surrogate_features=True,
               feature_builder=_stub_builder)
    )
    fs = report["full_stack_attempt"]
    assert fs["surrogate_features_enabled"] is True
    assert fs["rows_predicted"] == 2
    assert fs["feature_source"]["surrogate"] == 2
    assert fs["feature_source"]["archived"] == 0
    assert "full_stack_scored" in report
    assert report["full_stack_scored"]["per_league"]["NBA"]["sample_count"] == 2
    triage = report["triage"]
    assert triage["feature_builder_mismatch"]["active"] is False
    assert triage["feature_builder_mismatch"]["evidence"][
        "rows_predicted_via_models_loader"] == 2


def test_run_full_stack_triage_active_when_no_features():
    """Synthetic fixture has no features => feature_builder_mismatch ACTIVE."""
    rows = json.loads(FIXTURE.read_text())

    async def _never_called(*args, **kwargs):  # pragma: no cover
        raise AssertionError("synthetic fixture has no features => no predict")

    report = asyncio.run(
        bt.run(rows=rows, limit=None, leagues_filter=None,
               load_calibrators=False, full_stack=True,
               predict_fn=_never_called)
    )
    assert report["row_kept_count"] > 0
    fs = report["full_stack_attempt"]
    assert fs["rows_predicted"] == 0
    assert fs["skip_reasons"].get("feature_vector_unavailable", 0) > 0
    triage = report["triage"]
    assert triage["feature_builder_mismatch"]["active"] is True
    assert triage["calibrator_misalignment"]["active"] is True
    assert triage["artifact_drift"]["active"] is False


def test_run_full_stack_bypasses_double_calibration_end_to_end():
    """full_stack_scored MUST be free of double-calibration even when a
    fitted calibrator is active on the calibrator-only path."""
    async def _stub_predict(features, *, league):
        return _StubPrediction(0.7)

    spy_calls: list[tuple[float, str | None]] = []

    def _spy_apply(p, league):
        spy_calls.append((p, league))
        return (1.0 - p, {"applied": True, "type": "spy"})

    rows = [
        {"league": "NBA", "predicted_prob": 0.7, "actual_outcome": 1,
         "analysis_json": {"side": "yes", "feature_vector": [0.8, 0.8, 0.8]}},
        {"league": "NBA", "predicted_prob": 0.4, "actual_outcome": 0,
         "analysis_json": {"side": "yes", "feature_vector": [0.2, 0.2, 0.2]}},
        {"league": "NBA", "predicted_prob": 0.6, "actual_outcome": 1,
         "analysis_json": {"side": "yes", "feature_vector": [0.6, 0.6, 0.6]}},
    ]
    original = bt.calibrators.apply
    bt.calibrators.apply = _spy_apply  # type: ignore[assignment]
    try:
        report = asyncio.run(
            bt.run(rows=rows, limit=None, leagues_filter=None,
                   load_calibrators=False, full_stack=True,
                   predict_fn=_stub_predict)
        )
    finally:
        bt.calibrators.apply = original  # type: ignore[assignment]

    nba_cal = report["per_league"]["NBA"]
    assert nba_cal["calibrator_applied_count"] == 3
    assert nba_cal["brier_delta"] != pytest.approx(0.0, abs=1e-9)

    nba_fs = report["full_stack_scored"]["per_league"]["NBA"]
    assert nba_fs["calibrator_applied_count"] == 0
    assert nba_fs["calibrator_type"] is None
    assert nba_fs["brier_delta"] == pytest.approx(0.0, abs=1e-12)
    expected = ((0.7 - 1) ** 2 + (0.7 - 0) ** 2 + (0.7 - 1) ** 2) / 3
    assert nba_fs["brier_calibrated"] == pytest.approx(expected, abs=1e-9)
    # Spy invoked only on the calibrator-only path (3 NBA rows). 6 would
    # mean the full-stack path also re-calibrated.
    assert len(spy_calls) == 3


# --- triage + parity_gate_status -------------------------------------------


def test_build_triage_note_reflects_surrogate_mode_when_active():
    scored = {"summary": {"leagues_improved": [], "leagues_parity": [],
                          "leagues_regressed": []}}
    fs_on = {"rows_attempted": 100, "rows_predicted": 100, "skip_reasons": {},
             "feature_source": {"surrogate": 100, "archived": 0},
             "surrogate_features_enabled": True}
    note_on = bt._build_triage(scored, fs_on)["feature_builder_mismatch"]["note"]
    assert "surrogate" in note_on.lower() and "degraded" in note_on.lower()

    fs_off = {"rows_attempted": 100, "rows_predicted": 0,
              "skip_reasons": {"feature_vector_unavailable": 100}}
    triage_off = bt._build_triage(scored, fs_off)
    assert "zero usable rows" in triage_off["feature_builder_mismatch"]["note"].lower()
    assert triage_off["feature_builder_mismatch"]["active"] is True


def test_build_parity_gate_status_emits_blocked_and_evaluable_buckets():
    """Pin the gate names, statuses, and overall verdict rollup."""
    scored = {"summary": {"leagues_improved": ["NBA"], "leagues_parity": [],
                          "leagues_regressed": ["MLB"]}}
    fs = {"rows_attempted": 1000, "rows_predicted": 1000,
          "skip_reasons": {"feature_vector_unavailable": 0},
          "feature_source": {"surrogate": 1000, "archived": 0},
          "surrogate_features_enabled": True}
    pg = bt._build_parity_gate_status(scored, fs)
    assert set(pg["gates"]) == {
        "full_stack_replay_via_archived_features",
        "full_stack_replay_via_surrogate_features",
        "parity_within_2pct_vs_v1_calibrated",
    }
    assert pg["gates"]["full_stack_replay_via_archived_features"]["status"] == "BLOCKED"
    assert "#731" in pg["gates"]["full_stack_replay_via_archived_features"]["blocked_by"]
    surr = pg["gates"]["full_stack_replay_via_surrogate_features"]
    assert surr["status"] == "EVALUABLE_DEGRADED"
    assert surr["evidence"]["rows_predicted_via_surrogate"] == 1000
    assert pg["gates"]["parity_within_2pct_vs_v1_calibrated"]["status"] == "UNEVALUABLE"
    assert pg["summary"] == {
        "total_gates": 3, "blocked_or_unevaluable": 2,
        "evaluable_degraded": 1, "pass": 0,
    }
    assert pg["overall"] == "DEGRADED_PARTIAL_PENDING_BACKFILL"
    assert "#731" in pg["overall_blocked_by"]


def test_build_parity_gate_status_marks_surrogate_not_run_when_disabled():
    scored = {"summary": {"leagues_improved": [], "leagues_parity": [],
                          "leagues_regressed": []}}
    fs = {"rows_attempted": 1000, "rows_predicted": 0,
          "skip_reasons": {"feature_vector_unavailable": 1000}}
    pg = bt._build_parity_gate_status(scored, fs)
    assert pg["gates"]["full_stack_replay_via_surrogate_features"]["status"] == "NOT_RUN"
    assert pg["gates"]["full_stack_replay_via_archived_features"]["status"] == "BLOCKED"
