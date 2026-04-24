"""Per-persona daily Brier degradation alert.

Background asyncio loop that periodically computes today's per-persona
Brier (via `daily_summary.aggregate_persona_day`) and pages when any
persona's score crosses one of two thresholds:

  * absolute: today's Brier > `PERSONA_BRIER_ALERT_ABS_THRESHOLD`
    (default 0.30) AND today's debate count >= `PERSONA_BRIER_ALERT_MIN_DEBATES`
    (default 5);
  * jump: today's Brier exceeds the persona's all-time average by
    more than `PERSONA_BRIER_ALERT_JUMP_THRESHOLD` (default 0.10),
    again gated by the same min-debates floor.

Each finding:
  * persists a `model_health_alerts` row with `alert_type =
    'persona_daily_brier'` so the operator alerts UI surfaces it
    alongside the existing brier_drift / position_drift entries
    (the `vertical` column carries the persona id);
  * dispatches a `persona_brier_drift` operator email via the same
    `notifications.alerts._dispatch` pipeline used by every other
    drift alert, deduped per `(persona_id, severity, date_utc)` for
    24h so a flapping persona only pages once per day.

Configuration (all read from env, falling back to defaults):
  * ``PERSONA_BRIER_ALERT_ENABLED``        (default 1)
  * ``PERSONA_BRIER_ALERT_MIN_DEBATES``    (default 5)
  * ``PERSONA_BRIER_ALERT_ABS_THRESHOLD``  (default 0.30)
  * ``PERSONA_BRIER_ALERT_JUMP_THRESHOLD`` (default 0.10)
  * ``PERSONA_BRIER_CHECK_INTERVAL_SECS``  (default 600 — every 10m)
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any

from .. import db
from ..health import error_log
from . import alerts
from .daily_summary import aggregate_persona_day


log = logging.getLogger("axiom.notifications.persona_brier_alerts")


# ---------------------------------------------------------------------------
# Env-tunable knobs
# ---------------------------------------------------------------------------


def _enabled() -> bool:
    raw = (os.environ.get("PERSONA_BRIER_ALERT_ENABLED") or "1").strip().lower()
    return raw in ("1", "true", "yes", "on", "y")


def _min_debates() -> int:
    try:
        v = int(os.environ.get("PERSONA_BRIER_ALERT_MIN_DEBATES") or "5")
    except Exception:
        return 5
    return max(1, v)


def _abs_threshold() -> float:
    try:
        v = float(os.environ.get("PERSONA_BRIER_ALERT_ABS_THRESHOLD") or "0.30")
    except Exception:
        return 0.30
    # Brier on a [0, 1] outcome is bounded in [0, 1]; clamp defensively.
    return max(0.0, min(1.0, v))


def _jump_threshold() -> float:
    try:
        v = float(os.environ.get("PERSONA_BRIER_ALERT_JUMP_THRESHOLD") or "0.10")
    except Exception:
        return 0.10
    return max(0.0, min(1.0, v))


def _check_interval_secs() -> int:
    try:
        v = int(os.environ.get("PERSONA_BRIER_CHECK_INTERVAL_SECS") or "600")
    except Exception:
        return 600
    return max(30, v)


# ---------------------------------------------------------------------------
# All-time persona Brier baseline.
# ---------------------------------------------------------------------------


async def all_time_baselines() -> dict[str, float]:
    """Return the per-persona all-time average Brier score, keyed by
    persona id (`swarm_analyst_metrics.analyst_id`).

    Aggregates every (persona, vertical, league, market_type) sub-row
    in `swarm_analyst_metrics`, weighting each by its
    `total_predictions` so cold-start sub-rows can't drag the mean
    around.  Returns an empty dict on DB error so the caller can fall
    back to the absolute-threshold check alone.
    """
    out: dict[str, float] = {}
    try:
        rows = await db.fetch(
            """
            SELECT analyst_id,
                   sum(brier_score * total_predictions)::float AS num,
                   sum(total_predictions)::float                AS denom
              FROM swarm_analyst_metrics
             WHERE brier_score IS NOT NULL
               AND total_predictions > 0
             GROUP BY analyst_id
            """
        )
    except Exception as e:
        log.debug("all_time_baselines query failed (best-effort): %s", e)
        return out
    for r in rows:
        denom = float(r["denom"] or 0.0)
        if denom <= 0:
            continue
        pid = r["analyst_id"]
        if not pid:
            continue
        out[str(pid)] = float(r["num"] or 0.0) / denom
    return out


# ---------------------------------------------------------------------------
# Pure decision logic — easy to unit-test without a DB.
# ---------------------------------------------------------------------------


def compute_findings(
    *,
    personas: list[dict[str, Any]],
    baselines: dict[str, float],
    min_debates: int,
    abs_threshold: float,
    jump_threshold: float,
) -> list[dict[str, Any]]:
    """Return the subset of `personas` that crossed either threshold.

    Each finding dict has:
      ``persona_id``, ``debates``, ``brier``, ``baseline``,
      ``jump`` (today - baseline, or None when no baseline),
      ``severity`` ("warn" / "high"), and ``reason`` ("absolute",
      "jump", or "absolute+jump").
    """
    findings: list[dict[str, Any]] = []
    for row in personas or []:
        pid = row.get("persona_id")
        if not pid:
            continue
        try:
            debates = int(row.get("debates") or 0)
            brier = float(row.get("brier"))
        except (TypeError, ValueError):
            continue
        if debates < min_debates:
            continue

        absolute_breach = brier > abs_threshold

        baseline = baselines.get(str(pid))
        jump: float | None = None
        jump_breach = False
        if baseline is not None:
            jump = round(brier - float(baseline), 4)
            jump_breach = jump > jump_threshold

        if not (absolute_breach or jump_breach):
            continue

        if absolute_breach and jump_breach:
            reason = "absolute+jump"
        elif absolute_breach:
            reason = "absolute"
        else:
            reason = "jump"

        # "high" when both rules trip OR when the absolute breach is
        # severe (>=0.4 or >=10pp above threshold). Otherwise "warn".
        severity = "warn"
        if absolute_breach and jump_breach:
            severity = "high"
        elif absolute_breach and brier >= max(0.40, abs_threshold + 0.10):
            severity = "high"

        findings.append(
            {
                "persona_id": str(pid),
                "debates": debates,
                "brier": round(brier, 4),
                "baseline": (
                    round(float(baseline), 4) if baseline is not None else None
                ),
                "jump": jump,
                "severity": severity,
                "reason": reason,
                "abs_threshold": abs_threshold,
                "jump_threshold": jump_threshold,
            }
        )
    findings.sort(key=lambda f: (-f["brier"], f["persona_id"]))
    return findings


# ---------------------------------------------------------------------------
# Persistence + dispatch
# ---------------------------------------------------------------------------


async def _record_alert_row(
    *, finding: dict[str, Any], date_utc: str, message: str
) -> bool:
    """Insert a `model_health_alerts` row for this finding, suppressing
    same-day duplicates (vertical=persona_id, alert_type, severity).

    Returns True if a fresh row was inserted, False when an open row
    already covers this (persona, severity) for today.
    """
    pid = finding["persona_id"]
    severity = finding["severity"]
    metric_value = float(finding["brier"])
    try:
        recent = await db.fetchval(
            """
            SELECT 1 FROM model_health_alerts
             WHERE alert_type = 'persona_daily_brier'
               AND vertical = $1
               AND severity = $2
               AND created_at > now() - interval '24 hours'
             LIMIT 1
            """,
            pid,
            severity,
        )
        if recent:
            return False
        await db.execute(
            """
            INSERT INTO model_health_alerts
                (vertical, alert_type, severity, metric_name,
                 metric_value, threshold, message, acknowledged)
            VALUES ($1, 'persona_daily_brier', $2, $3, $4, $5, $6, false)
            """,
            pid,
            severity,
            f"persona_daily_brier:{date_utc}",
            metric_value,
            float(finding["abs_threshold"]),
            message,
        )
        return True
    except Exception as e:
        log.warning(
            "persona_daily_brier alert insert failed for %s: %s", pid, e
        )
        return False


def _build_message(finding: dict[str, Any], date_utc: str) -> str:
    parts = [
        f"Persona {finding['persona_id']} daily Brier "
        f"{finding['brier']:.4f} on {date_utc} "
        f"({finding['debates']} debates)"
    ]
    if finding["reason"] in ("absolute", "absolute+jump"):
        parts.append(f"abs>{finding['abs_threshold']:.2f}")
    if finding["reason"] in ("jump", "absolute+jump"):
        if finding["baseline"] is not None and finding["jump"] is not None:
            parts.append(
                f"jump +{finding['jump']:.4f} vs all-time "
                f"{finding['baseline']:.4f} (>+{finding['jump_threshold']:.2f})"
            )
    return " :: ".join(parts)


async def _dispatch_finding(*, finding: dict[str, Any], date_utc: str) -> bool:
    """Persist the alert row and dispatch the operator email.

    Email dispatch is gated by the generic `dedupe_or_skip` so a
    persona that flaps in and out of the window only pages once per
    day.  Insert + email are independently best-effort: a DB insert
    failure still tries to send the email so the operator hears about
    the degradation.
    """
    message = _build_message(finding, date_utc)
    raised = await _record_alert_row(
        finding=finding, date_utc=date_utc, message=message
    )
    try:
        ok = await alerts.dedupe_or_skip(
            "persona_daily_brier",
            f"{finding['persona_id']}:{finding['severity']}:{date_utc}",
            ttl_seconds=24 * 3600,
        )
    except Exception as e:  # pragma: no cover - dedupe fails open
        log.warning("persona_brier dedupe claim failed: %s", e)
        ok = True
    if ok:
        try:
            alerts.persona_brier_drift(
                persona_id=finding["persona_id"],
                date_utc=date_utc,
                brier=float(finding["brier"]),
                baseline=(
                    float(finding["baseline"])
                    if finding["baseline"] is not None
                    else None
                ),
                jump=(
                    float(finding["jump"])
                    if finding["jump"] is not None
                    else None
                ),
                debates=int(finding["debates"]),
                abs_threshold=float(finding["abs_threshold"]),
                jump_threshold=float(finding["jump_threshold"]),
                severity=str(finding["severity"]),
                reason=str(finding["reason"]),
                note=message,
            )
        except Exception as e:  # pragma: no cover
            log.warning(
                "persona_brier email dispatch failed for %s: %s",
                finding["persona_id"], e,
            )
    return raised


async def check_now(*, date_utc: str | None = None) -> dict[str, Any]:
    """Run one evaluation pass and return a summary dict.

    Safe to call from the scheduler loop, the test endpoint, or a
    one-shot admin task.  Always returns a dict (never raises) so a
    transient DB outage doesn't kill the parent loop.
    """
    if date_utc is None:
        date_utc = datetime.now(tz=timezone.utc).date().isoformat()
    out: dict[str, Any] = {
        "date_utc": date_utc,
        "checked_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "personas_checked": 0,
        "findings": [],
        "alerts_raised": 0,
        "min_debates": _min_debates(),
        "abs_threshold": _abs_threshold(),
        "jump_threshold": _jump_threshold(),
    }
    try:
        personas = await aggregate_persona_day(date_utc)
    except Exception as e:
        log.warning("aggregate_persona_day failed for %s: %s", date_utc, e)
        error_log("persona_brier_alerts.aggregate", f"{type(e).__name__}: {e}")
        return out
    out["personas_checked"] = len(personas or [])
    baselines = await all_time_baselines()
    findings = compute_findings(
        personas=personas or [],
        baselines=baselines,
        min_debates=out["min_debates"],
        abs_threshold=out["abs_threshold"],
        jump_threshold=out["jump_threshold"],
    )
    out["findings"] = findings
    raised_count = 0
    for f in findings:
        try:
            if await _dispatch_finding(finding=f, date_utc=date_utc):
                raised_count += 1
        except Exception as e:  # pragma: no cover
            log.warning(
                "persona_brier dispatch error for %s: %s",
                f.get("persona_id"), e,
            )
    out["alerts_raised"] = raised_count
    return out


# ---------------------------------------------------------------------------
# Background loop
# ---------------------------------------------------------------------------


_task: asyncio.Task | None = None
_stop = asyncio.Event()


async def _loop() -> None:
    interval = _check_interval_secs()
    log.info(
        "persona_brier_alerts loop started: interval=%ss "
        "min_debates=%s abs=%.3f jump=%.3f",
        interval, _min_debates(), _abs_threshold(), _jump_threshold(),
    )
    while not _stop.is_set():
        try:
            await check_now()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # pragma: no cover
            log.warning("persona_brier_alerts loop error: %s", e)
            error_log("persona_brier_alerts.loop", f"{type(e).__name__}: {e}")
        try:
            await asyncio.wait_for(_stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
        else:
            return


async def start() -> None:
    global _task
    if not _enabled():
        log.info("persona_brier_alerts disabled")
        return
    if _task and not _task.done():
        return
    _stop.clear()
    _task = asyncio.create_task(_loop(), name="persona-brier-alerts")


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
