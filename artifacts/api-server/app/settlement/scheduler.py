"""Periodic settlement loop — same shape as the orchestrator scheduler.

One asyncio task wakes every `settlement_interval_seconds`, runs
`run_cycle()`, and applies exponential backoff on failure.  Loop never
exits except via `stop()`.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .. import calibrators, margin_regressor
from ..config import get_settings
from .pipeline import run_cycle
from .retrain import run_retrain_cycle


log = logging.getLogger(__name__)


class _SettlementScheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task[Any] | None = None
        self._stop = asyncio.Event()
        self._last: dict[str, Any] = {}

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
