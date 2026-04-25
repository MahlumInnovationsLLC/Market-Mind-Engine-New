"""Scheduled crypto-binary backtest runner + drift watcher (task #421).

Wraps :func:`app.crypto.backtest.run_backtest` so the operator no
longer has to remember to open the dashboard and click *Run*. On a
configurable cadence (default: every 6 hours) we:

  1. iterate every supported ``(symbol, timeframe)`` pair
     (``app.crypto.tickers.SUPPORTED_SYMBOLS`` x
     ``SUPPORTED_TIMEFRAMES``);
  2. run an expanding-window backtest and persist the
     :class:`BacktestResult` into the ``crypto_backtest_runs`` table
     (introduced by migration ``0021_crypto_backtest_runs``) so the
     dashboard can chart Brier / log-loss / accuracy as a trend
     instead of just the latest snapshot;
  3. compare the fresh result to a rolling baseline of prior runs
     for the same pair and page the operator (per-pair-deduped) when
     either Brier or log-loss crosses an absolute threshold or jumps
     by more than the configured delta vs that baseline.

Failures never kill the loop — every cycle catches and logs so a sick
DB or a single bad pair can't stop us from evaluating the rest.

Configuration (all read from env, with sensible defaults):

  * ``CRYPTO_BACKTEST_SCHEDULER_ENABLED``     (default 1)
  * ``CRYPTO_BACKTEST_INTERVAL_HOURS``        (default 6.0)
  * ``CRYPTO_BACKTEST_DAYS``                  (default 14)
  * ``CRYPTO_BACKTEST_LOOKBACK``              (default 50)
  * ``CRYPTO_BACKTEST_MIN_EVALUATED``         (default 30)
  * ``CRYPTO_BACKTEST_BASELINE_RUNS``         (default 5)
  * ``CRYPTO_BACKTEST_BRIER_ABS_THRESHOLD``   (default 0.30)
  * ``CRYPTO_BACKTEST_LOGLOSS_ABS_THRESHOLD`` (default 0.85)
  * ``CRYPTO_BACKTEST_BRIER_JUMP_THRESHOLD``  (default 0.05)
  * ``CRYPTO_BACKTEST_LOGLOSS_JUMP_THRESHOLD``(default 0.10)
  * ``CRYPTO_BACKTEST_ALERT_TTL_SECONDS``     (default 21600 — 6h, matches default cadence)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from .. import db
from ..health import error_log
from ..notifications import alerts as notif_alerts
from . import backtest as crypto_backtest
from .backtest import BacktestResult
from .tickers import SUPPORTED_SYMBOLS, SUPPORTED_TIMEFRAMES


log = logging.getLogger("axiom.crypto.backtest_scheduler")


# ---------------------------------------------------------------------------
# Env-tunable knobs
# ---------------------------------------------------------------------------


def _enabled() -> bool:
    raw = (os.environ.get("CRYPTO_BACKTEST_SCHEDULER_ENABLED") or "1").strip().lower()
    return raw in ("1", "true", "yes", "on", "y")


def _interval_seconds() -> float:
    try:
        v = float(os.environ.get("CRYPTO_BACKTEST_INTERVAL_HOURS") or "6")
    except Exception:
        return 6.0 * 3600.0
    return max(60.0, v * 3600.0)


def _days() -> int:
    try:
        v = int(os.environ.get("CRYPTO_BACKTEST_DAYS") or "14")
    except Exception:
        return 14
    return max(1, min(90, v))


def _lookback() -> int:
    try:
        v = int(os.environ.get("CRYPTO_BACKTEST_LOOKBACK") or "50")
    except Exception:
        return 50
    return max(10, min(500, v))


def _min_evaluated() -> int:
    try:
        v = int(os.environ.get("CRYPTO_BACKTEST_MIN_EVALUATED") or "30")
    except Exception:
        return 30
    return max(1, v)


def _baseline_runs() -> int:
    try:
        v = int(os.environ.get("CRYPTO_BACKTEST_BASELINE_RUNS") or "5")
    except Exception:
        return 5
    return max(1, v)


def _brier_abs_threshold() -> float:
    try:
        v = float(os.environ.get("CRYPTO_BACKTEST_BRIER_ABS_THRESHOLD") or "0.30")
    except Exception:
        return 0.30
    return max(0.0, min(1.0, v))


def _logloss_abs_threshold() -> float:
    try:
        v = float(os.environ.get("CRYPTO_BACKTEST_LOGLOSS_ABS_THRESHOLD") or "0.85")
    except Exception:
        return 0.85
    # Log-loss is unbounded above; a sane upper clamp of 10 keeps a
    # typo from disabling the alert entirely.
    return max(0.0, min(10.0, v))


def _brier_jump_threshold() -> float:
    try:
        v = float(os.environ.get("CRYPTO_BACKTEST_BRIER_JUMP_THRESHOLD") or "0.05")
    except Exception:
        return 0.05
    return max(0.0, min(1.0, v))


def _logloss_jump_threshold() -> float:
    try:
        v = float(os.environ.get("CRYPTO_BACKTEST_LOGLOSS_JUMP_THRESHOLD") or "0.10")
    except Exception:
        return 0.10
    return max(0.0, min(10.0, v))


def _alert_ttl_seconds() -> int:
    try:
        v = int(os.environ.get("CRYPTO_BACKTEST_ALERT_TTL_SECONDS") or str(6 * 3600))
    except Exception:
        return 6 * 3600
    return max(60, v)


def supported_pairs() -> list[tuple[str, str]]:
    """Cartesian product of supported symbols x timeframes. Defined as a
    helper so tests can monkeypatch a smaller grid."""
    return [(sym, tf) for sym in SUPPORTED_SYMBOLS for tf in SUPPORTED_TIMEFRAMES]


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


async def persist_run(
    result: BacktestResult, *, days: int, lookback: int
) -> int | None:
    """Insert a `crypto_backtest_runs` row for the supplied result.

    Returns the new row id on success, ``None`` on failure. Failures
    are logged but never propagate so a transient DB hiccup can't kill
    the scheduler loop.
    """
    payload = result.as_dict()
    reliability_json = json.dumps(payload.get("reliability", []))
    try:
        row_id = await db.fetchval(
            """
            INSERT INTO crypto_backtest_runs
                (symbol, timeframe, days, lookback,
                 n_evaluated, accuracy, brier_score, log_loss,
                 reliability, started_at, completed_at)
            VALUES ($1, $2, $3, $4,
                    $5, $6, $7, $8,
                    $9::jsonb, $10::timestamptz, $11::timestamptz)
            RETURNING id
            """,
            result.symbol,
            result.timeframe,
            int(days),
            int(lookback),
            int(result.n_evaluated),
            float(result.accuracy),
            float(result.brier_score),
            float(result.log_loss),
            reliability_json,
            result.started_at,
            result.completed_at,
        )
        return int(row_id) if row_id is not None else None
    except Exception as e:
        log.warning(
            "persist_run failed for %s/%s: %s",
            result.symbol, result.timeframe, e,
        )
        return None


async def fetch_baseline(
    *, symbol: str, timeframe: str, limit: int, exclude_id: int | None = None
) -> dict[str, float | int]:
    """Average Brier / log-loss across the most recent ``limit`` prior
    runs for the pair, optionally excluding the row we just wrote.

    Returns a dict with keys ``count``, ``brier``, ``log_loss``.
    Empty (count=0) when there's no history yet.
    """
    out: dict[str, float | int] = {"count": 0, "brier": 0.0, "log_loss": 0.0}
    try:
        rows = await db.fetch(
            """
            SELECT brier_score, log_loss
              FROM crypto_backtest_runs
             WHERE symbol = $1 AND timeframe = $2
               AND ($3::bigint IS NULL OR id <> $3)
             ORDER BY completed_at DESC
             LIMIT $4
            """,
            symbol,
            timeframe,
            exclude_id,
            int(limit),
        )
    except Exception as e:
        log.debug(
            "fetch_baseline %s/%s failed (best-effort): %s",
            symbol, timeframe, e,
        )
        return out
    if not rows:
        return out
    n = len(rows)
    out["count"] = n
    out["brier"] = sum(float(r["brier_score"] or 0.0) for r in rows) / n
    out["log_loss"] = sum(float(r["log_loss"] or 0.0) for r in rows) / n
    return out


# ---------------------------------------------------------------------------
# Pure decision logic — easy to unit-test without a DB.
# ---------------------------------------------------------------------------


def compute_drift_findings(
    *,
    symbol: str,
    timeframe: str,
    n_evaluated: int,
    brier: float,
    log_loss: float,
    baseline: dict[str, Any] | None,
    min_evaluated: int,
    brier_abs_threshold: float,
    logloss_abs_threshold: float,
    brier_jump_threshold: float,
    logloss_jump_threshold: float,
) -> list[dict[str, Any]]:
    """Return zero or more drift findings for the supplied run.

    Each finding dict has ``metric`` ("brier" | "log_loss"), ``value``,
    ``threshold``, ``baseline``, ``jump`` (None when no baseline) and
    ``reason`` ("absolute", "jump", or "absolute+jump").

    Empty list when the run is below the min-evaluated floor (i.e. not
    enough samples to draw a conclusion) or both metrics are inside
    their bands.
    """
    findings: list[dict[str, Any]] = []
    if int(n_evaluated or 0) < int(min_evaluated):
        return findings

    baseline_count = 0
    baseline_brier: float | None = None
    baseline_logloss: float | None = None
    if baseline:
        try:
            baseline_count = int(baseline.get("count") or 0)
        except (TypeError, ValueError):
            baseline_count = 0
        if baseline_count > 0:
            try:
                baseline_brier = float(baseline.get("brier"))
            except (TypeError, ValueError):
                baseline_brier = None
            try:
                baseline_logloss = float(baseline.get("log_loss"))
            except (TypeError, ValueError):
                baseline_logloss = None

    def _evaluate(
        metric: str,
        value: float,
        abs_threshold: float,
        jump_threshold: float,
        baseline_value: float | None,
    ) -> dict[str, Any] | None:
        absolute_breach = value > abs_threshold
        jump: float | None = None
        jump_breach = False
        if baseline_value is not None:
            jump = round(value - baseline_value, 6)
            jump_breach = jump > jump_threshold
        if not (absolute_breach or jump_breach):
            return None
        if absolute_breach and jump_breach:
            reason = "absolute+jump"
        elif absolute_breach:
            reason = "absolute"
        else:
            reason = "jump"
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "metric": metric,
            "value": round(float(value), 6),
            "threshold": float(abs_threshold),
            "jump_threshold": float(jump_threshold),
            "baseline": (
                round(float(baseline_value), 6)
                if baseline_value is not None
                else None
            ),
            "baseline_runs": baseline_count,
            "jump": jump,
            "reason": reason,
            "n_evaluated": int(n_evaluated),
        }

    f = _evaluate(
        "brier",
        float(brier),
        brier_abs_threshold,
        brier_jump_threshold,
        baseline_brier,
    )
    if f is not None:
        findings.append(f)
    f = _evaluate(
        "log_loss",
        float(log_loss),
        logloss_abs_threshold,
        logloss_jump_threshold,
        baseline_logloss,
    )
    if f is not None:
        findings.append(f)
    return findings


# ---------------------------------------------------------------------------
# Persisted alert row + dispatch
# ---------------------------------------------------------------------------


def _build_drift_note(finding: dict[str, Any]) -> str:
    parts = [
        f"{finding['symbol']}/{finding['timeframe']} "
        f"{finding['metric']} {finding['value']:.4f} "
        f"(n={finding['n_evaluated']})"
    ]
    if finding["reason"] in ("absolute", "absolute+jump"):
        parts.append(f"abs>{finding['threshold']:.4f}")
    if finding["reason"] in ("jump", "absolute+jump"):
        if (
            finding.get("baseline") is not None
            and finding.get("jump") is not None
        ):
            parts.append(
                f"jump +{finding['jump']:.4f} vs baseline "
                f"{finding['baseline']:.4f} over {finding['baseline_runs']} runs "
                f"(>+{finding['jump_threshold']:.4f})"
            )
    return " :: ".join(parts)


async def _record_drift_row(*, finding: dict[str, Any], message: str) -> bool:
    """Insert a `model_health_alerts` row for this drift finding.

    Suppresses same-day duplicates keyed on
    ``(vertical=symbol/timeframe, alert_type='crypto_backtest_drift',
    metric_name=metric)``.

    Returns True when a fresh row landed, False when an open row
    already covers this incident.
    """
    pair_label = f"{finding['symbol']}/{finding['timeframe']}"
    metric_name = f"crypto_backtest_{finding['metric']}"
    try:
        recent = await db.fetchval(
            """
            SELECT 1 FROM model_health_alerts
             WHERE alert_type = 'crypto_backtest_drift'
               AND vertical = $1
               AND metric_name = $2
               AND created_at > now() - interval '24 hours'
             LIMIT 1
            """,
            pair_label,
            metric_name,
        )
        if recent:
            return False
        await db.execute(
            """
            INSERT INTO model_health_alerts
                (vertical, alert_type, severity, metric_name,
                 metric_value, threshold, message, acknowledged)
            VALUES ($1, 'crypto_backtest_drift', $2, $3, $4, $5, $6, false)
            """,
            pair_label,
            "warn",
            metric_name,
            float(finding["value"]),
            float(finding["threshold"]),
            message,
        )
        return True
    except Exception as e:
        log.warning(
            "crypto_backtest_drift alert insert failed for %s/%s: %s",
            finding["symbol"], finding["timeframe"], e,
        )
        return False


async def _dispatch_finding(*, finding: dict[str, Any]) -> bool:
    """Persist the alert row + dispatch the operator email.

    Email dispatch is gated by `notifications.alerts.dedupe_or_skip`
    keyed on ``(symbol, timeframe, metric, reason)`` so the same
    incident pages once per TTL window even when the loop fires every
    cadence tick.
    """
    message = _build_drift_note(finding)
    raised = await _record_drift_row(finding=finding, message=message)
    dedupe_key = (
        f"{finding['symbol']}:{finding['timeframe']}:"
        f"{finding['metric']}:{finding['reason']}"
    )
    try:
        ok = await notif_alerts.dedupe_or_skip(
            "crypto_backtest_drift",
            dedupe_key,
            ttl_seconds=_alert_ttl_seconds(),
        )
    except Exception as e:  # pragma: no cover — dedupe fails open
        log.warning("crypto_backtest_drift dedupe claim failed: %s", e)
        ok = True
    if ok:
        try:
            notif_alerts.crypto_backtest_drift(
                symbol=finding["symbol"],
                timeframe=finding["timeframe"],
                metric=finding["metric"],
                value=float(finding["value"]),
                threshold=float(finding["threshold"]),
                baseline=(
                    float(finding["baseline"])
                    if finding.get("baseline") is not None
                    else None
                ),
                baseline_runs=int(finding.get("baseline_runs") or 0),
                jump=(
                    float(finding["jump"])
                    if finding.get("jump") is not None
                    else None
                ),
                jump_threshold=float(finding["jump_threshold"]),
                reason=str(finding["reason"]),
                n_evaluated=int(finding["n_evaluated"]),
                note=message,
            )
        except Exception as e:  # pragma: no cover — email best-effort
            log.warning(
                "crypto_backtest_drift email dispatch failed for %s/%s: %s",
                finding["symbol"], finding["timeframe"], e,
            )
    return raised


# ---------------------------------------------------------------------------
# Per-pair runner
# ---------------------------------------------------------------------------


async def run_pair(
    *,
    symbol: str,
    timeframe: str,
    days: int | None = None,
    lookback: int | None = None,
) -> dict[str, Any]:
    """Run a single ``(symbol, timeframe)`` backtest, persist, evaluate
    drift, dispatch alerts.

    Returns a structured dict so the loop can log a one-line summary
    and so an operator-facing endpoint (or test) can call this
    synchronously and inspect the outcome.
    """
    days_v = int(days if days is not None else _days())
    lookback_v = int(lookback if lookback is not None else _lookback())
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    summary: dict[str, Any] = {
        "symbol": symbol,
        "timeframe": timeframe,
        "days": days_v,
        "lookback": lookback_v,
        "started_at": started_at,
        "ok": False,
        "row_id": None,
        "n_evaluated": 0,
        "brier_score": None,
        "log_loss": None,
        "accuracy": None,
        "baseline": None,
        "findings": [],
        "alerts_raised": 0,
        "error": None,
    }
    try:
        result = await crypto_backtest.run_backtest(
            symbol=symbol,
            timeframe=timeframe,
            days=days_v,
            lookback=lookback_v,
        )
    except Exception as e:
        log.warning(
            "run_backtest failed for %s/%s: %s",
            symbol, timeframe, e,
        )
        error_log(
            "crypto_backtest_scheduler.run",
            f"{symbol}/{timeframe}: {type(e).__name__}: {e}",
        )
        summary["error"] = f"{type(e).__name__}: {e}"
        return summary

    summary["ok"] = True
    summary["n_evaluated"] = int(result.n_evaluated)
    summary["brier_score"] = float(result.brier_score)
    summary["log_loss"] = float(result.log_loss)
    summary["accuracy"] = float(result.accuracy)

    row_id = await persist_run(result, days=days_v, lookback=lookback_v)
    summary["row_id"] = row_id

    # Skip drift evaluation when the run produced too few evaluations
    # to be meaningful (cold OHLCV or a too-short window).
    min_n = _min_evaluated()
    if result.n_evaluated < min_n:
        return summary

    baseline = await fetch_baseline(
        symbol=symbol,
        timeframe=timeframe,
        limit=_baseline_runs(),
        exclude_id=row_id,
    )
    summary["baseline"] = dict(baseline)
    findings = compute_drift_findings(
        symbol=symbol,
        timeframe=timeframe,
        n_evaluated=result.n_evaluated,
        brier=result.brier_score,
        log_loss=result.log_loss,
        baseline=baseline,
        min_evaluated=min_n,
        brier_abs_threshold=_brier_abs_threshold(),
        logloss_abs_threshold=_logloss_abs_threshold(),
        brier_jump_threshold=_brier_jump_threshold(),
        logloss_jump_threshold=_logloss_jump_threshold(),
    )
    summary["findings"] = findings
    raised = 0
    for f in findings:
        try:
            if await _dispatch_finding(finding=f):
                raised += 1
        except Exception as e:  # pragma: no cover — defensive
            log.warning(
                "crypto_backtest_drift dispatch error for %s/%s: %s",
                f.get("symbol"), f.get("timeframe"), e,
            )
    summary["alerts_raised"] = raised
    return summary


async def check_now(
    *, pairs: Iterable[tuple[str, str]] | None = None
) -> dict[str, Any]:
    """Run one full sweep across every supported pair (or the supplied
    subset). Always returns a dict — never raises — so the background
    loop and the admin endpoint can both call it the same way.
    """
    plan: Sequence[tuple[str, str]] = list(pairs) if pairs is not None else supported_pairs()
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out: dict[str, Any] = {
        "started_at": started_at,
        "pairs": [],
        "alerts_raised": 0,
        "errors": 0,
    }
    for symbol, timeframe in plan:
        try:
            res = await run_pair(symbol=symbol, timeframe=timeframe)
        except Exception as e:  # pragma: no cover — defensive
            log.warning(
                "run_pair raised unexpectedly for %s/%s: %s",
                symbol, timeframe, e,
            )
            res = {
                "symbol": symbol,
                "timeframe": timeframe,
                "ok": False,
                "error": f"{type(e).__name__}: {e}",
                "alerts_raised": 0,
            }
        out["pairs"].append(res)
        if not res.get("ok"):
            out["errors"] += 1
        out["alerts_raised"] += int(res.get("alerts_raised") or 0)
    out["completed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return out


# ---------------------------------------------------------------------------
# Background loop
# ---------------------------------------------------------------------------


_task: asyncio.Task | None = None
_stop = asyncio.Event()


async def _loop() -> None:
    interval = _interval_seconds()
    log.info(
        "crypto_backtest_scheduler loop started: "
        "interval=%.1fs days=%d lookback=%d brier_abs=%.3f log_abs=%.3f",
        interval, _days(), _lookback(),
        _brier_abs_threshold(), _logloss_abs_threshold(),
    )
    while not _stop.is_set():
        try:
            summary = await check_now()
            log.info(
                "crypto_backtest_scheduler sweep done: pairs=%d errors=%d alerts=%d",
                len(summary.get("pairs") or []),
                summary.get("errors", 0),
                summary.get("alerts_raised", 0),
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # pragma: no cover — defensive
            log.warning("crypto_backtest_scheduler loop error: %s", e)
            error_log(
                "crypto_backtest_scheduler.loop",
                f"{type(e).__name__}: {e}",
            )
        try:
            await asyncio.wait_for(_stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
        else:
            return


async def start() -> None:
    global _task
    if not _enabled():
        log.info("crypto_backtest_scheduler disabled")
        return
    if _task and not _task.done():
        return
    _stop.clear()
    _task = asyncio.create_task(_loop(), name="crypto-backtest-scheduler")


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
