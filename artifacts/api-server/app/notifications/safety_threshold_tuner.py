"""Periodic safety-threshold tuner (task #814).

Task #732 shipped ``scripts/tune_safety_thresholds.py`` and a tuned
set of per-vertical defaults derived from the V1 settled-trade tape.
Every meta-model retrain shifts the realised return distribution, so
the recommended thresholds drift over time.  This module:

* Exposes :func:`run_after_retrain` — a fire-and-forget hook the
  retrain pipeline (or its CLI wrapper) calls right after publishing
  a fresh ``meta_model.pkl``.  It runs the sweep, writes a fresh
  timestamped report to ``scripts/reports/``, and emits a
  ``safety_thresholds_tuned`` settlement-event so downstream
  listeners (operator dashboard refresh, audit log) can react.
* Runs a daily background loop as a belt-and-suspenders fallback so
  the report keeps refreshing on its own when an operator triggers
  a retrain out-of-band (e.g. a hot-fix on a checked-out CLI host
  that never hits our retrain hook).

The sweep itself is read-only against ``v1_archive.trade_analyses``
— it never writes to ``system_settings`` from here.  Promotion of
new recommendations into ``runtime_settings.PER_VERTICAL_NEW_GATE_DEFAULTS``
is a deliberate operator-review step (see
``docs/safety_threshold_tuner_runbook.md``).

Configuration:
    SAFETY_THRESHOLD_TUNER_ENABLED          (default: 1)
    SAFETY_THRESHOLD_TUNER_INTERVAL_SECONDS (default: 86400 — daily)
    SAFETY_THRESHOLD_TUNER_BOOT_GRACE_SECONDS (default: 120)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from ..health import error_log


log = logging.getLogger("axiom.notifications.safety_threshold_tuner")


def _enabled() -> bool:
    raw = (os.environ.get("SAFETY_THRESHOLD_TUNER_ENABLED") or "1").strip().lower()
    return raw in ("1", "true", "yes", "on", "y")


def _interval_seconds() -> float:
    try:
        return max(
            60.0,
            float(
                os.environ.get("SAFETY_THRESHOLD_TUNER_INTERVAL_SECONDS")
                or str(86400)
            ),
        )
    except (TypeError, ValueError):
        return 86400.0


async def run_after_retrain(*, reason: str = "manual") -> dict[str, Any]:
    """Re-run the safety-threshold sweep and persist a fresh report.

    Designed to be called from the retrain pipeline immediately after
    a new ``meta_model.pkl`` is published.  Returns a small summary
    dict the caller can log; never raises — a sweep failure logs the
    error and returns ``{"ok": False, "error": "..."}``.
    """
    started = time.monotonic()
    try:
        # Imported lazily so the module import chain stays cheap on
        # boot (the script pulls in statistics / numpy paths).
        from scripts.tune_safety_thresholds import run_and_write_report

        report = await run_and_write_report()
    except Exception as e:  # noqa: BLE001
        log.exception("safety threshold sweep failed: %s", e)
        error_log("safety_threshold_tuner", f"{type(e).__name__}: {e}")
        return {
            "ok": False,
            "reason": reason,
            "error": f"{type(e).__name__}: {e}",
            "duration_seconds": time.monotonic() - started,
        }
    # Drop the internal sweep list before logging — it isn't JSON
    # safe and the caller doesn't need it.
    report.pop("_sweeps_internal", None)
    duration = time.monotonic() - started
    log.info(
        "safety threshold sweep complete: reason=%s path=%s duration=%.1fs",
        reason,
        report.get("report_path"),
        duration,
    )
    return {
        "ok": True,
        "reason": reason,
        "report_path": report.get("report_path"),
        "generated_at": report.get("generated_at"),
        "duration_seconds": duration,
    }


# ---------------------------------------------------------------------------
# Background scheduler — daily fallback so the report keeps refreshing
# even when an operator retrains out-of-band.
# ---------------------------------------------------------------------------


_task: asyncio.Task | None = None
_stop = asyncio.Event()
_last_run_monotonic: float | None = None


async def _loop() -> None:
    global _last_run_monotonic
    interval = _interval_seconds()
    log.info(
        "safety threshold tuner scheduler started: interval=%ss",
        int(interval),
    )
    grace = float(
        os.environ.get("SAFETY_THRESHOLD_TUNER_BOOT_GRACE_SECONDS") or "120"
    )
    try:
        await asyncio.wait_for(_stop.wait(), timeout=grace)
        return
    except asyncio.TimeoutError:
        pass

    while not _stop.is_set():
        try:
            await run_after_retrain(reason="scheduled")
            _last_run_monotonic = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # pragma: no cover — defensive
            log.warning("safety threshold tuner loop error: %s", e)
        try:
            await asyncio.wait_for(_stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
        else:
            return


async def start() -> None:
    global _task
    if not _enabled():
        log.info("safety threshold tuner scheduler disabled")
        return
    if _task and not _task.done():
        return
    _stop.clear()
    _task = asyncio.create_task(_loop(), name="safety-threshold-tuner")


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
