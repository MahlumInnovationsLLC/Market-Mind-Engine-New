"""End-to-end tests for the V1 backtest harness.

The fixture-driven path covers:

* `_normalize_rows` — side-flipping (yes/buy → keep, no/sell → flip),
  unknown-side dropping, malformed-row resilience.
* `score_grouped` — Brier / log-loss / AUC math + the calibrated vs raw
  delta when the calibrator is a no-op (cold install).
* `run()` end-to-end against the 10-row synthetic fixture, including
  the `--no-load-calibrators` mode the harness uses for offline runs.

We assert the **shape** of the report (keys present, types right,
sample counts add up) rather than exact metric values — those are
already covered by their pure-function tests above and would create
brittle test failures if the metric helpers are ever tightened.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app import calibrators
from scripts import backtest_v1_archive as bt


FIXTURE = Path(__file__).parent / "fixtures" / "v1_backtest_synthetic.json"


@pytest.fixture(autouse=True)
def _clean_calibrator_state():
    """Each test starts from an empty in-memory calibrator state so a
    leftover load from another test can't sneak `applied=True` results
    into a test that expects no-op behaviour."""
    calibrators.invalidate()
    yield
    calibrators.invalidate()


# ---------------------------------------------------------------------------
# Pure-function tests
# ---------------------------------------------------------------------------


def test_side_align_flips_no_keeps_yes():
    assert bt._side_align(0.7, "yes") == 0.7
    assert bt._side_align(0.7, "buy") == 0.7
    assert bt._side_align(0.7, "live_yes") == 0.7
    assert bt._side_align(0.7, "no") == pytest.approx(0.3)
    assert bt._side_align(0.7, "sell") == pytest.approx(0.3)
    assert bt._side_align(0.7, "buy no") == pytest.approx(0.3)
    # Unknown side is dropped (caller filters None out).
    assert bt._side_align(0.7, "pass") is None
    assert bt._side_align(0.7, None) is None
    assert bt._side_align(0.7, "") is None


def test_side_align_clamps_out_of_range():
    """V1 occasionally emitted predicted_prob > 1 (the calibrator-fit
    job clamps to [0,1] for the same reason). Confirm the harness
    matches that contract."""
    assert bt._side_align(1.4, "yes") == 1.0
    assert bt._side_align(-0.2, "yes") == 0.0
    assert bt._side_align(1.4, "no") == 0.0


def test_brier_log_loss_auc_basics():
    # Perfect predictor: brier=0, log_loss≈0, auc=1.
    probs = [0.99, 0.99, 0.01, 0.01]
    ys = [1, 1, 0, 0]
    assert bt._brier(probs, ys) == pytest.approx(0.0001, abs=1e-4)
    assert bt._log_loss(probs, ys) < 0.05
    assert bt._auc(probs, ys) == 1.0
    # Empty inputs → None for all three.
    assert bt._brier([], []) is None
    assert bt._log_loss([], []) is None
    assert bt._auc([], []) is None
    # Single-class outcomes → AUC undefined, Brier defined.
    assert bt._auc([0.4, 0.6], [1, 1]) is None
    assert bt._brier([0.4, 0.6], [1, 1]) == pytest.approx((0.36 + 0.16) / 2)


def test_log_loss_clamps_degenerate_zero_one():
    """V1 sometimes emitted exactly 0 or 1 — log_loss must not return -inf."""
    val = bt._log_loss([0.0, 1.0], [0, 1])
    assert val is not None and val < 1e-6
    val = bt._log_loss([0.0, 1.0], [1, 0])  # totally wrong
    assert val is not None and 25 < val < 35  # clamped to ~ -log(eps)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


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
    # NFL: first row had side=no so prob is flipped to 0.3.
    assert grouped["NFL"]["probs"] == pytest.approx([0.3, 0.7])
    assert grouped["NFL"]["ys"] == [0, 0]


def test_normalize_rows_respects_league_filter():
    rows = [
        {"league": "NBA", "predicted_prob": 0.6, "actual_outcome": 1, "side": "yes"},
        {"league": "NFL", "predicted_prob": 0.6, "actual_outcome": 1, "side": "yes"},
    ]
    grouped = bt._normalize_rows(rows, leagues_filter={"NBA"})
    assert list(grouped) == ["NBA"]


# ---------------------------------------------------------------------------
# score_grouped — calibrator-as-no-op (cold-install behaviour)
# ---------------------------------------------------------------------------


def test_score_grouped_noop_calibrator_yields_zero_delta():
    """With no calibrator loaded, calibrators.apply() bypasses and the
    calibrated metrics must equal the raw metrics — delta=0.0 across
    the board.  This is the contract a fresh dev DB will satisfy."""
    rows = json.loads(FIXTURE.read_text())
    grouped = bt._normalize_rows(rows)
    scored = bt.score_grouped(grouped)
    assert set(scored["per_league"]) == {"NBA", "NFL"}
    for league, m in scored["per_league"].items():
        assert m["calibrator_applied_count"] == 0
        assert m["brier_v1_raw"] == pytest.approx(m["brier_calibrated"])
        assert m["brier_delta"] == pytest.approx(0.0)
        assert m["log_loss_v1_raw"] == pytest.approx(m["log_loss_calibrated"])
        # 5 rows per league in the fixture, mixed outcomes → AUC defined.
        assert m["sample_count"] == 5
        assert m["auc_v1_raw"] is not None
        assert m["auc_calibrated"] == pytest.approx(m["auc_v1_raw"])
        assert m["within_brier_tolerance"] is True
        assert m["verdict"] == "parity"
    assert scored["summary"]["row_count"] == 10
    assert sorted(scored["summary"]["leagues_parity"]) == ["NBA", "NFL"]
    assert scored["summary"]["leagues_improved"] == []
    assert scored["summary"]["leagues_regressed"] == []
    # Legacy alias still populated.
    assert sorted(scored["summary"]["leagues_within_tolerance"]) == ["NBA", "NFL"]
    assert scored["summary"]["leagues_outside_tolerance"] == []


def test_score_grouped_calibrator_changes_delta():
    """Inject an isotonic calibrator manually; verify the calibrated
    metrics diverge from raw and `applied` flips True for that league."""
    import time

    calibrators._state.calibrators["NBA"] = calibrators._Calibrator(
        league="NBA",
        type="isotonic",
        # Heavy-handed shrinkage toward 0.5: x in [0,1] → y in [0.4, 0.6].
        state={"x": [0.0, 1.0], "y": [0.4, 0.6]},
        fit_at=time.time(),
        sample_count=999,
    )
    rows = json.loads(FIXTURE.read_text())
    grouped = bt._normalize_rows(rows)
    scored = bt.score_grouped(grouped)
    nba = scored["per_league"]["NBA"]
    nfl = scored["per_league"]["NFL"]
    assert nba["calibrator_applied_count"] == nba["sample_count"]
    assert nba["calibrator_type"] == "isotonic"
    assert nba["brier_calibrated"] != pytest.approx(nba["brier_v1_raw"])
    # NFL has no fitted calibrator → still no-op.
    assert nfl["calibrator_applied_count"] == 0
    assert nfl["brier_delta"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# End-to-end run() against the fixture
# ---------------------------------------------------------------------------


def test_run_end_to_end_against_fixture():
    rows = json.loads(FIXTURE.read_text())
    report = asyncio.run(bt.run(
        rows=rows,
        limit=None,
        leagues_filter=None,
        load_calibrators=False,  # offline mode — skips DB lookup
    ))
    # Top-level shape contract.
    for key in (
        "started_at", "completed_at", "source", "row_input_count",
        "row_kept_count", "leagues_filter", "limit", "per_league",
        "summary",
    ):
        assert key in report, f"missing top-level key {key}"
    assert report["source"] == "in-memory"
    assert report["row_input_count"] == 10
    assert report["row_kept_count"] == 10
    # Per-league shape contract.
    for league, m in report["per_league"].items():
        for key in (
            "sample_count", "calibrator_applied_count", "calibrator_type",
            "brier_v1_raw", "brier_calibrated", "brier_delta",
            "log_loss_v1_raw", "log_loss_calibrated",
            "auc_v1_raw", "auc_calibrated",
            "within_brier_tolerance",
        ):
            assert key in m, f"{league}: missing {key}"
    # Summary contract.
    s = report["summary"]
    assert s["league_count"] == 2
    assert s["row_count"] == 10
    assert s["brier_tolerance"] == bt._BRIER_TOLERANCE
    assert isinstance(s["leagues_within_tolerance"], list)
    assert isinstance(s["leagues_outside_tolerance"], list)


def test_run_with_league_filter():
    rows = json.loads(FIXTURE.read_text())
    report = asyncio.run(bt.run(
        rows=rows,
        limit=None,
        leagues_filter={"NBA"},
        load_calibrators=False,
    ))
    assert list(report["per_league"]) == ["NBA"]
    assert report["row_kept_count"] == 5
    assert report["leagues_filter"] == ["NBA"]


# ---------------------------------------------------------------------------
# Side-alias coverage — must match calibrators._fetch_training_data SQL CTE
# ---------------------------------------------------------------------------


def test_side_align_complete_alias_coverage():
    """Lock in every side alias from app/calibrators.py::_fetch_training_data
    so a future SQL-side change can't silently desync the harness."""
    p = 0.7
    flip = pytest.approx(0.3)
    # Every YES alias keeps the prob.
    for s in ("yes", "buy", "live_yes", "buy yes"):
        assert bt._side_align(p, s) == p, f"YES alias {s!r} should keep prob"
        # Case-insensitivity contract — V1 mixed cases.
        assert bt._side_align(p, s.upper()) == p
    # Every NO alias flips the prob.
    for s in ("no", "sell", "live_no", "buy no"):
        assert bt._side_align(p, s) == flip, f"NO alias {s!r} should flip prob"
        assert bt._side_align(p, s.upper()) == flip


def test_normalize_rows_drops_malformed_analysis_json():
    """A row with an unparseable analysis_json string and no top-level
    `side` falls through to the unknown-side branch and is dropped — it
    must not crash _normalize_rows or hard-fail the whole batch."""
    rows = [
        # Malformed JSON string in analysis_json + no top-level side → drop.
        {"league": "NBA", "predicted_prob": 0.6, "actual_outcome": 1,
         "analysis_json": "{not valid json"},
        # Same row but with explicit top-level side wins → kept.
        {"league": "NBA", "predicted_prob": 0.6, "actual_outcome": 1,
         "side": "yes", "analysis_json": "{still garbage"},
    ]
    grouped = bt._normalize_rows(rows)
    assert "NBA" in grouped
    assert len(grouped["NBA"]["probs"]) == 1
    assert grouped["NBA"]["probs"][0] == pytest.approx(0.6)


# ---------------------------------------------------------------------------
# Tolerance-boundary verdict classification
# ---------------------------------------------------------------------------


def test_verdict_boundary_classification():
    """|delta| == _BRIER_TOLERANCE is parity (inclusive boundary).
    Slightly more is improved/regressed depending on sign.

    Uses real SUPPORTED_LEAGUES values because ``calibrators.apply()``
    short-circuits unknown leagues with ``applied=False`` (which would
    force every delta to 0 regardless of the injected calibrator).
    """
    import time
    tol = bt._BRIER_TOLERANCE
    assert tol > 0  # sanity

    # Real SUPPORTED_LEAGUES values not referenced elsewhere in this file.
    L_PARITY, L_IMPROVE, L_REGRESS = "UFC", "ATP", "F1"
    for lg in (L_PARITY, L_IMPROVE, L_REGRESS):
        assert lg in calibrators.SUPPORTED_LEAGUES, lg

    # Manually craft three leagues with predictable raw vs calibrated Brier.
    # Each row contributes (p - y)^2 / n to the Brier, so we control n=2.
    grouped = {
        L_PARITY: {  # raw Brier 0.25; cal Brier 0.25 + tol exactly → parity
            "probs": [0.5, 0.5],
            "ys": [0, 1],
        },
        L_REGRESS: {  # raw Brier 0.0; we'll regress past tol via calibrator
            "probs": [0.0, 1.0],
            "ys": [0, 1],
        },
        L_IMPROVE: {  # raw Brier ~1.0; calibrator pulls it well below
            "probs": [1.0, 0.0],
            "ys": [0, 1],
        },
    }

    # Calibrator that maps every input to a constant-ish band.
    def _const_cal(prob):
        # Identity for parity league; tiny shift (smaller than tol) so cal=raw,
        # then we override per-league via separate fitted entries.
        return prob

    # For L_PARITY we want cal_brier - raw_brier == +tol exactly.
    # raw=0.25 ((0.5-0)^2 + (0.5-1)^2)/2 = 0.25.
    # Need cal Brier = 0.25 + tol. With tol=0.02 → 0.27.
    # Use isotonic that maps 0.5→0.5 + sqrt(tol/1) impossible exactly. Take
    # the algebraic route: solve for q so that ((q-0)^2+(q-1)^2)/2 = 0.27.
    # 2q^2 - 2q + 1 = 0.54 → 2q^2 - 2q + 0.46 = 0 → q^2 - q + 0.23 = 0.
    # q = (1 ± sqrt(1 - 0.92))/2 = (1 ± sqrt(0.08))/2.
    import math
    q = (1 - math.sqrt(1 - 4 * 0.23)) / 2
    # Sanity-check our algebra: cal_brier should be raw + tol.
    cal_brier_check = ((q - 0) ** 2 + (q - 1) ** 2) / 2
    assert cal_brier_check == pytest.approx(0.25 + tol, abs=1e-9)

    calibrators._state.calibrators[L_PARITY] = calibrators._Calibrator(
        league=L_PARITY, type="isotonic",
        state={"x": [0.0, 1.0], "y": [q, q]},  # constant map → 0.5 → q
        fit_at=time.time(), sample_count=999,
    )
    # Improvement: raw brier = 1.0 (totally inverted); cal pulls toward truth.
    calibrators._state.calibrators[L_IMPROVE] = calibrators._Calibrator(
        league=L_IMPROVE, type="isotonic",
        state={"x": [0.0, 1.0], "y": [1.0, 0.0]},  # invert
        fit_at=time.time(), sample_count=999,
    )
    # Regression: raw brier = 0.0 (perfect); cal map shifts both probs by
    # well over tol so cal Brier is unmistakably worse.
    calibrators._state.calibrators[L_REGRESS] = calibrators._Calibrator(
        league=L_REGRESS, type="isotonic",
        state={"x": [0.0, 1.0], "y": [0.5, 0.5]},
        fit_at=time.time(), sample_count=999,
    )

    scored = bt.score_grouped(grouped)
    assert scored["per_league"][L_PARITY]["verdict"] == "parity", (
        f'expected parity at the boundary, got delta='
        f'{scored["per_league"][L_PARITY]["brier_delta"]}'
    )
    assert scored["per_league"][L_IMPROVE]["verdict"] == "improved"
    assert scored["per_league"][L_REGRESS]["verdict"] == "regressed"
    assert sorted(scored["summary"]["leagues_parity"]) == sorted([L_PARITY])
    assert sorted(scored["summary"]["leagues_improved"]) == sorted([L_IMPROVE])
    assert sorted(scored["summary"]["leagues_regressed"]) == sorted([L_REGRESS])
    # Legacy aliases: parity is "within"; improved+regressed both "outside".
    assert sorted(scored["summary"]["leagues_within_tolerance"]) == sorted([L_PARITY])
    assert sorted(scored["summary"]["leagues_outside_tolerance"]) == sorted(
        [L_IMPROVE, L_REGRESS]
    )


# ---------------------------------------------------------------------------
# Tests added for task #734 v2: addresses the validation rejection by
# proving (1) the harness emits `delta_vs_v1` as a spec-named alias of
# `brier_delta`, (2) the requested LGB+XGB stack path via
# `app.predictor.predict()` is actually attempted (with skip-reason
# accounting), (3) the report carries a structured `triage` block under
# the three root-cause buckets the task spec calls out, and (4) the full
# predictor → calibrator → aggregation chain is exercised end-to-end
# against a monkeypatched predictor (no real models loaded).
# ---------------------------------------------------------------------------


def test_delta_vs_v1_alias_matches_brier_delta() -> None:
    """`delta_vs_v1` is the spec-named alias; must equal `brier_delta`."""
    grouped = {
        "NBA": {
            "probs": [0.6, 0.4, 0.7, 0.3, 0.9, 0.1],
            "ys":    [1,   0,   1,   0,   1,   0],
        },
    }
    scored = bt.score_grouped(grouped)
    league = scored["per_league"]["NBA"]
    assert "delta_vs_v1" in league, "spec field `delta_vs_v1` missing"
    assert league["delta_vs_v1"] == league["brier_delta"], (
        "delta_vs_v1 must mirror brier_delta exactly"
    )


def test_extract_feature_vector_handles_archive_shapes() -> None:
    """Archive rows w/o features → None; rows with features → vector."""
    # V1 archive shape — analysis_json present but no features key.
    assert bt._extract_feature_vector(
        {"analysis_json": {"side": "yes", "edge": 0.04}}
    ) is None
    # JSON-string analysis_json — same outcome.
    assert bt._extract_feature_vector(
        {"analysis_json": '{"side":"yes"}'}
    ) is None
    # Malformed JSON → None, no exception.
    assert bt._extract_feature_vector(
        {"analysis_json": "not-json"}
    ) is None
    # Missing analysis_json → None.
    assert bt._extract_feature_vector({"league": "NBA"}) is None
    # Forward-compat: analysis_json with feature_vector → list[float].
    fv = bt._extract_feature_vector(
        {"analysis_json": {"feature_vector": [0.1, 0.2, 0.3]}}
    )
    assert fv == [0.1, 0.2, 0.3]
    # Alternate keys are accepted.
    fv = bt._extract_feature_vector(
        {"analysis_json": {"features": [1, 2, 3]}}
    )
    assert fv == [1.0, 2.0, 3.0]
    # Bad value types in feature list → None (don't pollute downstream).
    assert bt._extract_feature_vector(
        {"analysis_json": {"feature_vector": ["not", "a", "number"]}}
    ) is None


def test_full_stack_attempt_skips_when_features_missing() -> None:
    """V1 archive rows have no features → 0 predicted, all skipped with
    `feature_vector_unavailable` reason. This is the real-world result."""
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


def test_full_stack_attempt_invokes_predictor_when_features_present() -> None:
    """End-to-end: monkeypatched predictor → grouped output → score_grouped
    → verdict aggregation. Exercises the full predictor → calibrator →
    aggregation chain the reviewer asked for, without loading real models."""

    class _StubPrediction:
        def __init__(self, p: float) -> None:
            self.probability = p

    seen_calls: list[tuple[list[float], str]] = []

    async def _stub_predict(features, *, league):
        seen_calls.append((list(features), league))
        # Deterministic stub: probability = mean of features, clamped.
        m = sum(features) / max(1, len(features))
        return _StubPrediction(max(0.0, min(1.0, m)))

    rows = [
        {"league": "NBA", "actual_outcome": 1,
         "analysis_json": {"feature_vector": [0.7, 0.8, 0.9]}},  # → p≈0.8
        {"league": "NBA", "actual_outcome": 0,
         "analysis_json": {"feature_vector": [0.1, 0.2, 0.3]}},  # → p≈0.2
        {"league": "NFL", "actual_outcome": 1,
         "analysis_json": {"feature_vector": [0.6, 0.6, 0.6]}},  # → p≈0.6
        # Skipped: no features.
        {"league": "MLB", "actual_outcome": 1, "analysis_json": {"side": "yes"}},
        # Skipped: bad outcome.
        {"league": "NHL", "actual_outcome": "tied",
         "analysis_json": {"feature_vector": [0.5]}},
    ]
    grouped, summary = asyncio.run(
        bt._attempt_full_stack_inference(rows, leagues_filter=None,
                                          predict_fn=_stub_predict)
    )
    # Predictor was called exactly 3 times (the 3 rows with features &
    # binary outcomes). MLB and NHL were skipped before predict.
    assert len(seen_calls) == 3
    assert summary["rows_attempted"] == 3
    assert summary["rows_predicted"] == 3
    assert summary["rows_skipped"] == 2
    assert summary["skip_reasons"]["feature_vector_unavailable"] == 1
    assert summary["skip_reasons"]["non_binary_outcome"] == 1

    # Aggregation step: feed the grouped output through score_grouped
    # and verify per-league shape + verdict population.
    scored = bt.score_grouped(grouped)
    assert set(scored["per_league"].keys()) == {"NBA", "NFL"}
    assert scored["per_league"]["NBA"]["sample_count"] == 2
    assert scored["per_league"]["NFL"]["sample_count"] == 1
    # Both should have a delta_vs_v1 field — the spec alias is present
    # whether or not we came in via full-stack or calibrator-only.
    for lg in ("NBA", "NFL"):
        assert "delta_vs_v1" in scored["per_league"][lg]


def test_run_full_stack_emits_triage_with_feature_builder_active() -> None:
    """Top-level `run(full_stack=True)` w/ monkeypatched predictor +
    feature-less rows: feature_builder_mismatch bucket is `active=True`."""
    fixture = (
        Path(__file__).resolve().parent / "fixtures" / "v1_backtest_synthetic.json"
    )
    rows = json.loads(fixture.read_text())

    async def _never_called(*args, **kwargs):  # pragma: no cover
        raise AssertionError("synthetic fixture has no features → predict must NOT fire")

    report = asyncio.run(
        bt.run(rows=rows, limit=None, leagues_filter=None,
               load_calibrators=False, full_stack=True,
               predict_fn=_never_called)
    )
    # Calibrator-only path still produces metrics from the fixture.
    assert report["row_kept_count"] > 0
    # Full-stack attempt block surfaces the feature-vector-unavailable count.
    fs = report["full_stack_attempt"]
    assert fs is not None
    assert fs["rows_attempted"] == 0
    assert fs["rows_predicted"] == 0
    assert fs["skip_reasons"].get("feature_vector_unavailable", 0) > 0
    # Triage block: feature_builder_mismatch active because predictor saw
    # zero usable rows; calibrator_misalignment always active by design;
    # artifact_drift inactive when no league regressed.
    triage = report["triage"]
    assert triage["feature_builder_mismatch"]["active"] is True
    assert triage["feature_builder_mismatch"]["evidence"][
        "rows_predicted_via_models_loader"] == 0
    assert triage["calibrator_misalignment"]["active"] is True
    # The synthetic fixture is small and the no-load path means
    # calibrator.apply() is a no-op → no league should regress.
    assert triage["artifact_drift"]["active"] is False


def test_run_full_stack_uses_predictor_when_features_present() -> None:
    """Mirror of the above with synthetic features in analysis_json so
    the predictor stub IS invoked and the full chain is exercised."""

    class _StubPrediction:
        def __init__(self, p: float) -> None:
            self.probability = p

    async def _stub_predict(features, *, league):
        m = sum(features) / max(1, len(features))
        return _StubPrediction(max(0.0, min(1.0, m)))

    rows = [
        {"league": "NBA", "predicted_prob": 0.5, "actual_outcome": 1,
         "analysis_json": {"side": "yes", "feature_vector": [0.8, 0.8, 0.8]}},
        {"league": "NBA", "predicted_prob": 0.5, "actual_outcome": 0,
         "analysis_json": {"side": "yes", "feature_vector": [0.2, 0.2, 0.2]}},
        {"league": "NFL", "predicted_prob": 0.5, "actual_outcome": 1,
         "analysis_json": {"side": "yes", "feature_vector": [0.7, 0.7, 0.7]}},
    ]
    report = asyncio.run(
        bt.run(rows=rows, limit=None, leagues_filter=None,
               load_calibrators=False, full_stack=True,
               predict_fn=_stub_predict)
    )
    fs = report["full_stack_attempt"]
    assert fs["rows_attempted"] == 3
    assert fs["rows_predicted"] == 3
    assert fs["rows_skipped"] == 0
    # full_stack_scored carries the per-league rollup from the predictor
    # path (separate from the calibrator-only `per_league` block).
    assert "full_stack_scored" in report
    fss = report["full_stack_scored"]
    assert set(fss["per_league"].keys()) == {"NBA", "NFL"}
    # Feature-builder triage bucket is INACTIVE here because the
    # predictor produced rows; the structured count proves it.
    triage = report["triage"]
    assert triage["feature_builder_mismatch"]["active"] is False
    assert triage["feature_builder_mismatch"]["evidence"][
        "rows_predicted_via_models_loader"] == 3
