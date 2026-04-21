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

from . import db, storage
from .config import get_settings
from .feeds import registry as feed_registry
from .kalshi import bus as kalshi_bus, client as kalshi_client_mod, lifecycle as kalshi_lifecycle, ws as kalshi_ws
from .notifications import postmark
from . import models_loader
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
    if state.error:
        status = "down"
        detail = f"error: {state.error}"[:240]
    elif not state.ready:
        status = "degraded" if state.in_progress else "down"
        detail = "loading…" if state.in_progress else "not loaded"
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
        },
    )


def _probe_feeds() -> list[dict]:
    out: list[dict] = []
    for s in feed_registry.snapshot():
        if not s.get("enabled"):
            status = "unconfigured"
            detail = s.get("skipped_reason") or "disabled"
        else:
            last_ok = s.get("last_ok_at")
            last_err = s.get("last_error_at")
            # If last error is more recent than last success -> degraded.
            if last_err and (not last_ok or last_err > last_ok):
                status = "degraded"
                detail = (s.get("last_error") or "recent failure")[:240]
                error_log(f"feed:{s['name']}", str(s.get("last_error") or "")[:240])
            elif not last_ok:
                status = "degraded"
                detail = "no successful poll yet"
            else:
                status = "ok"
                detail = f"last ok {last_ok}; written={s.get('last_records_written')}"
        out.append(
            _card(
                key=f"feed:{s['name']}",
                label=f"Feed :: {s['name']}",
                category="passive",
                status=status,
                detail=detail,
                extras=s,
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
        return_exceptions=False,
    )
    passive = [
        _probe_kalshi_ws(),
        _probe_kill_switch(),
        _probe_orchestrator(),
        _probe_settlement(),
        _probe_models(),
        *_probe_feeds(),
    ]
    config = _probe_config()
    cards = list(live) + passive + config
    summary = {
        "ok": sum(1 for c in cards if c["status"] == "ok"),
        "degraded": sum(1 for c in cards if c["status"] == "degraded"),
        "down": sum(1 for c in cards if c["status"] == "down"),
        "unconfigured": sum(1 for c in cards if c["status"] == "unconfigured"),
        "errors_24h": errors_in_last_24h(),
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
