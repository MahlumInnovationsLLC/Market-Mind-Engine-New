"""V1 backtest harness — score V1 archive rows against the current
ensemble's calibrators and (optionally) the LGB+XGB stack via
``app.predictor.predict()``.

Three run modes (see ``--help``):

* default — calibrator-only audit on V1's archived ``predicted_prob``.
* ``--full-stack`` — also invoke ``app.predictor.predict()`` per row;
  rows without an archived feature vector are skipped (V1 did not
  snapshot the upstream vector).
* ``--full-stack --surrogate-features`` — DEGRADED stand-in: build a
  live 32-slot vector from V1's ``analysis_json`` (see
  ``_archive_to_candidate``); rest fall to per-spec defaults.

The output JSON carries a top-level ``parity_gate_status`` manifest
with three named gates and an ``overall`` verdict
(``PASS`` / ``DEGRADED_PARTIAL_PENDING_BACKFILL`` /
``BLOCKED_PENDING_BACKFILL`` / ``MIXED``).  Full audit narrative —
why ±2% parity vs V1 calibrated is UNEVALUABLE on this archive,
which gates fold into which verdict, and the canonical evidence
report — lives in ``replit.md`` under the "V1 backtest" section.
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

_HERE = Path(__file__).resolve()
_API_SERVER_ROOT = _HERE.parent.parent
if str(_API_SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_SERVER_ROOT))

from app import calibrators  # noqa: E402

# Side aliases mirror app/calibrators.py::_fetch_training_data so the
# backtest groups rows the same way the nightly refit does.
_NO_SIDES = frozenset({"no", "sell", "live_no", "buy no"})
_YES_SIDES = frozenset({"yes", "buy", "live_yes", "buy yes"})

# |delta| <= tolerance => parity; below => improved; above => regressed.
_BRIER_TOLERANCE = 0.02


def _brier(probs: list[float], ys: list[int]) -> float | None:
    if not probs:
        return None
    return sum((p - y) * (p - y) for p, y in zip(probs, ys)) / len(probs)


def _log_loss(probs: list[float], ys: list[int]) -> float | None:
    """Binary cross-entropy with [eps, 1-eps] clamp to avoid -inf on 0/1."""
    if not probs:
        return None
    eps = 1e-12
    s = 0.0
    for p, y in zip(probs, ys):
        p = max(eps, min(1.0 - eps, p))
        s += -(y * math.log(p) + (1 - y) * math.log(1.0 - p))
    return s / len(probs)


def _auc(probs: list[float], ys: list[int]) -> float | None:
    """ROC AUC via Mann–Whitney U with average-rank tie handling."""
    if not probs or not ys:
        return None
    pos = [p for p, y in zip(probs, ys) if y == 1]
    neg = [p for p, y in zip(probs, ys) if y == 0]
    if not pos or not neg:
        return None
    pairs = sorted(
        ((float(p), 1) for p in pos), key=lambda t: t[0]
    ) + sorted(((float(p), 0) for p in neg), key=lambda t: t[0])
    pairs.sort(key=lambda t: t[0])
    ranks: list[float] = [0.0] * len(pairs)
    i = 0
    while i < len(pairs):
        j = i
        while j + 1 < len(pairs) and pairs[j + 1][0] == pairs[i][0]:
            j += 1
        avg_rank = (i + 1 + j + 1) / 2.0
        for k in range(i, j + 1):
            ranks[k] = avg_rank
        i = j + 1
    rank_sum_pos = sum(r for r, (_p, y) in zip(ranks, pairs) if y == 1)
    n_pos = len(pos)
    n_neg = len(neg)
    u = rank_sum_pos - n_pos * (n_pos + 1) / 2.0
    return u / (n_pos * n_neg)


def _side_align(predicted_prob: float, side: str | None) -> float | None:
    """Flip ``predicted_prob`` to side-aligned. Returns ``None`` for unknown sides."""
    s = (side or "").strip().lower()
    if s in _NO_SIDES:
        return max(0.0, min(1.0, 1.0 - float(predicted_prob)))
    if s in _YES_SIDES:
        return max(0.0, min(1.0, float(predicted_prob)))
    return None


def _resolve_side(row: dict[str, Any]) -> str | None:
    side = row.get("side")
    if side is not None:
        return side
    aj = row.get("analysis_json")
    if isinstance(aj, str):
        try:
            aj = json.loads(aj)
        except (ValueError, TypeError):
            return None
    if isinstance(aj, dict):
        return aj.get("side")
    return None


def _normalize_rows(
    rows: list[dict[str, Any]],
    leagues_filter: set[str] | None = None,
) -> dict[str, dict[str, list]]:
    """Group raw rows into ``{league: {"probs":[...], "ys":[...]}}``."""
    grouped: dict[str, dict[str, list]] = {}
    _normalize_into(grouped, rows, leagues_filter)
    return grouped


# Sentinel league values V1 wrote when its own per-trade heuristic
# failed.  Treated identically to NULL so the calibrator-fit harness
# does NOT silently bucket ~99% of imported rows into a synthetic
# ``UNKNOWN`` league the per-league calibrator stack can never serve.
# See task #740 + ``app/v1_archive/league_inference.py``.
_LEAGUE_UNRESOLVED_LITERALS: frozenset[str] = frozenset(
    {"", "UNKNOWN", "NONE", "NULL"}
)


def _normalize_into(
    grouped: dict[str, dict[str, list]],
    rows: list[dict[str, Any]],
    leagues_filter: set[str] | None,
    *,
    unresolved_counter: dict[str, int] | None = None,
) -> int:
    """Merge ``rows`` into ``grouped`` in place. Returns rows appended.

    Rows whose ``league`` is the V1 ``unknown``/NULL sentinel are
    SKIPPED — they're counted into the report's
    ``league_unresolved_count`` instead of bucketed into a synthetic
    ``UNKNOWN`` league the calibrator stack can never serve.

    ``unresolved_counter``, when provided, is bumped under the keys
    ``"null"`` (Python ``None``/empty) and ``"literal_unknown"`` (the
    V1 ``'unknown'``/``'NONE'``/``'NULL'`` sentinel strings) so the
    report can distinguish the two failure modes for task #740 audit.
    """
    appended = 0
    for r in rows:
        raw_league = r.get("league")
        league = (raw_league or "").strip().upper()
        # Task #754: V1 crypto rows landed with the V1 ``unknown`` /
        # NULL ``league`` sentinel because no inference rule covered
        # KXBTC/KXETH.  They DO carry ``vertical='crypto'`` though,
        # which the calibrator treats as the ``CRYPTO`` per-league
        # bucket.  Promote those rows to ``league='CRYPTO'`` so the
        # harness reports a CRYPTO Brier delta instead of dropping
        # ~595k rows into ``literal_unknown``.
        vertical = (r.get("vertical") or "").strip().lower()
        is_crypto_vertical = vertical == "crypto"
        if (not league or league in _LEAGUE_UNRESOLVED_LITERALS) and is_crypto_vertical:
            league = "CRYPTO"
        elif not league:
            if unresolved_counter is not None:
                unresolved_counter["null"] = unresolved_counter.get("null", 0) + 1
            continue
        elif league in _LEAGUE_UNRESOLVED_LITERALS:
            if unresolved_counter is not None:
                unresolved_counter["literal_unknown"] = (
                    unresolved_counter.get("literal_unknown", 0) + 1
                )
            continue
        if leagues_filter and league not in leagues_filter:
            continue
        prob_raw = r.get("predicted_prob")
        outcome_raw = r.get("actual_outcome")
        if prob_raw is None or outcome_raw is None:
            continue
        # Crypto V1 rows have no ``side`` (no analysis_json side either):
        # the V1 ``predicted_prob`` is the YES-side probability already,
        # so treat side=YES when missing rather than dropping the row.
        side = _resolve_side(r)
        if side is None and league == "CRYPTO":
            side = "yes"
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
        appended += 1
    return appended


_DEFAULT_BATCH_SIZE = 50_000


async def _fetch_db_rows_batched(
    limit: int | None,
    leagues_filter: set[str] | None,
    batch_size: int = _DEFAULT_BATCH_SIZE,
):
    """Yield batches of v1_archive.trade_analyses rows (keyset paginated on id)."""
    from app import db  # noqa: PLC0415

    present = await db.fetchval(
        "SELECT to_regclass('v1_archive.trade_analyses') IS NOT NULL"
    )
    if not present:
        print("[backtest] v1_archive.trade_analyses not found; nothing to do.")
        return

    # The original (sports / mixed-vertical) path requires a resolved
    # ``league`` because side-alignment depends on it.  V1 crypto rows
    # (task #754) were imported with the V1 ``league='unknown'``
    # sentinel — there's no inference rule that maps them — but they
    # carry ``vertical='crypto'`` so we can synthesise
    # ``league='CRYPTO'`` downstream in ``_normalize_into``.  Hence
    # the WHERE clause accepts EITHER a resolved league OR the
    # crypto vertical.
    where_static: list[str] = [
        "predicted_prob IS NOT NULL",
        "actual_outcome IS NOT NULL",
        "(league IS NOT NULL OR lower(coalesce(vertical, '')) = 'crypto')",
    ]
    if leagues_filter:
        cleaned = ",".join(
            "'" + lg.replace("'", "''").upper() + "'" for lg in leagues_filter
        )
        # When CRYPTO is in the leagues filter we must keep the
        # crypto-vertical rows even if their ``league`` column is the
        # V1 unresolved sentinel — otherwise the filter masks them out.
        if "CRYPTO" in {lg.upper() for lg in leagues_filter}:
            where_static.append(
                f"(upper(coalesce(league, '')) IN ({cleaned}) "
                f"OR lower(coalesce(vertical, '')) = 'crypto')"
            )
        else:
            where_static.append(f"upper(league) IN ({cleaned})")

    last_id: str | None = None
    total_yielded = 0
    bs = max(1, int(batch_size))
    while True:
        if limit is not None:
            remaining = limit - total_yielded
            if remaining <= 0:
                return
            this_batch = min(bs, remaining)
        else:
            this_batch = bs

        where_clauses = list(where_static)
        params: list[Any] = []
        if last_id is not None:
            where_clauses.append("id > $1")
            params.append(last_id)
        sql = (
            "SELECT id, league, vertical, predicted_prob, actual_outcome, "
            "analysis_json "
            "FROM v1_archive.trade_analyses "
            f"WHERE {' AND '.join(where_clauses)} "
            f"ORDER BY id ASC LIMIT {int(this_batch)}"
        )
        rows = await db.fetch(sql, *params) if params else await db.fetch(sql)
        if not rows:
            return
        batch_rows = [dict(r) for r in rows]
        last_id = batch_rows[-1]["id"]
        total_yielded += len(batch_rows)
        yield batch_rows
        if len(batch_rows) < this_batch:
            return


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


def _extract_feature_vector(row: dict[str, Any]) -> list[float] | None:
    """Pull a feature vector out of an archived row, if one was snapshotted."""
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


_LIVE_FEATURE_COUNT = 32


def _archive_to_candidate(
    row: dict[str, Any],
) -> tuple[dict[str, Any], float | None] | None:
    """Map a V1 archive row to a live-shaped candidate dict + kelly override."""
    aj = row.get("analysis_json")
    if isinstance(aj, str):
        try:
            aj = json.loads(aj)
        except (ValueError, TypeError):
            aj = None
    if not isinstance(aj, dict):
        aj = {}

    pred_prob = row.get("predicted_prob")
    if pred_prob is None:
        pred_prob = aj.get("predicted_prob")
    if pred_prob is None:
        return None
    try:
        pred_prob = float(pred_prob)
    except (TypeError, ValueError):
        return None

    raw_side = aj.get("side")
    if raw_side is None:
        return None
    s = str(raw_side).strip().lower()
    if s in _NO_SIDES:
        side = "no"
    elif s in _YES_SIDES:
        side = "yes"
    else:
        return None

    candidate: dict[str, Any] = {
        "consensus_probability": pred_prob,
        "market_prob": aj.get("market_implied_prob"),
        "signal_strength": aj.get("signal_strength"),
        "edge": aj.get("edge_at_entry"),
        "side": side,
        "league": (row.get("league") or "").strip().upper() or None,
        "market_type": row.get("market_type"),
    }
    candidate = {k: v for k, v in candidate.items() if v is not None}

    kelly_override: float | None = None
    ku = aj.get("kelly_used")
    if ku is not None:
        try:
            kelly_override = float(ku)
        except (TypeError, ValueError):
            kelly_override = None
    return candidate, kelly_override


def _build_surrogate_vector(row: dict[str, Any]) -> list[float] | None:
    """Build a live 32-slot feature vector from a V1 archive row."""
    mapped = _archive_to_candidate(row)
    if mapped is None:
        return None
    candidate, kelly_override = mapped
    from app.features.vector import build_vector  # noqa: PLC0415

    fv = build_vector(candidate, kelly=kelly_override)
    return list(fv.values)


async def _attempt_full_stack_inference(
    rows: list[dict[str, Any]],
    leagues_filter: set[str] | None,
    predict_fn: Any | None = None,
    surrogate_features: bool = False,
    feature_builder: Any | None = None,
) -> tuple[dict[str, dict[str, list]], dict[str, Any]]:
    """Run the LGB+XGB stack via ``app.predictor`` per row.

    Returns ``(grouped, summary)`` with per-row attempt/predicted/skipped
    counts, skip-reason buckets, and an ``archived`` vs ``surrogate``
    feature-source split.
    """
    if predict_fn is None:
        from app import predictor  # noqa: PLC0415
        predict_fn = predictor.predict

    if surrogate_features and feature_builder is None:
        feature_builder = _build_surrogate_vector

    grouped: dict[str, dict[str, list]] = {}
    skip_reasons: dict[str, int] = {}
    feature_source_counts: dict[str, int] = {"archived": 0, "surrogate": 0}
    attempted = predicted = skipped = 0
    error_samples: list[dict[str, Any]] = []

    def _bump(reason: str) -> None:
        skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

    for r in rows:
        league = (r.get("league") or "").strip().upper()
        if not league or league in _LEAGUE_UNRESOLVED_LITERALS:
            # Treat the V1 ``unknown``/NULL sentinel identically to a
            # missing league so the full-stack pass doesn't try to
            # apply a calibrator that can never match (task #740).
            skipped += 1
            _bump("missing_league")
            continue
        if leagues_filter and league not in leagues_filter:
            continue
        outcome_raw = r.get("actual_outcome")
        if outcome_raw is None:
            skipped += 1
            _bump("missing_outcome")
            continue
        try:
            y = int(outcome_raw)
        except (TypeError, ValueError):
            skipped += 1
            _bump("non_binary_outcome")
            continue
        if y not in (0, 1):
            skipped += 1
            _bump("non_binary_outcome")
            continue
        # Capture V1's archived (raw, side-aligned) probability so the
        # paired full_stack vs V1 delta is computed on the same row set.
        v1_prob_raw = r.get("predicted_prob")
        if v1_prob_raw is None:
            skipped += 1
            _bump("missing_v1_predicted_prob")
            continue
        try:
            v1_prob_aligned = _side_align(float(v1_prob_raw), _resolve_side(r))
        except (TypeError, ValueError):
            v1_prob_aligned = None
        if v1_prob_aligned is None:
            skipped += 1
            _bump("v1_prob_unaligned_side")
            continue

        features = _extract_feature_vector(r)
        feature_source = "archived" if features is not None else None
        if features is None and feature_builder is not None:
            try:
                features = feature_builder(r)
            except Exception as e:  # pragma: no cover
                skipped += 1
                _bump(f"surrogate_build_error_{type(e).__name__}")
                if len(error_samples) < 5:
                    error_samples.append({
                        "league": league, "outcome": y, "error": str(e),
                        "type": type(e).__name__, "source": "surrogate_builder",
                    })
                continue
            if features is not None:
                feature_source = "surrogate"
        if features is None:
            skipped += 1
            _bump("feature_vector_unavailable")
            continue
        feature_source_counts[feature_source] = (
            feature_source_counts.get(feature_source, 0) + 1
        )

        attempted += 1
        try:
            pred = await predict_fn(features, league=league)
        except Exception as e:  # pragma: no cover
            skipped += 1
            _bump(f"predict_error_{type(e).__name__}")
            if len(error_samples) < 5:
                error_samples.append({"league": league, "error": f"{type(e).__name__}: {e}"})
            continue

        if hasattr(pred, "probability"):
            p = float(pred.probability)
        elif isinstance(pred, dict) and "probability" in pred:
            p = float(pred["probability"])
        else:
            skipped += 1
            _bump("bad_predict_shape")
            continue
        p = max(0.0, min(1.0, p))
        bucket = grouped.setdefault(
            league, {"probs": [], "ys": [], "v1_probs": [], "feature_sources": []}
        )
        bucket["probs"].append(p)
        bucket["ys"].append(y)
        bucket["v1_probs"].append(v1_prob_aligned)
        bucket["feature_sources"].append(feature_source)
        predicted += 1

    return grouped, {
        "rows_attempted": attempted,
        "rows_predicted": predicted,
        "rows_skipped": skipped,
        "skip_reasons": skip_reasons,
        "error_samples": error_samples,
        "feature_source": feature_source_counts,
    }


def score_grouped(
    grouped: dict[str, dict[str, list]],
    *,
    apply_calibrator: bool = True,
) -> dict[str, Any]:
    """Compute per-league raw + calibrated metrics. Pure function — no IO.

    ``apply_calibrator=False`` is for the full-stack path: probabilities
    were already calibrated by ``app.predictor.predict()``, so re-running
    them through ``calibrators.apply()`` would double-calibrate. In that
    mode raw == calibrated and ``brier_delta == 0``.
    """
    per_league: dict[str, Any] = {}
    leagues_parity: list[str] = []
    leagues_improved: list[str] = []
    leagues_regressed: list[str] = []
    for league in sorted(grouped.keys()):
        probs = grouped[league]["probs"]
        ys = grouped[league]["ys"]
        n = len(probs)
        if apply_calibrator:
            calibrated_pairs = [calibrators.apply(p, league) for p in probs]
            cal_probs = [cp for cp, _info in calibrated_pairs]
            applied_count = sum(1 for _cp, info in calibrated_pairs if info.get("applied"))
            cal_type = next(
                (info.get("type") for _cp, info in calibrated_pairs if info.get("applied")),
                None,
            )
        else:
            cal_probs = list(probs)
            applied_count = 0
            cal_type = None

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
            "delta_vs_v1": delta,  # spec-named alias of brier_delta
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


def score_full_stack_paired(
    fs_grouped: dict[str, dict[str, list]],
) -> dict[str, Any]:
    """Per-league paired metrics: full_stack vs V1 archived predicted_prob.

    For each league, computes brier/log_loss/auc on BOTH the full-stack
    model output and V1's archived predicted_prob, restricted to the same
    row set (only rows where the full-stack stack actually predicted).

    ``delta_vs_v1 = brier_full_stack - brier_v1_baseline`` is the spec's
    parity metric. Tolerance is ±_BRIER_TOLERANCE (2pp). Per-league
    ``feature_source_breakdown`` flags whether the row set rests on real
    archived feature vectors or on the degraded surrogate path.
    """
    per_league: dict[str, Any] = {}
    leagues_parity: list[str] = []
    leagues_improved: list[str] = []
    leagues_regressed: list[str] = []
    for league in sorted(fs_grouped.keys()):
        bucket = fs_grouped[league]
        fs_probs = bucket.get("probs", [])
        ys = bucket.get("ys", [])
        v1_probs = bucket.get("v1_probs", [])
        sources = bucket.get("feature_sources", [])
        n = len(fs_probs)
        # Drop rows where v1_prob was missing (paired metric requires both).
        paired = [
            (fs, v1, y) for fs, v1, y in zip(fs_probs, v1_probs, ys) if v1 is not None
        ]
        if not paired:
            per_league[league] = {
                "sample_count": n,
                "paired_sample_count": 0,
                "brier_full_stack": None,
                "brier_v1_baseline": None,
                "delta_vs_v1": None,
                "log_loss_full_stack": None,
                "log_loss_v1_baseline": None,
                "auc_full_stack": None,
                "auc_v1_baseline": None,
                "within_brier_tolerance": None,
                "verdict": None,
                "feature_source_breakdown": _count_sources(sources),
            }
            continue
        fs_p = [p for p, _v, _y in paired]
        v1_p = [v for _p, v, _y in paired]
        ys_p = [y for _p, _v, y in paired]
        brier_fs = _brier(fs_p, ys_p)
        brier_v1 = _brier(v1_p, ys_p)
        log_fs = _log_loss(fs_p, ys_p)
        log_v1 = _log_loss(v1_p, ys_p)
        auc_fs = _auc(fs_p, ys_p)
        auc_v1 = _auc(v1_p, ys_p)
        delta = (brier_fs - brier_v1) if (brier_fs is not None and brier_v1 is not None) else None
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
            "paired_sample_count": len(paired),
            "brier_full_stack": brier_fs,
            "brier_v1_baseline": brier_v1,
            "delta_vs_v1": delta,
            "log_loss_full_stack": log_fs,
            "log_loss_v1_baseline": log_v1,
            "auc_full_stack": auc_fs,
            "auc_v1_baseline": auc_v1,
            "within_brier_tolerance": within_tol,
            "verdict": verdict,
            "feature_source_breakdown": _count_sources(sources),
        }
    return {
        "per_league": per_league,
        "summary": {
            "league_count": len(per_league),
            "row_count": sum(v["sample_count"] for v in per_league.values()),
            "paired_row_count": sum(v["paired_sample_count"] for v in per_league.values()),
            "brier_tolerance": _BRIER_TOLERANCE,
            "leagues_parity": leagues_parity,
            "leagues_improved": leagues_improved,
            "leagues_regressed": leagues_regressed,
            "leagues_within_tolerance": leagues_parity,
            "leagues_outside_tolerance": leagues_improved + leagues_regressed,
        },
    }


def _count_sources(sources: list[str]) -> dict[str, int]:
    out: dict[str, int] = {"archived": 0, "surrogate": 0}
    for s in sources:
        out[s] = out.get(s, 0) + 1
    return out


def _parse_leagues(arg: str | None) -> set[str] | None:
    if not arg:
        return None
    out = {x.strip().upper() for x in arg.split(",") if x.strip()}
    return out or None


def _build_parity_2pct_gate(
    *,
    fss_paired_count: int,
    archived_used: int,
    surrogate_used: int,
    surrogate_enabled: bool,
    fss_per_league: dict[str, Any],
    fss_leagues_parity: list[str],
    fss_leagues_regressed: list[str],
) -> dict[str, Any]:
    """Decide PASS/FAIL/UNEVALUABLE for the parity-vs-V1 hard gate.

    Decision tree:
      - No paired data → UNEVALUABLE (full_stack never ran).
      - Paired data but only via surrogate → EVALUABLE_DEGRADED with
        DEGRADED_PASS / DEGRADED_FAIL verdict on the surrogate baseline.
      - Paired data via real archived features → EVALUABLE with PASS/FAIL.
    Baseline is V1's archived ``predicted_prob`` (RAW, pre-calibrator);
    the spec wording says "V1 calibrated" but V1 only persisted its
    pre-calibrator output — see replit.md for the audit narrative.
    """
    if fss_paired_count == 0:
        return {
            "status": "UNEVALUABLE",
            "blocked_by": "full_stack predict() produced zero paired rows",
            "note": "Run with --full-stack to populate this gate.",
        }
    per_league_deltas = {
        lg: {
            "delta_vs_v1": m.get("delta_vs_v1"),
            "within_brier_tolerance": m.get("within_brier_tolerance"),
            "verdict": m.get("verdict"),
            "paired_sample_count": m.get("paired_sample_count"),
        }
        for lg, m in fss_per_league.items()
    }
    fail = bool(fss_leagues_regressed)
    if archived_used > 0 and surrogate_used == 0:
        verdict = "FAIL" if fail else "PASS"
        status = "EVALUABLE"
        baseline_caveat = (
            "Baseline = V1's archived predicted_prob (raw, pre-calibrator); "
            "V1 did not persist its post-calibrator output."
        )
    else:
        verdict = "DEGRADED_FAIL" if fail else "DEGRADED_PASS"
        status = "EVALUABLE_DEGRADED"
        baseline_caveat = (
            "DEGRADED baseline: full_stack ran on surrogate features (~10/32 "
            "slots from V1, rest defaulted), against V1's RAW predicted_prob. "
            "Not a final-form parity verdict — wait on #731 for archived "
            "feature snapshots."
        )
    return {
        "status": status,
        "verdict": verdict,
        "evidence": {
            "paired_row_count": fss_paired_count,
            "rows_via_archived_features": archived_used,
            "rows_via_surrogate_features": surrogate_used,
            "leagues_parity": fss_leagues_parity,
            "leagues_regressed": fss_leagues_regressed,
            "per_league_delta_vs_v1": per_league_deltas,
        },
        "tolerance_pp": _BRIER_TOLERANCE,
        "baseline_caveat": baseline_caveat,
    }


def _build_parity_gate_status(
    scored: dict[str, Any],
    full_stack_summary: dict[str, Any] | None,
    full_stack_scored: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Top-level hard-gate manifest: 3 named gates + an ``overall`` verdict."""
    fs = full_stack_summary or {}
    fs_attempted = int(fs.get("rows_attempted", 0))
    fs_skip = dict(fs.get("skip_reasons", {}))
    fs_missing = int(fs_skip.get("feature_vector_unavailable", 0))
    surrogate_enabled = bool(fs.get("surrogate_features_enabled", False))
    feature_source = fs.get("feature_source") or {}
    surrogate_used = int(feature_source.get("surrogate", 0))
    archived_used = int(feature_source.get("archived", 0))

    fss = full_stack_scored or {}
    fss_summary = fss.get("summary", {}) if fss else {}
    fss_per_league = fss.get("per_league", {}) if fss else {}
    fss_paired_count = int(fss_summary.get("paired_row_count", 0))
    fss_leagues_parity = fss_summary.get("leagues_parity", [])
    fss_leagues_improved = fss_summary.get("leagues_improved", [])
    fss_leagues_regressed = fss_summary.get("leagues_regressed", [])

    gates: dict[str, Any] = {
        "full_stack_replay_via_archived_features": {
            "status": "EVALUABLE" if archived_used > 0 else "BLOCKED",
            # Availability gate: PASS once the archived-features replay path
            # produced any rows. Without an explicit verdict, _gate_class()
            # would (incorrectly) treat status=EVALUABLE as fail once #731
            # backfill activates this path.
            "verdict": "PASS" if archived_used > 0 else None,
            "blocked_by": (
                None if archived_used > 0
                else "#731 (V1 feature snapshot dumps required)"
            ),
            "evidence": {
                "rows_attempted": fs_attempted,
                "rows_predicted_via_archived_features": archived_used,
                "rows_skipped_missing_features": fs_missing,
            },
            "note": "V1 archive does not snapshot the upstream LGB+XGB feature vector.",
        },
        "full_stack_replay_via_surrogate_features": {
            "status": (
                "EVALUABLE_DEGRADED" if surrogate_enabled and surrogate_used > 0
                else "NOT_RUN"
            ),
            "evidence": {
                "surrogate_features_enabled": surrogate_enabled,
                "rows_predicted_via_surrogate": surrogate_used,
                # Per-league breakdown of the *paired* full_stack-vs-V1 metric
                # (only populated when full_stack_scored was computed).
                "leagues_improved": fss_leagues_improved,
                "leagues_parity": fss_leagues_parity,
                "leagues_regressed": fss_leagues_regressed,
            },
            "caveat": (
                "DEGRADED baseline: ~10 of 32 feature slots populated from "
                "V1; rest fall to per-spec defaults. Compares against V1's "
                "*raw* predicted_prob, not its final calibrated probability."
            ),
        },
        "parity_within_2pct_vs_v1_calibrated": _build_parity_2pct_gate(
            fss_paired_count=fss_paired_count,
            archived_used=archived_used,
            surrogate_used=surrogate_used,
            surrogate_enabled=surrogate_enabled,
            fss_per_league=fss_per_league,
            fss_leagues_parity=fss_leagues_parity,
            fss_leagues_regressed=fss_leagues_regressed,
        ),
    }
    # Two-tier classification: status (gate evaluability) and verdict
    # (pass/fail when evaluable). Verdict can be PASS / FAIL /
    # DEGRADED_PASS / DEGRADED_FAIL or absent.
    def _gate_class(g: dict[str, Any]) -> str:
        st = g.get("status")
        v = g.get("verdict")
        if st == "PASS":
            return "pass"
        if st == "EVALUABLE":
            return "pass" if v == "PASS" else "fail"
        if st == "EVALUABLE_DEGRADED":
            return "degraded_fail" if v == "DEGRADED_FAIL" else "degraded_pass"
        if st in {"BLOCKED", "UNEVALUABLE"}:
            return "blocked"
        if st == "NOT_RUN":
            return "blocked"  # folds into blocked semantics
        return "blocked"

    counts: dict[str, int] = {
        "pass": 0, "fail": 0, "degraded_pass": 0, "degraded_fail": 0, "blocked": 0,
    }
    for g in gates.values():
        counts[_gate_class(g)] += 1

    pass_count = counts["pass"]
    fail_count = counts["fail"]
    degraded_pass_count = counts["degraded_pass"]
    degraded_fail_count = counts["degraded_fail"]
    blocked_count = counts["blocked"]
    degraded_count = degraded_pass_count + degraded_fail_count
    blocked_or_unevaluable = sum(
        1 for g in gates.values() if g["status"] in {"BLOCKED", "UNEVALUABLE", "NOT_RUN"}
    )

    if fail_count > 0:
        overall = "FAIL"
    elif pass_count == len(gates):
        overall = "PASS"
    elif degraded_fail_count > 0 and pass_count + degraded_pass_count == 0:
        overall = "DEGRADED_FAIL_PENDING_BACKFILL"
    elif degraded_count > 0 and pass_count == 0:
        overall = "DEGRADED_PARTIAL_PENDING_BACKFILL"
    elif blocked_count == len(gates):
        overall = "BLOCKED_PENDING_BACKFILL"
    else:
        overall = "MIXED"

    return {
        "overall": overall,
        "overall_blocked_by": (
            "#731 (V1 feature snapshot dumps)" if blocked_or_unevaluable > 0 else None
        ),
        "summary": {
            "total_gates": len(gates),
            "blocked_or_unevaluable": blocked_or_unevaluable,
            "evaluable_degraded": degraded_count,
            "evaluable_degraded_pass": degraded_pass_count,
            "evaluable_degraded_fail": degraded_fail_count,
            "pass": pass_count,
            "fail": fail_count,
        },
        "gates": gates,
    }


def _build_triage(
    scored: dict[str, Any],
    full_stack_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    """Per-run evidence for the three triage buckets (machine-readable)."""
    summary = scored.get("summary", {})
    fs = full_stack_summary or {}
    fs_predicted = int(fs.get("rows_predicted", 0))
    fs_attempted = int(fs.get("rows_attempted", 0))
    fs_skip_reasons = dict(fs.get("skip_reasons", {}))
    fs_missing_features = int(fs_skip_reasons.get("feature_vector_unavailable", 0))
    surrogate_enabled = bool(fs.get("surrogate_features_enabled", False))
    surrogate_used = int((fs.get("feature_source") or {}).get("surrogate", 0))
    feature_builder_active = (
        full_stack_summary is not None
        and fs_predicted == 0
        and fs_missing_features > 0
    )
    if surrogate_enabled and surrogate_used > 0:
        note = (
            f"--surrogate-features active: {surrogate_used} archive rows "
            "mapped through the degraded build_vector path. NOT V1 parity."
        )
    else:
        note = (
            "V1 archive lacks the upstream feature vector; full-stack replay "
            "yields zero usable rows. Pass --surrogate-features for the "
            "degraded stand-in, or wait on #731 for real V1 feature dumps."
        )
    return {
        "feature_builder_mismatch": {
            "active": feature_builder_active,
            "evidence": {
                "full_stack_attempted": full_stack_summary is not None,
                "rows_attempted": fs_attempted,
                "rows_predicted_via_models_loader": fs_predicted,
                "rows_skipped_missing_features": fs_missing_features,
                "surrogate_features_enabled": surrogate_enabled,
                "rows_predicted_via_surrogate": surrogate_used,
            },
            "note": note,
        },
        "calibrator_misalignment": {
            "active": True,
            "evidence": {
                "leagues_improved": summary.get("leagues_improved", []),
                "leagues_parity": summary.get("leagues_parity", []),
            },
            "note": (
                "V1's archived predicted_prob is pre-calibrator stack output, "
                "so brier(predicted_prob, actual_outcome) is V1's raw baseline "
                "— not its final calibrated Brier."
            ),
        },
        "artifact_drift": {
            "active": bool(summary.get("leagues_regressed")),
            "evidence": {
                "leagues_regressed": summary.get("leagues_regressed", []),
            },
            "note": (
                "Per-league calibrated Brier worse than V1 raw beyond "
                "tolerance — would indicate on-disk calibrator drift."
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
    batch_size: int = _DEFAULT_BATCH_SIZE,
    surrogate_features: bool = False,
    feature_builder: Any | None = None,
) -> dict[str, Any]:
    """Programmatic entry point. DB mode walks v1_archive in keyset batches."""
    started_at = datetime.now(timezone.utc).isoformat()
    if load_calibrators:
        try:
            await calibrators.load_all()
        except Exception as e:  # pragma: no cover
            print(f"[backtest] calibrator load failed (continuing with no-op): {e}")

    grouped: dict[str, dict[str, list]] = {}
    fs_grouped: dict[str, dict[str, list]] = {}
    full_stack_summary: dict[str, Any] | None = (
        {
            "rows_attempted": 0,
            "rows_predicted": 0,
            "rows_skipped": 0,
            "skip_reasons": {},
            "error_samples": [],
            "feature_source": {"archived": 0, "surrogate": 0},
            "surrogate_features_enabled": bool(surrogate_features),
        }
        if full_stack
        else None
    )
    row_input_count = 0
    batch_count = 0
    # Task #740: distinguish NULL vs literal "unknown" sentinel rows
    # so the report shows whether the V1 archive backfill needs to
    # run before the calibrator-fit step trusts these results.
    unresolved_counter: dict[str, int] = {"null": 0, "literal_unknown": 0}

    async def _process_batch(batch_rows: list[dict[str, Any]]) -> None:
        nonlocal batch_count
        batch_count += 1
        _normalize_into(
            grouped, batch_rows, leagues_filter=leagues_filter,
            unresolved_counter=unresolved_counter,
        )
        if full_stack and full_stack_summary is not None:
            fs_g, fs_s = await _attempt_full_stack_inference(
                batch_rows,
                leagues_filter=leagues_filter,
                predict_fn=predict_fn,
                surrogate_features=surrogate_features,
                feature_builder=feature_builder,
            )
            for lg, bucket in fs_g.items():
                target = fs_grouped.setdefault(
                    lg, {"probs": [], "ys": [], "v1_probs": [], "feature_sources": []}
                )
                target["probs"].extend(bucket["probs"])
                target["ys"].extend(bucket["ys"])
                target["v1_probs"].extend(bucket.get("v1_probs", []))
                target["feature_sources"].extend(bucket.get("feature_sources", []))
            full_stack_summary["rows_attempted"] += fs_s["rows_attempted"]
            full_stack_summary["rows_predicted"] += fs_s["rows_predicted"]
            full_stack_summary["rows_skipped"] += fs_s["rows_skipped"]
            for k, v in fs_s["skip_reasons"].items():
                full_stack_summary["skip_reasons"][k] = (
                    full_stack_summary["skip_reasons"].get(k, 0) + v
                )
            for k, v in fs_s.get("feature_source", {}).items():
                full_stack_summary["feature_source"][k] = (
                    full_stack_summary["feature_source"].get(k, 0) + v
                )
            for sample in fs_s["error_samples"]:
                if len(full_stack_summary["error_samples"]) < 5:
                    full_stack_summary["error_samples"].append(sample)

    if rows is None:
        source = "db"
        async for batch in _fetch_db_rows_batched(
            limit=limit, leagues_filter=leagues_filter, batch_size=batch_size,
        ):
            row_input_count += len(batch)
            await _process_batch(batch)
    else:
        source = "in-memory"
        row_input_count = len(rows)
        bs = max(1, int(batch_size))
        for start in range(0, len(rows), bs):
            await _process_batch(rows[start:start + bs])

    scored = score_grouped(grouped)
    full_stack_scored: dict[str, Any] | None = None
    if full_stack:
        full_stack_scored = score_full_stack_paired(fs_grouped)

    triage = _build_triage(scored, full_stack_summary)
    parity_gate_status = _build_parity_gate_status(
        scored, full_stack_summary, full_stack_scored,
    )
    report: dict[str, Any] = {
        "started_at": started_at,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "row_input_count": row_input_count,
        "row_kept_count": scored["summary"]["row_count"],
        "batch_count": batch_count,
        "batch_size": batch_size,
        # Task #740: rows whose ``league`` is NULL or the V1 sentinel
        # ``'unknown'`` are NOT bucketed any longer; they're surfaced
        # here so the operator can see whether
        # ``scripts/backfill_v1_archive_leagues.py`` still needs to
        # run.  Sum of the two equals ``row_input_count - row_kept_count``
        # minus rows skipped for other reasons (missing prob/outcome,
        # bad side, etc.).
        "league_unresolved_count": (
            unresolved_counter["null"] + unresolved_counter["literal_unknown"]
        ),
        "league_unresolved_breakdown": dict(unresolved_counter),
        "leagues_filter": sorted(leagues_filter) if leagues_filter else None,
        "limit": limit,
        "calibrator_freshness": calibrators.get_state().freshness() if load_calibrators else None,
        "parity_gate_status": parity_gate_status,
        "full_stack_attempt": full_stack_summary,
        "triage": triage,
        # ``per_league``/``summary`` below are the CALIBRATOR-ONLY audit:
        # V1 archived predicted_prob → current calibrators. ``brier_v1_raw``
        # is V1's pre-calibrator output, ``brier_calibrated`` is that same
        # prob run through current calibrators. NOT the spec's full-stack
        # parity metric — that lives in ``full_stack_scored.per_league``.
        "audit_path": "calibrator_only_via_v1_predicted_prob",
        **scored,
    }
    if full_stack_scored is not None:
        # ``full_stack_scored.per_league[lg]`` carries:
        #   brier_full_stack vs brier_v1_baseline (paired on same row set)
        #   delta_vs_v1 = brier_full_stack - brier_v1_baseline
        # plus a feature_source_breakdown flagging archived vs surrogate.
        full_stack_scored["audit_path"] = "full_stack_paired_vs_v1_archived_predicted_prob"
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
                        help="Skip the DB calibrator load (apply() becomes no-op).")
    parser.add_argument("--full-stack", action="store_true",
                        help="Also run app.predictor.predict() per row.")
    parser.add_argument("--surrogate-features", action="store_true",
                        help="With --full-stack: build surrogate features when "
                             "row lacks an archived vector. DEGRADED baseline.")
    parser.add_argument("--batch-size", type=int, default=_DEFAULT_BATCH_SIZE,
                        help=f"Rows per DB batch (default {_DEFAULT_BATCH_SIZE}).")
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

    if args.full_stack:
        try:
            from app import models_loader  # noqa: PLC0415
            await models_loader.ensure_artifacts()
            print("[backtest] models_loader.ensure_artifacts() OK")
        except Exception as e:
            print(f"[backtest] models_loader.ensure_artifacts() FAILED: "
                  f"{type(e).__name__}: {e}")

    report = await run(
        rows=rows,
        limit=args.limit,
        leagues_filter=_parse_leagues(args.leagues),
        load_calibrators=not args.no_load_calibrators,
        full_stack=args.full_stack,
        batch_size=args.batch_size,
        surrogate_features=args.surrogate_features,
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
