"""Per-provider LLM fallback-rate spike monitor (task #145).

The swarm runner transparently falls over from each persona's primary
provider to its configured backup whenever the primary returns a
retryable error (rate-limit / 5xx). That keeps debates completing during
a partial provider outage, but it also makes the outage invisible: the
moderator still gets eight verdicts, just always from the backup. This
biases the swarm (the backup is now over-represented) and silently
inflates cost.

This module runs a small background loop that periodically queries
``llm_predictions`` for the trailing window, groups the
``swarm_persona`` rows by their *primary* provider, and raises an
operator alert (``alerts.provider_fallback_spike``) when any provider's
fallback rate crosses the configured threshold. ``alerts.dedupe_or_skip``
keys on the provider name with a TTL >= the window so a sustained
incident pages once, not once per debate.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..config import get_settings


log = logging.getLogger("axiom.swarm.fallback_monitor")


_task: asyncio.Task[None] | None = None


# Query: count, per primary_provider, the persona-call rows in the
# trailing window and how many of them used the fallback. We extract
# the metadata fields written by `swarm.runner._call_persona`. Any row
# whose `primary_provider` is missing (older rows from before the
# telemetry landed) is skipped via the WHERE filter so it can't dilute
# the rate denominator.
_QUERY = """
SELECT
    metadata ->> 'primary_provider'                   AS provider,
    count(*)                                          AS total,
    count(*) FILTER (
        WHERE (metadata ->> 'fallback_used')::boolean = true
    )                                                 AS fallback,
    array_remove(
        array_agg(
            DISTINCT NULLIF(metadata ->> 'fallback_reason', '')
        ) FILTER (
            WHERE (metadata ->> 'fallback_used')::boolean = true
        ),
        NULL
    )                                                 AS reasons
FROM llm_predictions
WHERE category = 'swarm_persona'
  AND created_at >= now() - make_interval(mins => $1)
  AND metadata ? 'primary_provider'
  AND (metadata ->> 'primary_provider') IS NOT NULL
GROUP BY metadata ->> 'primary_provider'
"""


async def fetch_provider_stats(window_minutes: int) -> list[dict[str, Any]]:
    """Return per-primary-provider fallback stats over the trailing window.

    Each entry is ``{"provider", "total", "fallback", "rate", "reasons"}``
    where ``rate`` is ``fallback / total`` in [0, 1]. The list is sorted
    by ``rate`` descending (with ``total`` as the tiebreaker) so callers
    that only care about the worst offenders can trim from the head.

    Errors are logged and swallowed (returns ``[]``) so the dashboard
    endpoint and the alert loop both stay resilient against a transient
    DB hiccup.
    """
    window = max(1, int(window_minutes))
    from .. import db
    try:
        rows = await db.fetch(_QUERY, window)
    except Exception as e:
        log.warning("fallback monitor query failed: %s", e)
        return []

    out: list[dict[str, Any]] = []
    for r in rows:
        provider = r["provider"] or "unknown"
        total = int(r["total"] or 0)
        fallback = int(r["fallback"] or 0)
        rate = (fallback / total) if total else 0.0
        reasons = list(r["reasons"] or [])
        out.append(
            {
                "provider": provider,
                "total": total,
                "fallback": fallback,
                "rate": rate,
                "reasons": reasons,
            }
        )
    out.sort(key=lambda s: (-s["rate"], -s["total"], s["provider"]))
    return out


async def check_once() -> list[dict[str, Any]]:
    """Run one fallback-rate check and dispatch alerts as needed.

    Returns a list of per-provider stat dicts (mostly for tests / the
    debug endpoint). Errors are logged and swallowed so the background
    loop never dies.
    """
    s = get_settings()
    if not s.llm_fallback_alert_enabled:
        return []

    window = s.llm_fallback_alert_window_minutes
    threshold = s.llm_fallback_alert_threshold_pct / 100.0
    min_samples = s.llm_fallback_alert_min_samples

    rows = await fetch_provider_stats(window)

    out: list[dict[str, Any]] = []
    from ..notifications import alerts as _alerts
    for r in rows:
        provider = r["provider"]
        total = int(r["total"])
        fallback = int(r["fallback"])
        rate = float(r["rate"])
        reasons = list(r["reasons"])
        stats = {
            "provider": provider,
            "total": total,
            "fallback": fallback,
            "rate": rate,
            "alerted": False,
        }
        out.append(stats)

        if total < min_samples:
            continue
        if rate <= threshold:
            continue

        # Dedupe TTL is one window so a long incident pages once per
        # window rather than once per check tick.
        try:
            ok = await _alerts.dedupe_or_skip(
                "llm-fallback-spike",
                provider,
                ttl_seconds=window * 60,
            )
        except Exception as e:  # pragma: no cover
            log.warning("fallback monitor dedupe failed: %s", e)
            ok = True
        if not ok:
            log.info(
                "fallback spike for %s suppressed by dedupe (rate=%.2f%%)",
                provider, rate * 100.0,
            )
            continue

        try:
            _alerts.provider_fallback_spike(
                provider=provider,
                fallback_rate=rate,
                threshold=threshold,
                fallback_count=fallback,
                sample_count=total,
                window_minutes=window,
                sample_reasons=reasons,
            )
            stats["alerted"] = True
            log.warning(
                "raised LLM fallback spike alert: provider=%s "
                "rate=%.1f%% (%d/%d) window=%dm",
                provider, rate * 100.0, fallback, total, window,
            )
        except Exception as e:  # pragma: no cover
            log.warning("fallback spike alert dispatch failed: %s", e)

    return out


async def _loop() -> None:
    while True:
        s = get_settings()
        try:
            await check_once()
        except Exception as e:  # pragma: no cover - defensive
            log.warning("fallback monitor loop iteration failed: %s", e)
        await asyncio.sleep(s.llm_fallback_alert_interval_seconds)


async def start() -> None:
    """Start the background monitor. Idempotent. No-op when disabled."""
    global _task
    s = get_settings()
    if not s.llm_fallback_alert_enabled:
        log.info("LLM fallback monitor disabled by config")
        return
    if _task and not _task.done():
        return
    _task = asyncio.create_task(_loop(), name="llm-fallback-monitor")
    log.info(
        "LLM fallback monitor started: window=%dm threshold=%.1f%% "
        "min_samples=%d interval=%ds",
        s.llm_fallback_alert_window_minutes,
        s.llm_fallback_alert_threshold_pct,
        s.llm_fallback_alert_min_samples,
        s.llm_fallback_alert_interval_seconds,
    )


async def stop() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except (asyncio.CancelledError, Exception):
        pass
    _task = None
