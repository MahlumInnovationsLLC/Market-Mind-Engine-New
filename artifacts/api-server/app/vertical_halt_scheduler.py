"""Per-vertical halt scheduler + TTL sweeper — task #713.

A single ``asyncio.Task`` ticks every ``AXIOM_VERTICAL_HALT_TICK_SECS``
seconds (default 30) and performs two passes:

1. **TTL sweep** — every halted vertical with an ``expires_at`` field
   in its JSON blob whose deadline has passed is auto-resumed via
   ``runtime_settings.clear_vertical_halt`` (with
   ``source='ttl_expiry'`` / ``event='ttl_expired'``).  An operator
   alert (``vertical_halt_ttl_expired``) is dispatched for each.

2. **Schedule eval** — every enabled row in
   ``vertical_halt_schedules`` whose 5-field cron expression matches
   the current minute (and which hasn't already fired in the last
   ~90s) is applied via ``runtime_settings.set_vertical_halted`` with
   the rule's ``duration_secs`` TTL, ``source='scheduled_rule'``, and
   ``schedule_id`` so a future TTL expiry can attribute the resume
   back to the right rule.  Every fire stamps ``last_fired_at`` so the
   sub-minute tick doesn't re-fire the same rule, and emits the
   ``vertical_halt_scheduled_fired`` operator alert.

Per-iteration failures are caught and logged so a single bad row
never stalls the loop.  The whole task is started from the API
lifespan and stopped at shutdown.

The cancel-resting-orders sweep that the manual halt endpoint runs
is intentionally *not* re-applied here — when the orchestrator
processes its next cycle the halt will already be in effect, and a
scheduled / TTL halt is by definition expected so we don't want a
mass-cancel storm bypassing the operator's ack.  This matches the
"existing manual halt path is preserved exactly" contract from the
task spec.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any

from . import runtime_settings as runtime_settings_mod
from .notifications import alerts as alerts_mod


log = logging.getLogger("axiom.vertical_halt_scheduler")


_DEFAULT_INTERVAL_SECS = 30


def _interval_secs() -> int:
    raw = os.getenv("AXIOM_VERTICAL_HALT_TICK_SECS")
    if not raw:
        return _DEFAULT_INTERVAL_SECS
    try:
        v = int(raw)
        return max(5, v)
    except ValueError:
        return _DEFAULT_INTERVAL_SECS


def _enabled() -> bool:
    raw = (os.getenv("AXIOM_VERTICAL_HALT_SCHEDULER_ENABLED") or "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


_task: asyncio.Task | None = None
_stop = asyncio.Event()


async def sweep_ttls_once(now: datetime | None = None) -> list[dict[str, Any]]:
    """Auto-resume every halt whose ``expires_at`` has passed.

    Returns a list of summaries (one per resumed vertical) for tests
    + structured logging.  Each resume:

      * deletes the ``system_settings`` row via ``clear_vertical_halt``
        (which writes the ``vertical_halt_history`` row + admin audit),
      * dispatches the ``vertical_halt_ttl_expired`` operator alert,
      * is wrapped in try/except so a single failure doesn't block
        the rest of the sweep.
    """
    out: list[dict[str, Any]] = []
    try:
        expired = await runtime_settings_mod.list_expired_vertical_halts(now=now)
    except Exception as e:
        log.warning("ttl sweeper: list_expired failed: %s", e)
        return out
    for vertical, meta in expired:
        try:
            prev_meta, changed = await runtime_settings_mod.clear_vertical_halt(
                vertical,
                actor_user_id="system:ttl_sweeper",
                actor_email=None,
                reason="ttl expired",
                source="ttl_expiry",
                event="ttl_expired",
            )
        except Exception as e:
            log.warning("ttl sweeper: clear_vertical_halt(%s) failed: %s", vertical, e)
            continue
        if not changed:
            continue
        try:
            schedule_id_raw = meta.get("schedule_id") if isinstance(meta, dict) else None
            schedule_id_int: int | None = None
            if isinstance(schedule_id_raw, (int, float)):
                schedule_id_int = int(schedule_id_raw)
            alerts_mod.vertical_halt_ttl_expired(
                vertical=vertical,
                halted_at=meta.get("at") if isinstance(meta, dict) else None,
                expires_at=meta.get("expires_at") if isinstance(meta, dict) else None,
                reason=meta.get("reason") if isinstance(meta, dict) else None,
                source=meta.get("source") if isinstance(meta, dict) else "manual_admin",
                schedule_id=schedule_id_int,
            )
        except Exception as e:
            log.warning("ttl sweeper alert dispatch (%s) failed: %s", vertical, e)
        out.append({"vertical": vertical, "previous_meta": prev_meta})
        log.info("vertical halt TTL expired: %s", vertical)
    return out


async def fire_due_schedules_once(now: datetime | None = None) -> list[dict[str, Any]]:
    """Apply every enabled schedule rule whose cron window is open.

    Idempotent within a minute because:

      * ``list_due_vertical_halt_schedules`` skips rules whose
        ``last_fired_at`` is already in the current minute,
      * ``mark_schedule_fired`` is an atomic conditional UPDATE that
        only succeeds when ``last_fired_at`` is still strictly less
        than the current minute boundary — that conditional is what
        prevents two concurrent api-server replicas from both
        applying the same scheduled halt.  We **claim first, halt
        second** so a lost race never leaves a stray halt in place.

    Returns a list of fire summaries — one row per attempted apply.
    """
    out: list[dict[str, Any]] = []
    fire_now = now or datetime.now(timezone.utc).replace(microsecond=0)
    try:
        due = await runtime_settings_mod.list_due_vertical_halt_schedules(now=fire_now)
    except Exception as e:
        log.warning("schedule firer: list_due failed: %s", e)
        return out
    for rule in due:
        rule_id = rule.get("id")
        vertical = rule.get("vertical") or ""
        cron_expr = rule.get("cron_expr") or ""
        duration = int(rule.get("duration_secs") or 0)
        reason = rule.get("reason") or "(scheduled)"
        if not vertical or duration <= 0:
            log.warning("schedule %s missing vertical/duration; skipping", rule_id)
            continue
        # Claim the fire atomically BEFORE halting, so a concurrent
        # tick (same process) or replica (other process) cannot both
        # halt the vertical for the same rule in the same minute.
        try:
            claimed = await runtime_settings_mod.mark_schedule_fired(
                int(rule_id), fired_at=fire_now,
            )
        except Exception as e:
            log.warning("schedule firer: claim(%s) failed: %s", rule_id, e)
            continue
        if not claimed:
            log.info(
                "schedule %s for %s skipped — already claimed for this minute",
                rule_id, vertical,
            )
            continue
        try:
            meta, changed = await runtime_settings_mod.set_vertical_halted(
                vertical,
                actor_user_id="system:scheduled_rule",
                actor_email=rule.get("created_by_email"),
                reason=f"scheduled: {reason}",
                ttl_secs=duration,
                source="scheduled_rule",
                schedule_id=int(rule_id) if rule_id is not None else None,
            )
        except Exception as e:
            log.warning(
                "schedule firer: set_vertical_halted(%s) failed: %s",
                vertical, e,
            )
            continue
        if not changed:
            log.info(
                "schedule %s for %s skipped — vertical already halted",
                rule_id, vertical,
            )
            out.append({
                "rule_id": rule_id, "vertical": vertical, "changed": False,
            })
            continue
        try:
            alerts_mod.vertical_halt_scheduled_fired(
                vertical=vertical,
                cron_expr=cron_expr,
                duration_secs=duration,
                reason=reason,
                schedule_id=int(rule_id) if rule_id is not None else None,
                expires_at=meta.get("expires_at") if isinstance(meta, dict) else None,
            )
        except Exception as e:
            log.warning(
                "schedule firer alert dispatch (%s) failed: %s", vertical, e
            )
        out.append({
            "rule_id": rule_id,
            "vertical": vertical,
            "changed": True,
            "expires_at": meta.get("expires_at") if isinstance(meta, dict) else None,
        })
        log.info(
            "vertical halt scheduled fire: vertical=%s rule=%s duration_secs=%s",
            vertical, rule_id, duration,
        )
    return out


async def tick_once(now: datetime | None = None) -> dict[str, Any]:
    """One full pass — TTL sweep then schedule eval. Test entrypoint."""
    expired = await sweep_ttls_once(now=now)
    fired = await fire_due_schedules_once(now=now)
    return {"ttl_expired": expired, "scheduled_fired": fired}


async def _loop() -> None:
    interval = _interval_secs()
    log.info("vertical_halt_scheduler started: interval=%ss", interval)
    while not _stop.is_set():
        try:
            await tick_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # pragma: no cover — outer try/except is paranoia
            log.warning("vertical_halt_scheduler loop error: %s", e)
        try:
            await asyncio.wait_for(_stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
        else:
            return


async def start() -> None:
    global _task
    if not _enabled():
        log.info("vertical_halt_scheduler disabled")
        return
    if _task and not _task.done():
        return
    _stop.clear()
    _task = asyncio.create_task(_loop(), name="vertical-halt-scheduler")


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
