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
from datetime import datetime, timezone
from typing import Any

from . import alerts as _alerts


log = logging.getLogger("axiom.notifications.cap_warnings")


# ---------------------------------------------------------------------------
# Last-cycle bookkeeping.
#
# `check_nfl_cap_warnings` runs once per orchestrator cycle. The dashboard
# panel from task #208 needs to tell silence ("nothing crossed the warn
# threshold") apart from quiet ("a bucket crossed but the dedupe row is
# still hiding the page from the on-call"), so the most recent cycle's
# fired/suppressed bucket keys are kept in process memory and surfaced
# alongside the persistent `notification_dedupe` rows.
#
# The state is intentionally per-process (resets on restart) — the
# persistent dedupe table is the source of truth for "was the on-call
# actually paged"; this in-memory snapshot only flags "did this cycle
# decide to suppress this bucket because of dedupe".
# ---------------------------------------------------------------------------


# Dedupe alert_type used by every cap-bucket warn dispatched from this
# module.  Centralised so the dashboard reader can scan the right rows.
DEDUPE_ALERT_TYPE = "nfl_cap_warn"


_LAST_CYCLE: dict[str, Any] | None = None


def _record_cycle(
    *,
    threshold: float,
    ttl_seconds: int,
    over_threshold: list[str],
    fired: list[str],
    suppressed: list[str],
) -> None:
    """Capture the result of the most recent ``check_nfl_cap_warnings``
    invocation so the dashboard can surface "fired this cycle" /
    "suppressed this cycle" without joining against historical cycle logs.
    """
    global _LAST_CYCLE
    _LAST_CYCLE = {
        "ran_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "threshold": float(threshold),
        "ttl_seconds": int(ttl_seconds),
        "over_threshold": list(over_threshold),
        "fired": list(fired),
        "suppressed": list(suppressed),
    }


def reset_last_cycle() -> None:
    """Test helper — wipe the in-memory last-cycle snapshot."""
    global _LAST_CYCLE
    _LAST_CYCLE = None


def last_cycle_snapshot() -> dict[str, Any] | None:
    """Return a copy of the most recent cycle result, or ``None`` if
    ``check_nfl_cap_warnings`` has not run since process start."""
    if _LAST_CYCLE is None:
        return None
    return {
        "ran_at": _LAST_CYCLE["ran_at"],
        "threshold": _LAST_CYCLE["threshold"],
        "ttl_seconds": _LAST_CYCLE["ttl_seconds"],
        "over_threshold": list(_LAST_CYCLE["over_threshold"]),
        "fired": list(_LAST_CYCLE["fired"]),
        "suppressed": list(_LAST_CYCLE["suppressed"]),
    }


async def recent_dispatch_history() -> dict[str, Any]:
    """Return the persistent dedupe history for cap-bucket warn alerts.

    Combines two signals so the operator console can answer
    "did the on-call already hear about this bucket?":

    * The ``notification_dedupe`` rows tagged ``nfl_cap_warn:*`` —
      keyed by the same ``{league}:{bucket}`` label
      ``check_nfl_cap_warnings`` claims, carrying ``sent_at`` and
      ``expires_at`` so the dashboard can show "last paged 7m ago,
      dedupe expires in 53m".
    * The most recent cycle's fired/suppressed lists from
      :func:`last_cycle_snapshot` so silence
      ("nothing crossed the threshold") can be told apart from quiet
      ("dedupe is hiding the page").

    Fails soft on any DB error: a missing dashboard panel detail is
    strictly better than a 500 on the operator console because the
    notifications schema is unreachable.
    """
    last_cycle = last_cycle_snapshot()

    from .. import db
    rows: list[Any] = []
    try:
        rows = await db.fetch(
            """
            SELECT key, sent_at, expires_at
              FROM notification_dedupe
             WHERE starts_with(key, $1)
            """,
            f"{DEDUPE_ALERT_TYPE}:",
        )
    except Exception as e:  # pragma: no cover — advisory read
        log.warning("recent_dispatch_history fetch failed: %s", e)
        rows = []

    now = datetime.now(tz=timezone.utc)
    by_bucket: dict[str, dict[str, Any]] = {}
    prefix = f"{DEDUPE_ALERT_TYPE}:"
    for r in rows:
        try:
            key = r["key"] if hasattr(r, "__getitem__") else r.get("key")
        except Exception:
            key = None
        if not key or not key.startswith(prefix):
            continue
        bucket_key = key[len(prefix):]
        try:
            sent_at = r["sent_at"] if hasattr(r, "__getitem__") else r.get("sent_at")
        except Exception:
            sent_at = None
        try:
            expires_at = (
                r["expires_at"] if hasattr(r, "__getitem__") else r.get("expires_at")
            )
        except Exception:
            expires_at = None

        ttl_remaining: int | None = None
        if expires_at is not None:
            try:
                delta = (expires_at - now).total_seconds()
                ttl_remaining = max(0, int(delta))
            except Exception:
                ttl_remaining = None

        sent_iso = None
        if sent_at is not None:
            try:
                sent_iso = sent_at.isoformat(timespec="seconds")
            except Exception:
                sent_iso = str(sent_at)
        expires_iso = None
        if expires_at is not None:
            try:
                expires_iso = expires_at.isoformat(timespec="seconds")
            except Exception:
                expires_iso = str(expires_at)

        by_bucket[bucket_key] = {
            "last_sent_at": sent_iso,
            "expires_at": expires_iso,
            "ttl_seconds_remaining": ttl_remaining,
        }

    return {
        "alert_type": DEDUPE_ALERT_TYPE,
        "last_cycle": last_cycle,
        "by_bucket": by_bucket,
    }


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
                DEDUPE_ALERT_TYPE, b["key"], ttl_seconds=ttl,
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

    over_keys = [b["key"] for b in over]
    _record_cycle(
        threshold=thr,
        ttl_seconds=ttl,
        over_threshold=over_keys,
        fired=fired,
        suppressed=suppressed,
    )

    return {
        "checked": True,
        "threshold": thr,
        "ttl_seconds": ttl,
        "over_threshold": over_keys,
        "fired": fired,
        "suppressed": suppressed,
    }
