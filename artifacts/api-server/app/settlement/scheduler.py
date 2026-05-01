"""Periodic settlement loop — same shape as the orchestrator scheduler.

One asyncio task wakes every `settlement_interval_seconds`, runs
`run_cycle()`, and applies exponential backoff on failure.  Loop never
exits except via `stop()`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .. import calibrators, margin_regressor
from ..config import get_settings
from .pipeline import run_cycle, _calibration_and_drift_shadow
from .retrain import run_retrain_cycle


log = logging.getLogger(__name__)


# Shadow predict + settle is intentionally lower-frequency than the
# settlement cycle (which can fire every few minutes during peak
# trading).  Hourly is the right granularity because:
#   * shadow_predictions is keyed (market_id, hour-floored predicted_at)
#     so a sub-hour run is a no-op for the predictor anyway.
#   * a write rate of ~1k active markets/hour keeps shadow_predictions
#     under 25k rows/day, well below storage thresholds.
SHADOW_INTERVAL_SECONDS: float = 3600.0


class _SettlementScheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task[Any] | None = None
        self._stop = asyncio.Event()
        self._last: dict[str, Any] = {}
        # Monotonic timestamp (seconds) of the last shadow cycle.
        # `0.0` makes the first settlement cycle after process start
        # also drive a shadow cycle, so a cold deploy doesn't sit
        # idle for an hour before the first snapshot lands.
        self._last_shadow_at: float = 0.0

    def status(self) -> dict[str, Any]:
        s = get_settings()
        return {
            "enabled": s.settlement_enabled,
            "running": bool(self._task and not self._task.done()),
            "interval_seconds": s.settlement_interval_seconds,
            "last_cycle": self._last,
        }

    async def start(self) -> None:
        s = get_settings()
        if not s.settlement_enabled:
            log.info("settlement scheduler disabled by config")
            return
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="settlement-loop")
        log.info(
            "settlement scheduler started: interval=%ss", s.settlement_interval_seconds
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                log.warning("settlement task exit error: %s", e)
        self._task = None
        log.info("settlement scheduler stopped")

    async def _run_shadow_cycle(self) -> dict[str, Any]:
        """Run one full shadow cycle: predict -> settle -> drift parity.

        Each subsection is wrapped independently so a failure in (say)
        the drift loop doesn't lose us the predict/settle counts.  All
        errors land in the returned summary; nothing here re-raises.
        """
        from ..learning import shadow_predictor, shadow_settler

        summary: dict[str, Any] = {"interval_seconds": SHADOW_INTERVAL_SECONDS}

        try:
            pr = await shadow_predictor.run_cycle()
            summary["predictor"] = {
                "started_at": pr.started_at,
                "completed_at": pr.completed_at,
                "duration_seconds": pr.duration_seconds,
                "candidates_scanned": pr.candidates_scanned,
                "snapshots_written": pr.snapshots_written,
                "skipped_duplicate": pr.skipped_duplicate,
                "skipped_no_prob": pr.skipped_no_prob,
                "errors": pr.errors,
            }
        except Exception as e:
            log.exception("shadow predictor cycle failed: %s", e)
            summary["predictor"] = {"error": f"{type(e).__name__}: {e}"}

        try:
            sr = await shadow_settler.run_cycle()
            summary["settler"] = {
                "started_at": sr.started_at,
                "completed_at": sr.completed_at,
                "duration_seconds": sr.duration_seconds,
                "candidates_scanned": sr.candidates_scanned,
                "settled": sr.settled,
                "buffer_inserted": sr.buffer_inserted,
                "errors": sr.errors,
            }
        except Exception as e:
            log.exception("shadow settler cycle failed: %s", e)
            summary["settler"] = {"error": f"{type(e).__name__}: {e}"}

        # Drift parity: walk the registered verticals and emit per-
        # vertical brier_drift snapshots/alerts off the shadow buffer.
        # We import the registry here (rather than at module top) so
        # the scheduler has zero import-order dependency on the
        # verticals package — relevant because settlement.scheduler is
        # imported by the API server bootstrap path.
        try:
            from .. import verticals as _v
            specs = _v.all_specs()
        except Exception as e:  # pragma: no cover — defensive
            log.warning("shadow drift: verticals registry unavailable: %s", e)
            specs = []
        drift_alerts = 0
        drift_errors: list[str] = []
        for spec in specs:
            try:
                drift_alerts += await _calibration_and_drift_shadow(spec.name)
            except Exception as e:
                drift_errors.append(f"{spec.name}: {type(e).__name__}: {e}")
        summary["drift"] = {
            "verticals_scanned": len(specs),
            "alerts_raised": drift_alerts,
            "errors": drift_errors,
        }
        log.info(
            "[shadow] cycle done predictor=%s settler=%s drift_alerts=%d",
            summary.get("predictor", {}).get("snapshots_written", "err"),
            summary.get("settler", {}).get("buffer_inserted", "err"),
            drift_alerts,
        )
        return summary

    async def _loop(self) -> None:
        backoff = 0.0
        while not self._stop.is_set():
            try:
                cr = await run_cycle()
                # After settling fresh outcomes, drain the online-learning
                # buffer and refit any vertical that crossed the warning
                # threshold (or accumulated enough fresh signal).  Run
                # in the same loop so we share the backoff/cadence logic
                # and don't need a second long-lived task.
                try:
                    rr = await run_retrain_cycle()
                    retrain_summary = {
                        "started_at": rr.started_at,
                        "completed_at": rr.completed_at,
                        "duration_seconds": rr.duration_seconds,
                        "verticals_examined": rr.verticals_examined,
                        "verticals_retrained": rr.verticals_retrained,
                        "rows_processed": rr.rows_processed,
                        "errors": rr.errors,
                    }
                except Exception as e:
                    log.exception("retrain cycle failed: %s", e)
                    retrain_summary = {"error": f"{type(e).__name__}: {e}"}
                # Nightly per-league calibrator refit. The helper
                # internally gates on a 24h cadence so this is cheap on
                # every other cycle and only does work once per day.
                try:
                    calib_summary = await calibrators.maybe_refit()
                except Exception as e:
                    log.exception("per-league calibrator refit failed: %s", e)
                    calib_summary = {"error": f"{type(e).__name__}: {e}"}
                # Refresh the per-league margin regressors against the
                # latest finished games + closing lines so spread/total
                # adjustments use freshly-fitted coefficients (not the
                # cold-start defaults).  Best-effort — failures here
                # never take down the settlement loop.
                try:
                    mr_summary = (await margin_regressor.refit_all_leagues()).as_dict()
                except Exception as e:
                    log.exception("margin regressor refit failed: %s", e)
                    mr_summary = {"error": f"{type(e).__name__}: {e}"}
                # Hourly shadow loop (task #753).  Snapshots calibrated
                # YES probabilities for every active Kalshi market, then
                # settles those whose underlying market resolved on the
                # live predictions table, then runs drift parity per
                # registered vertical.  Best-effort throughout —
                # any subsection failure is captured in the summary so
                # operator visibility is preserved without breaking the
                # core settlement cadence.
                shadow_summary: dict[str, Any] | None = None
                now_mono = time.monotonic()
                if (now_mono - self._last_shadow_at) >= SHADOW_INTERVAL_SECONDS:
                    shadow_summary = await self._run_shadow_cycle()
                    self._last_shadow_at = now_mono
                self._last = {
                    "started_at": cr.started_at,
                    "completed_at": cr.completed_at,
                    "duration_seconds": cr.duration_seconds,
                    "markets_examined": cr.markets_examined,
                    "markets_settled": cr.markets_settled,
                    "trades_settled": cr.trades_settled,
                    "persona_metrics_updated": cr.persona_metrics_updated,
                    "alerts_raised": cr.alerts_raised,
                    "errors": cr.errors,
                    "retrain": retrain_summary,
                    "calibrators": calib_summary,
                    "margin_regressor": mr_summary,
                    "shadow": shadow_summary or self._last.get("shadow"),
                }
                backoff = 0.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("settlement cycle failed: %s", e)
                backoff = min(120.0, max(5.0, backoff * 2 if backoff else 5.0))
            settings = get_settings()
            sleep_s = settings.settlement_interval_seconds + backoff
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=sleep_s)
            except asyncio.TimeoutError:
                pass
            else:
                return


scheduler = _SettlementScheduler()
