"""Periodic system-health collector.

A single asyncio background task that, every `interval_seconds`, probes
each external dependency and writes a snapshot to the in-memory
`get_snapshot()` slot.  The /api/health/snapshot endpoint serves the
last snapshot directly — there is no per-request fan-out so the health
page stays fast even with dozens of dependencies.

Probe categories:
  * `live`   — actively pinged (postgres, kalshi REST, storage)
  * `passive` — derived from background-task state already kept in
                memory (feeds, models, kalshi WS, orchestrator,
                settlement, kill switch)
  * `config`  — configured-or-not check (openai, anthropic, stripe,
                revenuecat, postmark, supabase) — these are cheap and
                we don't want to burn quota / dollars pinging them on
                a 30s loop.

Each card has the same shape so the UI renders one component:
    {
      "key": "postgres",
      "label": "Postgres",
      "category": "live",
      "status": "ok" | "degraded" | "down" | "unconfigured",
      "detail": "...",
      "checked_at": ISO-8601,
      "latency_ms": 12.3 | None,
      "extras": {...}    # free-form per-probe data
    }
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from . import calibrators, db, storage
from .config import get_settings
from .feeds import registry as feed_registry
from .kalshi import bus as kalshi_bus, client as kalshi_client_mod, lifecycle as kalshi_lifecycle, ws as kalshi_ws
from .notifications import postmark
from . import models_loader
from .elo import scheduler as elo_scheduler
from .orchestrator import scheduler as orchestrator_scheduler
from .settlement import scheduler as settlement_scheduler


log = logging.getLogger("axiom.health")


HEALTH_INTERVAL_SECONDS = max(
    10, int(os.environ.get("AXIOM_HEALTH_INTERVAL_SECONDS") or "30")
)
ERROR_LOG_MAX = 200


# ---------------------------------------------------------------------------
# Recent error ring buffer — populated by error_log() callers and by
# our own probes when they fail.  Surfaced via /api/health/errors.
# ---------------------------------------------------------------------------


@dataclass
class _ErrorEntry:
    at: float
    source: str
    message: str


_errors: deque[_ErrorEntry] = deque(maxlen=ERROR_LOG_MAX)


def error_log(source: str, message: str) -> None:
    _errors.append(_ErrorEntry(at=time.time(), source=source, message=message))


def recent_errors(limit: int = 100, *, since_seconds: float | None = None) -> list[dict]:
    cutoff = time.time() - since_seconds if since_seconds else 0.0
    out = [e for e in _errors if e.at >= cutoff]
    out = out[-limit:][::-1]
    return [
        {
            "at": _iso(e.at),
            "source": e.source,
            "message": e.message,
        }
        for e in out
    ]


def errors_in_last_24h() -> int:
    cutoff = time.time() - 86_400
    return sum(1 for e in _errors if e.at >= cutoff)


# ---------------------------------------------------------------------------
# Snapshot store
# ---------------------------------------------------------------------------


@dataclass
class _Snapshot:
    generated_at: float = 0.0
    duration_ms: float = 0.0
    cards: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)


_snapshot = _Snapshot()
_lock = asyncio.Lock()
_started_at: float = time.time()


def get_snapshot() -> dict:
    s = _snapshot
    return {
        "generated_at": _iso(s.generated_at) if s.generated_at else None,
        "duration_ms": s.duration_ms,
        "uptime_seconds": round(time.time() - _started_at, 1),
        "cards": s.cards,
        "summary": s.summary,
    }


def _iso(epoch: float) -> str | None:
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Card builder
# ---------------------------------------------------------------------------


def _card(
    *,
    key: str,
    label: str,
    category: str,
    status: str,
    detail: str = "",
    latency_ms: float | None = None,
    extras: dict | None = None,
) -> dict:
    return {
        "key": key,
        "label": label,
        "category": category,
        "status": status,
        "detail": detail,
        "checked_at": _iso(time.time()),
        "latency_ms": latency_ms,
        "extras": extras or {},
    }


# ---------------------------------------------------------------------------
# Live probes
# ---------------------------------------------------------------------------


async def _probe_postgres() -> dict:
    t0 = time.time()
    try:
        ver = await asyncio.wait_for(db.fetchval("SELECT 1"), timeout=5.0)
        ms = round((time.time() - t0) * 1000, 1)
        return _card(
            key="postgres",
            label="Postgres (Supabase)",
            category="live",
            status="ok" if ver == 1 else "degraded",
            detail="select 1 -> 1" if ver == 1 else f"select 1 -> {ver!r}",
            latency_ms=ms,
        )
    except Exception as e:
        ms = round((time.time() - t0) * 1000, 1)
        error_log("postgres", f"{type(e).__name__}: {e}")
        return _card(
            key="postgres",
            label="Postgres (Supabase)",
            category="live",
            status="down",
            detail=f"{type(e).__name__}: {e}"[:240],
            latency_ms=ms,
        )


async def _probe_kalshi() -> dict:
    t0 = time.time()
    try:
        res = await asyncio.wait_for(kalshi_client_mod.health_probe(), timeout=8.0)
        ms = round((time.time() - t0) * 1000, 1)
        ok = bool(res.get("ok"))
        if not ok:
            error_log("kalshi", str(res)[:240])
        return _card(
            key="kalshi",
            label="Kalshi REST",
            category="live",
            status="ok" if ok else "degraded",
            detail=str(res.get("detail") or res.get("error") or "")[:240],
            latency_ms=ms,
            extras=res,
        )
    except Exception as e:
        ms = round((time.time() - t0) * 1000, 1)
        error_log("kalshi", f"{type(e).__name__}: {e}")
        return _card(
            key="kalshi",
            label="Kalshi REST",
            category="live",
            status="down",
            detail=f"{type(e).__name__}: {e}"[:240],
            latency_ms=ms,
        )


async def _probe_storage() -> dict:
    t0 = time.time()
    try:
        items = await asyncio.wait_for(storage.list_path("", limit=1), timeout=8.0)
        ms = round((time.time() - t0) * 1000, 1)
        return _card(
            key="supabase_storage",
            label="Supabase Storage",
            category="live",
            status="ok",
            detail=f"bucket reachable ({len(items)} top-level entry sample)",
            latency_ms=ms,
        )
    except Exception as e:
        ms = round((time.time() - t0) * 1000, 1)
        error_log("storage", f"{type(e).__name__}: {e}")
        return _card(
            key="supabase_storage",
            label="Supabase Storage",
            category="live",
            status="down",
            detail=f"{type(e).__name__}: {e}"[:240],
            latency_ms=ms,
        )


# ---------------------------------------------------------------------------
# Passive probes — read in-memory state populated by other tasks.
# ---------------------------------------------------------------------------


def _probe_kalshi_ws() -> dict:
    ws = kalshi_ws.get_ws_subscriber()
    connected = bool(ws and ws.connected)
    return _card(
        key="kalshi_ws",
        label="Kalshi WebSocket",
        category="passive",
        status="ok" if connected else "degraded",
        detail=("connected" if connected else (ws.last_error if ws else "not started")),
        extras={
            "subscribers": kalshi_bus.subscriber_count,
            "last_error": ws.last_error if ws else None,
        },
    )


def _probe_kalshi_cluster() -> dict:
    """Surface the boot-time Kalshi cluster auth probe.

    The probe runs once at startup and signs an authenticated request
    against the configured cluster. If the key actually belongs to the
    *other* cluster, the operator sees a loud "set KALSHI_ENV=X"
    message here instead of having to grep startup logs.  Reads the
    cached state — does not re-issue the network call.
    """
    state = kalshi_client_mod.cluster_probe_state()
    # Surface the configured re-probe cadence so the dashboard can
    # decide what counts as a "stale" checked_at without having to
    # hard-code the interval in the frontend.
    extras = {
        **state,
        "probe_interval_seconds": kalshi_client_mod.KALSHI_CLUSTER_PROBE_INTERVAL_SECONDS,
    }
    if not state.get("checked"):
        return _card(
            key="kalshi_cluster",
            label="Kalshi cluster auth",
            category="passive",
            status="unconfigured",
            detail="cluster probe has not run yet",
            extras=extras,
        )
    ok = bool(state.get("ok"))
    if ok:
        return _card(
            key="kalshi_cluster",
            label="Kalshi cluster auth",
            category="passive",
            status="ok",
            detail=str(state.get("detail") or "authenticated")[:240],
            extras=extras,
        )
    return _card(
        key="kalshi_cluster",
        label="Kalshi cluster auth",
        category="passive",
        status="down",
        detail=str(state.get("error") or "cluster probe failed")[:240],
        extras=extras,
    )


def _probe_kalshi_reconciler() -> dict:
    """Surface the Kalshi position+fill reconciler's last-tick state.

    `unconfigured` when paper mode / missing creds (the loop is a no-op).
    `degraded` when the loop is supposed to be running but the last tick
    raised an error or surfaced unexpected drift / unknown fills.
    """
    from .kalshi import reconciler as kalshi_reconciler  # local import to avoid cycle
    s = kalshi_reconciler.state()
    settings = get_settings()
    creds = bool(settings.kalshi_api_key_id and settings.kalshi_private_key)
    env_killed = (os.environ.get("AXIOM_RECONCILER_ENABLED", "1") or "").strip() == "0"
    if env_killed or not creds or (settings.trading_mode or "").lower() == "paper":
        return _card(
            key="kalshi_reconciler",
            label="Kalshi reconciler",
            category="passive",
            status="unconfigured",
            detail=(
                "disabled via AXIOM_RECONCILER_ENABLED=0"
                if env_killed
                else "disabled (paper mode or no creds)"
            ),
            extras=s,
        )
    running = bool(s.get("running"))
    last_err = s.get("last_error")
    last_tick = s.get("last_tick_at")
    drift = int(s.get("last_drift_count") or 0)
    unknown = int(s.get("last_unknown_fills") or 0)
    age = (time.time() - float(last_tick)) if last_tick else None
    interval = float(s.get("interval_seconds") or 60.0)
    if not running:
        status, detail = "down", "reconciler not running"
    elif last_err:
        status = "degraded"
        detail = f"last tick error: {last_err}"[:240]
    elif age is not None and age > max(interval * 3, 180):
        status = "degraded"
        detail = f"stale: last tick {age:.0f}s ago"
    elif drift > 0 or unknown > 0:
        status = "degraded"
        detail = (
            f"last tick raised drift={drift}, unknown_fills={unknown} "
            f"(replayed={s.get('last_missed_fills') or 0})"
        )
    else:
        status = "ok"
        detail = (
            f"last tick {age:.0f}s ago | replayed={s.get('total_replayed_fills') or 0} "
            f"| drift_alerts={s.get('total_drift_alerts') or 0}"
            if age is not None
            else "started; awaiting first tick"
        )
    return _card(
        key="kalshi_reconciler",
        label="Kalshi reconciler",
        category="passive",
        status=status,
        detail=detail,
        extras=s,
    )


# ---------------------------------------------------------------------------
# Bankroll-sync silence detector
#
# The continuous Kalshi balance -> bankroll_events sync (`app.bankroll_sync`)
# already tracks `running`, `last_tick_at`, and `last_error`. Surfacing
# that state as a passive health card means a stalled sync flips the
# overall snapshot to `down` (and so any external readiness probe
# alongside it), and a small consecutive-snapshot counter + dedupe
# debounces the resulting Postmark page exactly the same way the WS
# silent-subscriber alert does.
# ---------------------------------------------------------------------------


# Per-incident consecutive-bad snapshot counter, keyed by a stable
# label so a sync that recovers and then re-stalls has to climb the
# threshold again.
_bankroll_sync_bad_snapshot_counts: dict[str, int] = {}

# How many consecutive snapshots the bankroll sync must look stalled
# before paging.  Default 3 -> ~90s at the standard 30s health cadence.
BANKROLL_SYNC_ALERT_THRESHOLD = max(
    1, int(os.environ.get("AXIOM_BANKROLL_SYNC_ALERT_SNAPSHOTS") or "3")
)

# Dedupe TTL for the resulting Postmark alert.  One page per incident
# per hour is plenty — the on-call is looking for the *first* notice,
# not a flood.
BANKROLL_SYNC_ALERT_TTL_SECONDS = max(
    60, int(os.environ.get("AXIOM_BANKROLL_SYNC_ALERT_TTL_SECONDS") or "3600")
)


def _bankroll_sync_status(state: dict) -> tuple[str, str, dict]:
    """Classify a `bankroll_sync.state()` snapshot.

    Returns ``(status, detail, signals)`` where ``status`` is one of
    ``ok | degraded | down``, ``detail`` is the human-readable card
    blurb, and ``signals`` carries the derived numbers (age + threshold)
    the alert + dashboard banner both want without re-deriving them.
    """
    interval = float(state.get("interval_seconds") or 60.0)
    threshold = max(interval * 3.0, 60.0)
    last_tick = state.get("last_tick_at")
    age = (time.time() - float(last_tick)) if last_tick else None
    running = bool(state.get("running"))
    last_err = state.get("last_error")
    signals = {
        "interval_seconds": interval,
        "threshold_seconds": threshold,
        "age_seconds": round(age, 1) if age is not None else None,
    }
    if not running:
        return (
            "down",
            "bankroll sync loop is not running",
            signals,
        )
    if age is None:
        # Loop is up but hasn't completed a single tick yet — the boot
        # window is short enough (default 60s interval) that a missing
        # first tick after the snapshot interval is a real signal.
        return (
            "degraded",
            "no bankroll sync tick observed since boot",
            signals,
        )
    if age > threshold:
        detail = (
            f"stale: last tick {age:.0f}s ago "
            f"(>{threshold:.0f}s threshold)"
        )
        if last_err:
            detail += f" | last_error={str(last_err)[:160]}"
        return ("down", detail, signals)
    if last_err:
        return (
            "degraded",
            f"recovering: last tick {age:.0f}s ago | last_error={str(last_err)[:160]}",
            signals,
        )
    return ("ok", f"last tick {age:.0f}s ago (<= {threshold:.0f}s)", signals)


def _maybe_dispatch_bankroll_sync_alert(
    *, state: dict, status: str, detail: str, signals: dict
) -> dict | None:
    """Update the consecutive-bad counter and fire one Postmark alert
    per incident when the count crosses the threshold.

    Returns the triggered payload (used by tests) or None when this
    snapshot was healthy or below the threshold.
    """
    label = "bankroll_sync"
    # The task spec defines "silent" narrowly as `running is False` or
    # `last_tick_at` older than `interval_seconds * 3`. The probe maps
    # both of those onto the `down` status; a `degraded` snapshot
    # (recovering from a transient REST error, or awaiting first tick)
    # is real but not page-worthy on its own. Gate the counter so a
    # flaky sync that ticks-then-errors doesn't accumulate toward a
    # page that misrepresents the failure mode.
    if status != "down":
        _bankroll_sync_bad_snapshot_counts.pop(label, None)
        return None
    cur = _bankroll_sync_bad_snapshot_counts.get(label, 0) + 1
    _bankroll_sync_bad_snapshot_counts[label] = cur
    if cur != BANKROLL_SYNC_ALERT_THRESHOLD:
        return None

    triggered = {
        "label": label,
        "status": status,
        "detail": detail,
        "consecutive_snapshots": cur,
        "running": bool(state.get("running")),
        "last_tick_at": _iso(state.get("last_tick_at")) if state.get("last_tick_at") else None,
        "last_error": state.get("last_error"),
        "last_balance_cents": state.get("last_balance_cents"),
        "interval_seconds": signals["interval_seconds"],
        "threshold_seconds": signals["threshold_seconds"],
        "age_seconds": signals["age_seconds"],
    }

    from .notifications import alerts as _alerts

    async def _runner() -> None:
        try:
            ok = await _alerts.dedupe_or_skip(
                "bankroll_sync_silent",
                label,
                ttl_seconds=BANKROLL_SYNC_ALERT_TTL_SECONDS,
            )
        except Exception as e:  # pragma: no cover — fail-open
            log.warning("bankroll-sync dedupe failed: %s", e)
            ok = True
        if not ok:
            return
        try:
            _alerts.bankroll_sync_silent(
                last_tick_at=triggered["last_tick_at"],
                age_seconds=triggered["age_seconds"],
                interval_seconds=triggered["interval_seconds"],
                threshold_seconds=triggered["threshold_seconds"],
                running=triggered["running"],
                last_error=triggered["last_error"],
                last_balance_cents=triggered["last_balance_cents"],
                consecutive_snapshots=triggered["consecutive_snapshots"],
            )
        except Exception as e:  # pragma: no cover
            log.warning("bankroll-sync alert dispatch failed: %s", e)

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_runner())
    except RuntimeError:
        # Sync test context — run inline so the test can observe it.
        try:
            asyncio.run(_runner())
        except Exception as e:  # pragma: no cover
            log.warning("inline bankroll-sync dispatch failed: %s", e)

    return triggered


def _probe_bankroll_sync() -> dict:
    """Surface ``bankroll_sync.state()`` as a passive health card.

    `down` when the loop is stopped or the last tick is older than
    ``interval_seconds * 3`` (capped at >=60s so a 5s test interval
    can't trigger on noise). `degraded` when the loop is up but
    recovering from an error or hasn't yet produced a first tick.
    `ok` otherwise. The card itself is what flips the snapshot
    `summary.overall` to `down`, and a separate Postmark alert is
    debounced via the shared notification dedupe table.
    """
    from . import bankroll_sync as _bankroll_sync  # local import to avoid cycle
    state = _bankroll_sync.state()
    status, detail, signals = _bankroll_sync_status(state)
    triggered = _maybe_dispatch_bankroll_sync_alert(
        state=state, status=status, detail=detail, signals=signals
    )
    if status != "ok":
        error_log("bankroll_sync", detail[:240])
    extras = {
        **state,
        **signals,
        "alert_threshold_snapshots": BANKROLL_SYNC_ALERT_THRESHOLD,
        "consecutive_bad_snapshots": _bankroll_sync_bad_snapshot_counts.get(
            "bankroll_sync", 0
        ),
        "alert_triggered_this_snapshot": triggered is not None,
    }
    return _card(
        key="bankroll_sync",
        label="Kalshi bankroll sync",
        category="passive",
        status=status,
        detail=detail,
        extras=extras,
    )


# ---------------------------------------------------------------------------
# Elo updater health + alert (task #524)
#
# The periodic team-Elo updater (`app.elo.scheduler`) is read-mostly:
# without it the operator panel still surfaces last-cycle counters and
# errors, but only when someone is looking. If the loop stops cycling
# (DB outage, deploy bug) or the most recent cycle returned errors,
# ratings silently freeze and downstream picks degrade against stale
# numbers. This probe maps both failure modes onto a `degraded` health
# card and (after a small consecutive-bad-snapshot debounce) fans out
# one Postmark alert per incident, deduped via the shared
# `notification_dedupe` table — the same shape as the bankroll-sync
# silence detector above.
# ---------------------------------------------------------------------------


# Per-incident consecutive-bad snapshot counter, keyed by a stable
# label so an updater that recovers and then re-degrades has to climb
# the threshold again before we re-alert.
_elo_updater_bad_snapshot_counts: dict[str, int] = {}

# How many consecutive snapshots the elo updater must look unhealthy
# before paging.  Default 3 -> ~90s at the standard 30s health cadence,
# enough debounce so a single transient stall doesn't page anyone.
ELO_UPDATER_ALERT_THRESHOLD = max(
    1, int(os.environ.get("AXIOM_ELO_UPDATER_ALERT_SNAPSHOTS") or "3")
)

# Dedupe TTL for the resulting Postmark alert.  One page per incident
# per hour matches the rest of the scheduler-silence alerts.
ELO_UPDATER_ALERT_TTL_SECONDS = max(
    60, int(os.environ.get("AXIOM_ELO_UPDATER_ALERT_TTL_SECONDS") or "3600")
)


def _elo_updater_disabled() -> bool:
    return (
        os.environ.get("AXIOM_ELO_UPDATER_DISABLED", "").lower()
        in ("1", "true", "yes")
    )


def _coerce_iso_to_ts(value: Any) -> float | None:
    """Accept ISO-8601 strings (the scheduler stores `completed_at` as
    a string) and return a UTC epoch float, or None when missing /
    unparseable."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    return None


def _elo_updater_status(state: dict) -> tuple[str, str, dict]:
    """Classify an `elo_scheduler.scheduler.status()` snapshot.

    Returns ``(status, detail, signals)`` where ``status`` is one of
    ``ok | degraded | down | unconfigured``, ``detail`` is the
    human-readable card blurb, and ``signals`` carries the derived
    numbers (age + threshold + reason) the alert dispatcher reuses.
    """
    cycle = float(state.get("cycle_seconds") or 300.0)
    threshold = max(cycle * 3.0, 60.0)
    last = state.get("last_cycle") or {}
    if not isinstance(last, dict):
        last = {}
    last_completed = last.get("completed_at")
    completed_ts = _coerce_iso_to_ts(last_completed)
    age = (time.time() - completed_ts) if completed_ts else None
    last_errors = list(last.get("errors") or [])
    running = bool(state.get("running"))
    games_examined = last.get("games_examined")
    games_updated = last.get("games_updated")
    signals = {
        "cycle_seconds": cycle,
        "threshold_seconds": threshold,
        "age_seconds": round(age, 1) if age is not None else None,
        "last_completed_at": last_completed,
        "last_errors": last_errors,
        "running": running,
        "games_examined": games_examined,
        "games_updated": games_updated,
        "reason": None,
    }

    if _elo_updater_disabled():
        return (
            "unconfigured",
            "elo updater disabled via AXIOM_ELO_UPDATER_DISABLED",
            signals,
        )

    if not running:
        signals["reason"] = "stalled"
        return (
            "degraded",
            "elo updater loop is not running",
            signals,
        )
    if completed_ts is None:
        signals["reason"] = "stalled"
        return (
            "degraded",
            f"running but no cycle completed yet (cadence={cycle:.0f}s)",
            signals,
        )
    if age is not None and age > threshold:
        signals["reason"] = "stalled"
        detail = (
            f"stalled: last cycle {age:.0f}s ago "
            f"(>{threshold:.0f}s threshold)"
        )
        return ("degraded", detail, signals)
    if last_errors:
        signals["reason"] = "errors"
        detail = (
            f"last cycle returned {len(last_errors)} error(s): "
            f"{str(last_errors[0])[:160]}"
        )
        return ("degraded", detail, signals)
    return (
        "ok",
        f"last cycle {age:.0f}s ago (<= {threshold:.0f}s) | "
        f"examined={games_examined} updated={games_updated}",
        signals,
    )


def _maybe_dispatch_elo_updater_alert(
    *, status: str, detail: str, signals: dict
) -> dict | None:
    """Update the consecutive-bad counter and fire one Postmark alert
    per incident when the count crosses the threshold.

    Returns the triggered payload (used by tests) or None when this
    snapshot was healthy or below the threshold.
    """
    label = "elo_updater"
    # Only `degraded` snapshots — stalled or last-cycle-errors — count
    # toward the page. `unconfigured` (disabled via env) and `ok` reset
    # the counter so a deploy that explicitly disables the updater
    # doesn't accumulate toward an alert, and a one-off slow cycle
    # that recovers next snapshot doesn't either.
    if status != "degraded":
        _elo_updater_bad_snapshot_counts.pop(label, None)
        return None
    cur = _elo_updater_bad_snapshot_counts.get(label, 0) + 1
    _elo_updater_bad_snapshot_counts[label] = cur
    if cur != ELO_UPDATER_ALERT_THRESHOLD:
        return None

    reason = signals.get("reason") or "stalled"
    triggered = {
        "label": label,
        "status": status,
        "detail": detail,
        "consecutive_snapshots": cur,
        "reason": reason,
        "running": bool(signals.get("running")),
        "last_completed_at": signals.get("last_completed_at"),
        "age_seconds": signals.get("age_seconds"),
        "cycle_seconds": signals.get("cycle_seconds"),
        "threshold_seconds": signals.get("threshold_seconds"),
        "last_errors": list(signals.get("last_errors") or []),
        "games_examined": signals.get("games_examined"),
        "games_updated": signals.get("games_updated"),
    }

    from .notifications import alerts as _alerts

    async def _runner() -> None:
        # Dedupe per (label, reason) so a stalled-then-erroring updater
        # can fan out two distinct pages — they describe different
        # failure modes and both want operator eyes.
        dedupe_key = f"{label}:{reason}"
        try:
            ok = await _alerts.dedupe_or_skip(
                "elo_updater_unhealthy",
                dedupe_key,
                ttl_seconds=ELO_UPDATER_ALERT_TTL_SECONDS,
            )
        except Exception as e:  # pragma: no cover — fail-open
            log.warning("elo-updater dedupe failed: %s", e)
            ok = True
        if not ok:
            return
        try:
            _alerts.elo_updater_unhealthy(
                reason=reason,
                last_completed_at=triggered["last_completed_at"],
                age_seconds=triggered["age_seconds"],
                cycle_seconds=triggered["cycle_seconds"],
                threshold_seconds=triggered["threshold_seconds"],
                running=triggered["running"],
                last_errors=triggered["last_errors"],
                games_examined=triggered["games_examined"],
                games_updated=triggered["games_updated"],
                consecutive_snapshots=triggered["consecutive_snapshots"],
            )
        except Exception as e:  # pragma: no cover
            log.warning("elo-updater alert dispatch failed: %s", e)

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_runner())
    except RuntimeError:
        # Sync test context — run inline so the test can observe it.
        try:
            asyncio.run(_runner())
        except Exception as e:  # pragma: no cover
            log.warning("inline elo-updater dispatch failed: %s", e)

    return triggered


def _probe_elo_updater() -> dict:
    """Surface ``elo_scheduler.scheduler.status()`` as a passive health
    card.

    `degraded` when the loop is stopped, no cycle has completed within
    ``cycle_seconds * 3`` (capped at >=60s so a fast test cadence
    can't trigger on noise), or the most recent cycle returned
    per-game errors. `unconfigured` when the scheduler is explicitly
    disabled via env var. `ok` otherwise. The card flips the snapshot
    `summary.overall` to `degraded` and a separate Postmark alert is
    debounced via the shared notification dedupe table.
    """
    state = elo_scheduler.scheduler.status()
    status, detail, signals = _elo_updater_status(state)
    triggered = _maybe_dispatch_elo_updater_alert(
        status=status, detail=detail, signals=signals
    )
    if status == "degraded":
        error_log("elo_updater", detail[:240])
    extras = {
        **state,
        **{k: v for k, v in signals.items() if k != "last_errors"},
        "alert_threshold_snapshots": ELO_UPDATER_ALERT_THRESHOLD,
        "consecutive_bad_snapshots": _elo_updater_bad_snapshot_counts.get(
            "elo_updater", 0
        ),
        "alert_triggered_this_snapshot": triggered is not None,
    }
    return _card(
        key="elo_updater",
        label="Elo updater",
        category="passive",
        status=status,
        detail=detail,
        extras=extras,
    )


def _probe_submit_recovery() -> dict:
    """Surface the order-submit recovery path (client_order_id lookup).

    The submit POST is single-shot by design. When it returns a timeout
    or 5xx we follow up with ``GET /portfolio/orders?client_order_id=``
    to learn the venue's truth. The counters below tell operators how
    often that path fires and whether it actually recovers real orders.

    Status policy: ``ok`` when there have been no recent lookup
    failures. ``degraded`` when the last recovery attempt ended in
    ``lookup_failed`` (the *lookup itself* errored) because that means
    the next cycle is flying a bit blind.
    """
    s = kalshi_lifecycle.submit_recovery_state()
    attempts = int(s.get("attempts") or 0)
    total = int(s.get("recovered_total") or 0)
    not_found = int(s.get("not_found") or 0)
    lookup_failed = int(s.get("lookup_failed") or 0)
    last_outcome = s.get("last_outcome")
    if attempts == 0:
        status = "ok"
        detail = "no submit-POST timeouts observed"
    elif last_outcome == "lookup_failed":
        status = "degraded"
        detail = (
            f"last lookup failed: {(s.get('last_error') or '')[:160]} "
            f"(attempts={attempts}, recovered={total}, not_found={not_found})"
        )
    else:
        status = "ok"
        detail = (
            f"attempts={attempts} recovered={total} not_found={not_found} "
            f"lookup_failed={lookup_failed} last={last_outcome}"
        )
    return _card(
        key="order_submit_recovery",
        label="Order submit recovery",
        category="passive",
        status=status,
        detail=detail,
        extras={**s, "order_submit_recovered": total},
    )


def _probe_stale_resting_cancelled() -> dict:
    """Surface the cycle-start stale-resting cancel sweep counters.

    The orchestrator cancels resting orders older than
    ``AXIOM_RESTING_ORDER_MAX_AGE_SECS`` before evaluating new
    candidates (see ``app.orchestrator.cycle._cancel_stale_resting_orders``).
    ``stale_resting_cancelled`` exposes the cumulative count of
    successful cancels; ``cancel_failed`` flags flaky-venue conditions
    the cycle swallows so it can continue.
    """
    s = kalshi_lifecycle.stale_resting_cancelled_state()
    cancelled = int(s.get("cancelled") or 0)
    failed = int(s.get("cancel_failed") or 0)
    scans = int(s.get("scans") or 0)
    if failed > 0:
        status = "degraded"
        detail = (
            f"cancelled={cancelled} failed={failed} scans={scans} "
            f"last_error={(s.get('last_error') or '')[:160]}"
        )
    elif scans == 0:
        status = "ok"
        detail = "no cycle-start sweeps yet"
    else:
        status = "ok"
        detail = f"cancelled={cancelled} scans={scans}"
    return _card(
        key="stale_resting_cancelled",
        label="Stale resting cancel sweep",
        category="passive",
        status=status,
        detail=detail,
        extras={**s, "stale_resting_cancelled": cancelled},
    )


def _probe_submit_dedupe() -> dict:
    """Surface the in-process duplicate-client_order_id rejection counters.

    ``submit_order`` short-circuits a second submission re-using a
    ``client_order_id`` already resting (or in-flight on another task).
    The call never reaches the wire and a ``rejected`` lifecycle row is
    written with reason ``duplicate_client_order_id``. A non-zero count
    here is an early signal that an upstream caller is re-using ids
    instead of minting fresh ones.

    Status policy: ``ok`` when the counter is zero. ``degraded`` once
    *any* dedupe rejection has been observed — the gate firing means a
    bug somewhere upstream has tried to double-submit, and an operator
    should look at the lifecycle audit trail to find the offender.
    """
    s = kalshi_lifecycle.submit_dedupe_state()
    rejected = int(s.get("rejected") or 0)
    last_coid = s.get("last_client_order_id")
    if rejected == 0:
        status = "ok"
        detail = "no in-process duplicate-client_order_id rejections observed"
    else:
        status = "degraded"
        detail = (
            f"rejected={rejected} last_client_order_id={last_coid or '—'}"
        )
    return _card(
        key="order_submit_dedupe",
        label="Duplicate order dedupe",
        category="passive",
        status=status,
        detail=detail,
        extras={**s, "duplicate_client_order_id_rejected": rejected},
    )


def _probe_orderbook_guard() -> dict:
    """Surface the slippage-guard orderbook freshness counters.

    The lifecycle slippage guard reads the in-process Kalshi orderbook
    cache. When the WS view is missing or stale the guard now tries a
    one-shot REST fetch as a fallback, and only falls back to
    degraded-allow when both paths are unusable. This card lets
    operators see the cache-vs-fallback-vs-degraded split so WS
    coverage gaps surface as a metric — a sustained ``degraded_allow``
    rate means the slippage guard is silently disabled for a chunk of
    submissions.

    Status policy: ``degraded`` when the most recent submission went
    degraded-allow (latest signal matters most for an oncall view) or
    when the degraded-allow ratio across all observed submissions
    exceeds 25%. ``ok`` otherwise.
    """
    s = kalshi_lifecycle.orderbook_guard_state()
    checks = int(s.get("checks") or 0)
    cache = int(s.get("cache_fresh") or 0)
    fallback = int(s.get("rest_fallback") or 0)
    degraded = int(s.get("degraded_allow") or 0)
    rest_failed = int(s.get("rest_failed") or 0)
    last_source = s.get("last_source")

    if checks == 0:
        status = "ok"
        detail = "no slippage-guard checks observed yet"
    else:
        deg_pct = (degraded / checks) * 100.0
        if last_source == "degraded" or deg_pct > 25.0:
            status = "degraded"
        else:
            status = "ok"
        detail = (
            f"checks={checks} cache={cache} fallback={fallback} "
            f"degraded={degraded} rest_failed={rest_failed} "
            f"last={last_source}"
        )
    return _card(
        key="orderbook_guard_freshness",
        label="Orderbook guard freshness",
        category="passive",
        status=status,
        detail=detail,
        extras={
            **s,
            "cache_pct": (cache / checks * 100.0) if checks else None,
            "fallback_pct": (fallback / checks * 100.0) if checks else None,
            "degraded_pct": (degraded / checks * 100.0) if checks else None,
        },
    )


# Per-pair consecutive "down" snapshot counters for the crypto bar
# freshness card.  Keyed by (symbol, granularity).  When a pair stays
# `down` for ``CRYPTO_FRESHNESS_ALERT_THRESHOLD`` consecutive snapshots
# we fan a Postmark alert (deduped via the shared
# ``notification_dedupe`` table) so the on-call is paged before the
# predictor sees stale prices.
_crypto_freshness_bad_counts: dict[tuple[str, str], int] = {}


# How many consecutive snapshots a (symbol, granularity) pair must
# stay `down` before we fan an operator alert.  Default 3 — at the
# 30s health interval that's ~90s of being stuck.
CRYPTO_FRESHNESS_ALERT_THRESHOLD = max(
    1, int(os.environ.get("AXIOM_CRYPTO_FRESHNESS_ALERT_SNAPSHOTS") or "3")
)


# Dedupe TTL for the resulting Postmark alert.  We share the
# ``notification_dedupe`` table with the rest of the alert helpers; one
# alert per (symbol, granularity) per hour is plenty for a stale-feed
# signal — bumping the cadence is a deliberate ops action.
CRYPTO_FRESHNESS_ALERT_TTL_SECONDS = max(
    60, int(os.environ.get("AXIOM_CRYPTO_FRESHNESS_ALERT_TTL_SECONDS") or "3600")
)


async def _maybe_dispatch_crypto_freshness_alert(
    down_pairs: list[dict],
) -> list[dict]:
    """Update consecutive-down counters per (symbol, granularity) and
    fire alerts for any pair that just crossed the threshold this
    snapshot.  Returns the list of pair dicts that triggered an alert
    (used by tests).
    """
    seen: set[tuple[str, str]] = set()
    triggered: list[dict] = []
    for p in down_pairs:
        sym = str(p.get("symbol") or "")
        gran = str(p.get("granularity") or "")
        if not sym or not gran:
            continue
        key = (sym, gran)
        seen.add(key)
        prev = _crypto_freshness_bad_counts.get(key, 0)
        cur = prev + 1
        _crypto_freshness_bad_counts[key] = cur
        if cur == CRYPTO_FRESHNESS_ALERT_THRESHOLD:
            triggered.append({**p, "consecutive_snapshots": cur})

    # Drop counters for pairs that recovered (not in this snapshot's
    # down list) so a future relapse has to climb the threshold again.
    stale = [k for k in _crypto_freshness_bad_counts if k not in seen]
    for k in stale:
        _crypto_freshness_bad_counts.pop(k, None)

    if not triggered:
        return []

    from .notifications import alerts as _alerts

    for p in triggered:
        sym = str(p.get("symbol") or "?")
        gran = str(p.get("granularity") or "?")
        try:
            ok = await _alerts.dedupe_or_skip(
                "crypto_feed_silent",
                f"{sym}:{gran}",
                ttl_seconds=CRYPTO_FRESHNESS_ALERT_TTL_SECONDS,
            )
        except Exception as e:  # pragma: no cover — fail-open
            log.warning("crypto-freshness dedupe failed: %s", e)
            ok = True
        if not ok:
            continue
        try:
            _alerts.crypto_feed_silent(
                symbol=sym,
                granularity=gran,
                expected_seconds=int(p.get("expected_seconds") or 0),
                age_seconds=p.get("age_seconds"),
                max_ts=p.get("max_ts"),
                red_threshold_seconds=int(p.get("red_threshold_seconds") or 0),
                consecutive_snapshots=int(p["consecutive_snapshots"]),
            )
        except Exception as e:  # pragma: no cover
            log.warning("crypto-freshness alert dispatch failed: %s", e)

    return triggered


async def _probe_crypto_freshness() -> dict:
    """Per-symbol freshness check for the `crypto_ohlcv` bar table.

    The generic feed snapshot tells operators that the crypto poller
    *ran*; this card tells them whether the *bars actually landed*. We
    read MAX(ts) per (symbol, granularity) and compare each to the
    granularity's expected cadence:

      * ``ok``       — newest bar within ``max(seconds * 3, 120s)``
      * ``degraded`` — within ``max(seconds * 10, 600s)``
      * ``down``     — older than that, or no bars at all

    Card status is the worst per-pair status. Returns ``unconfigured``
    when neither crypto provider key is set (the feeds themselves are
    skipped in that case so there's nothing to check).
    """
    from .feeds import crypto as crypto_feeds  # local import to avoid cycle

    coinapi_key = os.environ.get("COINAPI_API_KEY") or os.environ.get("COINAPI_KEY")
    coingecko_key = os.environ.get("COINGECKO_API_KEY")
    if not (coinapi_key or coingecko_key):
        return _card(
            key="crypto_feed_freshness",
            label="Crypto feed freshness",
            category="passive",
            status="unconfigured",
            detail="COINAPI_API_KEY / COINGECKO_API_KEY not set",
        )

    expected: list[tuple[str, str, int]] = []
    for sym in crypto_feeds.DEFAULT_SYMBOLS:
        for gran, meta in crypto_feeds.GRANULARITIES.items():
            expected.append((sym, gran, int(meta["seconds"])))

    try:
        rows = await asyncio.wait_for(
            db.fetch(
                "SELECT symbol, granularity, MAX(ts) AS max_ts "
                "FROM crypto_ohlcv "
                "WHERE symbol = ANY($1::text[]) "
                "  AND granularity = ANY($2::text[]) "
                "GROUP BY symbol, granularity",
                list({s for s, _, _ in expected}),
                list({g for _, g, _ in expected}),
            ),
            timeout=5.0,
        )
    except Exception as e:
        error_log("crypto_feed_freshness", f"{type(e).__name__}: {e}")
        return _card(
            key="crypto_feed_freshness",
            label="Crypto feed freshness",
            category="passive",
            status="down",
            detail=f"query failed: {type(e).__name__}: {e}"[:240],
        )

    now = time.time()
    latest: dict[tuple[str, str], float] = {}
    for r in rows or []:
        ts = _coerce_ts(r["max_ts"])
        if ts is None:
            continue
        latest[(str(r["symbol"]), str(r["granularity"]))] = ts

    pair_states: list[dict] = []
    worst = "ok"
    bad_summaries: list[str] = []
    for sym, gran, seconds in expected:
        max_ts = latest.get((sym, gran))
        yellow = max(seconds * 3, 120)
        red = max(seconds * 10, 600)
        if max_ts is None:
            pair_status = "down"
            age_seconds: float | None = None
            note = "no bars"
        else:
            age_seconds = max(0.0, now - max_ts)
            if age_seconds <= yellow:
                pair_status = "ok"
            elif age_seconds <= red:
                pair_status = "degraded"
            else:
                pair_status = "down"
            note = f"{age_seconds:.0f}s old"
        pair_states.append({
            "symbol": sym,
            "granularity": gran,
            "expected_seconds": seconds,
            "max_ts": _iso(max_ts) if max_ts else None,
            "age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
            "yellow_threshold_seconds": yellow,
            "red_threshold_seconds": red,
            "status": pair_status,
        })
        if pair_status == "down":
            worst = "down"
            bad_summaries.append(f"{sym}/{gran} {note}")
        elif pair_status == "degraded" and worst != "down":
            worst = "degraded"
            bad_summaries.append(f"{sym}/{gran} {note}")

    total = len(expected)
    fresh = sum(1 for p in pair_states if p["status"] == "ok")
    if worst == "ok":
        detail = f"{fresh}/{total} (symbol, granularity) pair(s) fresh"
    else:
        detail = (
            f"{fresh}/{total} fresh | "
            + "; ".join(bad_summaries[:6])
            + ("; …" if len(bad_summaries) > 6 else "")
        )
        error_log("crypto_feed_freshness", detail[:240])

    # Page the on-call once a (symbol, granularity) pair has been
    # `down` for ``CRYPTO_FRESHNESS_ALERT_THRESHOLD`` consecutive
    # snapshots.  Dispatch lives here (rather than the snapshot
    # assembly loop) so the per-pair counter resets the moment a pair
    # recovers, mirroring the WS-silent alert path.
    down_pairs = [p for p in pair_states if p["status"] == "down"]
    await _maybe_dispatch_crypto_freshness_alert(down_pairs)

    return _card(
        key="crypto_feed_freshness",
        label="Crypto feed freshness",
        category="passive",
        status=worst,
        detail=detail,
        extras={
            "pairs": pair_states,
            "fresh_count": fresh,
            "total_count": total,
            "stale_alert_threshold": CRYPTO_FRESHNESS_ALERT_THRESHOLD,
        },
    )


def _probe_kill_switch() -> dict:
    state = kalshi_lifecycle.kill_switch_state()
    halted = bool(state.get("halted"))
    settings = get_settings()
    return _card(
        key="trading_mode",
        label="Trading mode / kill-switch",
        category="passive",
        status=("degraded" if halted else "ok"),
        detail=(
            f"HALTED ({state.get('reason') or 'no reason given'})"
            if halted
            else f"mode={settings.trading_mode}"
        ),
        extras={**state, "trading_mode": settings.trading_mode},
    )


def _coerce_ts(v: Any) -> float | None:
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except Exception:
            return None
    if isinstance(v, datetime):
        return v.timestamp()
    return None


def _probe_orchestrator() -> dict:
    s = orchestrator_scheduler.status()
    running = bool(s.get("running"))
    # `last_cycle` is a per-vertical map: {vertical: {completed_at, ...}}.
    # Pick the most-recent completed_at across all verticals.
    last_cycle_ts: float | None = None
    last_cycle_iso: str | None = None
    last_vertical: str | None = None
    last = s.get("last_cycle") or {}
    if isinstance(last, dict):
        for vertical, entry in last.items():
            if not isinstance(entry, dict):
                continue
            ts = _coerce_ts(entry.get("completed_at"))
            if ts is None:
                continue
            if last_cycle_ts is None or ts > last_cycle_ts:
                last_cycle_ts = ts
                last_vertical = vertical
                ca = entry.get("completed_at")
                last_cycle_iso = (
                    ca if isinstance(ca, str)
                    else datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                )
    age = (time.time() - last_cycle_ts) if last_cycle_ts else None
    settings = get_settings()
    enabled = bool(s.get("enabled"))
    if not enabled:
        status = "unconfigured"
        detail = "scheduler disabled"
    elif not running:
        status = "down"
        detail = "scheduler not running"
    elif age is None:
        status = "degraded"
        detail = (
            f"running but no cycles completed yet "
            f"(cadence={settings.cycle_interval_seconds}s)"
        )
    elif age > 5 * settings.cycle_interval_seconds:
        status = "degraded"
        detail = (
            f"last cycle {last_cycle_iso} ({round(age,1)}s ago) "
            f"on {last_vertical} — cadence={settings.cycle_interval_seconds}s"
        )
    else:
        status = "ok"
        detail = (
            f"last cycle {last_cycle_iso} ({round(age,1)}s ago) "
            f"on {last_vertical} — cadence={settings.cycle_interval_seconds}s"
        )
    return _card(
        key="orchestrator",
        label="Trading orchestrator",
        category="passive",
        status=status,
        detail=detail,
        extras=s,
    )


def _probe_settlement() -> dict:
    s = settlement_scheduler.status()
    enabled = bool(s.get("enabled"))
    running = bool(s.get("running"))
    if not enabled:
        status = "unconfigured"
        detail = "settlement disabled"
    elif not running:
        status = "down"
        detail = "settlement loop not running"
    else:
        status = "ok"
        detail = f"interval={s.get('interval_seconds')}s"
    return _card(
        key="settlement",
        label="Settlement loop",
        category="passive",
        status=status,
        detail=detail,
        extras=s,
    )


def _probe_models() -> dict:
    state = models_loader.get_state()
    mcheck = state.manifest_check or {}
    manifest_ok = bool(mcheck.get("ok")) if mcheck else False
    if state.error:
        status = "down"
        detail = f"error: {state.error}"[:240]
    elif not state.ready:
        if state.in_progress:
            status = "degraded"
            detail = "loading…"
        elif not state.predictors:
            # No predictors loaded AND no error reported — either the
            # loader hasn't been kicked off yet, or it ran but the
            # manifest is unavailable (T007 deferred). Treat as
            # "unconfigured" rather than "down" so the trading banner
            # doesn't fire while artifacts are still pending. Trading
            # code paths that actually need predictors check
            # `state.ready` themselves and degrade gracefully.
            reason = (mcheck.get("reason") if mcheck else None)
            if reason == "manifest_unavailable":
                detail = (
                    "manifest not published yet — upload models_manifest.json "
                    "to the Supabase bucket and call /api/models/reload (T007)"
                )
            else:
                detail = (
                    "no model artifacts deployed — set the Supabase model "
                    "bucket path and call /api/models/reload (T007)"
                )
            status = "unconfigured"
        else:
            status = "down"
            detail = "not loaded"
    elif mcheck and not manifest_ok:
        status = "down"
        bad = [m["name"] for m in mcheck.get("missing", [])] + [
            m["name"] for m in mcheck.get("mismatch", [])
        ]
        detail = f"manifest mismatch: {len(bad)} bad file(s)"
    else:
        ok_loaded = sum(1 for v in state.loaded.values() if v.get("ok"))
        status = "ok" if ok_loaded > 0 else "degraded"
        detail = f"{ok_loaded} predictor(s) loaded"
    return _card(
        key="models",
        label="Model artifacts",
        category="passive",
        status=status,
        detail=detail,
        extras={
            "ready": state.ready,
            "in_progress": state.in_progress,
            "predictor_count": len(state.predictors),
            "manifest_ok": manifest_ok,
            "manifest_missing": len(mcheck.get("missing", [])),
            "manifest_mismatch": len(mcheck.get("mismatch", [])),
            # Surface signature/reason/error so the operator dashboard
            # can render a verification badge ("Verified" / "Not
            # enforced" / "Invalid") without re-deriving from raw JSON.
            "manifest_signature": mcheck.get("signature"),
            "manifest_reason": mcheck.get("reason"),
            "manifest_error": mcheck.get("error"),
        },
    )


def _probe_calibration() -> dict:
    """Per-league probability calibrator freshness.

    Status mapping:
      * ``ok``        — every supported league has a fresh artifact
      * ``degraded``  — some leagues are missing OR any loaded
                        artifact is older than ``FRESH_TTL_SECONDS``
      * ``down``      — no leagues have a loaded calibrator at all
    """
    snap = calibrators.get_state().freshness()
    loaded = int(snap["loaded_count"])
    stale = int(snap["stale_count"])
    missing = int(snap["missing_count"])
    last_refit = snap.get("last_refit_at")
    if loaded == 0 and not last_refit:
        # Fresh deploy: calibrator artifacts have never been fit. This is
        # not a failure — surface as "unconfigured" so the trading banner
        # doesn't fire while the operator runs the initial /api/calibrators
        # /refit-all (matches the T007/models semantics).
        status = "unconfigured"
        detail = (
            "no calibrators fit yet — run /api/calibrators/refit-all "
            "to seed per-league probability calibrators"
        )
    elif loaded == 0:
        status = "down"
        detail = "no per-league calibrators loaded"
    elif missing > 0 or stale > 0:
        status = "degraded"
        bits = []
        if missing:
            bits.append(f"{missing} league(s) missing")
        if stale:
            bits.append(f"{stale} stale")
        detail = "; ".join(bits)
    else:
        status = "ok"
        detail = f"{loaded}/{len(snap['supported'])} leagues fresh"
    last_refit = snap.get("last_refit_at")
    if last_refit:
        detail = (
            f"{detail} | last refit "
            f"{datetime.fromtimestamp(float(last_refit), tz=timezone.utc).isoformat()}"
        )
    return _card(
        key="calibrators",
        label="Calibration freshness",
        category="passive",
        status=status,
        detail=detail,
        extras=snap,
    )


# Per-subscriber consecutive "bad snapshot" counters.  Keyed by the
# Subscription's stable in-process id (monotonic across the lifetime of
# the connection).  When a sub disappears from the registry its entry
# is dropped so the dict can't grow without bound.
_ws_bad_snapshot_counts: dict[int, int] = {}

# Per-hub consecutive "bad snapshot" counters.  Keyed by hub name.
# Distinct from ``_ws_bad_snapshot_counts`` because subscribers reconnect
# with fresh ids — a misconfigured ceiling on a bursty hub (kalshi /
# polymarket orderbooks) sees overflow churn that the per-id counter
# can never accumulate.  The hub-level counter survives reconnect.
_ws_hub_bad_snapshot_counts: dict[str, int] = {}

# How many consecutive snapshots a subscriber must stay >=90% full /
# overflowed before we fan out an operator alert.  Default 3 — at the
# 30s health interval that's ~90s of being stuck.
WS_SILENT_ALERT_THRESHOLD = max(
    1, int(os.environ.get("AXIOM_WS_SILENT_ALERT_SNAPSHOTS") or "3")
)

# Dedupe TTL for the resulting Postmark alert.  We share the
# ``notification_dedupe`` table with the rest of the alert helpers; one
# alert per (hub, label) per hour is plenty for a stuck-client signal.
WS_SILENT_ALERT_TTL_SECONDS = max(
    60, int(os.environ.get("AXIOM_WS_SILENT_ALERT_TTL_SECONDS") or "3600")
)

# Per-hub overflow alert: how many consecutive snapshots a hub must
# show overflowed-or-near-full subscribers before paging.  Default 3
# matches the per-subscriber alert so both signals are observable on
# the same ~90s timescale.
WS_HUB_OVERFLOW_ALERT_THRESHOLD = max(
    1, int(os.environ.get("AXIOM_WS_HUB_OVERFLOW_ALERT_SNAPSHOTS") or "3")
)

# Per-hub overflow alert dedupe TTL — one alert per hub per hour by
# default; bumping the ceiling is a deliberate ops action so we don't
# need to re-page the on-call sooner.
WS_HUB_OVERFLOW_ALERT_TTL_SECONDS = max(
    60, int(os.environ.get("AXIOM_WS_HUB_OVERFLOW_ALERT_TTL_SECONDS") or "3600")
)


def _is_ws_subscriber_bad(sub: dict) -> bool:
    if sub.get("overflowed"):
        return True
    maxsize = sub.get("maxsize")
    qsize = sub.get("qsize") or 0
    return bool(maxsize) and qsize >= 0.9 * maxsize


def _maybe_dispatch_ws_silent_alert(bad_subs: list[dict]) -> list[dict]:
    """Update consecutive-bad counters and fire alerts for any sub that
    just crossed the threshold this snapshot.  Returns the list of
    subscriber dicts that triggered an alert (used by tests).
    """
    seen_ids: set[int] = set()
    triggered: list[dict] = []
    for sub in bad_subs:
        sid = int(sub.get("id") or 0)
        seen_ids.add(sid)
        prev = _ws_bad_snapshot_counts.get(sid, 0)
        cur = prev + 1
        _ws_bad_snapshot_counts[sid] = cur
        if cur == WS_SILENT_ALERT_THRESHOLD:
            triggered.append({**sub, "consecutive_snapshots": cur})

    # Drop counters for subs no longer present (or no longer bad).
    stale = [k for k in _ws_bad_snapshot_counts if k not in seen_ids]
    for k in stale:
        _ws_bad_snapshot_counts.pop(k, None)

    if not triggered:
        return []

    from .notifications import alerts as _alerts

    async def _runner() -> None:
        for s in triggered:
            hub = str(s.get("hub") or "?")
            label = str(s.get("label") or "?")
            try:
                ok = await _alerts.dedupe_or_skip(
                    "ws_subscriber_silent",
                    f"{hub}:{label}",
                    ttl_seconds=WS_SILENT_ALERT_TTL_SECONDS,
                )
            except Exception as e:  # pragma: no cover — fail-open
                log.warning("ws-silent dedupe failed: %s", e)
                ok = True
            if not ok:
                continue
            try:
                _alerts.ws_subscriber_silent(
                    hub=hub,
                    label=label,
                    qsize=int(s.get("qsize") or 0),
                    maxsize=s.get("maxsize"),
                    dropped=int(s.get("dropped") or 0),
                    overflowed=bool(s.get("overflowed")),
                    consecutive_snapshots=int(s["consecutive_snapshots"]),
                    age_seconds=s.get("age_seconds"),
                )
            except Exception as e:  # pragma: no cover
                log.warning("ws-silent alert dispatch failed: %s", e)

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_runner())
    except RuntimeError:
        # No running loop (sync test context) — run inline so the test
        # can observe the dispatch.
        try:
            asyncio.run(_runner())
        except Exception as e:  # pragma: no cover
            log.warning("inline ws-silent dispatch failed: %s", e)

    return triggered


def _compute_bad_hubs(
    subs: list[dict], queue_config: dict
) -> list[dict]:
    """Aggregate per-hub overflow / near-full stats from the subscriber
    snapshot.  Returns one entry per hub that has at least one
    overflowed subscriber or at least one subscriber >=90% full.

    Each entry carries the resolved maxsize and override env name (so
    the alert can tell the on-call what to bump) plus the counters the
    alert body renders.
    """
    by_hub_cfg = (queue_config or {}).get("by_hub") or {}
    bad: dict[str, dict] = {}
    for sub in subs:
        if not _is_ws_subscriber_bad(sub):
            continue
        hub = str(sub.get("hub") or "?")
        entry = bad.setdefault(
            hub,
            {
                "hub": hub,
                "overflowed": 0,
                "near_full": 0,
                "max_qsize": 0,
            },
        )
        if sub.get("overflowed"):
            entry["overflowed"] += 1
        else:
            entry["near_full"] += 1
        entry["max_qsize"] = max(entry["max_qsize"], int(sub.get("qsize") or 0))

    # Annotate with totals + resolved ceiling so the alert payload is
    # self-contained (the dispatcher doesn't need queue_config again).
    totals_by_hub: dict[str, int] = {}
    for sub in subs:
        h = str(sub.get("hub") or "?")
        totals_by_hub[h] = totals_by_hub.get(h, 0) + 1
    for hub, entry in bad.items():
        cfg = by_hub_cfg.get(hub) or {}
        entry["total_subscribers"] = totals_by_hub.get(hub, entry["overflowed"] + entry["near_full"])
        entry["maxsize"] = cfg.get("maxsize")
        entry["env_var"] = cfg.get("env")
    return list(bad.values())


def _maybe_dispatch_ws_hub_overflow_alert(
    bad_hubs: list[dict],
) -> list[dict]:
    """Update per-hub consecutive-bad counters and fire alerts for any
    hub that just crossed the threshold this snapshot.  Returns the
    list of hub stat dicts that triggered an alert (used by tests).
    """
    seen: set[str] = set()
    triggered: list[dict] = []
    for entry in bad_hubs:
        hub = str(entry.get("hub") or "?")
        seen.add(hub)
        prev = _ws_hub_bad_snapshot_counts.get(hub, 0)
        cur = prev + 1
        _ws_hub_bad_snapshot_counts[hub] = cur
        if cur == WS_HUB_OVERFLOW_ALERT_THRESHOLD:
            triggered.append({**entry, "consecutive_snapshots": cur})

    # Drop counters for hubs that recovered (not in the bad list this
    # snapshot) so a future spike has to climb the threshold again.
    stale = [k for k in _ws_hub_bad_snapshot_counts if k not in seen]
    for k in stale:
        _ws_hub_bad_snapshot_counts.pop(k, None)

    if not triggered:
        return []

    from .notifications import alerts as _alerts

    async def _runner() -> None:
        for entry in triggered:
            hub = str(entry.get("hub") or "?")
            try:
                ok = await _alerts.dedupe_or_skip(
                    "ws_hub_overflowing",
                    hub,
                    ttl_seconds=WS_HUB_OVERFLOW_ALERT_TTL_SECONDS,
                )
            except Exception as e:  # pragma: no cover — fail-open
                log.warning("ws-hub-overflow dedupe failed: %s", e)
                ok = True
            if not ok:
                continue
            try:
                _alerts.ws_hub_overflowing(
                    hub=hub,
                    maxsize=int(entry.get("maxsize") or 0),
                    env_var=str(entry.get("env_var") or "AXIOM_WS_QUEUE_MAXSIZE"),
                    overflowed_subscribers=int(entry.get("overflowed") or 0),
                    near_full_subscribers=int(entry.get("near_full") or 0),
                    total_subscribers=int(entry.get("total_subscribers") or 0),
                    max_qsize=int(entry.get("max_qsize") or 0),
                    consecutive_snapshots=int(entry["consecutive_snapshots"]),
                )
            except Exception as e:  # pragma: no cover
                log.warning("ws-hub-overflow alert dispatch failed: %s", e)

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_runner())
    except RuntimeError:
        # No running loop (sync test context) — run inline so the test
        # can observe the dispatch.
        try:
            asyncio.run(_runner())
        except Exception as e:  # pragma: no cover
            log.warning("inline ws-hub-overflow dispatch failed: %s", e)

    return triggered


def _probe_ws_subscribers() -> dict:
    """WebSocket subscriber backpressure card.

    Surfaces per-connection queue depth + drop counts for every active
    subscriber across the in-process fan-out hubs (pipeline,
    settlement, kalshi, polymarket, llm-logs).  A subscriber pinned at
    full depth or with non-zero drops is the smoking gun for a stuck
    client.

    When a subscriber stays bad (>=90% full or overflowed) for
    ``WS_SILENT_ALERT_THRESHOLD`` consecutive snapshots, we fan an
    alert into the operator Postmark pipeline (deduped via the shared
    ``notification_dedupe`` table).
    """
    from .ws_backpressure import queue_config_snapshot, registry_summary

    snap = registry_summary()
    # Surface the resolved per-hub queue ceiling (set by
    # AXIOM_WS_QUEUE_MAXSIZE / AXIOM_WS_QUEUE_MAXSIZE_<HUB>) so operators
    # can confirm an env-var override actually took effect.
    queue_config = queue_config_snapshot()
    snap["queue_config"] = queue_config
    subs = snap["subscribers"]
    by_hub = snap["by_hub"]
    total = snap["total_subscribers"]
    overflowed = sum(1 for s in subs if s.get("overflowed"))
    near_full = [
        s for s in subs
        if s.get("maxsize") and s["qsize"] >= 0.9 * s["maxsize"]
    ]
    bad_subs = [s for s in subs if _is_ws_subscriber_bad(s)]
    _maybe_dispatch_ws_silent_alert(bad_subs)
    # Surface the per-subscriber consecutive-bad counts on the snapshot
    # so the operator dashboard banner can render the same "stuck"
    # signal that drives the Postmark alert. ``stuck_subscribers`` only
    # contains subs that have already crossed the alert threshold so
    # the banner mirrors the email path 1:1.
    snap["silent_alert_threshold"] = WS_SILENT_ALERT_THRESHOLD
    stuck: list[dict] = []
    for s in bad_subs:
        sid = int(s.get("id") or 0)
        cnt = _ws_bad_snapshot_counts.get(sid, 0)
        if cnt >= WS_SILENT_ALERT_THRESHOLD:
            stuck.append({**s, "consecutive_snapshots": cnt})
    snap["stuck_subscribers"] = stuck
    # Hub-level alert: catches misconfigured ceilings on bursty hubs
    # where short-lived subscribers reconnect faster than the per-id
    # counter can accumulate.  Run *after* the per-subscriber dispatch
    # and ``stuck_subscribers`` accounting so the snapshot stays
    # consistent: the banner reflects the per-id signal, the per-hub
    # alert handles the reconnect-churn case the banner can't catch.
    bad_hubs = _compute_bad_hubs(subs, queue_config)
    _maybe_dispatch_ws_hub_overflow_alert(bad_hubs)
    if overflowed:
        status = "degraded"
        detail = f"{overflowed} subscriber(s) closed on overflow"
    elif near_full:
        status = "degraded"
        detail = (
            f"{len(near_full)} subscriber(s) >=90% full "
            f"(largest={max(s['qsize'] for s in near_full)})"
        )
    else:
        status = "ok"
        detail = f"{total} active subscriber(s) across {len(by_hub)} hub(s)"
    return _card(
        key="ws_backpressure",
        label="WebSocket subscribers",
        category="passive",
        status=status,
        detail=detail,
        extras=snap,
    )


def _probe_feeds() -> list[dict]:
    out: list[dict] = []
    for s in feed_registry.snapshot():
        polls = int(s.get("poll_count") or 0)
        fails = int(s.get("failure_count") or 0)
        # Bound to 4dp so the UI can render `xx.yy%` without further math.
        error_rate = round(fails / polls, 4) if polls > 0 else 0.0
        last_pull = s.get("last_finished_at") or s.get("last_ok_at")

        if not s.get("enabled"):
            status = "unconfigured"
            detail = s.get("skipped_reason") or "disabled"
        else:
            last_ok = s.get("last_ok_at")
            last_err = s.get("last_error_at")
            # If last error is more recent than last success -> degraded.
            if last_err and (not last_ok or last_err > last_ok):
                status = "degraded"
                detail = (
                    f"last pull {last_pull or 'n/a'} | err {error_rate * 100:.1f}% "
                    f"({fails}/{polls}) | "
                    + (s.get("last_error") or "recent failure")[:160]
                )
                error_log(f"feed:{s['name']}", str(s.get("last_error") or "")[:240])
            elif not last_ok:
                status = "degraded"
                detail = "no successful poll yet"
            else:
                status = "ok"
                detail = (
                    f"last pull {last_pull} | err {error_rate * 100:.1f}% "
                    f"({fails}/{polls}) | written={s.get('last_records_written')}"
                )

        # Promote last_pull + error_rate into the card extras so the
        # Health UI can render them as first-class metrics without
        # parsing the detail string.
        extras = dict(s)
        extras["last_pull"] = last_pull
        extras["error_rate"] = error_rate
        out.append(
            _card(
                key=f"feed:{s['name']}",
                label=f"Feed :: {s['name']}",
                category="passive",
                status=status,
                detail=detail,
                extras=extras,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Config probes — presence-only; cheap; safe to call every interval.
# ---------------------------------------------------------------------------


def _config_card(
    key: str,
    label: str,
    *,
    configured: bool,
    extras: dict | None = None,
    detail_ok: str = "credentials present",
    detail_missing: str = "not configured",
) -> dict:
    return _card(
        key=key,
        label=label,
        category="config",
        status="ok" if configured else "unconfigured",
        detail=detail_ok if configured else detail_missing,
        extras=extras or {},
    )


def _probe_config() -> list[dict]:
    settings = get_settings()
    cards = [
        _config_card(
            "openai", "OpenAI", configured=bool(settings.openai_api_key)
        ),
        _config_card(
            "anthropic", "Anthropic", configured=bool(settings.anthropic_api_key)
        ),
        _config_card(
            "stripe",
            "Stripe",
            configured=bool(os.environ.get("STRIPE_SECRET_KEY")),
            extras={"webhook_configured": bool(os.environ.get("STRIPE_WEBHOOK_SECRET"))},
        ),
        _config_card(
            "revenuecat",
            "RevenueCat",
            configured=bool(
                os.environ.get("REVENUECAT_SECRET_API_KEY")
                or os.environ.get("REVENUECAT_PUBLIC_API_KEY")
            ),
            detail_missing="not configured (mobile app pending)",
        ),
        _config_card(
            "postmark",
            "Postmark",
            configured=postmark.is_configured(),
            extras=postmark.status(),
            detail_ok=(
                f"recipients: "
                + (", ".join(postmark.alert_recipients()) or "(none)")
            ),
        ),
        _config_card(
            "sportradar",
            "Sportradar",
            configured=bool(os.environ.get("SPORTRADAR_API_KEY")),
            detail_missing="not configured (Odds API used as primary)",
        ),
        _config_card(
            "unusual_whales",
            "Unusual Whales",
            configured=bool(
                os.environ.get("UNUSUAL_WHALES_API_KEY")
                or os.environ.get("UW_API_TOKEN")
            ),
            detail_missing="not configured (no flow / dark-pool ingest)",
        ),
        _config_card(
            "bettingpros",
            "BettingPros (public-betting)",
            # BettingPros polls anonymously when no key is set; presence
            # of a key just unlocks higher rate limits.
            configured=True,
            extras={
                "api_key_present": bool(
                    os.environ.get("BETTINGPROS_API_KEY")
                    or os.environ.get("AXIOM_BETTINGPROS_API_KEY")
                ),
            },
            detail_ok=(
                "anonymous polling enabled"
                if not (
                    os.environ.get("BETTINGPROS_API_KEY")
                    or os.environ.get("AXIOM_BETTINGPROS_API_KEY")
                )
                else "api key present"
            ),
        ),
        _config_card(
            "sportsline",
            "SportsLine (public-betting fallback)",
            configured=bool(
                os.environ.get("SPORTSLINE_API_KEY")
                or os.environ.get("AXIOM_SPORTSLINE_API_KEY")
            ),
            detail_missing="not configured (BettingPros primary)",
        ),
    ]
    return cards


# ---------------------------------------------------------------------------
# Snapshot assembly
# ---------------------------------------------------------------------------


async def _build_snapshot() -> _Snapshot:
    t0 = time.time()
    live = await asyncio.gather(
        _probe_postgres(),
        _probe_kalshi(),
        _probe_storage(),
        _probe_crypto_freshness(),
        return_exceptions=False,
    )
    passive = [
        _probe_kalshi_ws(),
        _probe_kalshi_cluster(),
        _probe_kill_switch(),
        _probe_orchestrator(),
        _probe_settlement(),
        _probe_models(),
        _probe_calibration(),
        _probe_ws_subscribers(),
        _probe_kalshi_reconciler(),
        _probe_elo_updater(),
        _probe_bankroll_sync(),
        _probe_submit_recovery(),
        _probe_stale_resting_cancelled(),
        _probe_submit_dedupe(),
        _probe_orderbook_guard(),
        *_probe_feeds(),
    ]
    config = _probe_config()
    cards = list(live) + passive + config
    # Per-client retry / failure counters (populated by every outbound
    # REST call routed through ``app.http_retry``).  Lazy-imported to
    # keep this module's import graph free of cycles.
    try:
        from .http_retry import snapshot_counters as _retry_snapshot
        retry_counters = _retry_snapshot()
    except Exception:  # noqa: BLE001 — counters are best-effort observability
        retry_counters = {}
    try:
        from .swarm.runner import gatekeeper_failure_counters
        gatekeeper_failures = gatekeeper_failure_counters()
    except Exception:  # noqa: BLE001
        gatekeeper_failures = {}

    summary = {
        "ok": sum(1 for c in cards if c["status"] == "ok"),
        "degraded": sum(1 for c in cards if c["status"] == "degraded"),
        "down": sum(1 for c in cards if c["status"] == "down"),
        "unconfigured": sum(1 for c in cards if c["status"] == "unconfigured"),
        "errors_24h": errors_in_last_24h(),
        "retry_counters": retry_counters,
        "gatekeeper_failures": gatekeeper_failures,
    }
    if summary["down"] > 0:
        summary["overall"] = "down"
    elif summary["degraded"] > 0:
        summary["overall"] = "degraded"
    else:
        summary["overall"] = "ok"
    return _Snapshot(
        generated_at=time.time(),
        duration_ms=round((time.time() - t0) * 1000, 1),
        cards=cards,
        summary=summary,
    )


# ---------------------------------------------------------------------------
# Background loop
# ---------------------------------------------------------------------------


_task: asyncio.Task | None = None
_stop = asyncio.Event()


async def _loop() -> None:
    global _snapshot
    while not _stop.is_set():
        try:
            snap = await _build_snapshot()
            async with _lock:
                _snapshot = snap
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("health snapshot build failed: %s", e)
            error_log("health.collector", f"{type(e).__name__}: {e}")
        try:
            await asyncio.wait_for(_stop.wait(), timeout=HEALTH_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
        else:
            return


async def start() -> None:
    global _task, _started_at
    if _task and not _task.done():
        return
    _stop.clear()
    _started_at = time.time()
    # Kick a synchronous build so /api/health/snapshot has data on the
    # very first request after boot.
    try:
        snap = await _build_snapshot()
        async with _lock:
            globals()["_snapshot"] = snap
    except Exception as e:
        log.warning("initial health snapshot failed: %s", e)
    _task = asyncio.create_task(_loop(), name="health-collector")
    log.info("health collector started (interval=%ss)", HEALTH_INTERVAL_SECONDS)


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
