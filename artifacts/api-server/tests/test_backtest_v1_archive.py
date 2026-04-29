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


# ---------------------------------------------------------------------------
# v3 rework — regression tests (apply_calibrator=False + DB batching)
# ---------------------------------------------------------------------------


def test_score_grouped_apply_calibrator_false_skips_calibration() -> None:
    """Direct unit test of the new ``apply_calibrator=False`` mode.

    With this flag set, score_grouped MUST NOT call calibrators.apply()
    on the input probabilities. Both raw and calibrated metrics will
    therefore be identical and ``brier_delta``/``delta_vs_v1`` will
    be exactly 0.0. ``calibrator_applied_count`` is set to 0 and
    ``calibrator_type`` to None so the report makes the bypass
    explicit.

    Regression guard: an earlier draft of the full-stack path called
    score_grouped() with the default ``apply_calibrator=True`` —
    re-applying the per-league calibrator on top of probabilities that
    app.predictor.predict() had already calibrated. The double-call
    silently produced bogus brier_calibrated numbers.
    """
    apply_calls: list[tuple[float, str | None]] = []

    def _spy_apply(p: float, league: str | None):
        apply_calls.append((p, league))
        # Move probabilities aggressively so test would fail if called.
        return (1.0 - p, {"applied": True, "type": "spy"})

    grouped = {
        "NBA": {"probs": [0.7, 0.3, 0.6], "ys": [1, 0, 1]},
    }
    original = bt.calibrators.apply
    bt.calibrators.apply = _spy_apply  # type: ignore[assignment]
    try:
        scored = bt.score_grouped(grouped, apply_calibrator=False)
    finally:
        bt.calibrators.apply = original  # type: ignore[assignment]

    # The spy was never invoked — the bypass branch ran instead.
    assert apply_calls == [], (
        f"calibrators.apply must not be called when apply_calibrator=False; "
        f"got {len(apply_calls)} call(s): {apply_calls}"
    )
    nba = scored["per_league"]["NBA"]
    assert nba["calibrator_applied_count"] == 0
    assert nba["calibrator_type"] is None
    # Raw == calibrated because no calibration happened.
    assert nba["brier_v1_raw"] == pytest.approx(nba["brier_calibrated"])
    assert nba["brier_delta"] == pytest.approx(0.0, abs=1e-12)
    assert nba["delta_vs_v1"] == pytest.approx(0.0, abs=1e-12)
    assert scored["summary"]["leagues_parity"] == ["NBA"]
    assert scored["summary"]["leagues_improved"] == []
    assert scored["summary"]["leagues_regressed"] == []


def test_run_full_stack_bypasses_double_calibration_end_to_end() -> None:
    """End-to-end regression: when run() invokes the full-stack path,
    ``full_stack_scored`` MUST be free of double-calibration even when
    a fitted calibrator IS active.

    Setup: monkeypatch calibrators.apply() to return a wildly different
    probability whenever called (would corrupt brier_calibrated if the
    full-stack path naively re-applied it). The predictor stub returns
    a fixed p=0.7 regardless of features.

    Expectations:
      * calibrator-only ``per_league`` block shows brier_delta != 0
        (the spy calibrator IS applied to V1.predicted_prob — that's
        the whole point of the calibrator-only path).
      * ``full_stack_scored.per_league.<lg>.brier_delta`` is exactly 0
        (no double-calibration), and brier_calibrated == Brier(0.7, ys).
    """

    class _StubPrediction:
        def __init__(self, p: float) -> None:
            self.probability = p

    async def _stub_predict(features, *, league):
        # Constant 0.7 — deterministic so we can compute expected Brier.
        return _StubPrediction(0.7)

    spy_apply_calls: list[tuple[float, str | None]] = []

    def _spy_apply(p: float, league: str | None):
        spy_apply_calls.append((p, league))
        return (1.0 - p, {"applied": True, "type": "spy"})

    # NB: predicted_prob values must NOT be 0.5 — the spy returns 1-p
    # which would map 0.5 → 0.5 and the calibrator-only Brier delta
    # would (correctly) be zero, defeating the regression contrast.
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

    # Calibrator-only path: spy IS applied (would-flip every prob).
    nba_cal = report["per_league"]["NBA"]
    assert nba_cal["calibrator_applied_count"] == 3
    assert nba_cal["brier_delta"] != pytest.approx(0.0, abs=1e-9), (
        "spy calibrator should have moved brier_calibrated away from "
        "brier_v1_raw on the calibrator-only path"
    )

    # Full-stack path: no double-calibration.
    fss = report["full_stack_scored"]
    nba_fs = fss["per_league"]["NBA"]
    assert nba_fs["calibrator_applied_count"] == 0
    assert nba_fs["calibrator_type"] is None
    assert nba_fs["brier_delta"] == pytest.approx(0.0, abs=1e-12)
    assert nba_fs["delta_vs_v1"] == pytest.approx(0.0, abs=1e-12)
    # brier_calibrated must equal Brier(0.7, ys=[1,0,1]) =
    #   ((0.7-1)^2 + (0.7-0)^2 + (0.7-1)^2)/3 = (0.09+0.49+0.09)/3 = 0.2233...
    expected_brier = ((0.7 - 1) ** 2 + (0.7 - 0) ** 2 + (0.7 - 1) ** 2) / 3
    assert nba_fs["brier_calibrated"] == pytest.approx(expected_brier, abs=1e-9)
    # Spy calls came only from the calibrator-only path (3 NBA rows),
    # NOT from the full-stack path. If the full-stack path had double-
    # calibrated we'd see 6 calls instead of 3.
    assert len(spy_apply_calls) == 3, (
        f"expected exactly 3 spy calls (calibrator-only path) — got "
        f"{len(spy_apply_calls)}, suggesting double-calibration regression"
    )


def test_normalize_into_matches_normalize_rows() -> None:
    """``_normalize_into`` (streaming) and ``_normalize_rows`` (one-shot)
    must produce identical grouped dicts for the same input. Guards the
    streaming refactor against silent divergence."""
    rows = [
        {"league": "NBA", "predicted_prob": 0.7, "actual_outcome": 1,
         "analysis_json": {"side": "yes"}},
        {"league": "nba", "predicted_prob": 0.3, "actual_outcome": 0,
         "analysis_json": {"side": "no"}},  # → flipped to 0.7
        {"league": "NFL", "predicted_prob": 0.55, "actual_outcome": 1,
         "analysis_json": '{"side": "yes"}'},
        # Dropped: missing prob.
        {"league": "MLB", "predicted_prob": None, "actual_outcome": 1,
         "analysis_json": {"side": "yes"}},
    ]
    one_shot = bt._normalize_rows(rows, leagues_filter=None)
    streamed: dict[str, dict[str, list]] = {}
    appended = bt._normalize_into(streamed, rows, leagues_filter=None)
    assert appended == sum(len(v["probs"]) for v in one_shot.values())
    assert set(streamed.keys()) == set(one_shot.keys())
    for lg in one_shot:
        assert streamed[lg]["probs"] == one_shot[lg]["probs"]
        assert streamed[lg]["ys"] == one_shot[lg]["ys"]


def test_run_streams_in_batches_and_reports_batch_count() -> None:
    """In-memory mode uses the same streaming code path as DB mode:
    rows are chunked into batches of ``batch_size`` and the report
    surfaces ``batch_count`` so operators can verify batching is on.

    Also checks that final scored output is identical to the
    non-batched reference — i.e. batching is observationally
    invariant."""
    rows = [
        {"league": "NBA", "predicted_prob": 0.6, "actual_outcome": 1,
         "analysis_json": {"side": "yes"}},
        {"league": "NBA", "predicted_prob": 0.4, "actual_outcome": 0,
         "analysis_json": {"side": "yes"}},
        {"league": "NFL", "predicted_prob": 0.55, "actual_outcome": 1,
         "analysis_json": {"side": "yes"}},
        {"league": "MLB", "predicted_prob": 0.5, "actual_outcome": 0,
         "analysis_json": {"side": "yes"}},
        {"league": "MLB", "predicted_prob": 0.7, "actual_outcome": 1,
         "analysis_json": {"side": "yes"}},
    ]
    big = asyncio.run(bt.run(rows=rows, limit=None, leagues_filter=None,
                             load_calibrators=False, batch_size=1000))
    small = asyncio.run(bt.run(rows=rows, limit=None, leagues_filter=None,
                               load_calibrators=False, batch_size=2))
    # batch_count reflects the number of chunks consumed.
    assert big["batch_count"] == 1
    assert small["batch_count"] == 3  # ceil(5/2) = 3
    # Same input → same kept-row count, same per-league math.
    assert big["row_kept_count"] == small["row_kept_count"]
    assert big["row_input_count"] == small["row_input_count"] == 5
    for lg in big["per_league"]:
        assert (
            big["per_league"][lg]["brier_v1_raw"]
            == pytest.approx(small["per_league"][lg]["brier_v1_raw"], abs=1e-12)
        )
        assert (
            big["per_league"][lg]["sample_count"]
            == small["per_league"][lg]["sample_count"]
        )


def test_archive_to_candidate_maps_known_fields_and_returns_kelly_override() -> None:
    """Locks down the canonical V1-archive -> live-candidate mapping
    table documented in scripts/backtest_v1_archive.py.  Every field
    the surrogate path depends on is covered here so a future rename
    on either side trips a clear test failure rather than silently
    poisoning the surrogate output."""
    row = {
        "league": "nba",
        "predicted_prob": 0.62,
        "market_type": "moneyline",
        "analysis_json": {
            "side": "yes",
            "market_implied_prob": 0.55,
            "signal_strength": 0.18,
            "edge_at_entry": 0.07,
            "kelly_used": 0.025,
        },
    }
    result = bt._archive_to_candidate(row)
    assert result is not None
    candidate, kelly_override = result
    assert candidate["consensus_probability"] == pytest.approx(0.62)
    assert candidate["market_prob"] == pytest.approx(0.55)
    assert candidate["signal_strength"] == pytest.approx(0.18)
    assert candidate["edge"] == pytest.approx(0.07)
    assert candidate["side"] == "yes"
    assert candidate["league"] == "NBA"  # upper-cased
    assert candidate["market_type"] == "moneyline"
    assert kelly_override == pytest.approx(0.025)
    # None-valued mappings are dropped so build_vector falls back to
    # the documented per-spec defaults rather than coercing None.
    sparse = bt._archive_to_candidate(
        {"league": "NBA", "predicted_prob": 0.5,
         "analysis_json": {"side": "no"}}
    )
    assert sparse is not None
    candidate, kelly_override = sparse
    assert "market_prob" not in candidate
    assert "signal_strength" not in candidate
    assert kelly_override is None


def test_archive_to_candidate_rejects_rows_missing_minimum_inputs() -> None:
    """Surrogate is opt-in but still requires predicted_prob + side —
    otherwise build_vector would emit a degenerate vector and produce
    misleading metrics."""
    # Missing predicted_prob -> reject.
    assert bt._archive_to_candidate(
        {"league": "NBA", "analysis_json": {"side": "yes"}}
    ) is None
    # Missing side -> reject (build_vector needs it for side_val).
    assert bt._archive_to_candidate(
        {"league": "NBA", "predicted_prob": 0.5, "analysis_json": {}}
    ) is None
    # Non-numeric predicted_prob -> reject.
    assert bt._archive_to_candidate(
        {"league": "NBA", "predicted_prob": "n/a",
         "analysis_json": {"side": "yes"}}
    ) is None


def test_full_stack_attempt_uses_surrogate_when_features_missing() -> None:
    """When --surrogate-features is on, rows without an archived
    feature_vector are routed through a feature builder (injected
    here as a stub) and the predictor IS invoked.  Counts split into
    archived vs surrogate; the report's surrogate-rate is auditable."""

    class _StubPrediction:
        def __init__(self, p: float) -> None:
            self.probability = p

    seen_calls: list[tuple[list[float], str]] = []

    async def _stub_predict(features, *, league):
        seen_calls.append((list(features), league))
        m = sum(features) / max(1, len(features))
        return _StubPrediction(max(0.0, min(1.0, m)))

    # Stub feature builder: just returns predicted_prob expanded.
    def _stub_builder(row):
        pp = row.get("predicted_prob") or 0.5
        return [float(pp)] * 32

    rows = [
        # No feature_vector → goes through surrogate builder.
        {"league": "NBA", "actual_outcome": 1, "predicted_prob": 0.7,
         "analysis_json": {"side": "yes"}},
        {"league": "NBA", "actual_outcome": 0, "predicted_prob": 0.3,
         "analysis_json": {"side": "yes"}},
        # Has direct feature_vector → archived path (still preferred).
        {"league": "NFL", "actual_outcome": 1,
         "analysis_json": {"feature_vector": [0.6, 0.6, 0.6]}},
    ]
    grouped, summary = asyncio.run(
        bt._attempt_full_stack_inference(
            rows, leagues_filter=None, predict_fn=_stub_predict,
            surrogate_features=True, feature_builder=_stub_builder,
        )
    )
    # All 3 rows produce predictions (vs the 1 they would without surrogate).
    assert len(seen_calls) == 3
    assert summary["rows_predicted"] == 3
    assert summary["rows_skipped"] == 0
    assert summary["feature_source"]["archived"] == 1
    assert summary["feature_source"]["surrogate"] == 2
    # Aggregation still works.
    scored = bt.score_grouped(grouped)
    assert scored["per_league"]["NBA"]["sample_count"] == 2
    assert scored["per_league"]["NFL"]["sample_count"] == 1


def test_run_full_stack_surrogate_flag_propagates_to_summary() -> None:
    """End-to-end: run() with surrogate_features=True surfaces the
    surrogate counts on the final report so reviewers can audit how
    many rows landed via the degraded baseline."""

    class _StubPrediction:
        def __init__(self, p: float) -> None:
            self.probability = p

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
    # full_stack_scored block populated.
    assert "full_stack_scored" in report
    assert report["full_stack_scored"]["per_league"]["NBA"]["sample_count"] == 2


# ---------------------------------------------------------------------------
# v4 follow-ups: side-alias canonicalization + real build_vector integration
# (architect review pass — locks down the highest-risk mapping path so a
# regression in _resolve_side or in the V1 archive's side vocabulary trips
# a clear test rather than silently poisoning surrogate predictions).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw_side,expected", [
    ("yes", "yes"), ("YES", "yes"), ("buy", "yes"),
    ("live_yes", "yes"), ("buy yes", "yes"), ("Buy Yes", "yes"),
    ("no", "no"), ("NO", "no"), ("sell", "no"),
    ("live_no", "no"), ("buy no", "no"), ("  Sell  ", "no"),
])
def test_archive_to_candidate_canonicalizes_side_aliases(
    raw_side: str, expected: str,
) -> None:
    """V1 archive uses several side aliases (sell, live_no, buy no, ...).
    The live builder's _resolve_side() only treats literal 'NO' as no-side
    and would mis-classify the rest as YES, poisoning side_val.  The
    surrogate path canonicalizes to 'yes'/'no' before handing the dict
    to build_vector — this test pins that contract per alias."""
    result = bt._archive_to_candidate(
        {"league": "NBA", "predicted_prob": 0.55,
         "analysis_json": {"side": raw_side}}
    )
    assert result is not None, f"alias {raw_side!r} should canonicalize"
    candidate, _kelly = result
    assert candidate["side"] == expected


def test_archive_to_candidate_rejects_unknown_side_alias() -> None:
    """Unknown side strings are rejected outright rather than silently
    bucketed as YES — guessing here would corrupt side-flip math."""
    assert bt._archive_to_candidate(
        {"league": "NBA", "predicted_prob": 0.55,
         "analysis_json": {"side": "maybe"}}
    ) is None
    assert bt._archive_to_candidate(
        {"league": "NBA", "predicted_prob": 0.55,
         "analysis_json": {"side": ""}}
    ) is None


def test_build_surrogate_vector_against_real_build_vector() -> None:
    """Direct integration: feed an archive row through the real
    app.features.build_vector via _build_surrogate_vector and assert it
    returns the expected 32-slot vector with the side alias correctly
    routed (sell -> no -> side_val=0.0)."""
    from app.features.builders import _SPECS

    row = {"league": "NBA", "predicted_prob": 0.62,
           "market_type": "moneyline",
           "analysis_json": {"side": "sell", "market_implied_prob": 0.55,
                              "edge_at_entry": 0.07,
                              "signal_strength": 0.18}}
    vec = bt._build_surrogate_vector(row)
    assert vec is not None
    assert len(vec) == len(_SPECS)
    side_idx = next(i for i, s in enumerate(_SPECS) if s.name == "side_val")
    assert vec[side_idx] == 0.0  # 'sell' -> 'no' -> side_val=0.0

    # And a YES alias routes to 1.0.
    row_yes = {"league": "NBA", "predicted_prob": 0.62,
               "analysis_json": {"side": "buy yes"}}
    vec_yes = bt._build_surrogate_vector(row_yes)
    assert vec_yes is not None
    assert vec_yes[side_idx] == 1.0


def test_build_triage_note_reflects_surrogate_mode_when_active() -> None:
    """When --surrogate-features predicts rows, the triage note must stop
    claiming 'zero usable rows' and explicitly mention the degraded
    surrogate baseline + #731 follow-up — otherwise reviewers reading
    the JSON in isolation get a contradictory picture."""
    scored = {"summary": {"leagues_improved": [], "leagues_parity": [],
                          "leagues_regressed": []}}
    fs = {"rows_attempted": 100, "rows_predicted": 100,
          "skip_reasons": {}, "feature_source": {"surrogate": 100,
                                                   "archived": 0},
          "surrogate_features_enabled": True}
    triage = bt._build_triage(scored, fs)
    note = triage["feature_builder_mismatch"]["note"]
    assert "surrogate" in note.lower()
    assert "degraded" in note.lower()
    assert "zero usable rows" not in note.lower()
    ev = triage["feature_builder_mismatch"]["evidence"]
    assert ev["surrogate_features_enabled"] is True
    assert ev["rows_predicted_via_surrogate"] == 100

    # And without the flag, the original "zero usable rows" framing stands.
    fs_off = {"rows_attempted": 100, "rows_predicted": 0,
              "skip_reasons": {"feature_vector_unavailable": 100}}
    triage_off = bt._build_triage(scored, fs_off)
    assert "zero" in triage_off["feature_builder_mismatch"]["note"].lower()
    assert triage_off["feature_builder_mismatch"]["active"] is True
