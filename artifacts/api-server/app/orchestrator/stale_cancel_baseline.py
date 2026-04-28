"""Rolling 24h baseline + anomaly evaluator for the cycle-start
stale-resting cancel sweep (task #711).

The sweep itself lives in
``app.orchestrator.cycle._cancel_stale_resting_orders`` and produces
``{scanned, cancelled, failed, max_age}`` per cycle. This module:

  * Persists each cycle's counters to ``stale_cancel_observations`` so
    we have a 7d time series per vertical.
  * Computes the rolling 24h baseline (mean + sample stdev) of the
    ``cancelled`` series per vertical.
  * Evaluates two anomaly conditions and dispatches the matching
    operator alert exactly once per dedupe window:

      - **Volume anomaly** — current cycle's `cancelled` is more than
        ``sigma_threshold`` (default 3.0) standard deviations above the
        24h mean *and* both the current value and the absolute floor
        cap are large enough that we're not pattern-matching noise.

      - **Failure spike** — the latest ``failure_window`` cycles
        (default 2) all had `failed > 0`. Sustained venue failures are
        the real production signal; a one-off blip is normal.

Everything fails open: a DB error in the recorder doesn't block the
cycle, and an evaluator failure logs + returns rather than raising.
The baseline reader returns sentinel values when the rolling window is
empty so the evaluator can short-circuit without spurious alerts on
fresh deploys.
"""
from __future__ import annotations

import logging
import math
import os
import time
from typing import Any

from .. import db
from ..notifications import alerts as alerts_mod


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables (env defaults; per-vertical and global system_settings rows
# beat env which beats these constants — same precedence as
# ``_resolve_resting_max_age`` in cycle.py).
# ---------------------------------------------------------------------------

DEFAULT_BASELINE_WINDOW_SECS: int = 24 * 60 * 60   # 24h rolling window
DEFAULT_VOLUME_SIGMA: float = 3.0                  # >= 3σ above mean
DEFAULT_VOLUME_FLOOR: int = 10                     # ignore alerts below this absolute count
DEFAULT_FAILURE_WINDOW: int = 2                    # consecutive cycles
DEFAULT_FAILURE_FLOOR: int = 3                     # per-cycle threshold: each
                                                   # cycle in the tail must
                                                   # have ``failed >= floor``
                                                   # for the spike to fire.
DEFAULT_ALERT_COOLDOWN_SECS: int = 1800            # 30m dedupe window — single
                                                   # cooldown shared by the
                                                   # volume anomaly + failure
                                                   # spike alerts per the
                                                   # task #711 settings spec
                                                   # (`risk.stale_cancel_alert_cooldown_secs(.<vertical>)`).
RETENTION_DAYS: int = 7                            # observation table GC
GC_INTERVAL_SECS: int = 86400                      # at most one GC run per
                                                   # day per process — wired
                                                   # via ``maybe_gc_observations``
                                                   # from the cycle tail.

# In-process throttle for ``maybe_gc_observations``. Module-level so it
# survives across run_cycle invocations within the same worker process.
_LAST_GC_TS: float = 0.0


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _resolve_int(
    vertical: str,
    overrides: dict[str, Any],
    base_key: str,
    env_name: str,
    default: int,
) -> int:
    """Settings precedence — same shape as ``_resolve_resting_max_age``.

    ``risk.<base_key>.<vertical>`` > ``risk.<base_key>`` > env > default.
    """
    per_vert = overrides.get(f"{base_key}.{vertical}")
    glb = overrides.get(base_key)
    for v in (per_vert, glb):
        if v is None or v == "":
            continue
        try:
            return int(float(v))
        except (TypeError, ValueError):
            continue
    return _env_int(env_name, default)


def _resolve_float(
    vertical: str,
    overrides: dict[str, Any],
    base_key: str,
    env_name: str,
    default: float,
) -> float:
    per_vert = overrides.get(f"{base_key}.{vertical}")
    glb = overrides.get(base_key)
    for v in (per_vert, glb):
        if v is None or v == "":
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return _env_float(env_name, default)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


async def record_observation(
    *,
    vertical: str,
    cycle_id: int | None,
    scanned: int,
    cancelled: int,
    failed: int,
    max_age: int,
) -> None:
    """Append one row to ``stale_cancel_observations``.

    Called from the cycle tail after the sweep returns. Failures are
    logged + swallowed — the rolling baseline degrades gracefully when
    rows are missing (reader returns ``count=0`` and the evaluator
    skips); it's never worth blocking a cycle on a telemetry write.
    """
    try:
        await db.execute(
            """
            INSERT INTO stale_cancel_observations
                   (vertical, cycle_id, scanned, cancelled, failed, max_age)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            vertical,
            int(cycle_id) if cycle_id is not None else None,
            int(scanned),
            int(cancelled),
            int(failed),
            int(max_age),
        )
    except Exception as e:  # pragma: no cover — defensive
        log.warning(
            "stale_cancel_observations insert failed for vertical=%s: %s",
            vertical, e,
        )


async def maybe_gc_observations(
    *,
    retention_days: int = RETENTION_DAYS,
    interval_secs: int = GC_INTERVAL_SECS,
    now: float | None = None,
) -> int:
    """Day-throttled wrapper around :func:`gc_observations`.

    The cycle tail calls this every cycle but it only actually runs the
    DELETE once per ``interval_secs`` per worker process — the module
    keeps a ``_LAST_GC_TS`` mark and short-circuits otherwise. This
    avoids needing a separate scheduler/cron just for one daily
    housekeeping query while still keeping retention bounded.

    Returns the row-delete count from the run that actually executed
    (i.e. when the throttle gate opened) or ``0`` for short-circuit
    calls.

    The ``now`` kwarg is for tests; production callers can omit it.
    """
    global _LAST_GC_TS
    current = float(now if now is not None else time.time())
    if current - _LAST_GC_TS < float(interval_secs):
        return 0
    deleted = await gc_observations(retention_days=retention_days)
    _LAST_GC_TS = current
    return deleted


async def gc_observations(*, retention_days: int = RETENTION_DAYS) -> int:
    """Delete observations older than the retention window.

    Returns the number of rows deleted (best-effort — returns 0 on
    any error). Designed to be called from a periodic GC tick.
    """
    try:
        result = await db.execute(
            """
            DELETE FROM stale_cancel_observations
             WHERE observed_at < now() - make_interval(days => $1)
            """,
            int(retention_days),
        )
    except Exception as e:  # pragma: no cover — defensive
        log.warning("gc_observations failed: %s", e)
        return 0
    # asyncpg DELETE returns "DELETE <n>" — parse defensively.
    try:
        return int(str(result).split()[-1])
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Baseline computation
# ---------------------------------------------------------------------------


async def compute_baseline(
    vertical: str,
    *,
    window_secs: int = DEFAULT_BASELINE_WINDOW_SECS,
    exclude_cycle_id: int | None = None,
) -> dict[str, Any]:
    """Compute mean + sample stdev of `cancelled` over the rolling window.

    Returns a dict with the shape:
      {
        "vertical": str,
        "window_secs": int,
        "count": int,                # number of observations in window
        "mean": float,               # arithmetic mean of `cancelled`
        "stdev": float,              # sample stdev (n-1 denom); 0 when count < 2
        "max": int,                  # peak in the window
        "recent_failed_cycles": int, # tail count of cycles with failed > 0
        "recent_failures_total": int,# sum of `failed` across tail
        "recent_cancelled": list[int],   # most recent ~30 cancelled values, oldest -> newest
        "recent_failures": list[int],    # most recent ~30 failed values, parallel to recent_cancelled
      }

    Empty windows return ``count=0`` with all other numeric fields
    zeroed; callers (and the evaluator) treat that as "not enough data
    to alert".

    ``exclude_cycle_id`` lets the cycle-tail evaluator score the
    *current* cycle against history that does *not* include itself —
    which matters when the cycle row was already written before the
    evaluator runs (e.g. via a separate timer) so the just-recorded
    spike doesn't dilute the z-score it's being compared against. The
    standard cycle.py call path inverts the order (evaluate first,
    then record) so this exclusion is normally a no-op, but the kwarg
    is supported as defense in depth and for any future caller that
    needs strict "history only" semantics.
    """
    out: dict[str, Any] = {
        "vertical": vertical,
        "window_secs": int(window_secs),
        "count": 0,
        "mean": 0.0,
        "stdev": 0.0,
        "max": 0,
        "recent_failed_cycles": 0,
        "recent_failures_total": 0,
        "recent_cancelled": [],
        "recent_failures": [],
        # Newest observation timestamp in the window (None when empty).
        # Surfaced to the operator dashboard as the "last observation"
        # marker so a vertical with stale telemetry (sweep stopped
        # writing rows) is visually distinct from a quiet but healthy
        # one.
        "last_observation": None,
    }
    try:
        if exclude_cycle_id is not None:
            rows = await db.fetch(
                """
                SELECT cancelled, failed, observed_at
                  FROM stale_cancel_observations
                 WHERE vertical = $1
                   AND observed_at >= now() - make_interval(secs => $2)
                   AND (cycle_id IS NULL OR cycle_id <> $3)
                 ORDER BY observed_at ASC
                """,
                vertical, int(window_secs), int(exclude_cycle_id),
            )
        else:
            rows = await db.fetch(
                """
                SELECT cancelled, failed, observed_at
                  FROM stale_cancel_observations
                 WHERE vertical = $1
                   AND observed_at >= now() - make_interval(secs => $2)
                 ORDER BY observed_at ASC
                """,
                vertical, int(window_secs),
            )
    except Exception as e:  # pragma: no cover — defensive
        log.warning("compute_baseline fetch failed for %s: %s", vertical, e)
        return out
    if not rows:
        return out
    cancelled_vals = [int(r["cancelled"] or 0) for r in rows]
    failed_vals = [int(r["failed"] or 0) for r in rows]
    n = len(cancelled_vals)
    mean = sum(cancelled_vals) / n
    if n >= 2:
        var = sum((x - mean) ** 2 for x in cancelled_vals) / (n - 1)
        stdev = math.sqrt(var)
    else:
        stdev = 0.0
    # Sparkline: keep the trailing 24 values (one per hourly cycle for
    # ~last 24h) so the UI can render a compact history without
    # shipping the whole window every refresh. Aligns with the task
    # #711 spec text ("last 24 observations").
    # `recent_failures` is the parallel array used by the failure-spike
    # evaluator to check the strict tail (last N consecutive cycles)
    # rather than the 24h aggregate, which would latch the alert.
    sparkline = cancelled_vals[-24:]
    failure_tail = failed_vals[-24:]
    # Rows are ORDER BY observed_at ASC, so the last row is the newest.
    last_obs = rows[-1].get("observed_at")
    out.update(
        count=n,
        mean=float(mean),
        stdev=float(stdev),
        max=int(max(cancelled_vals)),
        recent_failed_cycles=sum(1 for v in failed_vals if v > 0),
        recent_failures_total=int(sum(failed_vals)),
        recent_cancelled=sparkline,
        recent_failures=failure_tail,
        last_observation=last_obs,
    )
    return out


async def list_baselines(
    *, window_secs: int = DEFAULT_BASELINE_WINDOW_SECS
) -> list[dict[str, Any]]:
    """Compute baselines for every vertical seen in the rolling window.

    Used by ``/api/health/snapshot`` to populate per-vertical anomaly
    cards on the dashboard. Returns an empty list on DB error so the
    snapshot endpoint stays alive.
    """
    try:
        rows = await db.fetch(
            """
            SELECT DISTINCT vertical
              FROM stale_cancel_observations
             WHERE observed_at >= now() - make_interval(secs => $1)
             ORDER BY vertical ASC
            """,
            int(window_secs),
        )
    except Exception as e:  # pragma: no cover — defensive
        log.warning("list_baselines fetch failed: %s", e)
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        v = str(r["vertical"] or "").strip()
        if not v:
            continue
        out.append(await compute_baseline(v, window_secs=window_secs))
    return out


# ---------------------------------------------------------------------------
# Anomaly evaluator (called from the cycle tail)
# ---------------------------------------------------------------------------


def _is_volume_anomaly(
    *,
    current_cancelled: int,
    baseline: dict[str, Any],
    sigma_threshold: float,
    floor: int,
) -> tuple[bool, dict[str, Any]]:
    """Return (is_anomaly, telemetry).

    Requires:
      - baseline.count >= 5 (enough history that a 3σ threshold is
        statistically meaningful — fewer samples means an outlier
        can't be distinguished from "we just started recording").
      - baseline.stdev > 0 (a flat series is not informative; we'd
        alert on every tick).
      - current_cancelled >= floor (don't page on noise even when the
        sigma maths technically says yes).
      - current_cancelled > mean + sigma_threshold * stdev.
    """
    telemetry = {
        "current": int(current_cancelled),
        "mean": float(baseline.get("mean", 0.0)),
        "stdev": float(baseline.get("stdev", 0.0)),
        "count": int(baseline.get("count", 0)),
        "sigma_threshold": float(sigma_threshold),
        "floor": int(floor),
        "z_score": None,
    }
    if telemetry["count"] < 5:
        return False, telemetry
    if telemetry["stdev"] <= 0.0:
        return False, telemetry
    if telemetry["current"] < telemetry["floor"]:
        return False, telemetry
    z = (telemetry["current"] - telemetry["mean"]) / telemetry["stdev"]
    telemetry["z_score"] = float(z)
    return (z >= sigma_threshold), telemetry


def _is_failure_spike(
    *,
    baseline: dict[str, Any],
    failure_window: int,
    floor: int,
) -> tuple[bool, dict[str, Any]]:
    """Return (is_anomaly, telemetry).

    Per task #711 spec: fires when *each* of the most recent
    ``failure_window`` cycles has ``failed >= floor``. This is an
    edge-triggered, per-cycle threshold check:

      * Tail ``[3, 3]`` with floor=3 fires (each cycle hits the bar).
      * Tail ``[1, 2]`` with floor=3 does NOT fire (neither cycle
        reaches the per-cycle threshold even though the sum is 3).
      * Tail ``[5, 0]`` does NOT fire — recovery is immediate, no
        latching on the 24h aggregate.

    ``floor`` is the *per-cycle* failure count tolerated before paging
    — operators tune it against cycle cadence ("how bad does a single
    cycle have to be before I want a page?").

    The telemetry returned still surfaces the *24h aggregate* counts
    (``failed_cycles``, ``failed_total``) for the alert email so the
    operator sees both "this just tripped" and "context: 8 of the
    last 24h were bad" — which helps decide whether to halt vs. wait.
    """
    failure_window = max(1, int(failure_window))
    floor = int(floor)
    failed_cycles_24h = int(baseline.get("recent_failed_cycles", 0))
    failed_total_24h = int(baseline.get("recent_failures_total", 0))
    tail = baseline.get("recent_failures") or []
    # Take the strictly-most-recent ``failure_window`` entries.
    strict_tail = list(tail)[-failure_window:]
    # Spec: each cycle in the tail must satisfy ``failed >= floor``.
    tail_all_above_floor = (
        len(strict_tail) >= failure_window
        and all(int(v) >= floor for v in strict_tail)
    )
    tail_sum = int(sum(int(v) for v in strict_tail))
    telemetry = {
        "failed_cycles": failed_cycles_24h,
        "failed_total": failed_total_24h,
        "failure_window": failure_window,
        "floor": floor,
        "tail": [int(v) for v in strict_tail],
        "tail_sum": tail_sum,
        # Naming kept stable across the schema for dashboard consumers,
        # but the semantics are now "all >= floor", not "all > 0".
        "tail_all_bad": tail_all_above_floor,
    }
    return tail_all_above_floor, telemetry


async def evaluate_and_alert(
    vertical: str,
    overrides: dict[str, Any],
    sweep_stats: dict[str, int],
    *,
    cycle_id: int | None = None,
) -> dict[str, Any]:
    """Run both anomaly evaluators against the current cycle's stats
    and dispatch alerts when triggered.

    Returns a small telemetry dict so the caller can include it in the
    cycle's `_publish` event for dashboard streams. Never raises —
    failure modes are logged and reflected in the return shape.
    """
    sigma = _resolve_float(
        vertical, overrides,
        "stale_cancel_volume_sigma",
        "AXIOM_STALE_CANCEL_VOLUME_SIGMA",
        DEFAULT_VOLUME_SIGMA,
    )
    vol_floor = _resolve_int(
        vertical, overrides,
        "stale_cancel_volume_floor",
        "AXIOM_STALE_CANCEL_VOLUME_FLOOR",
        DEFAULT_VOLUME_FLOOR,
    )
    fail_window = _resolve_int(
        vertical, overrides,
        "stale_cancel_failure_window",
        "AXIOM_STALE_CANCEL_FAILURE_WINDOW",
        DEFAULT_FAILURE_WINDOW,
    )
    fail_floor = _resolve_int(
        vertical, overrides,
        "stale_cancel_failure_floor",
        "AXIOM_STALE_CANCEL_FAILURE_FLOOR",
        DEFAULT_FAILURE_FLOOR,
    )
    # Single per-vertical cooldown shared by both alert types per the
    # task #711 settings spec. Operators tune one knob
    # (`risk.stale_cancel_alert_cooldown_secs.<vertical>`) and both the
    # volume anomaly and the failure spike use it for dedupe TTL —
    # avoids the calibration trap of "I changed one and forgot the
    # other and now my pager is misbehaving asymmetrically".
    cooldown_secs = _resolve_int(
        vertical, overrides,
        "stale_cancel_alert_cooldown_secs",
        "AXIOM_STALE_CANCEL_ALERT_COOLDOWN_SECS",
        DEFAULT_ALERT_COOLDOWN_SECS,
    )
    vol_ttl = cooldown_secs
    fail_ttl = cooldown_secs

    out: dict[str, Any] = {
        "vertical": vertical,
        "alerts": [],
        "volume": None,
        "failure": None,
        "baseline": None,
    }

    try:
        # Defense-in-depth: also pass exclude_cycle_id so that even if
        # call ordering ever drifts (someone moves record_observation
        # back in front of evaluate_and_alert), the current cycle row
        # is filtered out of the baseline being scored against itself.
        # The current ordering in cycle.py already prevents this, but
        # belt-and-suspenders is cheap and the query branch is tested.
        baseline = await compute_baseline(
            vertical, exclude_cycle_id=cycle_id
        )
    except Exception as e:  # pragma: no cover — defensive
        log.warning("compute_baseline failed for %s: %s", vertical, e)
        return out
    out["baseline"] = baseline

    current_cancelled = int(sweep_stats.get("cancelled") or 0)
    current_failed = int(sweep_stats.get("failed") or 0)

    # --- Volume anomaly ---
    # NOTE on asymmetry vs. the failure-spike branch below: the volume
    # evaluator scores the *current* cycle against PRIOR HISTORY ONLY
    # — the baseline reader runs before record_observation, so the
    # current row is correctly absent from `mean`/`stdev`. That is the
    # right shape for a z-score (you don't want the spike contaminating
    # the population it's scored against).
    #
    # The failure-spike evaluator is the opposite: it asks "is the
    # most recent run of N cycles all bad?", which fundamentally needs
    # the current cycle in the tail or detection lags by one cycle. We
    # synthesize an augmented tail below that includes the current
    # `failed` count.
    try:
        is_vol, vol_tel = _is_volume_anomaly(
            current_cancelled=current_cancelled,
            baseline=baseline,
            sigma_threshold=sigma,
            floor=vol_floor,
        )
        out["volume"] = vol_tel
        if is_vol:
            claimed = await alerts_mod.dedupe_or_skip(
                "stale_cancel_volume_anomaly",
                vertical,
                ttl_seconds=vol_ttl,
            )
            if claimed:
                try:
                    alerts_mod.stale_cancel_volume_anomaly(
                        vertical=vertical,
                        cycle_id=cycle_id,
                        current=vol_tel["current"],
                        mean=vol_tel["mean"],
                        stdev=vol_tel["stdev"],
                        z_score=vol_tel.get("z_score") or 0.0,
                        sigma_threshold=vol_tel["sigma_threshold"],
                        floor=vol_tel["floor"],
                        sample_count=vol_tel["count"],
                    )
                    out["alerts"].append("stale_cancel_volume_anomaly")
                except Exception as e:  # pragma: no cover — defensive
                    log.warning(
                        "stale_cancel_volume_anomaly dispatch failed for %s: %s",
                        vertical, e,
                    )
    except Exception as e:  # pragma: no cover — defensive
        log.warning("volume anomaly evaluation failed for %s: %s", vertical, e)

    # --- Failure spike ---
    # Augment the tail with the current cycle's `failed` so the strict
    # check ("last N consecutive cycles all failed") includes the
    # cycle we're evaluating. Without this the evaluator sees N-1
    # history and would either:
    #   - miss the second consecutive failure (current cycle is the
    #     second bad one but it's not in the tail yet), OR
    #   - keep firing one cycle after recovery (current cycle is clean
    #     but the tail doesn't know yet).
    augmented = dict(baseline)
    augmented["recent_failures"] = list(
        baseline.get("recent_failures") or []
    ) + [current_failed]
    # Mirror the 24h aggregate counts to include the current cycle so
    # the alert telemetry stays consistent with the strict-tail decision.
    if current_failed > 0:
        augmented["recent_failed_cycles"] = (
            int(baseline.get("recent_failed_cycles", 0)) + 1
        )
        augmented["recent_failures_total"] = (
            int(baseline.get("recent_failures_total", 0)) + current_failed
        )
    try:
        is_fail, fail_tel = _is_failure_spike(
            baseline=augmented,
            failure_window=fail_window,
            floor=fail_floor,
        )
        out["failure"] = fail_tel
        if is_fail:
            claimed = await alerts_mod.dedupe_or_skip(
                "stale_cancel_failure_spike",
                vertical,
                ttl_seconds=fail_ttl,
            )
            if claimed:
                try:
                    alerts_mod.stale_cancel_failure_spike(
                        vertical=vertical,
                        cycle_id=cycle_id,
                        failed_cycles=fail_tel["failed_cycles"],
                        failed_total=fail_tel["failed_total"],
                        failure_window=fail_tel["failure_window"],
                        floor=fail_tel["floor"],
                    )
                    out["alerts"].append("stale_cancel_failure_spike")
                except Exception as e:  # pragma: no cover — defensive
                    log.warning(
                        "stale_cancel_failure_spike dispatch failed for %s: %s",
                        vertical, e,
                    )
    except Exception as e:  # pragma: no cover — defensive
        log.warning("failure spike evaluation failed for %s: %s", vertical, e)

    return out
