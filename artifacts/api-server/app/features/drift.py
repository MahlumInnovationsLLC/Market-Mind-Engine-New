"""Feature-distribution drift report.

Maintains an in-process rolling sample of the feature vectors the
predictor has seen, then compares their per-column moments against a
training-time baseline.  Surfaced via ``GET /api/features/drift`` and
periodically logged by :class:`DriftSnapshotScheduler`.

The baseline lives in ``app/features/baselines/training_v1.json`` and
is shipped with the repo so a fresh install can produce a meaningful
report on the first request.  Operators can drop a different baseline
file in place (or set ``AXIOM_FEATURE_DRIFT_BASELINE`` to a different
path) without code changes.

Thresholds for ``z_drift_warn`` / ``z_drift_alert`` come from
``runtime_settings`` so an operator can dial sensitivity from the
admin panel without a redeploy.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from .registry import registry


log = logging.getLogger("axiom.features.drift")


# How many recent observations we keep per process.  ~500 vectors at
# 32 floats each ≈ 130 KiB — negligible memory cost, big enough to
# smooth out the per-cycle noise without making the window so wide
# that yesterday's regime drowns out today's drift signal.
_DEFAULT_WINDOW_SIZE = 500

# Minimum observations before we report any drift z-score; below this
# the std estimate is too noisy to be useful and would just produce
# false alerts on cold start.
_MIN_SAMPLES_FOR_DRIFT = 25


@dataclass(frozen=True)
class _BaselineRow:
    """Per-feature training-time stats."""
    name: str
    mean: float
    std: float
    n: int


class _RollingStats:
    """Welford-style running mean/std over a fixed-size window.

    We could keep raw samples and recompute on read, but the cycle can
    fire dozens of vectors per minute and the operator console hits
    the endpoint on a poll loop — recomputing each time would burn
    cycles.  A rolling buffer + summation gives us O(1) updates.
    """

    __slots__ = ("_buf", "_sum", "_sumsq")

    def __init__(self, maxlen: int) -> None:
        self._buf: deque[float] = deque(maxlen=maxlen)
        self._sum = 0.0
        self._sumsq = 0.0

    def add(self, x: float) -> None:
        if len(self._buf) == self._buf.maxlen:
            # Evicted sample — back it out of the running totals so we
            # stay numerically aligned with the buffer contents.
            old = self._buf[0]
            self._sum -= old
            self._sumsq -= old * old
        self._buf.append(x)
        self._sum += x
        self._sumsq += x * x

    @property
    def n(self) -> int:
        return len(self._buf)

    def mean(self) -> float:
        if not self._buf:
            return 0.0
        return self._sum / len(self._buf)

    def std(self) -> float:
        n = len(self._buf)
        if n < 2:
            return 0.0
        m = self._sum / n
        var = max(0.0, (self._sumsq / n) - (m * m))
        return math.sqrt(var)


class FeatureDriftCollector:
    """Process-wide rolling stats per registered feature."""

    def __init__(
        self,
        *,
        window_size: int = _DEFAULT_WINDOW_SIZE,
        baseline_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self._window_size = window_size
        self._lock = threading.Lock()
        self._stats: dict[str, _RollingStats] = {}
        self._observations = 0
        self._baseline_path = (
            Path(baseline_path) if baseline_path else _default_baseline_path()
        )
        self._baseline: dict[str, _BaselineRow] = {}
        self._baseline_loaded_from: str | None = None
        self._reload_baseline()
        self._reset_stats()

    def _reset_stats(self) -> None:
        for spec in registry.specs():
            self._stats[spec.name] = _RollingStats(self._window_size)

    def _reload_baseline(self) -> None:
        try:
            raw = json.loads(self._baseline_path.read_text())
            features = raw.get("features") or {}
            self._baseline = {
                name: _BaselineRow(
                    name=name,
                    mean=float(row.get("mean", 0.0)),
                    std=float(row.get("std", 0.0)),
                    n=int(row.get("n", 0)),
                )
                for name, row in features.items()
            }
            self._baseline_loaded_from = str(self._baseline_path)
        except FileNotFoundError:
            self._baseline = {}
            self._baseline_loaded_from = None
        except Exception as e:  # noqa: BLE001 — sick baseline shouldn't break drift
            log.warning(
                "feature drift baseline %s unreadable: %s",
                self._baseline_path, e,
            )
            self._baseline = {}
            self._baseline_loaded_from = None

    # ----- write path -----------------------------------------------------

    def record(self, values: Sequence[float]) -> None:
        names = registry.names()
        if len(values) != len(names):
            # Defensive: a future model column add should be caught at
            # import time by the assert in :func:`build_vector`, but
            # if a hand-rolled caller hits us with a short row we just
            # truncate so we don't poison the stats with NaNs.
            n = min(len(values), len(names))
        else:
            n = len(names)
        with self._lock:
            self._observations += 1
            for i in range(n):
                v = values[i]
                if v is None:
                    continue
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    continue
                if math.isnan(fv) or math.isinf(fv):
                    continue
                self._stats[names[i]].add(fv)

    # ----- read path ------------------------------------------------------

    def report(self) -> dict[str, Any]:
        """Snapshot of every feature's live stats vs baseline.

        ``z_drift`` is a unitless drift score — how many baseline-σ the
        live mean has shifted by — capped to ``±50`` so a degenerate
        ``baseline.std == 0`` row doesn't return ``inf`` and break the
        JSON serializer.  ``status`` is ``ok`` / ``warn`` / ``alert``
        based on the absolute z-drift; thresholds default to 2σ / 4σ
        and can be overridden via runtime settings.
        """
        warn_threshold, alert_threshold = _drift_thresholds()
        with self._lock:
            rows: list[dict[str, Any]] = []
            warn_count = 0
            alert_count = 0
            for spec in registry.specs():
                rs = self._stats[spec.name]
                base = self._baseline.get(spec.name)
                live_mean = rs.mean()
                live_std = rs.std()
                z = None
                std_ratio = None
                status = "insufficient_samples"
                if rs.n >= _MIN_SAMPLES_FOR_DRIFT and base is not None:
                    if base.std > 0:
                        z = (live_mean - base.mean) / base.std
                        # Hard cap so JSON stays finite; drift > 50σ is
                        # already "everything is on fire" territory.
                        z = max(-50.0, min(50.0, z))
                    else:
                        # Constant feature in baseline — surface ANY
                        # live std as drift; otherwise mean-difference
                        # becomes the drift signal.
                        z = 0.0 if live_mean == base.mean else math.copysign(50.0, live_mean - base.mean)
                    if base.std > 0 and live_std >= 0:
                        std_ratio = live_std / base.std
                    abs_z = abs(z)
                    if abs_z >= alert_threshold:
                        status = "alert"
                        alert_count += 1
                    elif abs_z >= warn_threshold:
                        status = "warn"
                        warn_count += 1
                    else:
                        status = "ok"
                rows.append({
                    "name": spec.name,
                    "dtype": spec.dtype,
                    "n_live": rs.n,
                    "live_mean": _round(live_mean),
                    "live_std": _round(live_std),
                    "baseline_mean": _round(base.mean) if base else None,
                    "baseline_std": _round(base.std) if base else None,
                    "baseline_n": base.n if base else None,
                    "z_drift": _round(z) if z is not None else None,
                    "std_ratio": _round(std_ratio) if std_ratio is not None else None,
                    "status": status,
                })
            return {
                "window_size": self._window_size,
                "observations": self._observations,
                "min_samples_for_drift": _MIN_SAMPLES_FOR_DRIFT,
                "warn_threshold_z": warn_threshold,
                "alert_threshold_z": alert_threshold,
                "baseline_path": self._baseline_loaded_from,
                "baseline_features": len(self._baseline),
                "warn_count": warn_count,
                "alert_count": alert_count,
                "items": rows,
            }

    # ----- test / admin helpers ------------------------------------------

    def reset(self) -> None:
        """Drop all observations and reload the baseline file."""
        with self._lock:
            self._observations = 0
            self._reset_stats()
            self._reload_baseline()


def _round(v: float | None, digits: int = 6) -> float | None:
    if v is None:
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return round(float(v), digits)


def _default_baseline_path() -> Path:
    env = os.environ.get("AXIOM_FEATURE_DRIFT_BASELINE")
    if env:
        return Path(env)
    return Path(__file__).parent / "baselines" / "training_v1.json"


def _drift_thresholds() -> tuple[float, float]:
    """Read warn/alert z-score cutoffs from runtime settings (best-effort).

    Defaults: warn at 2σ, alert at 4σ — modest sensitivity that
    matches the rule-of-thumb ranges used by the existing PnL drift
    alerts so an operator only sees one threshold language.
    """
    warn = float(os.environ.get("AXIOM_FEATURE_DRIFT_WARN_Z", "2") or 2.0)
    alert = float(os.environ.get("AXIOM_FEATURE_DRIFT_ALERT_Z", "4") or 4.0)
    return warn, alert


# ---------------------------------------------------------------------------
# Module-level singleton + free functions used by the rest of the app.
# ---------------------------------------------------------------------------


collector = FeatureDriftCollector()


def record_observation(values: Iterable[float]) -> None:
    """Public hook called by :func:`build_features` on every cycle."""
    collector.record(list(values))


def report() -> dict[str, Any]:
    return collector.report()


def reset() -> None:
    collector.reset()


# ---------------------------------------------------------------------------
# Periodic snapshot scheduler
# ---------------------------------------------------------------------------


class DriftSnapshotScheduler:
    """Logs the drift summary every ``interval_seconds`` seconds.

    A heavy-weight (database-backed) scheduled job would be overkill
    for an in-process rolling window — a simple log line per interval
    gives operators a time series in their existing log aggregator
    without a new table.  The HTTP endpoint remains the source of
    truth for ad-hoc inspection.
    """

    def __init__(self, interval_seconds: float = 300.0) -> None:
        self._interval = max(30.0, float(interval_seconds))
        self._task: asyncio.Task[Any] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="features-drift-snapshot")
        log.info("feature drift snapshot scheduler started (interval=%.0fs)", self._interval)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
                return  # stop fired
            except asyncio.TimeoutError:
                pass
            try:
                snap = collector.report()
                log.info(
                    "feature_drift snapshot: obs=%d warn=%d alert=%d baseline=%s",
                    snap["observations"],
                    snap["warn_count"],
                    snap["alert_count"],
                    snap["baseline_path"] or "<none>",
                )
            except Exception as e:  # noqa: BLE001 — never let logging crash the loop
                log.debug("feature drift snapshot failed: %s", e)


scheduler = DriftSnapshotScheduler()
