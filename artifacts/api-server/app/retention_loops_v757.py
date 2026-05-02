"""Registry + lifecycle helpers for the task #757 retention loops.

This module is the single seam the rest of the application uses to
discover the 22 archive / delete-only loops added by task #757:

  * The ``main.lifespan`` startup / shutdown hooks call
    :func:`start_all` / :func:`stop_all` instead of having to
    enumerate 22 individual try / except blocks.
  * The dashboard wrappers in :mod:`app.dashboard` iterate
    :data:`ARCHIVE_MODULES` + :data:`DELETE_ONLY_MODULES` to render
    one health row per loop.
  * The one-shot CLI ``scripts/archive_and_prune_backlog.py``
    iterates the same lists so dry-run output covers every loop.

Each entry is the (already-imported) per-table module — they all
expose the same ``start() / stop() / state() / cleanup_once()``
surface, courtesy of :mod:`app.retention_base`.

The pre-existing retention loops (``crypto_ohlcv_retention``,
``order_fills_retention``, ``free_tier_views_retention``,
``crypto_backtest_retention``, ``sportradar_leaderboards_retention``)
are *intentionally not* included here — they default to enabled with
their own historical defaults and are still wired through their
existing main.lifespan blocks.
"""
from __future__ import annotations

import logging
from typing import Any

from . import (
    admin_audit_log_retention,
    autoresearch_features_retention,
    bankroll_events_retention,
    billing_churn_events_retention,
    gatekeeper_setting_history_retention,
    line_snapshots_retention,
    market_price_ticks_retention,
    mobile_notification_log_retention,
    model_rollback_log_retention,
    order_recovery_attempts_retention,
    pipeline_events_retention,
    position_drift_resolutions_retention,
    precip_emos_observations_retention,
    predictions_retention,
    rc_events_retention,
    rc_webhook_deliveries_retention,
    settlement_events_retention,
    sportradar_tournament_replays_retention,
    stale_cancel_observations_retention,
    stripe_events_retention,
    tech_evidence_retention,
    vertical_halt_history_retention,
)


log = logging.getLogger("axiom.retention_loops_v757")


# Archive-then-delete loops (90-day default).  These tables hold rows
# we still want re-trainable later, so the helper streams them to
# Supabase Storage as gzip JSONL before deleting.
ARCHIVE_MODULES: tuple[Any, ...] = (
    predictions_retention,
    market_price_ticks_retention,
    autoresearch_features_retention,
    tech_evidence_retention,
    line_snapshots_retention,
    sportradar_tournament_replays_retention,
    precip_emos_observations_retention,
)

# Delete-only loops (30-day default).  Pure ``DELETE`` past the
# horizon — operational / audit logs no consumer needs long-term.
DELETE_ONLY_MODULES: tuple[Any, ...] = (
    pipeline_events_retention,
    settlement_events_retention,
    mobile_notification_log_retention,
    bankroll_events_retention,
    rc_events_retention,
    stripe_events_retention,
    rc_webhook_deliveries_retention,
    billing_churn_events_retention,
    admin_audit_log_retention,
    position_drift_resolutions_retention,
    order_recovery_attempts_retention,
    stale_cancel_observations_retention,
    vertical_halt_history_retention,
    gatekeeper_setting_history_retention,
    model_rollback_log_retention,
)

ALL_MODULES: tuple[Any, ...] = ARCHIVE_MODULES + DELETE_ONLY_MODULES


def _name_of(module: Any) -> str:
    """Stable display name for log + dashboard rows."""
    state = module.state()
    return str(state.get("name") or module.__name__)


async def start_all() -> None:
    """Best-effort start of every task #757 loop.

    Each loop's ``start()`` already short-circuits when its
    ``AXIOM_<PREFIX>_RETENTION_ENABLED`` env knob is unset / 0, so
    calling this on a fresh deploy is a no-op.  We still wrap each
    one in try / except so a single misconfigured loop can't take
    down the lifespan startup.
    """
    for module in ALL_MODULES:
        name = _name_of(module)
        try:
            await module.start()
        except Exception as e:  # noqa: BLE001
            log.warning("retention loop %s start failed: %s", name, e)


async def stop_all() -> None:
    """Best-effort stop of every task #757 loop."""
    for module in ALL_MODULES:
        name = _name_of(module)
        try:
            await module.stop()
        except Exception as e:  # noqa: BLE001
            log.warning("retention loop %s stop error: %s", name, e)


def states() -> list[dict[str, Any]]:
    """Snapshot every loop's ``state()`` dict for the dashboard."""
    out: list[dict[str, Any]] = []
    for module in ALL_MODULES:
        try:
            out.append(module.state())
        except Exception as e:  # noqa: BLE001
            out.append(
                {
                    "name": _name_of(module),
                    "error": f"{type(e).__name__}: {e}",
                }
            )
    return out
