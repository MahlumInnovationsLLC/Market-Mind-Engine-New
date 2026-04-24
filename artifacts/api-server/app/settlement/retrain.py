"""Online-learning consumer.

Drains `online_learning_buffer WHERE processed=false` and, for any
vertical that has either

  (a) at least `MIN_BATCH` un-processed rows, or
  (b) an unacknowledged `brier_drift` row in `model_health_alerts`
      raised in the last hour,

re-fits a per-vertical isotonic calibrator on the buffered
``(predicted_prob, actual_outcome)`` pairs, persists the new
calibrator's knot points to a fresh ``calibration_snapshots`` row
tagged ``drift_status={"status":"retrained"}``, marks the consumed
buffer rows ``processed=true``, and broadcasts the decision + outcome
on the settlement event hub so the operator dashboard can light up.

The worker is intentionally additive: it never mutates the predictor
files on disk.  The newly-fitted calibrator state is captured in
``calibration_snapshots.calibrator_state`` (knot ``x``/``y`` arrays),
which the trading pipeline can layer on top of the cold-storage
predictor.  This closes the autonomy loop without coupling settlement
to the model-loading subsystem.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .. import db
from ..weather import precip_emos
from .pipeline import BRIER_DRIFT_CRIT, BRIER_DRIFT_WARN
from .streams import settlement_event_hub


log = logging.getLogger(__name__)


# Minimum number of fresh outcomes we want in the buffer before we
# touch the calibrator, unless an active brier_drift alert overrides
# the threshold.  Keeps isotonic refits stable.
MIN_BATCH = 25
# Per-vertical safety cap so a runaway buffer can't OOM the worker.
MAX_BATCH = 5_000


@dataclass
class VerticalRetrainResult:
    vertical: str
    rows_consumed: int = 0
    triggered_by: str | None = None  # 'batch' | 'alert' | None
    refit: bool = False
    brier_before: float | None = None
    brier_after: float | None = None
    snapshot_id: int | None = None
    error: str | None = None
    knot_count: int = 0
    rolled_back: bool = False
    rollback_id: int | None = None


@dataclass
class RetrainCycleResult:
    started_at: str
    completed_at: str | None = None
    duration_seconds: float = 0.0
    verticals_examined: int = 0
    verticals_retrained: int = 0
    rows_processed: int = 0
    rollbacks: int = 0
    details: list[VerticalRetrainResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # Status of the precipitation EMOS coefficient refit (task #280).
    # Empty dict means the refit step didn't run (the cycle's hook is
    # additive — see ``_refit_precip_emos_from_observations``).
    precip_emos_status: dict[str, Any] = field(default_factory=dict)


# Tolerance band: refits whose post-fit Brier exceeds the pre-fit
# raw Brier by *more* than this much trip the rollback path. Tiny
# (< ROLLBACK_TOLERANCE) regressions are noise from sampling and
# isotonic's monotone constraint and are allowed through.
ROLLBACK_TOLERANCE = 1e-4


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _publish(event: dict[str, Any]) -> None:
    await settlement_event_hub.publish(event)


async def _verticals_to_consider() -> list[tuple[str, int, bool]]:
    """Return ``[(vertical, unprocessed_count, has_active_alert)]``.

    ``has_active_alert`` is true when an unacknowledged ``brier_drift``
    row exists in the last hour, which lowers the size threshold below
    ``MIN_BATCH`` (we still need at least 1 buffered row to have any
    signal at all).
    """
    try:
        rows = await db.fetch(
            """
            WITH buf AS (
              SELECT vertical, count(*)::int AS n
                FROM online_learning_buffer
               WHERE processed = false
                 AND vertical IS NOT NULL
                 AND predicted_prob IS NOT NULL
                 AND actual_outcome IS NOT NULL
               GROUP BY vertical
            ), alerts AS (
              SELECT DISTINCT vertical
                FROM model_health_alerts
               WHERE alert_type = 'brier_drift'
                 AND coalesce(acknowledged, false) = false
                 AND created_at > now() - interval '1 hour'
                 AND vertical IS NOT NULL
            )
            SELECT b.vertical, b.n,
                   (a.vertical IS NOT NULL) AS active_alert
              FROM buf b
              LEFT JOIN alerts a ON a.vertical = b.vertical
            """
        )
    except Exception as e:
        log.warning("retrain: vertical scan failed: %s", e)
        return []
    return [
        (r["vertical"], int(r["n"] or 0), bool(r["active_alert"]))
        for r in rows
    ]


async def _drain_buffer(vertical: str, limit: int) -> list[dict[str, Any]]:
    try:
        rows = await db.fetch(
            """
            SELECT id,
                   predicted_prob::float AS p,
                   actual_outcome::int   AS y
              FROM online_learning_buffer
             WHERE processed = false
               AND vertical = $1
               AND predicted_prob IS NOT NULL
               AND actual_outcome IS NOT NULL
             ORDER BY id
             LIMIT $2
            """,
            vertical, limit,
        )
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning("retrain: buffer drain failed for %s: %s", vertical, e)
        return []


async def _mark_processed(ids: list[int]) -> None:
    if not ids:
        return
    try:
        await db.execute(
            "UPDATE online_learning_buffer SET processed = true WHERE id = ANY($1::bigint[])",
            ids,
        )
    except Exception as e:
        log.warning("retrain: mark processed failed: %s", e)


def _fit_isotonic(probs: list[float], outcomes: list[int]) -> dict[str, Any]:
    """Fit an isotonic calibrator and return a tiny serializable state.

    We only persist the knot ``x``/``y`` pairs (not a pickled estimator)
    so the snapshot is self-describing and forward-compatible across
    sklearn upgrades.  Returned dict:

        {"type": "isotonic", "x": [...], "y": [...], "n": N,
         "brier_before": float, "brier_after": float}
    """
    from sklearn.isotonic import IsotonicRegression  # local: optional

    n = len(probs)
    # Pre-fit Brier on the raw model probabilities.
    brier_before = sum((p - y) * (p - y) for p, y in zip(probs, outcomes)) / n

    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(probs, outcomes)
    calibrated = iso.predict(probs)
    brier_after = sum(
        (float(c) - y) * (float(c) - y) for c, y in zip(calibrated, outcomes)
    ) / n

    # `X_thresholds_` / `y_thresholds_` are the public knot arrays.
    xs = [float(v) for v in getattr(iso, "X_thresholds_", [])]
    ys = [float(v) for v in getattr(iso, "y_thresholds_", [])]
    return {
        "type": "isotonic",
        "fit_at": datetime.now(timezone.utc).isoformat(),
        "n": int(n),
        "x": xs,
        "y": ys,
        "brier_before": float(brier_before),
        "brier_after": float(brier_after),
    }


async def _write_rollback(
    vertical: str,
    snapshot_id: int | None,
    state: dict[str, Any],
    triggered_by: str,
) -> int | None:
    """Persist a `model_rollback_log` row and raise a matching
    `model_health_alerts` row so the operator dashboard surfaces it.

    Best-effort: any DB failure is logged and the function returns
    `None`.  We never raise from the rollback path because the parent
    retrain cycle must continue processing the remaining verticals.
    """
    pinned = os.environ.get("AXIOM_MODEL_VERSION") or None
    # Resolve the *previous* good snapshot for this vertical — i.e.
    # the most recent calibration_snapshots row that wasn't itself a
    # rollback.  That row stays in service: the loader picks the
    # newest non-rolled-back snapshot per vertical, so by tagging the
    # bad refit's drift_status we effectively pin to this id.
    prev_snapshot_id: int | None = None
    try:
        prev_snapshot_id = await db.fetchval(
            """
            SELECT id FROM calibration_snapshots
             WHERE vertical = $1
               AND id <> COALESCE($2, -1)
               AND COALESCE(drift_status->>'status', '') <> 'rolled_back'
             ORDER BY created_at DESC
             LIMIT 1
            """,
            vertical, snapshot_id,
        )
    except Exception as e:
        log.warning("rollback: prev-snapshot lookup failed for %s: %s",
                    vertical, e)
    brier_before = state.get("brier_before")
    brier_after = state.get("brier_after")
    reason = (
        f"retrain Brier worsened: before={brier_before:.4f} "
        f"after={brier_after:.4f}"
        if isinstance(brier_before, (int, float))
        and isinstance(brier_after, (int, float))
        else "retrain Brier worsened"
    )
    try:
        rollback_id = await db.fetchval(
            """
            INSERT INTO model_rollback_log
                (vertical, snapshot_id, brier_before, brier_after,
                 pinned_version, reason, details)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
            RETURNING id
            """,
            vertical,
            snapshot_id,
            float(brier_before) if brier_before is not None else None,
            float(brier_after) if brier_after is not None else None,
            pinned,
            reason,
            json.dumps({
                "triggered_by": triggered_by,
                "knot_count": len(state.get("x") or []),
                "n": int(state.get("n") or 0),
                # The previous good snapshot is what the loader will
                # serve until a successful retrain promotes a new one;
                # surfacing it here gives the operator runbook §6 a
                # single place to point at for "restored to id=N".
                "pinned_snapshot_id": prev_snapshot_id,
            }),
        )
    except Exception as e:
        log.warning("rollback log insert failed for %s: %s", vertical, e)
        return None

    # Raise a matching health alert so /system → health flips red.
    try:
        await db.execute(
            """
            INSERT INTO model_health_alerts
                (vertical, alert_type, severity, metric_name,
                 metric_value, threshold, message, acknowledged)
            VALUES ($1, 'retrain_rollback', 'warning', 'brier_score',
                    $2, $3, $4, false)
            """,
            vertical,
            float(brier_after) if brier_after is not None else None,
            float(brier_before) if brier_before is not None else None,
            reason,
        )
    except Exception as e:
        log.warning("rollback alert insert failed for %s: %s", vertical, e)

    # Tag the snapshot so the calibrator-loader skips it.
    if snapshot_id is not None:
        try:
            await db.execute(
                """
                UPDATE calibration_snapshots
                   SET drift_status = jsonb_set(
                       coalesce(drift_status, '{}'::jsonb),
                       '{status}',
                       to_jsonb('rolled_back'::text),
                       true
                   )
                 WHERE id = $1
                """,
                snapshot_id,
            )
        except Exception as e:
            log.warning("snapshot drift_status update failed for %s: %s",
                        vertical, e)

    return int(rollback_id) if rollback_id is not None else None


async def _write_snapshot(
    vertical: str, n: int, state: dict[str, Any], triggered_by: str
) -> int | None:
    """Append a new ``calibration_snapshots`` row tagged retrained.

    Returns the new row id.  We deliberately recompute Brier from the
    same buffered rows we just fit on (post-calibration) so the snapshot
    is internally consistent: future drift checks compare apples to
    apples.
    """
    try:
        snap_id = await db.fetchval(
            """
            INSERT INTO calibration_snapshots
                (vertical, sub_category, ece, mce, brier_score, reliability,
                 resolution, sample_count, calibrator_state, drift_status, config)
            VALUES ($1, 'retrain', $2, $3, $4, NULL, NULL, $5,
                    $6::jsonb,
                    jsonb_build_object(
                        'status', 'retrained',
                        'triggered_by', $7::text,
                        'brier_warn', $8::float,
                        'brier_crit', $9::float
                    ),
                    jsonb_build_object('source', 'online_learning_buffer'))
            RETURNING id
            """,
            vertical,
            float(state.get("brier_after") or 0.0),  # ece (post-calibration MAE-ish proxy)
            float(state.get("brier_after") or 0.0),  # mce (same — we don't bin here)
            float(state.get("brier_after") or 0.0),
            int(n),
            json.dumps(state),
            triggered_by,
            BRIER_DRIFT_WARN,
            BRIER_DRIFT_CRIT,
        )
        return int(snap_id) if snap_id is not None else None
    except Exception as e:
        log.warning("retrain: snapshot insert failed for %s: %s", vertical, e)
        return None


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


async def run_retrain_cycle() -> RetrainCycleResult:
    """One pass: drain the buffer, refit calibrators where warranted,
    publish events, return a result.  Always non-fatal — individual
    vertical failures are captured per-row and never abort the cycle.
    """
    started = time.perf_counter()
    started_at = datetime.now(timezone.utc).isoformat()
    cr = RetrainCycleResult(started_at=started_at)
    await _publish({"type": "retrain_cycle_start", "started_at": started_at})

    candidates = await _verticals_to_consider()
    cr.verticals_examined = len(candidates)

    for vertical, count, has_alert in candidates:
        # Decide whether to retrain.
        triggered_by: str | None = None
        if has_alert and count >= 1:
            triggered_by = "alert"
        elif count >= MIN_BATCH:
            triggered_by = "batch"

        if triggered_by is None:
            # Not enough signal — leave rows in the buffer for the next
            # cycle and announce the no-op so the dashboard can show
            # buffer pressure climbing.
            await _publish({
                "type": "retrain_skipped",
                "vertical": vertical,
                "unprocessed": count,
                "min_batch": MIN_BATCH,
                "active_alert": has_alert,
            })
            continue

        await _publish({
            "type": "retrain_decision",
            "vertical": vertical,
            "unprocessed": count,
            "triggered_by": triggered_by,
        })

        rows = await _drain_buffer(vertical, MAX_BATCH)
        if not rows:
            cr.details.append(VerticalRetrainResult(
                vertical=vertical, triggered_by=triggered_by,
                error="no rows fetched",
            ))
            continue

        probs = [float(r["p"]) for r in rows]
        outcomes = [int(r["y"]) for r in rows]
        ids = [int(r["id"]) for r in rows]

        try:
            state = await _fit_isotonic_async(probs, outcomes)
        except Exception as e:
            log.exception("retrain: fit failed for %s", vertical)
            cr.details.append(VerticalRetrainResult(
                vertical=vertical, triggered_by=triggered_by,
                rows_consumed=0, error=f"{type(e).__name__}: {e}",
            ))
            cr.errors.append(f"{vertical}: fit {e}")
            await _publish({
                "type": "retrain_failed",
                "vertical": vertical,
                "error": str(e),
            })
            continue

        snap_id = await _write_snapshot(vertical, len(rows), state, triggered_by)

        # Drift-rollback gate: if the freshly-fitted calibrator scored a
        # *worse* Brier on its own training data than the raw model did
        # (beyond a tiny noise tolerance), the refit is a regression —
        # don't promote, write a rollback log, raise an alert, and
        # leave the buffer rows un-processed so the next cycle gets
        # another shot with a (hopefully larger) sample.
        brier_before = state.get("brier_before")
        brier_after = state.get("brier_after")
        rolled_back = False
        rollback_id: int | None = None
        if (
            isinstance(brier_before, (int, float))
            and isinstance(brier_after, (int, float))
            and brier_after > brier_before + ROLLBACK_TOLERANCE
        ):
            # Fail-closed: regression *detected* is the source of truth
            # for rollback. _write_rollback can fail (DB blip), but we
            # must still skip promotion + leave rows un-acked. The
            # rollback_id may be None if the audit-log insert failed;
            # the calibrator-loader filter on drift_status would also
            # fail in that case, but the un-ack of rows guarantees we
            # retry the refit on the next cycle, which is the safe
            # path (no buffer loss, no bad calibrator promoted).
            rolled_back = True
            cr.rollbacks += 1
            try:
                rollback_id = await _write_rollback(
                    vertical, snap_id, state, triggered_by
                )
            except Exception as e:
                log.warning(
                    "rollback log write raised for %s: %s", vertical, e
                )
                rollback_id = None
            await _publish({
                "type": "retrain_rolled_back",
                "vertical": vertical,
                "brier_before": float(brier_before),
                "brier_after": float(brier_after),
                "snapshot_id": snap_id,
                "rollback_id": rollback_id,
                "triggered_by": triggered_by,
                "pinned_version": os.environ.get("AXIOM_MODEL_VERSION"),
            })
        # Only mark rows processed once the snapshot landed AND the
        # refit was not rolled back. A rolled-back refit leaves the
        # rows un-processed so the next cycle re-tries with a larger
        # sample. Transient DB errors on snapshot insert similarly
        # leave the buffer untouched.
        if snap_id is not None and not rolled_back:
            await _mark_processed(ids)
            cr.rows_processed += len(ids)
            cr.verticals_retrained += 1

        result = VerticalRetrainResult(
            vertical=vertical,
            rows_consumed=(
                len(ids) if snap_id is not None and not rolled_back else 0
            ),
            triggered_by=triggered_by,
            refit=snap_id is not None and not rolled_back,
            brier_before=brier_before,
            brier_after=brier_after,
            snapshot_id=snap_id,
            knot_count=len(state.get("x") or []),
            rolled_back=rolled_back,
            rollback_id=rollback_id,
            error=(
                None if snap_id is not None
                else "snapshot insert failed"
            ),
        )
        cr.details.append(result)
        await _publish({
            "type": "retrain_complete",
            "vertical": vertical,
            "rows_consumed": result.rows_consumed,
            "triggered_by": triggered_by,
            "brier_before": result.brier_before,
            "brier_after": result.brier_after,
            "snapshot_id": snap_id,
            "knot_count": result.knot_count,
            "rolled_back": rolled_back,
            "rollback_id": rollback_id,
            "ok": snap_id is not None and not rolled_back,
        })

    # Task #254: opportunistic refit of the temperature vertical's
    # per-(city, metric) GRU output head + EMOS coefficients.  This is
    # gated on the operator opt-in env var because a full sweep across
    # every supported city/metric (~21 fits) takes several seconds and
    # we don't want to insert that latency into the calibrator-only
    # retrain path on every cycle.  When enabled the refit is
    # non-fatal: a failure here never breaks the surrounding isotonic
    # refits or the cycle event stream.
    if os.environ.get("AXIOM_RETRAIN_WEATHER_TEMP", "").strip().lower() in (
        "1", "true", "yes", "on",
    ):
        try:
            await _run_weather_temp_retrain(cr)
        except Exception as e:  # pragma: no cover — defensive
            log.warning("retrain: weather temp refit raised: %s", e)
            cr.errors.append(f"weather_temp: {e}")

    # Best-effort precipitation EMOS refit (task #280). The result
    # rides the same cycle the isotonic refits run on so operators
    # see "EMOS refit happened" in the same dashboard event stream.
    # Failures here NEVER abort the cycle — the trading loop keeps
    # using whatever coefficients are currently active.
    try:
        emos_status = await _refit_precip_emos_from_observations()
        cr.precip_emos_status = emos_status
    except Exception as e:
        log.warning("precip_emos refit raised: %s", e)
        cr.precip_emos_status = {"status": "error", "reason": str(e)}

    cr.duration_seconds = round(time.perf_counter() - started, 3)
    cr.completed_at = datetime.now(timezone.utc).isoformat()
    await _publish({
        "type": "retrain_cycle_complete",
        "started_at": cr.started_at,
        "completed_at": cr.completed_at,
        "duration_seconds": cr.duration_seconds,
        "verticals_examined": cr.verticals_examined,
        "verticals_retrained": cr.verticals_retrained,
        "rows_processed": cr.rows_processed,
        "rollbacks": cr.rollbacks,
        "errors": cr.errors,
    })
    return cr


async def _fit_isotonic_async(
    probs: list[float], outcomes: list[int]
) -> dict[str, Any]:
    """Run the (small but CPU-bound) sklearn fit off the event loop."""
    import asyncio
    return await asyncio.to_thread(_fit_isotonic, probs, outcomes)


async def _run_weather_temp_retrain(cr: RetrainCycleResult) -> None:
    """Refit per-(city, metric) GRU + EMOS coefficients for the
    temperature vertical and clear the predictor's process-local
    bundle cache so the next call picks up the freshly persisted
    weights.  Each promoted fit is published on the settlement event
    hub so the operator dashboard can show which (city, metric) pairs
    moved.

    Per-pair errors are swallowed (they're surfaced in the
    ``TrainResult.error`` field on the event payload) so a single
    bad fit doesn't take down the entire weather refit sweep.
    """
    from ..weather import training as weather_training
    from ..weather import predictor as weather_predictor

    started_at = datetime.now(timezone.utc).isoformat()
    await _publish({
        "type": "weather_temp_retrain_start",
        "started_at": started_at,
    })
    results = await weather_training.train_all()
    promoted = 0
    skipped = 0
    pair_summaries: list[dict[str, Any]] = []
    for r in results:
        promoted += int(r.promoted)
        skipped += int(not r.promoted)
        pair_summaries.append({
            "city": r.city,
            "metric": r.metric,
            "promoted": r.promoted,
            "n_samples": r.n_samples,
            "n_holdout": r.n_holdout,
            "baseline_brier": r.baseline_brier,
            "holdout_brier": r.holdout_brier,
            "error": r.error,
        })

    if promoted > 0:
        # Drop the predictor's bundle cache so the next prediction
        # pulls the freshly persisted coefficients from the DB.
        try:
            weather_predictor._trained_cache_clear()  # noqa: SLF001
        except Exception:  # pragma: no cover — defensive
            log.debug("weather_temp: predictor cache invalidation failed")

    await _publish({
        "type": "weather_temp_retrain_complete",
        "started_at": started_at,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "promoted": promoted,
        "skipped": skipped,
        "pairs": pair_summaries,
    })

    # Make the result visible on the parent cycle stats so the
    # ``run_retrain_cycle`` return value carries weather signal too.
    cr.verticals_examined += len(results)
    cr.verticals_retrained += promoted


# ---------------------------------------------------------------------------
# Precipitation EMOS coefficient refit (task #280)
# ---------------------------------------------------------------------------
#
# The KXRAIN predictor ships with hand-picked EMOS coefficients
# (a=0, b=1, c=0.04, d=1.20, logistic_scale=5.0) sanity-checked
# against synthetic gamma draws. Once enough KXRAIN markets have
# settled, we refit these coefficients by maximising the binary
# log-likelihood of the gamma / logistic link against the realised
# YES/NO outcomes the predictor stored in `precip_emos_observations`.
#
# Done = the nightly refit happens AND the Brier on recent N markets
# is materially lower than the shipped defaults. The Brier-improvement
# gate below enforces the second half of "done" — we only promote the
# new coefficients when they actually beat the active baseline by more
# than `_PRECIP_EMOS_BRIER_TOLERANCE`.

# Pull at most this many recent observations into a single refit so
# the optimizer doesn't grind on a year of data when only the tail
# is statistically relevant.
PRECIP_EMOS_MAX_SAMPLES = 5_000

# Don't promote a refit unless its in-sample Brier improves on the
# active coefficients by more than this margin (tiny improvements are
# noise from the optimizer's terminal ftol).
_PRECIP_EMOS_BRIER_TOLERANCE = 1e-4

# Persisted-coefficients key for `system_settings`.
_PRECIP_EMOS_SETTINGS_KEY = "precipitation_emos_coefficients"


async def _load_precip_emos_observations(limit: int) -> list[dict[str, Any]]:
    try:
        rows = await db.fetch(
            """
            SELECT market_id,
                   threshold_in::float  AS threshold_in,
                   direction,
                   ensemble_mean::float AS ensemble_mean,
                   ensemble_var::float  AS ensemble_var,
                   actual_outcome_yes::int AS actual_outcome_yes
              FROM precip_emos_observations
             WHERE settled_at IS NOT NULL
               AND actual_outcome_yes IS NOT NULL
               AND ensemble_mean IS NOT NULL
               AND ensemble_var IS NOT NULL
             ORDER BY settled_at DESC
             LIMIT $1
            """,
            int(limit),
        )
    except Exception as e:
        log.debug("precip_emos: observation fetch failed: %s", e)
        return []
    return [dict(r) for r in rows]


async def _persist_precip_emos_coefficients(
    coef: precip_emos.PrecipEMOSCoefficients,
    *,
    n: int,
    brier_after: float,
    brier_baseline: float,
) -> bool:
    """Write the refit JSON into ``system_settings`` so a fresh API
    process picks the new coefficients up via
    ``load_persisted_precip_emos_coefficients`` at boot.

    Returns True on success. Failures are logged and signalled — the
    caller still installs the cache locally so the live process gets
    the benefit, but the next cold start would lose the fit.
    """
    payload = {
        "coefficients": precip_emos.coefficients_to_dict(coef),
        "n": int(n),
        "brier_after": float(brier_after),
        "brier_baseline": float(brier_baseline),
        "fit_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        # ``system_settings.value`` is TEXT (see runtime_settings.py),
        # so we serialize the refit payload to JSON ourselves and let
        # the load path parse it back. Matches the convention every
        # other consumer of this table uses.
        from .. import runtime_settings as _rs
        await _rs._ensure_system_settings_schema()
        await db.execute(
            """
            INSERT INTO system_settings (key, value, updated_at)
            VALUES ($1, $2, now())
            ON CONFLICT (key) DO UPDATE
               SET value = EXCLUDED.value,
                   updated_at = now()
            """,
            _PRECIP_EMOS_SETTINGS_KEY, json.dumps(payload),
        )
        return True
    except Exception as e:
        log.warning("precip_emos: settings persist failed: %s", e)
        return False


async def load_persisted_precip_emos_coefficients() -> bool:
    """Hydrate the in-process EMOS cache from ``system_settings``.

    Called from API startup so the trading loop doesn't drop back to
    the shipped defaults across a deploy. Returns True iff a row was
    found and installed; False (silently) when no refit has landed yet
    or the row can't be read.
    """
    try:
        raw = await db.fetchval(
            "SELECT value FROM system_settings WHERE key = $1",
            _PRECIP_EMOS_SETTINGS_KEY,
        )
    except Exception as e:
        log.debug("precip_emos: settings load failed: %s", e)
        return False
    if raw is None:
        return False
    try:
        payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
        coef_dict = payload.get("coefficients") or {}
        coef = precip_emos.coefficients_from_dict(coef_dict)
    except Exception as e:
        log.warning("precip_emos: settings parse failed: %s", e)
        return False
    precip_emos.set_persisted_coefficients(coef)
    return True


async def _refit_precip_emos_from_observations() -> dict[str, Any]:
    """Refit the precipitation EMOS coefficients on settled KXRAIN
    markets. Best-effort: returns a status dict and never raises.

    Status shape::

        {"status": "skipped"   | "fit"    | "rejected"  | "error",
         "n":              int,
         "brier_baseline": float | None,
         "brier_after":    float | None,
         "reason":         str   | None,
         "promoted":       bool}
    """
    rows = await _load_precip_emos_observations(PRECIP_EMOS_MAX_SAMPLES)
    if len(rows) < precip_emos.MIN_REFIT_SAMPLES:
        return {
            "status": "skipped",
            "n": len(rows),
            "reason": "not_enough_settled_observations",
            "promoted": False,
        }

    # Canonicalize (above/below) onto the gamma-SF axis the fitter wants.
    samples: list[precip_emos.PrecipEMOSTrainingSample] = []
    for r in rows:
        try:
            samples.append(precip_emos.to_training_sample(
                ensemble_mean=r["ensemble_mean"],
                ensemble_var=r["ensemble_var"],
                threshold_in=r["threshold_in"],
                direction=r["direction"],
                actual_outcome_yes=r["actual_outcome_yes"],
            ))
        except Exception as e:
            log.debug("precip_emos: skipping malformed obs %s: %s",
                      r.get("market_id"), e)

    if len(samples) < precip_emos.MIN_REFIT_SAMPLES:
        return {
            "status": "skipped",
            "n": len(samples),
            "reason": "not_enough_clean_samples",
            "promoted": False,
        }

    # Run the (CPU-bound) fit off the event loop.
    import asyncio
    try:
        baseline = precip_emos.coefficients_from_env()
        fit = await asyncio.to_thread(
            precip_emos.fit_precip_emos_coefficients,
            samples, initial=baseline,
        )
    except Exception as e:
        log.warning("precip_emos: fit raised: %s", e)
        return {
            "status": "error",
            "n": len(samples),
            "reason": f"{type(e).__name__}: {e}",
            "promoted": False,
        }

    improved = (fit.brier_baseline - fit.brier) > _PRECIP_EMOS_BRIER_TOLERANCE
    if not improved:
        await _publish({
            "type": "precip_emos_refit",
            "promoted": False,
            "n": fit.n_samples,
            "brier_baseline": fit.brier_baseline,
            "brier_after": fit.brier,
            "reason": "no_brier_improvement",
        })
        return {
            "status": "rejected",
            "n": fit.n_samples,
            "brier_baseline": fit.brier_baseline,
            "brier_after": fit.brier,
            "reason": "no_brier_improvement",
            "promoted": False,
        }

    # Promote: cache locally so the very next predict picks it up,
    # then persist for the next cold start.
    precip_emos.set_persisted_coefficients(fit.coefficients)
    persisted = await _persist_precip_emos_coefficients(
        fit.coefficients,
        n=fit.n_samples,
        brier_after=fit.brier,
        brier_baseline=fit.brier_baseline,
    )

    await _publish({
        "type": "precip_emos_refit",
        "promoted": True,
        "persisted": persisted,
        "n": fit.n_samples,
        "brier_baseline": fit.brier_baseline,
        "brier_after": fit.brier,
        "log_likelihood": fit.log_likelihood,
        "converged": fit.converged,
    })
    return {
        "status": "fit",
        "n": fit.n_samples,
        "brier_baseline": fit.brier_baseline,
        "brier_after": fit.brier,
        "reason": None,
        "promoted": True,
        "persisted": persisted,
    }
