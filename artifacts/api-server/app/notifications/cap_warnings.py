"""Background check that pages operators when an NFL exposure cap
bucket crosses the warn threshold.

The dashboard panel from task #208 surfaces a visual warning when
per-league / per-game / Sunday-window utilisation crosses 80% of the
configured cap, but operators only see it if they happen to be looking
at the page.  This module computes the same buckets
``dashboard.get_nfl_exposure()`` returns and dispatches an operator
alert (via ``notifications.alerts.cap_bucket_warn``) the first time a
bucket crosses the threshold — so the on-call hears about it before
the next bet is silently blocked.

Dedup is handled by ``alerts.dedupe_or_skip`` keyed on a stable
``(league, bucket)`` label so the same warn does not spam every cycle:
within ``AXIOM_NFL_CAP_WARN_TTL_SECONDS`` (default 1h) only the first
crossing of a given bucket dispatches, follow-ups are suppressed.

The warn threshold matches the panel's ``warn_threshold`` (default
``0.80``) and is overridable via ``AXIOM_NFL_CAP_WARN_THRESHOLD``.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from . import alerts as _alerts


log = logging.getLogger("axiom.notifications.cap_warnings")


def _read_threshold() -> float:
    raw = os.environ.get("AXIOM_NFL_CAP_WARN_THRESHOLD")
    if not raw:
        return 0.80
    try:
        v = float(raw)
    except ValueError:
        log.warning(
            "ignoring non-numeric AXIOM_NFL_CAP_WARN_THRESHOLD=%r; "
            "falling back to 0.80",
            raw,
        )
        return 0.80
    # Clamp into a sensible range — a 0% threshold would page on every
    # bucket on every cycle; a >100% threshold would never fire.
    if v <= 0.0 or v > 1.0:
        log.warning(
            "AXIOM_NFL_CAP_WARN_THRESHOLD=%r outside (0,1]; "
            "falling back to 0.80",
            raw,
        )
        return 0.80
    return v


def _read_ttl_seconds() -> int:
    raw = os.environ.get("AXIOM_NFL_CAP_WARN_TTL_SECONDS")
    if not raw:
        return 3600
    try:
        v = int(raw)
    except ValueError:
        log.warning(
            "ignoring non-numeric AXIOM_NFL_CAP_WARN_TTL_SECONDS=%r; "
            "falling back to 3600",
            raw,
        )
        return 3600
    return max(60, v)


def _collect_buckets(
    snap: dict[str, Any], threshold: float
) -> list[dict[str, Any]]:
    """Walk an exposure snapshot (shape from ``get_nfl_exposure``) and
    return the buckets whose utilisation is at or above ``threshold``.

    Each returned dict carries the metadata the alert helper needs plus
    a stable ``key`` suitable for dedupe.
    """
    league = str(snap.get("league") or "NFL")
    out: list[dict[str, Any]] = []

    pl = snap.get("per_league") or {}
    util = pl.get("utilization")
    if util is not None and float(util) >= threshold:
        out.append({
            "key": f"{league}:per_league",
            "scope": "per_league",
            "label": "per-league cap",
            "matchup": None,
            "open_usd": float(pl.get("open_usd") or 0.0),
            "cap_usd": float(pl.get("cap_usd") or 0.0),
            "cap_pct": float(pl.get("cap_pct") or 0.0),
            "utilization": float(util),
        })

    sw = snap.get("sunday_window") or {}
    util = sw.get("utilization")
    if util is not None and float(util) >= threshold:
        out.append({
            "key": f"{league}:sunday_window",
            "scope": "sunday_window",
            "label": "Sunday-window cap",
            "matchup": None,
            "open_usd": float(sw.get("open_usd") or 0.0),
            "cap_usd": float(sw.get("cap_usd") or 0.0),
            "cap_pct": float(sw.get("cap_pct") or 0.0),
            "utilization": float(util),
        })

    for m in snap.get("per_matchup") or []:
        util = m.get("utilization")
        if util is None or float(util) < threshold:
            continue
        matchup = str(m.get("matchup") or "?")
        out.append({
            "key": f"{league}:per_matchup:{matchup}",
            "scope": "per_matchup",
            "label": f"per-game cap ({matchup})",
            "matchup": matchup,
            "open_usd": float(m.get("open_usd") or 0.0),
            "cap_usd": float(m.get("cap_usd") or 0.0),
            "cap_pct": float(m.get("cap_pct") or 0.0),
            "utilization": float(util),
        })

    return out


async def check_nfl_cap_warnings(
    *,
    threshold: float | None = None,
    ttl_seconds: int | None = None,
) -> dict[str, Any]:
    """Compute the same buckets ``dashboard.get_nfl_exposure()`` returns
    and dispatch an operator alert per bucket whose utilisation has
    crossed the warn threshold.

    Returns a summary dict listing the buckets evaluated, dispatched,
    and suppressed by dedupe — useful for the cycle-result log line and
    the existing test suite.

    All errors are swallowed: this is a notify-on-best-effort hook
    called from the trading cycle, and failing the cycle because the
    notifier could not reach Postmark is the wrong tradeoff.
    """
    thr = threshold if threshold is not None else _read_threshold()
    ttl = ttl_seconds if ttl_seconds is not None else _read_ttl_seconds()

    from .. import dashboard as _dashboard
    try:
        snap = await _dashboard.get_nfl_exposure()
    except Exception as e:  # pragma: no cover — defensive
        log.warning("cap-warn check failed reading NFL exposure: %s", e)
        return {
            "checked": False,
            "error": str(e),
            "threshold": thr,
            "fired": [],
            "suppressed": [],
            "over_threshold": [],
        }

    league = str(snap.get("league") or "NFL")
    bankroll = snap.get("bankroll_usd")

    over = _collect_buckets(snap, thr)
    fired: list[str] = []
    suppressed: list[str] = []

    for b in over:
        try:
            ok = await _alerts.dedupe_or_skip(
                "nfl_cap_warn", b["key"], ttl_seconds=ttl,
            )
        except Exception as e:  # pragma: no cover — fail-open
            log.warning("cap-warn dedupe failed for %s: %s", b["key"], e)
            ok = True
        if not ok:
            suppressed.append(b["key"])
            continue
        try:
            _alerts.cap_bucket_warn(
                league=league,
                bucket=b["scope"],
                label=b["label"],
                matchup=b["matchup"],
                open_usd=b["open_usd"],
                cap_usd=b["cap_usd"],
                cap_pct=b["cap_pct"],
                utilization=b["utilization"],
                warn_threshold=thr,
                bankroll_usd=bankroll,
            )
            fired.append(b["key"])
        except Exception as e:  # pragma: no cover
            log.warning("cap-warn alert dispatch failed for %s: %s", b["key"], e)

    return {
        "checked": True,
        "threshold": thr,
        "ttl_seconds": ttl,
        "over_threshold": [b["key"] for b in over],
        "fired": fired,
        "suppressed": suppressed,
    }
