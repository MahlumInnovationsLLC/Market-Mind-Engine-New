"""Live-state freshness page (task #862).

The Live-Games-Now dashboard tile (``dashboard.get_live_now_tile``)
colours a sport red when the oldest ``sportradar_live_state`` row for
that sport exceeds 2× the per-sport freshness budget from
``risk.gates.LIVE_STATE_FRESHNESS_BUDGET_SECONDS``.  That's fine when
an operator is watching the console, but a wedged feed could sit
unnoticed overnight.

This module closes that gap: a background check evaluates the same
condition on a cadence and pages operators (through the same
Postmark + Telegram fan-out the round-2 audit alerts use) once the
condition has persisted for N consecutive checks — long enough that a
single transient gap between Sportradar pushes doesn't fire a page,
but short enough that a real outage surfaces inside a couple of
minutes.

The alert is debounced via ``alerts.dedupe_or_skip`` keyed on the
sport so a sustained incident pages once per TTL, and the dedupe row
is dropped as soon as freshness recovers — at which point a
companion ``live_state_recovered`` notice is dispatched so a
long-lived incident has a clean close-out.

All state needed across cycles is kept in-process (consecutive-checks
counter + active-alert set), matching the convention the other
``notifications.*`` cycle hooks use (`tail_risk_burst`,
`cap_warnings`). The cadence is driven by the orchestrator cycle
loop, so a restart simply resets the counters and the next cycle
starts fresh — which is the conservative behaviour we want.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from . import alerts as _alerts


log = logging.getLogger("axiom.notifications.live_state_staleness")


# Dedupe alert_type used by both helpers below.
STALE_ALERT_TYPE = "live_state_stale"
RECOVERED_ALERT_TYPE = "live_state_recovered"


# Multiplier on the per-sport freshness budget that defines "stale".
# Mirrors the Live-Games-Now tile reddening at 2× the budget so the
# alert and the tile colour change agree on what "stale" means.
DEFAULT_STALENESS_MULTIPLIER = 2.0


# Number of consecutive over-threshold checks before the alert fires.
# The orchestrator cycle runs roughly once a minute, so the default of
# 2 means a wedged feed pages after ~1-2 minutes — matching the task
# brief ("more than a minute or two").
DEFAULT_CONSECUTIVE_CHECKS = 2


# How long the dedupe claim is held when the alert fires. A sustained
# incident pages once per window; once the row expires (or we
# explicitly drop it on recovery) the next over-threshold check pages
# again so a long-lived outage doesn't fall off the on-call's radar.
def _read_dedupe_ttl_seconds() -> int:
    raw = os.environ.get("AXIOM_LIVE_STATE_STALE_DEDUPE_TTL_SECONDS")
    if not raw:
        return 3600
    try:
        v = int(raw)
    except ValueError:
        log.warning(
            "ignoring non-numeric "
            "AXIOM_LIVE_STATE_STALE_DEDUPE_TTL_SECONDS=%r; "
            "falling back to 3600",
            raw,
        )
        return 3600
    return max(60, v)


# In-process state. Keyed by sport name (lowercase, matching
# ``SPORT_LEAGUES``). The counter increments per over-threshold check
# and resets to zero as soon as freshness recovers (or the sport has
# no live games). The alerted set tracks which sports currently have
# an active page so the recovery notice only fires once per incident.
_consecutive_stale: dict[str, int] = {}
_alerted_sports: set[str] = set()


def reset_state() -> None:
    """Test hook: clear the in-process counters and alerted set."""
    _consecutive_stale.clear()
    _alerted_sports.clear()


def _dashboard_url() -> str:
    """Deep-link to the Live-Games-Now tile on the operator dashboard.

    Returns the operator-console origin's ``/`` page when a public
    base URL is configured; falls back to the relative anchor so the
    body still surfaces something the operator can click."""
    try:
        base = _alerts._public_base_url()
    except Exception:  # pragma: no cover — base-url resolver is best-effort
        base = ""
    base = (base or "").rstrip("/")
    if not base:
        return "/#live-now"
    return f"{base}/#live-now"


def _sport_budget_seconds(leagues: tuple[str, ...]) -> float:
    """Per-sport freshness budget = min of the per-league budgets.

    Matches the convention ``dashboard_live_now`` uses to attach
    ``staleness_budget_seconds`` to each per-sport entry: take the
    tightest league budget so the alert agrees with the tile colour.
    """
    from ..risk.gates import (
        LIVE_STATE_FRESHNESS_BUDGET_SECONDS,
        DEFAULT_LIVE_STATE_FRESHNESS_BUDGET_SECONDS,
    )
    if not leagues:
        return DEFAULT_LIVE_STATE_FRESHNESS_BUDGET_SECONDS
    budgets = [
        LIVE_STATE_FRESHNESS_BUDGET_SECONDS.get(
            league, DEFAULT_LIVE_STATE_FRESHNESS_BUDGET_SECONDS
        )
        for league in leagues
    ]
    return float(min(budgets))


async def check_live_state_staleness(
    *,
    multiplier: float | None = None,
    consecutive_checks: int | None = None,
    dedupe_ttl_seconds: int | None = None,
) -> dict[str, Any]:
    """Evaluate the per-sport oldest-live-state staleness and page
    operators for any sport that has crossed the threshold for N
    consecutive checks.

    Returns a structured summary of what was checked, fired,
    recovered, and suppressed so the cycle log line can capture the
    decision and the test suite can assert against it.

    All errors are swallowed: this is a fire-and-forget hook called
    from the trading cycle, and failing the cycle because the
    notifier could not reach Postmark / the DB is the wrong tradeoff.
    """
    mult = float(multiplier) if multiplier is not None else DEFAULT_STALENESS_MULTIPLIER
    threshold_checks = (
        int(consecutive_checks)
        if consecutive_checks is not None
        else DEFAULT_CONSECUTIVE_CHECKS
    )
    if threshold_checks < 1:
        threshold_checks = 1
    ttl = (
        int(dedupe_ttl_seconds)
        if dedupe_ttl_seconds is not None
        else _read_dedupe_ttl_seconds()
    )

    out: dict[str, Any] = {
        "checked": True,
        "multiplier": mult,
        "threshold_checks": threshold_checks,
        "ttl_seconds": ttl,
        "fired": [],
        "recovered": [],
        "suppressed": [],
        "over_threshold": [],
        "per_sport": {},
    }

    try:
        from .. import dashboard as _dashboard
        from ..learning.in_game_loop import SPORT_LEAGUES as _SPORT_LEAGUES
    except Exception as e:  # pragma: no cover — defensive
        log.warning("live-state staleness check imports failed: %s", e)
        return out

    try:
        tile = await _dashboard.get_live_now_tile()
    except Exception as e:  # pragma: no cover — degraded read
        log.warning("live-state staleness tile read failed: %s", e)
        return out

    per = (tile or {}).get("per_sport") or {}
    dashboard_url = _dashboard_url()

    for sport, leagues in _SPORT_LEAGUES.items():
        entry = per.get(sport) or {}
        live_games = int(entry.get("live_games") or 0)
        staleness = entry.get("oldest_staleness_seconds")
        budget = _sport_budget_seconds(tuple(leagues))
        threshold_seconds = mult * budget

        per_sport_summary: dict[str, Any] = {
            "live_games": live_games,
            "oldest_staleness_seconds": (
                float(staleness) if staleness is not None else None
            ),
            "budget_seconds": budget,
            "threshold_seconds": threshold_seconds,
            "consecutive_checks": 0,
        }

        # A sport with no live games (or a tile that returned no
        # staleness datum) is not "stale" — it's idle. Reset the
        # counter and, if we had an active page, fire a recovery
        # notice so the incident closes cleanly.
        is_stale = (
            live_games > 0
            and staleness is not None
            and float(staleness) > threshold_seconds
        )
        if is_stale:
            new_count = _consecutive_stale.get(sport, 0) + 1
            _consecutive_stale[sport] = new_count
            per_sport_summary["consecutive_checks"] = new_count
            if new_count >= threshold_checks:
                out["over_threshold"].append(sport)
                try:
                    ok = await _alerts.dedupe_or_skip(
                        STALE_ALERT_TYPE, sport, ttl_seconds=ttl,
                    )
                except Exception as e:  # pragma: no cover — fail-open
                    log.warning(
                        "live-state-stale dedupe failed for %s: %s",
                        sport, e,
                    )
                    ok = True
                if not ok:
                    out["suppressed"].append(sport)
                    # Even when deduped, the operator is still in an
                    # alerted state — record it so the eventual
                    # recovery notice fires.
                    _alerted_sports.add(sport)
                    out["per_sport"][sport] = per_sport_summary
                    continue
                try:
                    _alerts.live_state_stale(
                        sport=sport,
                        oldest_staleness_seconds=float(staleness),
                        budget_seconds=budget,
                        multiplier=mult,
                        consecutive_checks=new_count,
                        threshold_checks=threshold_checks,
                        live_games=live_games,
                        dashboard_url=dashboard_url,
                    )
                    out["fired"].append(sport)
                    _alerted_sports.add(sport)
                except Exception as e:  # pragma: no cover — must not raise
                    log.warning(
                        "live-state-stale alert dispatch failed for %s: %s",
                        sport, e,
                    )
        else:
            # Fresh again (or idle). Drop the counter and, if we had
            # paged for this sport, send the recovery notice + clear
            # the dedupe claim so the next incident can page again
            # immediately rather than waiting out the TTL.
            _consecutive_stale[sport] = 0
            if sport in _alerted_sports:
                await _release_stale_dedupe(sport)
                try:
                    _alerts.live_state_recovered(
                        sport=sport,
                        oldest_staleness_seconds=(
                            float(staleness) if staleness is not None else None
                        ),
                        budget_seconds=budget,
                        dashboard_url=dashboard_url,
                    )
                    out["recovered"].append(sport)
                except Exception as e:  # pragma: no cover — must not raise
                    log.warning(
                        "live-state-recovered alert dispatch failed for %s: %s",
                        sport, e,
                    )
                _alerted_sports.discard(sport)

        out["per_sport"][sport] = per_sport_summary

    return out


async def _release_stale_dedupe(sport: str) -> None:
    """Drop the ``live_state_stale`` dedupe row for ``sport`` so the
    next incident pages immediately. Best-effort — a DB failure here
    just means the next page waits out the TTL, no incident is missed.
    """
    full_key = _alerts._compose_key(STALE_ALERT_TYPE, sport)
    try:
        from .. import db
        await db.execute(
            "DELETE FROM notification_dedupe WHERE key = $1",
            full_key,
        )
    except Exception as e:  # pragma: no cover — best-effort
        log.warning(
            "live-state-stale dedupe release failed for %s: %s",
            sport, e,
        )
