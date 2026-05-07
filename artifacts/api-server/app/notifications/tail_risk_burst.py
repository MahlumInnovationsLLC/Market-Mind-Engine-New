"""Burst-rate alert for the tail-risk gates (Task #746).

The two new tail-risk policy gates (`gate_tail_price_size_cap` and
`gate_jumbo_quantity_cap`, task #743) already log every veto into the
structured `risk_gate_trips` table and dispatch a per-veto info-severity
alert (`tail_risk_gate_trip`, task #750), but neither of those signals
is suitable for "the model just started recommending deep-OTM /
jumbo bets in burst" — the per-veto alert is deduped per
`(gate_name, market_id)` for 6 hours so a fresh batch of *different*
markets all tripping the same gate could still slip past the on-call
until they next look at the dashboard tile.

This module closes that gap: once per orchestrator cycle it counts the
trips for each tail-risk gate inside a rolling 60-minute window and
pages operators when the count crosses the operator-tunable burst
threshold (`risk.tail_risk_burst_threshold`, default 10). Each page
goes out through the same Postmark + Telegram fan-out the round-2
audit gates' alerts use, with a per-vertical breakdown plus a
deep-link to the tail-risk dashboard tile from task #744.

Dedup is handled by `alerts.dedupe_or_skip` keyed on `gate_name` so a
sustained burst pages once per `AXIOM_TAIL_RISK_BURST_DEDUPE_TTL_SECONDS`
(default 1h, matching the rolling window).
"""
from __future__ import annotations

import logging
import os
from typing import Any

from . import alerts as _alerts


log = logging.getLogger("axiom.notifications.tail_risk_burst")


# Dedup alert_type used by every burst alert dispatched from this
# module.  Centralised so the dashboard reader / suppression-counter
# scan can find the right rows.
DEDUPE_ALERT_TYPE = "tail_risk_burst"


# The two tail-risk gates whose vetoes feed the burst counter.
# Centralised so a future fifth tail-risk gate can join without a
# fresh migration.
TAIL_RISK_GATE_NAMES: tuple[str, ...] = (
    "tail_price_size_cap",
    "jumbo_quantity_cap",
)


# Default burst threshold (vetoes per gate per 60-minute window) when
# `risk.tail_risk_burst_threshold` is missing from `system_settings`.
# Matches the in-code default in the operator-tunable knob spec
# (`runtime_settings.RISK_KNOB_SPECS::tail_risk_burst_threshold`); keep
# both sides in sync.
DEFAULT_BURST_THRESHOLD = 10


# Rolling window the count is evaluated over.  60 minutes is short
# enough that a fresh regime change pages within an hour and long
# enough that the dedupe TTL doesn't have to clamp to the cycle
# cadence.
WINDOW_MINUTES = 60


def _read_dedupe_ttl_seconds() -> int:
    raw = os.environ.get("AXIOM_TAIL_RISK_BURST_DEDUPE_TTL_SECONDS")
    if not raw:
        return 3600
    try:
        v = int(raw)
    except ValueError:
        log.warning(
            "ignoring non-numeric "
            "AXIOM_TAIL_RISK_BURST_DEDUPE_TTL_SECONDS=%r; "
            "falling back to 3600",
            raw,
        )
        return 3600
    return max(60, v)


async def _read_burst_threshold() -> int:
    """Read the operator-tunable burst threshold from
    `system_settings.risk.tail_risk_burst_threshold`.

    Falls back to :data:`DEFAULT_BURST_THRESHOLD` on any read failure or
    when the row is missing — matches the rest of the risk knobs (the
    gate keeps an in-code fallback so a torn-down DB doesn't disarm
    the safety net)."""
    try:
        from .. import runtime_settings as _rs
        overrides = await _rs.get_risk_knob_overrides()
    except Exception as e:  # pragma: no cover — defensive
        log.debug(
            "tail-risk burst threshold read failed (best-effort): %s", e
        )
        return DEFAULT_BURST_THRESHOLD
    raw = overrides.get("tail_risk_burst_threshold")
    if raw is None:
        return DEFAULT_BURST_THRESHOLD
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_BURST_THRESHOLD


def _dashboard_url() -> str:
    """Deep-link to the tail-risk dashboard tile (task #744).

    Returns the operator-console origin's `/system` page when a public
    base URL is configured; falls back to the relative path so the
    body still surfaces *something* the operator can click."""
    try:
        base = _alerts._public_base_url()
    except Exception:  # pragma: no cover — base-url resolver is best-effort
        base = ""
    base = (base or "").rstrip("/")
    if not base:
        return "/system#tail-risk"
    return f"{base}/system#tail-risk"


async def _gate_burst_summary(
    gate_name: str,
) -> dict[str, Any]:
    """Count vetoes for ``gate_name`` in the rolling window plus the
    per-vertical breakdown and the latest market_id / timestamp.

    Returns ``{"count": int, "by_vertical": [(vertical, n), ...],
    "last_market_id": str|None, "last_at": str|None}``.  Failures
    surface as ``count=0`` so the burst alert never fires off a
    DB-degraded read.
    """
    out: dict[str, Any] = {
        "count": 0,
        "by_vertical": [],
        "last_market_id": None,
        "last_at": None,
    }
    from .. import db
    try:
        rows = await db.fetch(
            f"""
            SELECT COALESCE(vertical, '(unknown)') AS vertical,
                   count(*)::int                   AS n
              FROM risk_gate_trips
             WHERE gate_name = $1
               AND created_at > now()
                                 - interval '{WINDOW_MINUTES} minutes'
             GROUP BY COALESCE(vertical, '(unknown)')
             ORDER BY n DESC, vertical ASC
            """,
            gate_name,
        )
    except Exception as e:  # pragma: no cover — degraded read
        log.warning(
            "tail-risk burst count failed for gate=%s: %s", gate_name, e
        )
        return out
    by_vertical: list[tuple[str, int]] = []
    total = 0
    for r in rows or []:
        n = int(r["n"] or 0)
        total += n
        by_vertical.append((str(r["vertical"]), n))
    out["count"] = total
    out["by_vertical"] = by_vertical

    if total == 0:
        return out

    try:
        last = await db.fetchrow(
            f"""
            SELECT market_id,
                   to_char(created_at,
                           'YYYY-MM-DD"T"HH24:MI:SSZ') AS last_at
              FROM risk_gate_trips
             WHERE gate_name = $1
               AND created_at > now()
                                 - interval '{WINDOW_MINUTES} minutes'
             ORDER BY created_at DESC
             LIMIT 1
            """,
            gate_name,
        )
    except Exception as e:  # pragma: no cover — degraded read
        log.warning(
            "tail-risk burst last-trip lookup failed for gate=%s: %s",
            gate_name, e,
        )
        last = None
    if last is not None:
        out["last_market_id"] = last["market_id"]
        out["last_at"] = last["last_at"]
    return out


async def check_tail_risk_burst(
    *,
    threshold: int | None = None,
    dedupe_ttl_seconds: int | None = None,
) -> dict[str, Any]:
    """Evaluate the rolling-window veto count for both tail-risk gates
    and dispatch an operator alert per gate that crossed the burst
    threshold.

    Returns a structured summary of what was checked / fired /
    suppressed so the orchestrator log line can capture the cycle
    decision and the test suite can assert against it.

    All errors are swallowed: this is a fire-and-forget hook called
    from the trading cycle, and failing the cycle because the
    notifier could not reach Postmark is the wrong tradeoff.
    """
    thr = threshold if threshold is not None else await _read_burst_threshold()
    ttl = (
        dedupe_ttl_seconds
        if dedupe_ttl_seconds is not None
        else _read_dedupe_ttl_seconds()
    )

    fired: list[str] = []
    suppressed: list[str] = []
    over_threshold: list[str] = []
    per_gate: dict[str, dict[str, Any]] = {}

    if thr <= 0:
        # Operator opt-out — disable the burst page entirely without
        # touching the gate (the per-veto alert + dashboard tile are
        # still active).  Return a structured "disabled" summary so
        # the cycle log line distinguishes silence from "below
        # threshold".
        return {
            "checked": True,
            "disabled": True,
            "threshold": thr,
            "window_minutes": WINDOW_MINUTES,
            "ttl_seconds": ttl,
            "over_threshold": [],
            "fired": [],
            "suppressed": [],
            "per_gate": {},
        }

    for gate_name in TAIL_RISK_GATE_NAMES:
        try:
            summary = await _gate_burst_summary(gate_name)
        except Exception as e:  # pragma: no cover — defensive
            log.warning(
                "tail-risk burst summary failed for gate=%s: %s",
                gate_name, e,
            )
            continue
        per_gate[gate_name] = summary
        if summary["count"] <= thr:
            continue
        over_threshold.append(gate_name)
        try:
            ok = await _alerts.dedupe_or_skip(
                DEDUPE_ALERT_TYPE, gate_name, ttl_seconds=ttl,
            )
        except Exception as e:  # pragma: no cover — fail-open
            log.warning(
                "tail-risk burst dedupe failed for %s: %s", gate_name, e
            )
            ok = True
        if not ok:
            suppressed.append(gate_name)
            continue
        try:
            _alerts.tail_risk_burst(
                gate_name=gate_name,
                count=int(summary["count"]),
                threshold=int(thr),
                window_minutes=WINDOW_MINUTES,
                by_vertical=list(summary["by_vertical"]),
                last_market_id=summary["last_market_id"],
                last_at=summary["last_at"],
                dashboard_url=_dashboard_url(),
            )
            fired.append(gate_name)
        except Exception as e:  # pragma: no cover — alert dispatch must not raise
            log.warning(
                "tail-risk burst alert dispatch failed for %s: %s",
                gate_name, e,
            )

    return {
        "checked": True,
        "disabled": False,
        "threshold": int(thr),
        "window_minutes": WINDOW_MINUTES,
        "ttl_seconds": ttl,
        "over_threshold": over_threshold,
        "fired": fired,
        "suppressed": suppressed,
        "per_gate": per_gate,
    }
