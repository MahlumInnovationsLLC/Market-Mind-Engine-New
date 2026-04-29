"""V1 backtest harness — replay V1's historical predictions through the
current per-league calibrators and report parity vs V1's raw baseline.

What this script does
---------------------

For every (league, predicted_prob, actual_outcome) row in
``v1_archive.trade_analyses`` (populated by ``import_v1_history.py``):

1. **Side-flip ``predicted_prob``** to match the side V1 actually bet
   on, using ``analysis_json->>'side'`` (the same canonicalization the
   calibrator-fit query in ``app/calibrators.py::_fetch_training_data``
   uses).  ``actual_outcome`` is already side-aligned in V1, so no flip
   on ``y``.
2. **Compute V1 raw baseline** Brier / log-loss / AUC on the side-aligned
   pairs.  This is the floor the current pipeline must not regress.
3. **Apply the current per-league calibrator** (``app/calibrators.py
   ::apply``) to each side-aligned probability and recompute the same
   three metrics on the calibrated values.
4. **Aggregate per league** and write a JSON report with
   ``brier_v1_raw``, ``brier_calibrated``, ``brier_delta``,
   ``log_loss_*``, ``auc_*``, ``sample_count``, plus a top-level
   ``leagues_within_2pct_brier`` rollup.

Why this is the meaningful backtest
-----------------------------------

The plan called for "re-running each row through the current ensemble
(LGB + XGB stack)".  The V1 archive does **not** preserve the input
feature vector that produced ``predicted_prob`` — it only preserves the
stack's output and a small ``analysis_json`` blob (see ``explore``
notes captured during planning).  Many of the features the live
pipeline depends on (real-time market_prob, sharp money signals, sharp
money confidence, EV-source agreement) are transient and were not
snapshotted at trade time.

So the only way to put V1 history through the current code path is to
treat V1's ``predicted_prob`` as the input to the **calibrator** stage
of the ensemble (which is exactly how the nightly refit job uses it).
``brier_calibrated < brier_v1_raw`` means the current calibrators
improve V1's stack output; equality / regression means the calibrators
either bypassed (cold-install no-fit) or are worse than V1's untreated
output.  Either case is documented per-league in the report.

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


async def run(
    *,
    rows: list[dict[str, Any]] | None,
    limit: int | None,
    leagues_filter: set[str] | None,
    load_calibrators: bool,
) -> dict[str, Any]:
    """Programmatic entry point. Used by the pytest fixture."""
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
    grouped = _normalize_rows(rows, leagues_filter=leagues_filter)
    scored = score_grouped(grouped)
    return {
        "started_at": started_at,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "row_input_count": len(rows),
        "row_kept_count": scored["summary"]["row_count"],
        "leagues_filter": sorted(leagues_filter) if leagues_filter else None,
        "limit": limit,
        "calibrator_freshness": calibrators.get_state().freshness() if load_calibrators else None,
        **scored,
    }


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
