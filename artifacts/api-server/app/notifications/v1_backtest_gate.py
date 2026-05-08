"""Weekly V1 backtest gate (Task #756 / roadmap T-22).

Once per week, replay the V1 archive through the live calibrators (the
existing ``scripts.backtest_v1_archive.run`` entry point), compute the
``parity_gate_status`` manifest, compare each league's current
``brier_calibrated`` against the prior week's persisted value, and
INSERT a row into ``v1_backtest_gate_runs`` so the dashboard tile can
go red the moment any league regresses.

Configuration:
    V1_BACKTEST_GATE_ENABLED          (default: 1)
    V1_BACKTEST_GATE_INTERVAL_SECONDS (default: 7*86400 = 604800 — weekly)
    V1_BACKTEST_GATE_LIMIT            (default: 10000 — keeps each cron
                                       run bounded; set to 0 for "no
                                       cap, walk the whole archive")
    V1_BACKTEST_GATE_LEAGUES          (default: "" — comma list, blank =
                                       all leagues)
    V1_BACKTEST_GATE_WRITE_JSON       (default: 0 — when 1, also writes
                                       the full report to
                                       ``reports/v1_backtest_full_archive_<ts>.json``
                                       so the investor-deck script can
                                       pick it up)

Schema (migration 0046):
    id, run_at, overall_verdict, parity_gate_status, per_league_brier,
    regressed_leagues, report_path, duration_seconds, notes
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import db
from ..health import error_log


log = logging.getLogger("axiom.notifications.v1_backtest_gate")


# Re-export the same Brier tolerance the offline script uses so the
# weekly cron and the ad-hoc command-line invocation reach the same
# verdicts.  Imported lazily so importing this module never pulls
# `app.predictor` etc. through `scripts.backtest_v1_archive`.
def _brier_tolerance() -> float:
    from scripts import backtest_v1_archive as _bt
    return float(_bt._BRIER_TOLERANCE)


# ---------------------------------------------------------------------------
# Env knobs.
# ---------------------------------------------------------------------------


def _enabled() -> bool:
    raw = (os.environ.get("V1_BACKTEST_GATE_ENABLED") or "1").strip().lower()
    return raw in ("1", "true", "yes", "on", "y")


def _interval_seconds() -> float:
    try:
        return max(
            60.0,
            float(
                os.environ.get("V1_BACKTEST_GATE_INTERVAL_SECONDS")
                or str(7 * 86400)
            ),
        )
    except (TypeError, ValueError):
        return 7 * 86400.0


def _limit() -> int | None:
    raw = (os.environ.get("V1_BACKTEST_GATE_LIMIT") or "10000").strip()
    if not raw:
        return 10000
    try:
        v = int(raw)
        return None if v <= 0 else v
    except (TypeError, ValueError):
        return 10000


def _leagues_filter() -> set[str] | None:
    raw = (os.environ.get("V1_BACKTEST_GATE_LEAGUES") or "").strip()
    if not raw:
        return None
    out = {tok.strip().upper() for tok in raw.split(",") if tok.strip()}
    return out or None


def _write_json_enabled() -> bool:
    raw = (os.environ.get("V1_BACKTEST_GATE_WRITE_JSON") or "0").strip().lower()
    return raw in ("1", "true", "yes", "on", "y")


# ---------------------------------------------------------------------------
# Regression diff
# ---------------------------------------------------------------------------


def _per_league_brier(report: dict[str, Any]) -> dict[str, float]:
    """Pull per-league ``brier_calibrated`` out of the audit report.

    Falls back to ``brier_v1_raw`` if the calibrator path produced no
    score (e.g. league has no calibrator yet) so the prior-vs-current
    comparison still has *something* to compare against, and missing
    values are simply omitted from the diff.
    """
    out: dict[str, float] = {}
    for league, row in (report.get("per_league") or {}).items():
        if not isinstance(row, dict):
            continue
        b = row.get("brier_calibrated")
        if b is None:
            b = row.get("brier_v1_raw")
        if b is None:
            continue
        try:
            out[str(league).upper()] = float(b)
        except (TypeError, ValueError):
            continue
    return out


def detect_regressions(
    *,
    current: dict[str, float],
    prior: dict[str, float] | None,
    tolerance: float | None = None,
) -> list[dict[str, Any]]:
    """Leagues whose Brier got *worse* by more than ``tolerance``.

    Returns a list of ``{"league", "current", "prior", "delta"}`` dicts
    so the dashboard tile and the runbook playbook can both read the
    raw before/after numbers without re-querying.  When there is no
    prior week the list is always empty (no baseline ⇒ no regression
    can be detected yet).
    """
    if not prior:
        return []
    tol = float(tolerance) if tolerance is not None else _brier_tolerance()
    out: list[dict[str, Any]] = []
    for league, cur in sorted(current.items()):
        prv = prior.get(league)
        if prv is None:
            continue
        delta = float(cur) - float(prv)
        if delta > tol:
            out.append(
                {
                    "league": league,
                    "current": round(float(cur), 6),
                    "prior": round(float(prv), 6),
                    "delta": round(delta, 6),
                }
            )
    return out


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


async def _fetch_prior_run() -> dict[str, Any] | None:
    """Latest persisted row, or None if the table is empty."""
    try:
        row = await db.fetchrow(
            """
            SELECT id,
                   to_char(run_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS run_at,
                   overall_verdict,
                   parity_gate_status,
                   per_league_brier,
                   regressed_leagues,
                   report_path,
                   duration_seconds
              FROM v1_backtest_gate_runs
             ORDER BY run_at DESC
             LIMIT 1
            """
        )
    except Exception as e:
        log.warning("v1_backtest_gate_runs prior fetch failed: %s", e)
        return None
    if not row:
        return None
    return _decode_row(dict(row))


def _decode_row(row: dict[str, Any]) -> dict[str, Any]:
    """asyncpg may hand JSONB back as already-decoded dict or as raw
    text depending on driver setup.  Normalize."""

    def _to_obj(v: Any) -> Any:
        if v is None or isinstance(v, (dict, list)):
            return v
        if isinstance(v, (str, bytes, bytearray)):
            try:
                return _json.loads(v)
            except (ValueError, TypeError):
                return None
        return v

    row["parity_gate_status"] = _to_obj(row.get("parity_gate_status")) or {}
    row["per_league_brier"] = _to_obj(row.get("per_league_brier")) or {}
    row["regressed_leagues"] = list(row.get("regressed_leagues") or [])
    if row.get("duration_seconds") is not None:
        row["duration_seconds"] = float(row["duration_seconds"])
    return row


async def _persist_run(
    *,
    overall_verdict: str,
    parity_gate_status: dict[str, Any],
    per_league_brier: dict[str, float],
    regressed_leagues: list[str],
    report_path: str | None,
    duration_seconds: float,
    notes: str | None = None,
) -> int | None:
    """INSERT into ``v1_backtest_gate_runs``; returns the new row id."""
    try:
        row = await db.fetchrow(
            """
            INSERT INTO v1_backtest_gate_runs (
                overall_verdict,
                parity_gate_status,
                per_league_brier,
                regressed_leagues,
                report_path,
                duration_seconds,
                notes
            ) VALUES ($1, $2::jsonb, $3::jsonb, $4::text[], $5, $6, $7)
            RETURNING id
            """,
            overall_verdict,
            _json.dumps(parity_gate_status, default=str),
            _json.dumps(per_league_brier, default=str),
            list(regressed_leagues),
            report_path,
            float(duration_seconds),
            notes,
        )
    except Exception as e:
        log.warning("v1_backtest_gate_runs INSERT failed: %s", e)
        return None
    return int(row["id"]) if row else None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def run_weekly_v1_backtest(
    *,
    limit: int | None = None,
    leagues_filter: set[str] | None = None,
    write_json: bool | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """One-shot weekly backtest gate run.

    Returns a dict the operator console can echo back without any
    further DB fetches:
        {
          "id": <row id or None>,
          "overall_verdict": "PASS" | "FAIL" | ...,
          "parity_gate_status": {...},
          "per_league_brier": {league: brier_cal},
          "regressed_leagues": ["NBA", ...],
          "regression_detail": [{league, current, prior, delta}, ...],
          "report_path": "reports/...json" | None,
          "duration_seconds": float,
        }
    """
    from scripts import backtest_v1_archive as _bt

    started = time.monotonic()
    eff_limit = limit if limit is not None else _limit()
    eff_leagues = leagues_filter if leagues_filter is not None else _leagues_filter()
    eff_write_json = (
        write_json if write_json is not None else _write_json_enabled()
    )
    log.info(
        "v1_backtest_gate run starting: limit=%s leagues=%s write_json=%s",
        eff_limit,
        sorted(eff_leagues) if eff_leagues else None,
        eff_write_json,
    )

    report: dict[str, Any]
    try:
        report = await _bt.run(
            rows=None,
            limit=eff_limit,
            leagues_filter=eff_leagues,
            load_calibrators=True,
            full_stack=False,
            # Small batches ensure asyncio.sleep(0) fires frequently so the
            # event loop stays responsive (healthz, feeds) during the replay.
            batch_size=500,
        )
    except Exception as e:
        log.exception("v1_backtest_gate replay failed: %s", e)
        error_log("v1_backtest_gate", f"{type(e).__name__}: {e}")
        # Persist an ERROR row so the weekly history isn't silent about
        # crashed runs — otherwise the dashboard tile shows the prior
        # week's PASS as if nothing happened.  Best-effort: a DB-side
        # failure here is logged but does not change the return value.
        err_msg = f"{type(e).__name__}: {e}"[:240]
        err_status = {"overall": "ERROR", "error": err_msg}
        err_id: int | None = None
        if persist:
            err_id = await _persist_run(
                overall_verdict="ERROR",
                parity_gate_status=err_status,
                per_league_brier={},
                regressed_leagues=[],
                report_path=None,
                duration_seconds=time.monotonic() - started,
                notes=err_msg,
            )
        return {
            "id": err_id,
            "overall_verdict": "ERROR",
            "parity_gate_status": err_status,
            "per_league_brier": {},
            "regressed_leagues": [],
            "regression_detail": [],
            "report_path": None,
            "duration_seconds": time.monotonic() - started,
            "error": err_msg,
        }

    parity = report.get("parity_gate_status") or {}
    overall_verdict = str(parity.get("overall") or "UNKNOWN")
    per_league_brier = _per_league_brier(report)
    prior = await _fetch_prior_run()
    regression_detail = detect_regressions(
        current=per_league_brier,
        prior=(prior or {}).get("per_league_brier"),
    )
    regressed_leagues = sorted({d["league"] for d in regression_detail})

    report_path: str | None = None
    if eff_write_json:
        try:
            ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            out_dir = Path(__file__).resolve().parents[2] / "reports"
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"v1_backtest_full_archive_{ts}.json"
            out_path.write_text(_json.dumps(report, indent=2, default=str))
            report_path = str(out_path)
        except Exception as e:
            log.warning("v1_backtest_gate report write failed: %s", e)

    # F762 — best-effort auto-build the investor deck from this run's
    # JSON report so the operator/IR team always has a fresh PPTX
    # without an extra manual step.  Failure is logged but never
    # blocks the cron — the deck is a downstream artefact, not a gate.
    investor_deck_path: str | None = None
    if report_path:
        investor_deck_path = await _build_investor_deck_best_effort(
            report_path
        )

    notes_payload: str | None = None
    if investor_deck_path:
        notes_payload = _json.dumps(
            {"investor_deck_path": investor_deck_path}
        )

    duration = time.monotonic() - started
    new_id: int | None = None
    if persist:
        new_id = await _persist_run(
            overall_verdict=overall_verdict,
            parity_gate_status=parity,
            per_league_brier=per_league_brier,
            regressed_leagues=regressed_leagues,
            report_path=report_path,
            duration_seconds=duration,
            notes=notes_payload,
        )

    return {
        "id": new_id,
        "overall_verdict": overall_verdict,
        "parity_gate_status": parity,
        "per_league_brier": per_league_brier,
        "regressed_leagues": regressed_leagues,
        "regression_detail": regression_detail,
        "report_path": report_path,
        "investor_deck_path": investor_deck_path,
        "duration_seconds": duration,
        "row_kept_count": int(report.get("row_kept_count") or 0),
        "row_input_count": int(report.get("row_input_count") or 0),
    }


# Fixed location for the latest deck so the daily honesty report can
# always link to a stable path.  Older decks (per-run, timestamped)
# can be added later if archival becomes a requirement.
_INVESTOR_DECK_LATEST_NAME = "investor_deck_latest.pptx"


async def _build_investor_deck_best_effort(report_path: str) -> str | None:
    """Run :func:`scripts.build_investor_deck.build_deck` in a worker
    thread (the slide builder uses ``python-pptx`` synchronously).

    Returns the resulting deck path on success, ``None`` on any
    failure — the cron must not fail because the deck builder did.
    """
    try:
        from scripts.build_investor_deck import build_deck

        in_path = Path(report_path)
        if not in_path.exists():
            log.warning(
                "investor deck skipped: report_path missing %s", report_path
            )
            return None
        out_path = in_path.parent / _INVESTOR_DECK_LATEST_NAME
        result = await asyncio.to_thread(
            build_deck,
            report_path=in_path,
            output_path=out_path,
        )
        log.info("investor deck rebuilt: %s", result)
        return str(result)
    except Exception as e:  # pragma: no cover — defensive
        log.warning("investor deck auto-build failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Background scheduler — fires every `interval_seconds`.
# ---------------------------------------------------------------------------


_task: asyncio.Task | None = None
_stop = asyncio.Event()
_last_run_monotonic: float | None = None


async def _loop() -> None:
    global _last_run_monotonic
    interval = _interval_seconds()
    log.info(
        "v1_backtest_gate scheduler started: interval=%ss limit=%s",
        int(interval),
        _limit(),
    )
    # Stagger first run by a short grace so we don't pile onto boot.
    grace = float(os.environ.get("V1_BACKTEST_GATE_BOOT_GRACE_SECONDS") or "60")
    try:
        await asyncio.wait_for(_stop.wait(), timeout=grace)
        return
    except asyncio.TimeoutError:
        pass

    while not _stop.is_set():
        try:
            await run_weekly_v1_backtest()
            _last_run_monotonic = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("v1_backtest_gate loop error: %s", e)
        try:
            await asyncio.wait_for(_stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
        else:
            return


async def start() -> None:
    global _task
    if not _enabled():
        log.info("v1_backtest_gate scheduler disabled")
        return
    if _task and not _task.done():
        return
    _stop.clear()
    _task = asyncio.create_task(_loop(), name="v1-backtest-gate")


async def stop() -> None:
    global _task
    _stop.set()
    if _task:
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):
            pass
    _task = None
