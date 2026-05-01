"""Build the monthly investor deck (Task #756 / roadmap T-23).

Reads the most recent ``reports/v1_backtest_full_archive_*.json``
audit report, plus an optional per-league Brier-delta CSV, and writes
``docs/investor-deck-template.pptx`` — a five-slide template covering
the cover, equity TL;DR, per-league Brier delta, the parity gate
status manifest, and the road-to-live timeline.

Usage:
    pnpm --filter @workspace/api-server exec python -m scripts.build_investor_deck
    pnpm --filter @workspace/api-server exec python -m scripts.build_investor_deck \\
        --report artifacts/api-server/reports/v1_backtest_full_archive_<ts>.json \\
        --output docs/investor-deck-2026-05.pptx

The script is deterministic: re-running against the same JSON refreshes
the deck without producing a different binary modulo timestamps.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any


def _repo_root() -> Path:
    # artifacts/api-server/scripts/build_investor_deck.py → /repo
    return Path(__file__).resolve().parents[3]


def _default_reports_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "reports"


def _default_output() -> Path:
    return _repo_root() / "docs" / "investor-deck-template.pptx"


def _latest_report(reports_dir: Path) -> Path | None:
    if not reports_dir.is_dir():
        return None
    candidates = sorted(reports_dir.glob("v1_backtest_full_archive_*.json"))
    return candidates[-1] if candidates else None


def _safe_load(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except Exception as e:
        raise SystemExit(f"failed to parse report {path}: {e}") from e


def _equity_tldr(report: dict[str, Any]) -> dict[str, Any]:
    """Top-line numbers we want on the second slide."""
    summary = report.get("summary") or {}
    return {
        "row_input_count": int(report.get("row_input_count") or 0),
        "row_kept_count": int(report.get("row_kept_count") or 0),
        "league_count": int(summary.get("league_count") or 0),
        "brier_tolerance": summary.get("brier_tolerance"),
        "leagues_parity": summary.get("leagues_parity") or [],
        "leagues_improved": summary.get("leagues_improved") or [],
        "leagues_regressed": summary.get("leagues_regressed") or [],
        "completed_at": report.get("completed_at"),
        "source": report.get("source") or "",
    }


def _per_league_table(report: dict[str, Any]) -> list[tuple[str, ...]]:
    """Header + one row per league for the Brier-delta slide."""
    header = (
        "League",
        "n",
        "Brier (V1 raw)",
        "Brier (calibrated)",
        "Δ vs V1",
        "Verdict",
        "Calibrator",
    )
    rows: list[tuple[str, ...]] = [header]
    pl = report.get("per_league") or {}
    for league in sorted(pl.keys()):
        row = pl[league] or {}

        def _f(v: Any, p: int = 4) -> str:
            return f"{float(v):.{p}f}" if isinstance(v, (int, float)) else "—"

        rows.append(
            (
                str(league),
                str(int(row.get("sample_count") or 0)),
                _f(row.get("brier_v1_raw")),
                _f(row.get("brier_calibrated")),
                _f(row.get("brier_delta")),
                str(row.get("verdict") or "—"),
                str(row.get("calibrator_type") or "—"),
            )
        )
    return rows


def _parity_gate_lines(report: dict[str, Any]) -> list[str]:
    parity = report.get("parity_gate_status") or {}
    overall = parity.get("overall") or "UNKNOWN"
    lines = [f"Overall verdict: {overall}"]
    blocked_by = parity.get("overall_blocked_by")
    if blocked_by:
        lines.append(f"Blocked by: {blocked_by}")
    summary = parity.get("summary") or {}
    if summary:
        lines.append(
            "Gates: "
            f"{summary.get('pass', 0)} pass · "
            f"{summary.get('fail', 0)} fail · "
            f"{summary.get('evaluable_degraded', 0)} degraded · "
            f"{summary.get('blocked_or_unevaluable', 0)} blocked"
        )
    for name, gate in (parity.get("gates") or {}).items():
        status = gate.get("status") or "?"
        verdict = gate.get("verdict")
        suffix = f" / {verdict}" if verdict else ""
        lines.append(f"  • {name}: {status}{suffix}")
        bb = gate.get("blocked_by")
        if bb:
            lines.append(f"      blocked by: {bb}")
    return lines


def _road_to_live_bullets(report: dict[str, Any]) -> list[str]:
    """Hand-curated milestones — the JSON is just for context."""
    parity = (report.get("parity_gate_status") or {}).get("overall") or "UNKNOWN"
    summary = report.get("summary") or {}
    regressed = summary.get("leagues_regressed") or []
    return [
        "Calibrator-only audit replays V1 archive nightly.",
        "Weekly V1 backtest gate persists `parity_gate_status` and "
        "regresses-vs-prior diff (T-22).",
        f"Latest parity verdict: {parity}.",
        (
            "Regressed leagues to investigate: " + ", ".join(regressed)
            if regressed
            else "No per-league Brier regressions vs prior week."
        ),
        "Tail-risk guard auto-armed at $X cap; trips logged into "
        "`risk_gate_trips` (T-21).",
        "Daily honesty report delivers shadow-vs-live divergence + "
        "calibrator drift + tail risk + 24 h gate trips at 09:00 ET.",
        "Next milestone: archived-features full-stack replay unblocked "
        "by #731 (V1 feature snapshot dumps).",
    ]


# ---------------------------------------------------------------------------
# Slide builders
# ---------------------------------------------------------------------------


def build_deck(
    *,
    report_path: Path,
    output_path: Path,
    extra_brier_csv: Path | None = None,
) -> Path:
    """Render the deck and return the output path."""
    try:
        from pptx import Presentation
        from pptx.util import Inches, Pt
        from pptx.enum.text import PP_ALIGN
    except ImportError as e:  # pragma: no cover
        raise SystemExit(
            "python-pptx not installed. Run: "
            "pnpm --filter @workspace/api-server add python-pptx"
        ) from e

    report = _safe_load(report_path)
    tldr = _equity_tldr(report)
    league_rows = _per_league_table(report)
    parity_lines = _parity_gate_lines(report)
    road_bullets = _road_to_live_bullets(report)

    # Optional CSV overlay — appends extra (league, delta) rows the
    # operator may want to splice in (e.g. a hand-curated 30-day delta).
    extra_rows: list[tuple[str, str]] = []
    if extra_brier_csv is not None and extra_brier_csv.is_file():
        for line in extra_brier_csv.read_text().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2 and parts[0] and parts[0].lower() != "league":
                extra_rows.append((parts[0], parts[1]))

    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)

    blank_layout = prs.slide_layouts[6]  # Blank
    title_layout = prs.slide_layouts[0]  # Title

    def _add_textbox(
        slide: Any,
        *,
        left: float,
        top: float,
        width: float,
        height: float,
        text: str,
        font_size: int = 14,
        bold: bool = False,
        align: Any = None,
    ) -> None:
        box = slide.shapes.add_textbox(
            Inches(left), Inches(top), Inches(width), Inches(height)
        )
        tf = box.text_frame
        tf.word_wrap = True
        tf.text = text
        for para in tf.paragraphs:
            if align is not None:
                para.alignment = align
            for run in para.runs:
                run.font.size = Pt(font_size)
                run.font.bold = bold

    def _add_bullets(
        slide: Any,
        *,
        left: float,
        top: float,
        width: float,
        height: float,
        bullets: list[str],
        font_size: int = 14,
    ) -> None:
        box = slide.shapes.add_textbox(
            Inches(left), Inches(top), Inches(width), Inches(height)
        )
        tf = box.text_frame
        tf.word_wrap = True
        for i, line in enumerate(bullets):
            para = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            para.text = line
            for run in para.runs:
                run.font.size = Pt(font_size)

    # -- Slide 1: cover -----------------------------------------------------
    s1 = prs.slides.add_slide(title_layout)
    s1.shapes.title.text = "Axiom-Predict — Investor update"
    subtitle_idx = next(
        (i for i, ph in enumerate(s1.placeholders) if ph.placeholder_format.idx == 1),
        None,
    )
    if subtitle_idx is not None:
        s1.placeholders[1].text = (
            f"Generated {dt.date.today().isoformat()} · "
            f"V1 backtest report: {report_path.name}"
        )

    # -- Slide 2: equity TL;DR ---------------------------------------------
    s2 = prs.slides.add_slide(blank_layout)
    _add_textbox(
        s2,
        left=0.5,
        top=0.4,
        width=12.3,
        height=0.7,
        text="Equity TL;DR",
        font_size=28,
        bold=True,
    )
    bullets_2 = [
        f"Audit source: {tldr['source']} · completed {tldr['completed_at']}",
        f"Rows replayed: {tldr['row_kept_count']:,} / {tldr['row_input_count']:,} input",
        f"Leagues scored: {tldr['league_count']} · "
        f"Brier tolerance: ±{tldr['brier_tolerance']}",
        f"Parity (within tol): {len(tldr['leagues_parity'])} — "
        f"{', '.join(tldr['leagues_parity']) or '(none)'}",
        f"Improved vs V1: {len(tldr['leagues_improved'])} — "
        f"{', '.join(tldr['leagues_improved']) or '(none)'}",
        f"Regressed vs V1: {len(tldr['leagues_regressed'])} — "
        f"{', '.join(tldr['leagues_regressed']) or '(none)'}",
    ]
    _add_bullets(
        s2, left=0.7, top=1.5, width=12.0, height=5.5, bullets=bullets_2, font_size=18
    )

    # -- Slide 3: per-league Brier delta table -----------------------------
    s3 = prs.slides.add_slide(blank_layout)
    _add_textbox(
        s3,
        left=0.5,
        top=0.4,
        width=12.3,
        height=0.7,
        text="Per-league Brier delta (V1 raw → calibrated)",
        font_size=24,
        bold=True,
    )
    n_rows = len(league_rows)
    n_cols = len(league_rows[0])
    table_top = 1.3
    table_height = max(2.0, min(5.8, 0.32 * n_rows))
    table_shape = s3.shapes.add_table(
        n_rows, n_cols, Inches(0.5), Inches(table_top), Inches(12.3), Inches(table_height)
    )
    table = table_shape.table
    for ci, header in enumerate(league_rows[0]):
        cell = table.cell(0, ci)
        cell.text = header
        for para in cell.text_frame.paragraphs:
            for run in para.runs:
                run.font.bold = True
                run.font.size = Pt(11)
    for ri, row in enumerate(league_rows[1:], start=1):
        for ci, val in enumerate(row):
            cell = table.cell(ri, ci)
            cell.text = val
            for para in cell.text_frame.paragraphs:
                for run in para.runs:
                    run.font.size = Pt(10)
    if extra_rows:
        _add_textbox(
            s3,
            left=0.5,
            top=table_top + table_height + 0.1,
            width=12.3,
            height=0.5,
            text="Additional context (CSV overlay): "
            + ", ".join(f"{lg}={d}" for lg, d in extra_rows[:8]),
            font_size=10,
        )

    # -- Slide 4: parity gate status ---------------------------------------
    s4 = prs.slides.add_slide(blank_layout)
    _add_textbox(
        s4,
        left=0.5,
        top=0.4,
        width=12.3,
        height=0.7,
        text="Parity gate status",
        font_size=28,
        bold=True,
    )
    _add_bullets(
        s4,
        left=0.7,
        top=1.4,
        width=12.0,
        height=5.5,
        bullets=parity_lines,
        font_size=14,
    )

    # -- Slide 5: road to live ---------------------------------------------
    s5 = prs.slides.add_slide(blank_layout)
    _add_textbox(
        s5,
        left=0.5,
        top=0.4,
        width=12.3,
        height=0.7,
        text="Road to live",
        font_size=28,
        bold=True,
    )
    _add_bullets(
        s5,
        left=0.7,
        top=1.4,
        width=12.0,
        height=5.5,
        bullets=road_bullets,
        font_size=16,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(output_path))
    return output_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Path to a v1_backtest_full_archive_*.json file. Defaults "
        "to the most recent file under artifacts/api-server/reports.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=_default_output(),
        help="Destination .pptx (default: docs/investor-deck-template.pptx).",
    )
    parser.add_argument(
        "--extra-brier-csv",
        type=Path,
        default=None,
        help="Optional CSV with `league,delta` rows appended to slide 3.",
    )
    args = parser.parse_args(argv)

    report_path = args.report or _latest_report(_default_reports_dir())
    if report_path is None or not report_path.is_file():
        print(
            "[build_investor_deck] no V1 backtest report found. "
            "Run scripts/backtest_v1_archive.py with --json-out first.",
            file=sys.stderr,
        )
        return 1
    out = build_deck(
        report_path=report_path,
        output_path=args.output,
        extra_brier_csv=args.extra_brier_csv,
    )
    print(f"[build_investor_deck] wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
