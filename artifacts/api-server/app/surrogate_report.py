"""Latest-surrogate-backtest report loader.

The surrogate V1 backtest (``scripts/backtest_v1_archive.py
--surrogate-features``) writes a per-league JSON dump to
``artifacts/api-server/scripts/reports/v1_backtest_sample_surrogate_*.json``
on every run. The operator dashboard surfaces those numbers so the
team can see at a glance which leagues the live stack is winning on
without grepping report JSON.

This module is a thin read-only adapter:

* :func:`latest_report_path` finds the most-recent surrogate report on
  disk (lexicographic sort over the date-stamped filenames the script
  writes — the runner always uses ``YYYY-MM-DD`` so lexicographic ==
  chronological).
* :func:`load_latest` parses that file and returns a small, stable
  payload — per-league delta_vs_v1 + sample_count + surrogate-coverage%
  + a few summary fields — that the API endpoint can hand back as-is.

Everything is wrapped in defensive try/except: a missing or malformed
report must not bring the dashboard down, it just degrades the panel
to a "no report available" placeholder.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any


log = logging.getLogger("axiom.surrogate_report")


# ``app/surrogate_report.py`` -> ``app/`` -> ``api-server/`` ->
# ``scripts/reports``. Keep the resolution lazy so a test can monkey-
# patch the override env var without re-importing the module.
def _default_reports_dir() -> Path:
    here = Path(__file__).resolve()
    return here.parent.parent / "scripts" / "reports"


def reports_dir() -> Path:
    override = os.environ.get("AXIOM_SURROGATE_REPORTS_DIR")
    if override:
        return Path(override)
    return _default_reports_dir()


# Filename glob the surrogate backtest writes. The current runner
# emits ``v1_backtest_sample_surrogate_<size>_YYYY-MM-DD.json`` — match
# both the bare ``surrogate_*`` and the size-suffixed ``surrogate_8k_*``
# variants so the loader survives a future rename of the size token.
SURROGATE_REPORT_GLOB = "v1_backtest_sample_surrogate_*.json"


# Filenames look like ``v1_backtest_sample_surrogate[_<size>]_YYYY-MM-DD.json``.
# Pull the trailing date out so two reports on the same day fall back
# to mtime instead of getting reordered by the size token sorting
# lexicographically before the date.
_DATE_IN_NAME_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _sort_key(path: Path) -> tuple[str, float]:
    """Sort by parsed YYYY-MM-DD date (chronological), then by file
    mtime so two reports written on the same day still resolve to
    the most-recent on-disk file. Filenames missing a date stamp sort
    to the very bottom."""
    m = _DATE_IN_NAME_RE.search(path.name)
    date_token = m.group(1) if m else ""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return (date_token, mtime)


def latest_report_path(directory: Path | None = None) -> Path | None:
    """Return the most-recent surrogate report on disk, or ``None``
    when the reports directory is missing or empty."""
    d = directory or reports_dir()
    try:
        if not d.is_dir():
            return None
        candidates = list(d.glob(SURROGATE_REPORT_GLOB))
    except OSError as e:
        log.warning("surrogate report dir unreadable (%s): %s", d, e)
        return None
    if not candidates:
        return None
    candidates.sort(key=_sort_key)
    return candidates[-1]


def _coerce_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # Reject NaN/inf — they break JSON serialisation downstream and
    # the dashboard renders "—" for missing values anyway.
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _coerce_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _summarise_per_league(per_league: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten the report's per-league dict into a stable list of rows
    sorted by best-improvement-first (most-negative ``delta_vs_v1``).
    A regression sorts to the bottom so it stays visible without
    burying the wins."""
    rows: list[dict[str, Any]] = []
    for league, payload in per_league.items():
        if not isinstance(payload, dict):
            continue
        sample = _coerce_int(payload.get("sample_count")) or 0
        applied = _coerce_int(payload.get("calibrator_applied_count")) or 0
        # Surrogate-coverage% — fraction of scored rows for which the
        # full stack actually applied a calibrator (anything < 100%
        # means rows fell back to the raw probability and hint at a
        # missing or stale per-league calibrator).
        coverage_pct: float | None = None
        if sample > 0:
            coverage_pct = round(100.0 * applied / sample, 2)
        rows.append(
            {
                "league": str(league),
                "sample_count": sample,
                "calibrator_applied_count": applied,
                "surrogate_coverage_pct": coverage_pct,
                "calibrator_type": payload.get("calibrator_type"),
                "delta_vs_v1": _coerce_float(payload.get("delta_vs_v1")),
                "brier_v1_raw": _coerce_float(payload.get("brier_v1_raw")),
                "brier_calibrated": _coerce_float(payload.get("brier_calibrated")),
                "verdict": payload.get("verdict"),
            }
        )
    # Sort: improvements (negative delta) first, biggest improvement
    # at the top, regressions (positive delta) at the bottom, missing
    # delta last. Stable on league name so the table doesn't reshuffle
    # between renders.
    rows.sort(key=lambda r: r["league"])
    rows.sort(
        key=lambda r: (
            r["delta_vs_v1"] is None,
            r["delta_vs_v1"] if r["delta_vs_v1"] is not None else 0.0,
        )
    )
    return rows


def load_latest(directory: Path | None = None) -> dict[str, Any]:
    """Load the most-recent surrogate report and shape it for the
    operator dashboard. Always returns a dict; ``available`` is False
    when no report could be loaded."""
    path = latest_report_path(directory)
    if path is None:
        return {
            "available": False,
            "reason": "no surrogate backtest report on disk",
            "report_path": None,
            "report_filename": None,
            "generated_at": None,
            "leagues": [],
            "summary": None,
            "row_kept_count": None,
            "row_input_count": None,
            "degraded_baseline": True,
            "disclaimer": (
                "DEGRADED baseline — surrogate features stand in for "
                "real V1 feature dumps until #731 lands."
            ),
        }
    try:
        with path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        log.warning("surrogate report %s unreadable: %s", path, e)
        return {
            "available": False,
            "reason": f"failed to parse {path.name}: {e}",
            "report_path": str(path),
            "report_filename": path.name,
            "generated_at": None,
            "leagues": [],
            "summary": None,
            "row_kept_count": None,
            "row_input_count": None,
            "degraded_baseline": True,
            "disclaimer": (
                "DEGRADED baseline — surrogate features stand in for "
                "real V1 feature dumps until #731 lands."
            ),
        }

    per_league = payload.get("per_league") or {}
    if not isinstance(per_league, dict):
        per_league = {}
    leagues = _summarise_per_league(per_league)

    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else None
    return {
        "available": True,
        "reason": None,
        "report_path": str(path),
        "report_filename": path.name,
        "generated_at": payload.get("completed_at") or payload.get("started_at"),
        "started_at": payload.get("started_at"),
        "completed_at": payload.get("completed_at"),
        "row_input_count": _coerce_int(payload.get("row_input_count")),
        "row_kept_count": _coerce_int(payload.get("row_kept_count")),
        "leagues": leagues,
        "summary": summary,
        "degraded_baseline": True,
        "disclaimer": (
            "DEGRADED baseline — surrogate features stand in for real "
            "V1 feature dumps until #731 lands. delta_vs_v1 compares "
            "calibrated probabilities against V1's raw archived "
            "predicted_prob, not V1's final calibrated Brier."
        ),
    }
