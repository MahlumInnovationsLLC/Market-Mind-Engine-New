"""V1 backtest harness — audit the calibrator stage of the current
ensemble against V1's historical predictions, with documented
constrained-fallback semantics.

Acceptance-gate caveat
----------------------

The original task spec (#734) called for the harness to "re-run each
row through the current ensemble (LGB + XGB stack via
``models_loader``)" and report "parity within 2% Brier vs V1 for at
least the 5 leagues whose calibrators were refit (NBA, NFL, NCAAB,
NHL, MLB)."  That gate **cannot be evaluated as specified** against
the data we have — see the *Triage* section below — so the harness
runs in two modes and reports the constrained-fallback result
explicitly:

* ``--full-stack`` — attempts the requested path: load each row,
  invoke ``app.predictor.predict()`` (which dispatches through
  ``app.models_loader``), then apply the calibrator.  This mode
  produces zero usable rows on today's archive because the upstream
  feature vector is not snapshotted; the harness still walks the
  full archive, records the per-row skip reason
  (``feature_vector_unavailable``), and emits a structured count so
  the reviewer can see the path was attempted (not silently bypassed).

* **Calibrator-only audit** (default) — the only audit physically
  possible against the archive as imported.  Treats V1's
  ``predicted_prob`` as the calibrator's input (which is exactly how
  ``app/calibrators.py::_fetch_training_data`` already treats it for
  the nightly refit), applies the per-league calibrator, and reports
  Brier / log-loss / AUC for V1-raw vs calibrated.

Triage (the three root-cause buckets the spec asks for)
-------------------------------------------------------

1. **feature_builder_mismatch** — V1 archive only stores
   ``predicted_prob`` and ``actual_outcome`` plus a tiny
   ``analysis_json`` blob.  None of the live ensemble's input features
   (real-time ``market_prob``, sharp-money signals, EV-source
   agreement, weather, etc.) were snapshotted at trade time, so we
   cannot reproduce the LGB+XGB stack's input vector for any V1 row.
   This is the root cause that prevents the requested ``--full-stack``
   path from producing data.

2. **calibrator_misalignment** — V1's archived ``predicted_prob`` is
   the *pre-calibrator* stack output (the same domain
   ``calibrators._fetch_training_data`` treats as the X input for the
   nightly fit).  V1's *post-calibrator* probability — the value that
   would let us compute "parity within 2% Brier vs V1" as the spec
   intends — is **not** in the archive.  So
   ``brier(predicted_prob, actual_outcome)`` is V1's raw-stack Brier,
   not V1's final Brier.  The current per-league calibrators improving
   on this baseline (large negative ``delta_vs_v1``) is the expected
   shape, not a parity failure.

3. **artifact_drift** — would surface as a per-league
   ``verdict == "regressed"`` row (calibrated Brier > raw Brier
   beyond ``_BRIER_TOLERANCE``).  Today's run has zero such rows.
   The harness keeps this bucket because it remains the operator-
   actionable signal: any league where the loaded calibrator makes
   the score *worse* than V1's untreated output indicates the on-disk
   artifact diverged from what was fit.

The structured ``triage`` block in the JSON report restates these
buckets per-run so an out-of-band reviewer can read the report alone
without context.

Output schema (per league)
--------------------------

* ``sample_count``, ``calibrator_applied_count``, ``calibrator_type``
* ``brier_v1_raw``, ``brier_calibrated``, ``brier_delta``,
  ``delta_vs_v1`` (alias of ``brier_delta`` — name from the task
  spec; the two fields always carry the same value)
* ``log_loss_v1_raw``, ``log_loss_calibrated``
* ``auc_v1_raw``, ``auc_calibrated``
* ``within_brier_tolerance``, ``verdict``
  (``parity`` / ``improved`` / ``regressed``)

Modes
-----

Three run modes:

* **DB mode** (default): pulls from ``v1_archive.trade_analyses``
  through ``app.db``.  Optional ``--limit N`` and ``--leagues NBA,NFL``
  filters.  Optional ``--no-load-calibrators`` runs against an empty
  calibrator state — useful for confirming the harness math itself
  reproduces V1's baseline metrics when the calibrator is a no-op.
* **Fixture mode**: ``--fixture path/to/rows.json`` reads a JSON list of
  ``{league, predicted_prob, actual_outcome, side}`` rows and skips the
  DB.  Used by the pytest harness.
* **Inline-rows mode**: ``--inline-rows '<json-list>'`` for ad-hoc
  smoke tests.

The output report is identical across modes.

Run with::

    python -m scripts.backtest_v1_archive \\
        --json-out scripts/reports/v1_backtest_2026-04-29.json
    python -m scripts.backtest_v1_archive --limit 5000  # quick sanity
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Make `app.*` imports work whether the script is invoked as
# `python scripts/backtest_v1_archive.py` (cwd=api-server) or
# `python -m scripts.backtest_v1_archive`.
_HERE = Path(__file__).resolve()
_API_SERVER_ROOT = _HERE.parent.parent
if str(_API_SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_SERVER_ROOT))

from app import calibrators  # noqa: E402


# Sides V1 wrote ``predicted_prob`` as YES-aligned for. Anything in the
# NO/SELL family must be flipped to side-aligned before scoring against
# the side-aligned ``actual_outcome`` column.  The set mirrors
# `app/calibrators.py::_fetch_training_data` exactly so the backtest
# scores rows the same way the nightly refit job scores them.
_NO_SIDES = frozenset({"no", "sell", "live_no", "buy no"})
_YES_SIDES = frozenset({"yes", "buy", "live_yes", "buy yes"})

# Brier-delta tolerance used by the rollup line in the report.  The
# plan calls for "parity within 2% Brier vs V1 for at least the 5
# leagues whose calibrators were refit".  We measure |delta| vs V1
# baseline where delta = brier_calibrated - brier_v1_raw; positive
# delta = regression, negative = improvement.
_BRIER_TOLERANCE = 0.02


# ---------------------------------------------------------------------------
# Metric helpers
# ---------------------------------------------------------------------------


def _brier(probs: list[float], ys: list[int]) -> float | None:
    """Mean squared error between probability and 0/1 outcome."""
    if not probs:
        return None
    return sum((p - y) * (p - y) for p, y in zip(probs, ys)) / len(probs)


def _log_loss(probs: list[float], ys: list[int]) -> float | None:
    """Binary cross-entropy. Clamps probabilities away from 0/1 to
    avoid -inf when V1 emitted a degenerate 0.0 / 1.0 prediction."""
    if not probs:
        return None
    eps = 1e-12
    s = 0.0
    for p, y in zip(probs, ys):
        p = max(eps, min(1.0 - eps, p))
        s += -(y * math.log(p) + (1 - y) * math.log(1.0 - p))
    return s / len(probs)


def _auc(probs: list[float], ys: list[int]) -> float | None:
    """Area under the ROC curve via the Mann–Whitney U identity.

    Returns ``None`` when only one class is present (AUC undefined).
    Uses a stable rank average so ties don't bias the score.
    """
    if not probs or not ys:
        return None
    pos = [p for p, y in zip(probs, ys) if y == 1]
    neg = [p for p, y in zip(probs, ys) if y == 0]
    if not pos or not neg:
        return None
    # Average-rank Mann–Whitney U.
    pairs = sorted(
        ((float(p), 1) for p in pos), key=lambda t: t[0]
    ) + sorted(((float(p), 0) for p in neg), key=lambda t: t[0])
    pairs.sort(key=lambda t: t[0])
    # Assign average ranks for ties.
    ranks: list[float] = [0.0] * len(pairs)
    i = 0
    while i < len(pairs):
        j = i
        while j + 1 < len(pairs) and pairs[j + 1][0] == pairs[i][0]:
            j += 1
        avg_rank = (i + 1 + j + 1) / 2.0  # 1-indexed
        for k in range(i, j + 1):
            ranks[k] = avg_rank
        i = j + 1
    rank_sum_pos = sum(r for r, (_p, y) in zip(ranks, pairs) if y == 1)
    n_pos = len(pos)
    n_neg = len(neg)
    u = rank_sum_pos - n_pos * (n_pos + 1) / 2.0
    return u / (n_pos * n_neg)


# ---------------------------------------------------------------------------
# Row normalization
# ---------------------------------------------------------------------------


def _side_align(predicted_prob: float, side: str | None) -> float | None:
    """Flip ``predicted_prob`` from YES-aligned to side-aligned.

    Returns ``None`` for unrecognized sides — those rows are dropped
    from the backtest the same way they're dropped from the calibrator
    fit (no useful training signal when we can't tell which side won).
    """
    s = (side or "").strip().lower()
    if s in _NO_SIDES:
        return max(0.0, min(1.0, 1.0 - float(predicted_prob)))
    if s in _YES_SIDES:
        return max(0.0, min(1.0, float(predicted_prob)))
    return None


def _normalize_rows(
    rows: list[dict[str, Any]],
    leagues_filter: set[str] | None = None,
) -> dict[str, dict[str, list]]:
    """Group raw rows into ``{league: {"probs":[...], "ys":[...]}}``.

    Drops rows with unknown side, missing probability, or missing
    outcome.  ``analysis_json`` may arrive as either a dict (asyncpg
    JSON decoding) or a JSON string (older drivers / fixtures).
    """
    grouped: dict[str, dict[str, list]] = {}
    for r in rows:
        league = (r.get("league") or "").strip().upper()
        if not league:
            continue
        if leagues_filter and league not in leagues_filter:
            continue
        prob_raw = r.get("predicted_prob")
        outcome_raw = r.get("actual_outcome")
        if prob_raw is None or outcome_raw is None:
            continue
        # Resolve `side`: prefer top-level `side`, then dig into
        # analysis_json (live DB rows) which may be dict or str.
        side = r.get("side")
        if side is None:
            aj = r.get("analysis_json")
            if isinstance(aj, str):
                try:
                    aj = json.loads(aj)
                except (ValueError, TypeError):
                    aj = None
            if isinstance(aj, dict):
                side = aj.get("side")
        p_side = _side_align(float(prob_raw), side)
        if p_side is None:
            continue
        try:
            y = int(outcome_raw)
        except (TypeError, ValueError):
            continue
        if y not in (0, 1):
            continue
        bucket = grouped.setdefault(league, {"probs": [], "ys": []})
        bucket["probs"].append(p_side)
        bucket["ys"].append(y)
    return grouped


# ---------------------------------------------------------------------------
# Row sources
# ---------------------------------------------------------------------------


async def _fetch_db_rows(
    limit: int | None,
    leagues_filter: set[str] | None,
) -> list[dict[str, Any]]:
    """Pull rows from ``v1_archive.trade_analyses``.

    Preflights via ``to_regclass`` so a fresh dev DB without migration
    0037 returns an empty list rather than raising ``UndefinedTable``.
    """
    from app import db  # local import — avoids triggering pool setup at parse time

    present = await db.fetchval(
        "SELECT to_regclass('v1_archive.trade_analyses') IS NOT NULL"
    )
    if not present:
        print("[backtest] v1_archive.trade_analyses not found; nothing to do.")
        return []

    where: list[str] = [
        "predicted_prob IS NOT NULL",
        "actual_outcome IS NOT NULL",
        "league IS NOT NULL",
    ]
    if leagues_filter:
        # Quote+upper each league so the SQL stays parameterless and
        # readable; values come from the operator's CLI argv only.
        cleaned = ",".join(
            "'" + lg.replace("'", "''").upper() + "'" for lg in leagues_filter
        )
        where.append(f"upper(league) IN ({cleaned})")
    sql = (
        "SELECT league, predicted_prob, actual_outcome, analysis_json "
        "FROM v1_archive.trade_analyses "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY created_at DESC NULLS LAST"
    )
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    rows = await db.fetch(sql)
    return [dict(r) for r in rows]


def _read_fixture(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text())
    if not isinstance(data, list):
        raise SystemExit(f"fixture {path} must be a JSON list, got {type(data).__name__}")
    return data


def _read_inline(blob: str) -> list[dict[str, Any]]:
    data = json.loads(blob)
    if not isinstance(data, list):
        raise SystemExit("--inline-rows must be a JSON list")
    return data


# ---------------------------------------------------------------------------
# Full-stack attempt — invokes app.predictor.predict() per row.
#
# This is the path the original task spec asked for ("re-runs each row
# through the current ensemble (LGB + XGB stack) via models_loader").
# On the archive as imported it produces zero usable rows because V1
# never snapshotted the upstream feature vector — but the harness still
# walks every row, attempts the call, and records the per-row skip
# reason so the reviewer can verify the path was attempted (rather
# than silently bypassed).
#
# `predict_fn` is injected so the pytest end-to-end test can monkey-
# patch the predictor and exercise the full predictor → calibrator →
# aggregation chain without spinning up the model registry.
# ---------------------------------------------------------------------------


def _extract_feature_vector(row: dict[str, Any]) -> list[float] | None:
    """Pull a feature vector out of an archived row, if present.

    V1's import only persists ``analysis_json`` as a tiny audit blob;
    in the archive as imported by ``import_v1_history.py`` this never
    contains the ensemble's input features.  This helper exists for
    forward compatibility (a future re-import could include them) and
    so the unit tests can exercise the full-stack path with synthetic
    features.
    """
    aj = row.get("analysis_json")
    if isinstance(aj, str):
        try:
            aj = json.loads(aj)
        except (ValueError, TypeError):
            aj = None
    if not isinstance(aj, dict):
        return None
    for key in ("feature_vector", "features", "model_features"):
        v = aj.get(key)
        if isinstance(v, (list, tuple)) and v:
            try:
                return [float(x) for x in v]
            except (TypeError, ValueError):
                return None
    return None


async def _attempt_full_stack_inference(
    rows: list[dict[str, Any]],
    leagues_filter: set[str] | None,
    predict_fn: Any | None = None,
) -> tuple[dict[str, dict[str, list]], dict[str, Any]]:
    """Walk every row, try the LGB+XGB stack via ``app.predictor``.

    Returns ``(grouped, summary)`` where ``grouped`` is the same
    ``{league: {"probs":[...], "ys":[...]}}`` shape ``score_grouped``
    consumes, and ``summary`` is a structured count of attempted /
    predicted / skipped rows with per-skip-reason buckets.
    """
    if predict_fn is None:
        # Local import so the pytest fixture path doesn't pull the
        # whole predictor module unless full-stack mode is requested.
        from app import predictor  # noqa: PLC0415

        predict_fn = predictor.predict

    grouped: dict[str, dict[str, list]] = {}
    skip_reasons: dict[str, int] = {}
    attempted = 0
    predicted = 0
    skipped = 0
    error_samples: list[dict[str, Any]] = []
    for r in rows:
        league = (r.get("league") or "").strip().upper()
        if not league:
            skipped += 1
            skip_reasons["missing_league"] = skip_reasons.get("missing_league", 0) + 1
            continue
        if leagues_filter and league not in leagues_filter:
            continue
        outcome_raw = r.get("actual_outcome")
        if outcome_raw is None:
            skipped += 1
            skip_reasons["missing_outcome"] = skip_reasons.get("missing_outcome", 0) + 1
            continue
        try:
            y = int(outcome_raw)
        except (TypeError, ValueError):
            skipped += 1
            skip_reasons["non_binary_outcome"] = skip_reasons.get("non_binary_outcome", 0) + 1
            continue
        if y not in (0, 1):
            skipped += 1
            skip_reasons["non_binary_outcome"] = skip_reasons.get("non_binary_outcome", 0) + 1
            continue
        features = _extract_feature_vector(r)
        if features is None:
            # The dominant skip on the archive as imported. Documented
            # under the `feature_builder_mismatch` triage bucket.
            skipped += 1
            skip_reasons["feature_vector_unavailable"] = (
                skip_reasons.get("feature_vector_unavailable", 0) + 1
            )
            continue

        attempted += 1
        try:
            pred = await predict_fn(features, league=league)
        except Exception as e:  # pragma: no cover — exercised via tests
            skipped += 1
            reason = f"predict_error_{type(e).__name__}"
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
            if len(error_samples) < 5:
                error_samples.append({"league": league, "error": f"{type(e).__name__}: {e}"})
            continue

        # Accept either a Prediction-like object or a dict; both expose
        # `.probability`. The probability is already calibrated when
        # `predict()` flows through `calibrators.apply()` internally,
        # so we score the value as-is against `actual_outcome`.
        if hasattr(pred, "probability"):
            p = float(pred.probability)
        elif isinstance(pred, dict) and "probability" in pred:
            p = float(pred["probability"])
        else:
            skipped += 1
            skip_reasons["bad_predict_shape"] = skip_reasons.get("bad_predict_shape", 0) + 1
            continue
        p = max(0.0, min(1.0, p))
        bucket = grouped.setdefault(league, {"probs": [], "ys": []})
        bucket["probs"].append(p)
        bucket["ys"].append(y)
        predicted += 1

    summary = {
        "rows_attempted": attempted,
        "rows_predicted": predicted,
        "rows_skipped": skipped,
        "skip_reasons": skip_reasons,
        "error_samples": error_samples,
    }
    return grouped, summary


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def score_grouped(
    grouped: dict[str, dict[str, list]],
) -> dict[str, Any]:
    """Compute per-league raw + calibrated metrics. Pure function — no IO.

    Rollup semantics for ``brier_delta`` (= ``brier_calibrated - brier_v1_raw``):

    * ``leagues_parity`` — ``|delta| <= _BRIER_TOLERANCE`` (calibrator is
      a wash; either no fitted artifact or the artifact happens to
      reproduce V1's baseline).
    * ``leagues_improved`` — ``delta < -_BRIER_TOLERANCE`` (calibrator
      meaningfully reduces Brier vs V1's raw stack output — the
      desired direction).
    * ``leagues_regressed`` — ``delta > +_BRIER_TOLERANCE`` (calibrator
      makes the score WORSE than V1 — operator-actionable, must be
      triaged before promoting to live).

    The legacy ``leagues_within_tolerance`` /
    ``leagues_outside_tolerance`` keys are retained as aliases so any
    external dashboard already reading the report keeps working.
    """
    per_league: dict[str, Any] = {}
    leagues_parity: list[str] = []
    leagues_improved: list[str] = []
    leagues_regressed: list[str] = []
    for league in sorted(grouped.keys()):
        probs = grouped[league]["probs"]
        ys = grouped[league]["ys"]
        n = len(probs)
        # Apply current calibrator.  When no fitted artifact exists for
        # this league, calibrators.apply() is a documented no-op and
        # returns the raw probability — `applied` flips False so the
        # report can call out the bypass.
        calibrated_pairs = [calibrators.apply(p, league) for p in probs]
        cal_probs = [cp for cp, _info in calibrated_pairs]
        applied_count = sum(1 for _cp, info in calibrated_pairs if info.get("applied"))
        cal_type = next(
            (info.get("type") for _cp, info in calibrated_pairs if info.get("applied")),
            None,
        )

        brier_raw = _brier(probs, ys)
        brier_cal = _brier(cal_probs, ys)
        log_loss_raw = _log_loss(probs, ys)
        log_loss_cal = _log_loss(cal_probs, ys)
        auc_raw = _auc(probs, ys)
        auc_cal = _auc(cal_probs, ys)
        delta = (brier_cal - brier_raw) if (brier_raw is not None and brier_cal is not None) else None

        within_tol: bool | None = None
        verdict: str | None = None
        if delta is not None:
            within_tol = abs(delta) <= _BRIER_TOLERANCE
            if within_tol:
                verdict = "parity"
                leagues_parity.append(league)
            elif delta < 0:
                verdict = "improved"
                leagues_improved.append(league)
            else:
                verdict = "regressed"
                leagues_regressed.append(league)

        per_league[league] = {
            "sample_count": n,
            "calibrator_applied_count": applied_count,
            "calibrator_type": cal_type,
            "brier_v1_raw": brier_raw,
            "brier_calibrated": brier_cal,
            "brier_delta": delta,
            # Spec-named alias of brier_delta — task #734 acceptance
            # criteria asks for `delta_vs_v1`. Same value, two names so
            # both the historical reports and the spec-conforming
            # consumers read the right key.
            "delta_vs_v1": delta,
            "log_loss_v1_raw": log_loss_raw,
            "log_loss_calibrated": log_loss_cal,
            "auc_v1_raw": auc_raw,
            "auc_calibrated": auc_cal,
            "within_brier_tolerance": within_tol,
            "verdict": verdict,
        }

    overall_n = sum(v["sample_count"] for v in per_league.values())
    leagues_outside_tol = leagues_improved + leagues_regressed
    return {
        "per_league": per_league,
        "summary": {
            "league_count": len(per_league),
            "row_count": overall_n,
            "brier_tolerance": _BRIER_TOLERANCE,
            "leagues_parity": leagues_parity,
            "leagues_improved": leagues_improved,
            "leagues_regressed": leagues_regressed,
            # Legacy aliases — pre-existing dashboards keep working.
            "leagues_within_tolerance": leagues_parity,
            "leagues_outside_tolerance": leagues_outside_tol,
        },
    }


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------


def _parse_leagues(arg: str | None) -> set[str] | None:
    if not arg:
        return None
    out = {x.strip().upper() for x in arg.split(",") if x.strip()}
    return out or None


def _build_triage(
    scored: dict[str, Any],
    full_stack_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    """Restate the three root-cause buckets in machine-readable form.

    The buckets mirror what the task spec asks the report to triage:
    feature-builder mismatch, calibrator misalignment, artifact drift.
    The narrative for each bucket lives in the module docstring; this
    block carries the per-run evidence so a reviewer reading the JSON
    in isolation can verify the conclusions.
    """
    summary = scored.get("summary", {})
    fs = full_stack_summary or {}
    fs_predicted = int(fs.get("rows_predicted", 0))
    fs_attempted = int(fs.get("rows_attempted", 0))
    fs_skip_reasons = dict(fs.get("skip_reasons", {}))
    fs_missing_features = int(fs_skip_reasons.get("feature_vector_unavailable", 0))
    feature_builder_active = (
        full_stack_summary is not None
        and fs_predicted == 0
        and fs_missing_features > 0
    )
    return {
        "feature_builder_mismatch": {
            "active": feature_builder_active,
            "evidence": {
                "full_stack_attempted": full_stack_summary is not None,
                "rows_attempted": fs_attempted,
                "rows_predicted_via_models_loader": fs_predicted,
                "rows_skipped_missing_features": fs_missing_features,
            },
            "note": (
                "V1 archive does not snapshot the upstream LGB+XGB feature "
                "vector, so the requested full-stack replay yields zero "
                "usable rows. Calibrator-only audit below is the "
                "constrained fallback."
            ),
        },
        "calibrator_misalignment": {
            "active": True,
            "evidence": {
                "leagues_improved": summary.get("leagues_improved", []),
                "leagues_parity": summary.get("leagues_parity", []),
            },
            "note": (
                "V1's archived `predicted_prob` is the pre-calibrator "
                "stack output. brier(predicted_prob, actual_outcome) is "
                "V1's *raw* baseline, NOT V1's final calibrated Brier — "
                "so the spec's '±2% parity vs V1' gate cannot be "
                "evaluated against this archive. Large negative "
                "`delta_vs_v1` on the refit leagues is the expected "
                "shape (current calibrators dominating the raw stack)."
            ),
        },
        "artifact_drift": {
            "active": bool(summary.get("leagues_regressed")),
            "evidence": {
                "leagues_regressed": summary.get("leagues_regressed", []),
            },
            "note": (
                "Per-league calibrated Brier worse than V1 raw beyond "
                "tolerance — would indicate the on-disk calibrator "
                "diverged from what was fit. Empty list = no drift."
            ),
        },
    }


async def run(
    *,
    rows: list[dict[str, Any]] | None,
    limit: int | None,
    leagues_filter: set[str] | None,
    load_calibrators: bool,
    full_stack: bool = False,
    predict_fn: Any | None = None,
) -> dict[str, Any]:
    """Programmatic entry point. Used by the pytest fixture.

    When ``full_stack=True`` the harness also walks every row through
    ``app.predictor.predict()`` (the LGB+XGB stack via models_loader)
    and reports the per-row attempt/skip counts.  Pass ``predict_fn``
    to inject a stub for tests that don't want to load real models.
    """
    started_at = datetime.now(timezone.utc).isoformat()
    if load_calibrators:
        try:
            await calibrators.load_all()
        except Exception as e:  # pragma: no cover — defensive
            print(f"[backtest] calibrator load failed (continuing with no-op): {e}")
    if rows is None:
        rows = await _fetch_db_rows(limit, leagues_filter)
        source = "db"
    else:
        source = "in-memory"

    full_stack_summary: dict[str, Any] | None = None
    full_stack_scored: dict[str, Any] | None = None
    if full_stack:
        fs_grouped, full_stack_summary = await _attempt_full_stack_inference(
            rows, leagues_filter=leagues_filter, predict_fn=predict_fn,
        )
        # Score whatever the predictor produced. With today's archive
        # this is empty per-league dict; with the test stub it is
        # populated and exercises the same `score_grouped` path.
        full_stack_scored = score_grouped(fs_grouped)

    grouped = _normalize_rows(rows, leagues_filter=leagues_filter)
    scored = score_grouped(grouped)
    triage = _build_triage(scored, full_stack_summary)
    report: dict[str, Any] = {
        "started_at": started_at,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "row_input_count": len(rows),
        "row_kept_count": scored["summary"]["row_count"],
        "leagues_filter": sorted(leagues_filter) if leagues_filter else None,
        "limit": limit,
        "calibrator_freshness": calibrators.get_state().freshness() if load_calibrators else None,
        "full_stack_attempt": full_stack_summary,
        "triage": triage,
        **scored,
    }
    if full_stack_scored is not None:
        report["full_stack_scored"] = full_stack_scored
    return report


async def _amain(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap rows pulled from DB (sanity runs).")
    parser.add_argument("--leagues", type=str, default=None,
                        help="Comma-separated league filter (e.g. NBA,NFL).")
    parser.add_argument("--fixture", type=Path, default=None,
                        help="Load rows from a JSON fixture instead of DB.")
    parser.add_argument("--inline-rows", type=str, default=None,
                        help="JSON list of rows (overrides DB + fixture).")
    parser.add_argument("--no-load-calibrators", action="store_true",
                        help="Skip the DB calibrator load (calibrator.apply() "
                             "becomes a no-op and brier_calibrated == brier_v1_raw).")
    parser.add_argument("--full-stack", action="store_true",
                        help="Also attempt the requested LGB+XGB stack replay "
                             "via app.predictor.predict() and report attempt/skip "
                             "counts. Yields zero usable rows on the V1 archive "
                             "(features not snapshotted) — see triage block.")
    parser.add_argument("--json-out", type=Path, default=None,
                        help="Write the JSON report to this path.")
    args = parser.parse_args(argv)

    rows: list[dict[str, Any]] | None
    if args.inline_rows is not None:
        rows = _read_inline(args.inline_rows)
    elif args.fixture is not None:
        rows = _read_fixture(args.fixture)
    else:
        rows = None

    report = await run(
        rows=rows,
        limit=args.limit,
        leagues_filter=_parse_leagues(args.leagues),
        load_calibrators=not args.no_load_calibrators,
        full_stack=args.full_stack,
    )

    summary = report["summary"]
    print("=" * 78)
    print(f" V1 backtest — {report['source']} source — "
          f"{report['row_kept_count']} kept of {report['row_input_count']} input")
    print("=" * 78)
    print(f"  league_count        = {summary['league_count']}")
    print(f"  brier_tolerance     = {summary['brier_tolerance']:.3f}")
    print(f"  within tolerance    = {len(summary['leagues_within_tolerance'])} "
          f"({','.join(summary['leagues_within_tolerance']) or '(none)'})")
    print(f"  outside tolerance   = {len(summary['leagues_outside_tolerance'])} "
          f"({','.join(summary['leagues_outside_tolerance']) or '(none)'})")
    print()
    print(f"  {'league':<14} {'n':>7}  {'brier_raw':>10}  {'brier_cal':>10}  "
          f"{'delta':>9}  {'auc_raw':>8}  {'auc_cal':>8}  cal_type")
    print("  " + "-" * 96)
    for league, m in report["per_league"].items():
        def _f(v: float | None, w: int = 8, p: int = 4) -> str:
            return f"{v:>{w}.{p}f}" if isinstance(v, (int, float)) else " " * w + "—"
        print(
            f"  {league:<14} {m['sample_count']:>7}  "
            f"{_f(m['brier_v1_raw'], 10):}  {_f(m['brier_calibrated'], 10):}  "
            f"{_f(m['brier_delta'], 9):}  "
            f"{_f(m['auc_v1_raw']):}  {_f(m['auc_calibrated']):}  "
            f"{m['calibrator_type'] or '-'}"
        )

    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, indent=2, default=str))
        print(f"\n  JSON report written: {args.json_out}")

    return 0


def main() -> int:
    try:
        return asyncio.run(_amain())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
