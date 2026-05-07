"""Historical sweep to tune the three round-2 audit safety thresholds
(task #732).

The round-2 audit (#730) shipped three new gates with conservative
launch defaults that were chosen for safety, not measured against the
V1 settled-trade tape:

  * ``risk.sharpe_min_ratio``      — trailing-Sharpe floor (default 0.0)
  * ``risk.ruin_max_probability``  — bootstrap-MC P(ruin) cap (default 0.05)
  * ``risk.segment_max_losses``    — per-(league, market_type) loss
                                     count over a 24h trailing window
                                     (default 3)

This script replays each candidate from the V1 settled-trade tape
(``v1_archive.trade_analyses``) through a *paper* version of each
gate, sweeping a reasonable grid of values per knob and per vertical,
and computes the counterfactual PnL / kept-trade-count / max-drawdown
for each grid point.  It then picks the *recommended* default per
vertical:

  * Sharpe / segment knobs — the value that maximises retained PnL
    (sum of kept-trade pnl) subject to keeping at least 50% of
    candidates.  When several values are within 1% PnL of one
    another we pick the *tighter* one (smaller window of risk
    exposure for the same realised gain).
  * Ruin knob — the smallest cap that retains 95% of historical PnL,
    so a tail-heavy vertical isn't penalised more than necessary.

Outputs:

  * A JSON report under ``scripts/reports/safety_threshold_tuning_<UTC>.json``
    with the full grid (so future analysts can re-derive the picks
    without re-running the sweep).
  * The printed summary mirrors the JSON ``recommendations`` block;
    operators can copy the per-vertical defaults into
    ``runtime_settings.PER_VERTICAL_NEW_GATE_DEFAULTS`` and the
    spec-level globals into ``RISK_KNOB_SPECS`` directly.

Usage::

    # Read-only audit (default — no DB writes).
    python -m scripts.tune_safety_thresholds

    # Apply the recommendations as ``risk.<knob>.<vertical>`` /
    # ``risk.<knob>`` rows in ``system_settings`` (per-vertical
    # override pipeline; takes effect on the next orchestrator cycle).
    python -m scripts.tune_safety_thresholds --apply

    # Limit the sweep to a single vertical (debug / quick re-run).
    python -m scripts.tune_safety_thresholds --vertical crypto

    # Use a specific report path (otherwise dated, under reports/).
    python -m scripts.tune_safety_thresholds --report /tmp/sweep.json

When the V1 tape is empty (e.g. a fresh dev DB) the script emits an
empty-grid report so CI can still smoke-test the wiring without
silently producing bogus picks.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

_HERE = Path(__file__).resolve()
_API_SERVER_ROOT = _HERE.parent.parent
if str(_API_SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_SERVER_ROOT))

from app import db  # noqa: E402


REPORTS_DIR = _HERE.parent / "reports"


# ---------------------------------------------------------------------------
# Sweep grids.  Tight enough that the report fits on one screen, wide
# enough to bracket the launch default in either direction so the
# picker can demonstrate "tighter than launch" or "looser than launch"
# without an extra run.
# ---------------------------------------------------------------------------
SHARPE_GRID: tuple[float, ...] = (-1.0, -0.75, -0.5, -0.25, 0.0, 0.25, 0.5)
RUIN_GRID: tuple[float, ...] = (0.01, 0.025, 0.05, 0.075, 0.10, 0.15, 0.20)
SEGMENT_GRID: tuple[int, ...] = (2, 3, 4, 5, 6, 7, 8, 10)

# Trailing-window sizes for the Sharpe / segment replays.  These mirror
# the gate defaults so the sweep measures the actual production
# behaviour of each knob, not a hypothetical wider/narrower window.
SHARPE_WINDOW = 30
SHARPE_MIN_SAMPLES = 10
SEGMENT_WINDOW_HOURS = 24.0

# Picker thresholds (see module docstring).
MIN_KEEP_RATIO = 0.50
RUIN_PNL_RETENTION = 0.95
TIE_TOLERANCE_PCT = 0.01


@dataclass
class _Trade:
    """One settled candidate from ``v1_archive.trade_analyses``."""
    vertical: str
    league: str
    market_type: str
    pnl: float
    notional: float  # >= 1.0 (clamped) so return = pnl / notional is finite
    settled_at: datetime

    @property
    def realised_return(self) -> float:
        return self.pnl / self.notional


@dataclass
class _GridPoint:
    value: float
    kept: int
    rejected: int
    pnl_kept: float
    pnl_rejected: float
    max_drawdown_kept: float

    def to_dict(self) -> dict[str, Any]:
        total = self.kept + self.rejected
        return {
            "value": self.value,
            "kept": self.kept,
            "rejected": self.rejected,
            "keep_ratio": (self.kept / total) if total else None,
            "pnl_kept": round(self.pnl_kept, 2),
            "pnl_rejected": round(self.pnl_rejected, 2),
            "max_drawdown_kept": round(self.max_drawdown_kept, 2),
        }


@dataclass
class _VerticalSweep:
    vertical: str
    sample_count: int
    baseline_pnl: float
    sharpe: list[_GridPoint] = field(default_factory=list)
    ruin: list[_GridPoint] = field(default_factory=list)
    segment: list[_GridPoint] = field(default_factory=list)
    recommended: dict[str, float | int | None] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Data loading.
# ---------------------------------------------------------------------------


def _load_trades_from_fixture(
    path: Path, vertical_filter: str | None
) -> list[_Trade]:
    """Load trades from a JSON fixture.  Format: a list of objects with
    ``vertical``, ``league``, ``market_type``, ``pnl``, ``notional``
    (optional, defaults 1.0), and ``settled_at`` (ISO-8601 string or
    UNIX seconds; required).  Used by the committed sweep report so the
    recommendations are reproducible without the production V1 dump."""
    raw = json.loads(path.read_text())
    if not isinstance(raw, list):
        raise ValueError(f"fixture {path} must be a JSON list")
    vf = (vertical_filter or "").strip().lower()
    out: list[_Trade] = []
    for item in raw:
        vert = (item.get("vertical") or "").strip().lower()
        if vf and vert != vf:
            continue
        settled = item.get("settled_at")
        if isinstance(settled, (int, float)):
            settled_dt = datetime.fromtimestamp(float(settled), tz=timezone.utc)
        elif isinstance(settled, str):
            s = settled.strip()
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            settled_dt = datetime.fromisoformat(s)
            if settled_dt.tzinfo is None:
                settled_dt = settled_dt.replace(tzinfo=timezone.utc)
        else:
            raise ValueError(f"fixture row missing settled_at: {item}")
        out.append(
            _Trade(
                vertical=vert or "unknown",
                league=(item.get("league") or "").strip().upper(),
                market_type=(item.get("market_type") or "").strip().lower(),
                pnl=float(item.get("pnl", 0.0)),
                notional=max(float(item.get("notional", 1.0)), 1.0),
                settled_at=settled_dt,
            )
        )
    out.sort(key=lambda t: t.settled_at)
    return out


async def _load_trades(vertical_filter: str | None) -> list[_Trade]:
    """Read settled V1 trades.  Returns empty list when the table is
    missing or empty so the script is idempotent against a fresh DB."""
    try:
        rows = await db.fetch(
            """
            SELECT
                coalesce(lower(vertical), '')        AS vertical,
                coalesce(upper(league), '')          AS league,
                coalesce(lower(market_type), '')     AS market_type,
                pnl::float                           AS pnl,
                coalesce(created_at, now())          AS settled_at,
                analysis_json
              FROM v1_archive.trade_analyses
             WHERE pnl IS NOT NULL
               AND actual_outcome IS NOT NULL
               AND ($1 = '' OR coalesce(lower(vertical), '') = $1)
             ORDER BY created_at ASC NULLS LAST
            """,
            (vertical_filter or "").strip().lower(),
        )
    except Exception as e:
        print(
            f"[tune_safety_thresholds] WARN: V1 archive read failed: {e}; "
            "report will be empty.",
            file=sys.stderr,
        )
        return []
    out: list[_Trade] = []
    for r in rows:
        notional = _resolve_notional(r)
        if notional <= 0:
            continue
        settled = r["settled_at"]
        if isinstance(settled, datetime) and settled.tzinfo is None:
            settled = settled.replace(tzinfo=timezone.utc)
        out.append(
            _Trade(
                vertical=r["vertical"] or "unknown",
                league=r["league"] or "",
                market_type=r["market_type"] or "",
                pnl=float(r["pnl"]),
                notional=notional,
                settled_at=settled,
            )
        )
    return out


def _resolve_notional(row: dict[str, Any]) -> float:
    """V1 doesn't snapshot a per-trade notional; reconstruct from the
    analysis_json blob (``contract_count`` × ``entry_price`` when both
    are present), else fall back to a unit notional so the return is
    just ``pnl``.  Clamped to >= 1.0 so the realised-return divisor
    can't blow up."""
    aj = row.get("analysis_json")
    if isinstance(aj, str):
        try:
            aj = json.loads(aj)
        except (ValueError, TypeError):
            aj = None
    if isinstance(aj, dict):
        qty = aj.get("contract_count") or aj.get("quantity")
        price = aj.get("entry_price") or aj.get("price")
        try:
            n = float(qty) * float(price)
            if n >= 1.0:
                return n
        except (TypeError, ValueError):
            pass
    return 1.0


# ---------------------------------------------------------------------------
# Gate replays.  Each function walks the trades chronologically, builds
# the gate's input from the prior history, and decides whether THIS
# candidate would have been kept or rejected at the given threshold.
# The candidate's PnL contributes to ``kept_pnl`` if the gate passes,
# ``rejected_pnl`` otherwise.
# ---------------------------------------------------------------------------


def _running_max_drawdown(pnls: Iterable[float]) -> float:
    peak = 0.0
    cum = 0.0
    worst = 0.0
    for p in pnls:
        cum += p
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > worst:
            worst = dd
    return worst


def _replay_sharpe(trades: list[_Trade], threshold: float) -> _GridPoint:
    """Replay trades chronologically, vetoing any whose trailing window
    of returns has Sharpe below ``threshold``."""
    history: list[float] = []
    kept_pnls: list[float] = []
    pnl_kept = 0.0
    pnl_rejected = 0.0
    kept = 0
    rejected = 0
    for t in trades:
        if len(history) >= SHARPE_MIN_SAMPLES:
            window = history[-SHARPE_WINDOW:]
            mean = statistics.fmean(window)
            stdev = statistics.pstdev(window)
            if stdev > 0:
                sharpe = mean / stdev
                if sharpe < threshold:
                    rejected += 1
                    pnl_rejected += t.pnl
                    history.append(t.realised_return)
                    continue
        kept += 1
        pnl_kept += t.pnl
        kept_pnls.append(t.pnl)
        history.append(t.realised_return)
    return _GridPoint(
        value=threshold,
        kept=kept,
        rejected=rejected,
        pnl_kept=pnl_kept,
        pnl_rejected=pnl_rejected,
        max_drawdown_kept=_running_max_drawdown(kept_pnls),
    )


def _replay_segment(trades: list[_Trade], max_losses: int) -> _GridPoint:
    """Per-(league, market_type) loss-count throttle in a rolling
    24-hour window."""
    by_segment: dict[tuple[str, str], list[datetime]] = {}
    kept = 0
    rejected = 0
    pnl_kept = 0.0
    pnl_rejected = 0.0
    kept_pnls: list[float] = []
    for t in trades:
        seg = (t.league, t.market_type)
        if t.league and t.market_type:
            losses = by_segment.setdefault(seg, [])
            cutoff = t.settled_at.timestamp() - SEGMENT_WINDOW_HOURS * 3600
            losses[:] = [ts for ts in losses if ts.timestamp() > cutoff]
            if len(losses) >= max_losses:
                rejected += 1
                pnl_rejected += t.pnl
                if t.pnl < 0:
                    losses.append(t.settled_at)
                continue
        kept += 1
        pnl_kept += t.pnl
        kept_pnls.append(t.pnl)
        if t.pnl < 0 and t.league and t.market_type:
            by_segment.setdefault(seg, []).append(t.settled_at)
    return _GridPoint(
        value=float(max_losses),
        kept=kept,
        rejected=rejected,
        pnl_kept=pnl_kept,
        pnl_rejected=pnl_rejected,
        max_drawdown_kept=_running_max_drawdown(kept_pnls),
    )


def _replay_ruin(trades: list[_Trade], threshold: float) -> _GridPoint:
    """Bootstrap-MC P(ruin) replay.  At each step we estimate the
    probability that the trailing-return distribution would breach
    the bankroll floor over a 100-trade horizon (matching the gate's
    default).  Conservative simplification: use a fixed bankroll of
    100 × the mean notional and fail when the bootstrap mean of N=200
    paths drops below 0."""
    history: list[float] = []
    kept_pnls: list[float] = []
    kept = 0
    rejected = 0
    pnl_kept = 0.0
    pnl_rejected = 0.0
    horizon = 100
    paths = 200
    import random
    rng = random.Random(0xA1C7)
    floor_fraction = -0.5  # path "ruins" when cumulative return <= -0.5
    for t in trades:
        if len(history) >= 20:
            window = history[-200:] if len(history) >= 200 else history
            ruined = 0
            n = len(window)
            for _ in range(paths):
                cum = 0.0
                for _ in range(horizon):
                    cum += window[rng.randrange(n)]
                    if cum < floor_fraction:
                        ruined += 1
                        break
            p_ruin = ruined / paths
            if p_ruin > threshold:
                rejected += 1
                pnl_rejected += t.pnl
                history.append(t.realised_return)
                continue
        kept += 1
        pnl_kept += t.pnl
        kept_pnls.append(t.pnl)
        history.append(t.realised_return)
    return _GridPoint(
        value=threshold,
        kept=kept,
        rejected=rejected,
        pnl_kept=pnl_kept,
        pnl_rejected=pnl_rejected,
        max_drawdown_kept=_running_max_drawdown(kept_pnls),
    )


# ---------------------------------------------------------------------------
# Picker — translates a sweep into a single recommended default.
# ---------------------------------------------------------------------------


def _pick_max_pnl(
    grid: list[_GridPoint], min_keep: float, prefer_tighter: bool = True
) -> float | int | None:
    """Pick the value that maximises kept PnL subject to keeping at
    least ``min_keep`` of trades.  Ties (within ``TIE_TOLERANCE_PCT``)
    break toward the tighter (larger-value) threshold so we don't ship
    a looser knob than the data supports."""
    eligible = [g for g in grid if (g.kept + g.rejected) > 0
                and (g.kept / (g.kept + g.rejected)) >= min_keep]
    if not eligible:
        return None
    # If the gate never tripped on the historical tape (every grid
    # point keeps every trade), the data is silent on the optimal
    # threshold; return None so the caller falls through to the
    # spec's safety default rather than shipping the loosest grid
    # value as an unsupported "recommendation".
    if all(g.rejected == 0 for g in eligible):
        return None
    best = max(eligible, key=lambda g: g.pnl_kept)
    tol = abs(best.pnl_kept) * TIE_TOLERANCE_PCT if best.pnl_kept != 0 else 1.0
    tied = [g for g in eligible if abs(g.pnl_kept - best.pnl_kept) <= tol]
    if prefer_tighter:
        tied.sort(key=lambda g: -g.value)
    else:
        tied.sort(key=lambda g: g.value)
    return tied[0].value


def _pick_smallest_cap_with_retention(
    grid: list[_GridPoint], baseline_pnl: float, retention: float
) -> float | None:
    """Smallest knob value (= tightest cap) that still retains
    ``retention`` × ``baseline_pnl``.  Used for the ruin cap so we
    don't loosen further than necessary."""
    if baseline_pnl <= 0:
        # Nothing to retain; data is silent on the optimal cap.  Let
        # the caller fall through to the spec's safety default.
        return None
    if all(g.rejected == 0 for g in grid):
        # Gate never tripped on the historical tape — no signal to
        # justify deviating from the spec's safety default.
        return None
    target = baseline_pnl * retention
    candidates = [g for g in grid if g.pnl_kept >= target]
    if not candidates:
        return None
    candidates.sort(key=lambda g: g.value)
    return candidates[0].value


# ---------------------------------------------------------------------------
# Sweep driver.
# ---------------------------------------------------------------------------


def _sweep_for_vertical(name: str, trades: list[_Trade]) -> _VerticalSweep:
    sweep = _VerticalSweep(
        vertical=name,
        sample_count=len(trades),
        baseline_pnl=sum(t.pnl for t in trades),
    )
    if not trades:
        return sweep
    for v in SHARPE_GRID:
        sweep.sharpe.append(_replay_sharpe(trades, v))
    for v in SEGMENT_GRID:
        sweep.segment.append(_replay_segment(trades, v))
    for v in RUIN_GRID:
        sweep.ruin.append(_replay_ruin(trades, v))
    sweep.recommended["sharpe_min_ratio"] = _pick_max_pnl(
        sweep.sharpe, MIN_KEEP_RATIO, prefer_tighter=True
    )
    # Segment cap is "tighter" at *smaller* values (fewer losses
    # tolerated before veto), so pass prefer_tighter=False to break
    # ties toward the smaller grid value.
    sweep.recommended["segment_max_losses"] = _pick_max_pnl(
        sweep.segment, MIN_KEEP_RATIO, prefer_tighter=False
    )
    sweep.recommended["ruin_max_probability"] = _pick_smallest_cap_with_retention(
        sweep.ruin, sweep.baseline_pnl, RUIN_PNL_RETENTION
    )
    # Cast segment recommendation back to int (grid is integer-valued
    # but ``_GridPoint.value`` is a float for uniformity).
    if sweep.recommended.get("segment_max_losses") is not None:
        sweep.recommended["segment_max_losses"] = int(
            sweep.recommended["segment_max_losses"]
        )
    return sweep


# ---------------------------------------------------------------------------
# Apply / report.
# ---------------------------------------------------------------------------


async def _apply_recommendations(
    sweeps: list[_VerticalSweep], actor: str
) -> list[dict[str, Any]]:
    """Write per-vertical and global ``risk.<knob>(.<vertical>)`` rows
    into ``system_settings``.  Idempotent — re-running with the same
    sweep produces the same end state."""
    written: list[dict[str, Any]] = []
    global_picks: dict[str, float | int] = {}
    for sweep in sweeps:
        if sweep.vertical == "__all__":
            for k, v in sweep.recommended.items():
                if v is not None:
                    global_picks[k] = v
            continue
        for knob, value in sweep.recommended.items():
            if value is None:
                continue
            key = f"risk.{knob}.{sweep.vertical}"
            await db.execute(
                """
                INSERT INTO system_settings (key, value, updated_at)
                VALUES ($1, $2, now())
                ON CONFLICT (key) DO UPDATE SET
                    value = EXCLUDED.value,
                    updated_at = EXCLUDED.updated_at
                """,
                key,
                str(value),
            )
            written.append({"key": key, "value": value})
    for knob, value in global_picks.items():
        key = f"risk.{knob}"
        await db.execute(
            """
            INSERT INTO system_settings (key, value, updated_at)
            VALUES ($1, $2, now())
            ON CONFLICT (key) DO UPDATE SET
                value = EXCLUDED.value,
                updated_at = EXCLUDED.updated_at
            """,
            key,
            str(value),
        )
        written.append({"key": key, "value": value})
    return written


def _format_report(
    sweeps: list[_VerticalSweep], source: str
) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "config": {
            "sharpe_grid": list(SHARPE_GRID),
            "ruin_grid": list(RUIN_GRID),
            "segment_grid": list(SEGMENT_GRID),
            "sharpe_window_trades": SHARPE_WINDOW,
            "sharpe_min_samples": SHARPE_MIN_SAMPLES,
            "segment_window_hours": SEGMENT_WINDOW_HOURS,
            "min_keep_ratio": MIN_KEEP_RATIO,
            "ruin_pnl_retention": RUIN_PNL_RETENTION,
        },
        "sweeps": [
            {
                "vertical": s.vertical,
                "sample_count": s.sample_count,
                "baseline_pnl": round(s.baseline_pnl, 2),
                "sharpe": [g.to_dict() for g in s.sharpe],
                "ruin": [g.to_dict() for g in s.ruin],
                "segment": [g.to_dict() for g in s.segment],
                "recommended": s.recommended,
            }
            for s in sweeps
        ],
    }


def _print_summary(sweeps: list[_VerticalSweep]) -> None:
    print("\nRecommended defaults (per vertical):")
    print(f"  {'vertical':<14} {'samples':>8} {'sharpe':>8} {'ruin':>6} {'segment':>8}")
    for s in sweeps:
        rec = s.recommended
        print(
            f"  {s.vertical:<14} {s.sample_count:>8} "
            f"{(str(rec.get('sharpe_min_ratio')) if rec.get('sharpe_min_ratio') is not None else '-'):>8} "
            f"{(str(rec.get('ruin_max_probability')) if rec.get('ruin_max_probability') is not None else '-'):>6} "
            f"{(str(rec.get('segment_max_losses')) if rec.get('segment_max_losses') is not None else '-'):>8}"
        )


async def _amain(args: argparse.Namespace) -> int:
    if args.from_json:
        trades = _load_trades_from_fixture(Path(args.from_json), args.vertical)
        source_label = f"fixture:{args.from_json}"
    else:
        trades = await _load_trades(args.vertical)
        source_label = "v1_archive.trade_analyses"
    by_vertical: dict[str, list[_Trade]] = {}
    for t in trades:
        by_vertical.setdefault(t.vertical, []).append(t)
    sweeps: list[_VerticalSweep] = [_sweep_for_vertical("__all__", trades)]
    for name in sorted(by_vertical):
        sweeps.append(_sweep_for_vertical(name, by_vertical[name]))
    report = _format_report(sweeps, source_label)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    if args.report:
        report_path = Path(args.report)
    else:
        date_tag = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        report_path = REPORTS_DIR / f"safety_threshold_tuning_{date_tag}.json"
    report_path.write_text(json.dumps(report, indent=2, default=str))
    print(f"[tune_safety_thresholds] wrote {report_path}")
    _print_summary(sweeps)
    if args.apply:
        if not trades:
            print(
                "[tune_safety_thresholds] --apply skipped: no trades in tape "
                "(would clobber tuned defaults with NULL)",
                file=sys.stderr,
            )
            return 2
        actor = args.actor or "tune_safety_thresholds"
        written = await _apply_recommendations(sweeps, actor)
        print(f"[tune_safety_thresholds] wrote {len(written)} system_settings rows")
        for row in written:
            print(f"  {row['key']} = {row['value']}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--vertical",
        default=None,
        help="Limit the sweep to a single vertical (debug / quick re-run).",
    )
    p.add_argument(
        "--report",
        default=None,
        help="Output path for the JSON report (default: dated under reports/).",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Write the recommended per-vertical defaults into "
            "system_settings as risk.<knob>(.<vertical>) rows."
        ),
    )
    p.add_argument(
        "--actor",
        default=None,
        help="Audit actor for the --apply path (default: script name).",
    )
    p.add_argument(
        "--from-json",
        default=None,
        help=(
            "Load trades from a JSON fixture instead of the DB.  Used by the "
            "committed sweep report so the recommendations are reproducible "
            "without the production V1 dump.  See "
            "scripts/fixtures/v1_settled_trades_synthetic.json for the shape."
        ),
    )
    args = p.parse_args()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
