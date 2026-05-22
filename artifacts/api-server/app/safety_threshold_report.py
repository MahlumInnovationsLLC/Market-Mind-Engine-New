"""Latest safety-threshold tuning report loader (task #814).

The safety-threshold tuner (``scripts/tune_safety_thresholds.py``)
writes a per-vertical sweep report to
``artifacts/api-server/scripts/reports/safety_threshold_tuning_*.json``
each time it runs.  Task #814 added a scheduler that re-runs the
sweep after every meta-model retrain so the recommended defaults
stay aligned with the live realised-return distribution.

This module is a thin read-only adapter that the operator console
talks to:

* :func:`latest_report_path` / :func:`prior_report_path` resolve the
  two most-recent reports on disk (lexicographic sort on the
  timestamp suffix; ``YYYY-MM-DD[THHMMSSZ]`` is monotonic).
* :func:`load_latest` parses the latest report, flattens it into a
  per-vertical row shape the dashboard can render, and computes
  drift (delta vs the prior report) for every (vertical, knob)
  pair so operators can see at a glance which recommendations are
  shifting.

Everything is wrapped in defensive try/except so a missing or
malformed report degrades the panel to a "no report available"
placeholder instead of taking the dashboard down.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any


log = logging.getLogger("axiom.safety_threshold_report")


SAFETY_REPORT_GLOB = "safety_threshold_tuning_*.json"
_TS_IN_NAME_RE = re.compile(
    r"safety_threshold_tuning_(\d{4}-\d{2}-\d{2}(?:T\d{6}Z)?)\.json$"
)

# Knobs we surface in the drift table.  The tuner emits picks for
# these three; everything else falls through to the spec defaults.
RECOMMENDED_KNOBS: tuple[str, ...] = (
    "sharpe_min_ratio",
    "ruin_max_probability",
    "segment_max_losses",
)


def _default_reports_dir() -> Path:
    # ``app/safety_threshold_report.py`` -> ``app/`` -> ``api-server/``
    # -> ``scripts/reports``.  Lazy so a test can monkey-patch the
    # override env var without re-importing the module.
    here = Path(__file__).resolve()
    return here.parent.parent / "scripts" / "reports"


def reports_dir() -> Path:
    override = os.environ.get("AXIOM_SAFETY_THRESHOLD_REPORTS_DIR")
    if override:
        return Path(override)
    return _default_reports_dir()


def _sort_key(path: Path) -> tuple[str, float]:
    """Sort by parsed timestamp suffix (chronological), then by file
    mtime as a tiebreaker.  Filenames missing a timestamp sort to the
    bottom so the picker keeps working if an analyst drops a hand-
    named report in the directory."""
    m = _TS_IN_NAME_RE.search(path.name)
    ts = m.group(1) if m else ""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return (ts, mtime)


def _sorted_reports(directory: Path | None = None) -> list[Path]:
    d = directory or reports_dir()
    try:
        if not d.is_dir():
            return []
        candidates = list(d.glob(SAFETY_REPORT_GLOB))
    except OSError as e:
        log.warning("safety threshold report dir unreadable (%s): %s", d, e)
        return []
    candidates.sort(key=_sort_key)
    return candidates


def latest_report_path(directory: Path | None = None) -> Path | None:
    cands = _sorted_reports(directory)
    return cands[-1] if cands else None


def prior_report_path(directory: Path | None = None) -> Path | None:
    """Second-most-recent report — the comparison baseline for the
    drift column.  Returns ``None`` when there is only one report on
    disk (no baseline to diff against yet)."""
    cands = _sorted_reports(directory)
    return cands[-2] if len(cands) >= 2 else None


def _load_recommendations(path: Path) -> dict[str, dict[str, float | int | None]]:
    """Parse a sweep report into ``{vertical: {knob: value}}``.  Returns
    an empty dict on any parse error so the caller can fall through
    cleanly."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("safety threshold report %s unreadable: %s", path, e)
        return {}
    out: dict[str, dict[str, float | int | None]] = {}
    for s in payload.get("sweeps") or []:
        if not isinstance(s, dict):
            continue
        vert = str(s.get("vertical") or "").strip()
        if not vert:
            continue
        rec = s.get("recommended") or {}
        if not isinstance(rec, dict):
            continue
        out[vert] = {
            k: rec.get(k) for k in RECOMMENDED_KNOBS
        }
    return out


def _coerce_numeric(v: Any) -> float | int | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    if isinstance(v, int) or (isinstance(v, float) and f.is_integer() and abs(f) < 1e15):
        # Preserve int-ness for the segment knob so the UI doesn't
        # render "2.0" instead of "2".
        try:
            return int(v) if isinstance(v, int) else (
                int(f) if f.is_integer() else f
            )
        except (TypeError, ValueError):
            return f
    return f


def _delta(current: Any, prior: Any) -> float | None:
    a = _coerce_numeric(current)
    b = _coerce_numeric(prior)
    if a is None or b is None:
        return None
    return float(a) - float(b)


def _build_rows(
    current: dict[str, dict[str, float | int | None]],
    prior: dict[str, dict[str, float | int | None]],
) -> list[dict[str, Any]]:
    """One row per vertical: current recommendations + drift vs prior
    for each knob.  ``__all__`` (the global mix) sorts first; the
    rest sort alphabetically."""
    verticals = sorted(
        set(current) | set(prior),
        key=lambda v: (v != "__all__", v),
    )
    rows: list[dict[str, Any]] = []
    for vert in verticals:
        cur = current.get(vert) or {}
        prv = prior.get(vert) or {}
        row: dict[str, Any] = {"vertical": vert, "knobs": {}}
        for knob in RECOMMENDED_KNOBS:
            cur_v = _coerce_numeric(cur.get(knob))
            prv_v = _coerce_numeric(prv.get(knob))
            row["knobs"][knob] = {
                "current": cur_v,
                "prior": prv_v,
                "delta": _delta(cur.get(knob), prv.get(knob)),
                # Only flag as ``changed`` when both sides exist and
                # differ — a first-time pick (no prior) isn't drift,
                # it's the baseline establishing itself.
                "changed": (
                    cur_v is not None
                    and prv_v is not None
                    and cur_v != prv_v
                ),
            }
        rows.append(row)
    return rows


def load_latest(directory: Path | None = None) -> dict[str, Any]:
    """Load the most-recent sweep + drift vs the prior sweep, shaped
    for the operator console.  Always returns a dict; ``available``
    is False when no report could be loaded."""
    path = latest_report_path(directory)
    if path is None:
        return {
            "available": False,
            "reason": "no safety-threshold tuning report on disk",
            "report_path": None,
            "report_filename": None,
            "prior_report_path": None,
            "prior_report_filename": None,
            "generated_at": None,
            "source": None,
            "rows": [],
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("safety threshold report %s unreadable: %s", path, e)
        return {
            "available": False,
            "reason": f"failed to parse {path.name}: {e}",
            "report_path": str(path),
            "report_filename": path.name,
            "prior_report_path": None,
            "prior_report_filename": None,
            "generated_at": None,
            "source": None,
            "rows": [],
        }

    current_recs = _load_recommendations(path)
    prior_path = prior_report_path(directory)
    prior_recs = _load_recommendations(prior_path) if prior_path else {}
    rows = _build_rows(current_recs, prior_recs)

    # Per-vertical sample counts so operators can spot a vertical
    # whose recommendation shifted because the sample size collapsed.
    samples: dict[str, int] = {}
    for s in payload.get("sweeps") or []:
        if isinstance(s, dict) and s.get("vertical"):
            try:
                samples[str(s["vertical"])] = int(s.get("sample_count") or 0)
            except (TypeError, ValueError):
                samples[str(s["vertical"])] = 0
    for row in rows:
        row["sample_count"] = samples.get(row["vertical"], 0)

    return {
        "available": True,
        "reason": None,
        "report_path": str(path),
        "report_filename": path.name,
        "prior_report_path": str(prior_path) if prior_path else None,
        "prior_report_filename": prior_path.name if prior_path else None,
        "generated_at": payload.get("generated_at"),
        "source": payload.get("source"),
        "rows": rows,
    }
