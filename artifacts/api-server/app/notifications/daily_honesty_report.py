"""Daily honesty report (Task #756 / roadmap T-21).

Once per UTC day this scheduler bundles four operator-visible signals
into a single Telegram + email digest so the on-call doesn't have to
open seven dashboards to read the engine's day-over-day health:

    1. Shadow-vs-live divergence — per-league absolute Brier delta
       between ``shadow_learning_buffer`` (settled shadow predictions)
       and ``online_learning_buffer`` (settled live predictions) over
       the last 7 days.
    2. Calibrator drift status — one row per supported league derived
       from the most recent ``calibration_snapshots`` row, classified
       GREEN / YELLOW / RED on Brier-after, drift_status, and freshness.
    3. Tail-risk tile summary — headline numbers from
       ``dashboard.get_tail_risk_exposure(days=30)``.
    4. Gate-trip log — ``SELECT gate_name, count(*) FROM risk_gate_trips
       WHERE created_at > now() - interval '24 hours' GROUP BY gate_name``.

Configuration:
    DAILY_HONESTY_REPORT_ENABLED       (default: 1)
    DAILY_HONESTY_REPORT_HOUR_UTC      (default: 13 — ~9am ET)
    DAILY_HONESTY_REPORT_MINUTE_UTC    (default: 0)
    DAILY_HONESTY_REPORT_DIVERGENCE_DAYS  (default: 7)
    DAILY_HONESTY_REPORT_MIN_SAMPLES      (default: 30 per side)

Send pattern mirrors :mod:`app.notifications.daily_summary` — a
``notification_dedupe`` row with key ``daily_honesty_report`` makes
restart-safe at-most-once-per-day delivery the default.  The
admin-only ``POST /api/admin/honesty-report/fire-now`` endpoint is the
manual sanity-test path that bypasses the dedupe (operators trigger
it with ``dry_run`` first to inspect the body without dispatching).
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover — stdlib in py3.9+
    ZoneInfo = None  # type: ignore[assignment]
    ZoneInfoNotFoundError = Exception  # type: ignore[assignment, misc]

from .. import db
from ..health import error_log
from . import alerts


log = logging.getLogger("axiom.notifications.daily_honesty_report")


# Where the PDF + PNG artifacts land.  Same convention as the V1
# backtest archive (see runbook §22) so operators only have one path
# pattern to remember.  Lazy-created on first write.
REPORTS_DIR = Path(__file__).resolve().parents[2] / "reports"


# ---------------------------------------------------------------------------
# Env knobs (mirrors daily_summary.py for muscle-memory).
# ---------------------------------------------------------------------------


def _enabled() -> bool:
    raw = (os.environ.get("DAILY_HONESTY_REPORT_ENABLED") or "1").strip().lower()
    return raw in ("1", "true", "yes", "on", "y")


# ---------------------------------------------------------------------------
# Local-time scheduling (G1).
#
# Two-mode design so the cutover from "UTC only" to "local TZ" doesn't
# silently shift the existing 13:00-UTC operator habit:
#
#   * Default: ``DAILY_HONESTY_REPORT_TZ=America/New_York`` and the
#     scheduler reads ``_HOUR`` / ``_MINUTE`` (in that TZ).  When those
#     env vars are absent we synthesise them from the legacy
#     ``_HOUR_UTC`` / ``_MINUTE_UTC`` by converting today's UTC clock
#     into the configured local TZ — this preserves wall-clock
#     ~9-am-ET behavior across DST, modulo a one-hour shift on the
#     spring-forward / fall-back day itself (acceptable for a single
#     daily digest).
#   * ``_TZ=UTC`` keeps strict legacy behavior.
#
# Dedupe is keyed on the configured-TZ date so a 21:00-PT send doesn't
# get re-fired by a UTC midnight rollover.
# ---------------------------------------------------------------------------


_DEFAULT_TZ = "America/New_York"


def _local_tz() -> "ZoneInfo":  # pragma: no cover — trivial
    name = (os.environ.get("DAILY_HONESTY_REPORT_TZ") or _DEFAULT_TZ).strip()
    if ZoneInfo is None:
        return timezone.utc  # type: ignore[return-value]
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        log.warning(
            "DAILY_HONESTY_REPORT_TZ=%r is not a valid IANA name; "
            "falling back to UTC",
            name,
        )
        return ZoneInfo("UTC")


def _now_local() -> datetime:
    return datetime.now(tz=_local_tz())


def _local_today_str() -> str:
    return _now_local().date().isoformat()


def _send_hour() -> int:
    """Local-TZ hour the digest fires at (default: derived from
    ``_HOUR_UTC`` for backcompat — see module docstring)."""
    raw = os.environ.get("DAILY_HONESTY_REPORT_HOUR")
    if raw is None or raw == "":
        # Legacy fallback: convert UTC HH:MM to the configured local TZ
        # using today as the conversion anchor (DST-aware).
        try:
            hu = max(
                0,
                min(
                    23,
                    int(os.environ.get("DAILY_HONESTY_REPORT_HOUR_UTC") or "13"),
                ),
            )
            mu = max(
                0,
                min(
                    59,
                    int(os.environ.get("DAILY_HONESTY_REPORT_MINUTE_UTC") or "0"),
                ),
            )
        except (TypeError, ValueError):
            hu, mu = 13, 0
        anchor = datetime.now(tz=timezone.utc).replace(
            hour=hu, minute=mu, second=0, microsecond=0
        )
        return anchor.astimezone(_local_tz()).hour
    try:
        return max(0, min(23, int(raw)))
    except (TypeError, ValueError):
        return 9  # ~typical 9am operator habit


def _send_minute() -> int:
    raw = os.environ.get("DAILY_HONESTY_REPORT_MINUTE")
    if raw is None or raw == "":
        try:
            hu = max(
                0,
                min(
                    23,
                    int(os.environ.get("DAILY_HONESTY_REPORT_HOUR_UTC") or "13"),
                ),
            )
            mu = max(
                0,
                min(
                    59,
                    int(os.environ.get("DAILY_HONESTY_REPORT_MINUTE_UTC") or "0"),
                ),
            )
        except (TypeError, ValueError):
            hu, mu = 13, 0
        anchor = datetime.now(tz=timezone.utc).replace(
            hour=hu, minute=mu, second=0, microsecond=0
        )
        return anchor.astimezone(_local_tz()).minute
    try:
        return max(0, min(59, int(raw)))
    except (TypeError, ValueError):
        return 0


def _divergence_days() -> int:
    try:
        return max(
            1,
            min(
                90,
                int(
                    os.environ.get("DAILY_HONESTY_REPORT_DIVERGENCE_DAYS") or "7"
                ),
            ),
        )
    except (TypeError, ValueError):
        return 7


def _min_samples_per_side() -> int:
    try:
        return max(
            1,
            int(os.environ.get("DAILY_HONESTY_REPORT_MIN_SAMPLES") or "30"),
        )
    except (TypeError, ValueError):
        return 30


_DEDUPE_KEY = "daily_honesty_report"
_DISPATCH_TAG = "daily-honesty-report"


# ---------------------------------------------------------------------------
# Restart-safe dedupe — same shape as daily_summary's helper.  We keep
# our own copy (instead of importing daily_summary's) so the two
# scheduler loops stay independently testable + own their own keys.
# ---------------------------------------------------------------------------


async def _get_last_sent_date() -> str | None:
    try:
        row = await db.fetchrow(
            "SELECT date_utc::text AS d FROM notification_dedupe WHERE key=$1",
            _DEDUPE_KEY,
        )
        return row["d"] if row else None
    except Exception as e:
        log.warning("honesty-report dedupe read failed: %s", e)
        return None


async def _mark_sent(date_utc: str) -> bool:
    """Atomically claim today.  True ⇒ this caller should send."""
    try:
        row = await db.fetchrow(
            """
            INSERT INTO notification_dedupe (key, date_utc)
            VALUES ($1, $2::date)
            ON CONFLICT (key) DO UPDATE
              SET date_utc = EXCLUDED.date_utc,
                  sent_at  = now()
              WHERE notification_dedupe.date_utc < EXCLUDED.date_utc
            RETURNING 1 AS ok
            """,
            _DEDUPE_KEY,
            date_utc,
        )
        return bool(row)
    except Exception as e:
        log.warning("honesty-report dedupe claim failed: %s", e)
        # Fail open — better to risk a duplicate than silently skip.
        return True


# ---------------------------------------------------------------------------
# Section builders.  Each one swallows DB errors and returns an empty
# section with an ``error`` marker so a single broken query doesn't
# black out the whole report.
# ---------------------------------------------------------------------------


def _brier(probs: list[float], outcomes: list[int]) -> float | None:
    if not probs or len(probs) != len(outcomes):
        return None
    return sum((float(p) - int(y)) ** 2 for p, y in zip(probs, outcomes)) / len(probs)


async def collect_shadow_vs_live_divergence(
    *, days: int | None = None, min_samples: int | None = None
) -> dict[str, Any]:
    """Per-league |Brier(shadow) − Brier(live)| over the last `days`.

    Both buffers are settled-only (the SQL filters on
    ``actual_outcome IS NOT NULL`` — both schemas carry the column).
    Leagues with fewer than ``min_samples`` rows on either side are
    surfaced under ``samples_too_small`` rather than silently dropped
    so the operator can see *why* a league is missing.
    """
    days = days if days is not None else _divergence_days()
    min_samples = (
        min_samples if min_samples is not None else _min_samples_per_side()
    )
    out: dict[str, Any] = {
        "days": int(days),
        "min_samples_per_side": int(min_samples),
        "items": [],
        "samples_too_small": [],
        "error": None,
    }
    sql = """
        SELECT $1::text                AS source,
               coalesce(upper(league),'')  AS league,
               predicted_prob::float    AS predicted_prob,
               actual_outcome::int      AS actual_outcome
          FROM {table}
         WHERE actual_outcome IS NOT NULL
           AND predicted_prob IS NOT NULL
           AND created_at > (now() - ($2::int || ' days')::interval)
    """
    try:
        live_rows = await db.fetch(
            sql.format(table="online_learning_buffer"), "live", int(days)
        )
        shadow_rows = await db.fetch(
            sql.format(table="shadow_learning_buffer"), "shadow", int(days)
        )
    except Exception as e:
        log.warning("shadow_vs_live divergence query failed: %s", e)
        out["error"] = f"{type(e).__name__}: {e}"[:240]
        return out

    by_league: dict[str, dict[str, list]] = {}
    for r in list(live_rows) + list(shadow_rows):
        league = (r["league"] or "").strip().upper()
        if not league:
            continue
        bucket = by_league.setdefault(
            league, {"live_p": [], "live_y": [], "shadow_p": [], "shadow_y": []}
        )
        if r["source"] == "live":
            bucket["live_p"].append(float(r["predicted_prob"]))
            bucket["live_y"].append(int(r["actual_outcome"]))
        else:
            bucket["shadow_p"].append(float(r["predicted_prob"]))
            bucket["shadow_y"].append(int(r["actual_outcome"]))

    items: list[dict[str, Any]] = []
    too_small: list[dict[str, Any]] = []
    for league in sorted(by_league):
        buck = by_league[league]
        n_live = len(buck["live_p"])
        n_shadow = len(buck["shadow_p"])
        if n_live < min_samples or n_shadow < min_samples:
            too_small.append(
                {
                    "league": league,
                    "n_live": n_live,
                    "n_shadow": n_shadow,
                    "min_samples_per_side": int(min_samples),
                }
            )
            continue
        b_live = _brier(buck["live_p"], buck["live_y"])
        b_shadow = _brier(buck["shadow_p"], buck["shadow_y"])
        if b_live is None or b_shadow is None:
            continue
        delta = abs(b_shadow - b_live)
        items.append(
            {
                "league": league,
                "n_live": n_live,
                "n_shadow": n_shadow,
                "brier_live": round(b_live, 6),
                "brier_shadow": round(b_shadow, 6),
                "abs_delta": round(delta, 6),
            }
        )
    items.sort(key=lambda it: it["abs_delta"], reverse=True)
    out["items"] = items
    out["samples_too_small"] = too_small
    return out


def _classify_drift(
    *,
    brier_after: float | None,
    drift_status: str | None,
    age_hours: float | None,
) -> tuple[str, str]:
    """Return ``(tone, why)`` — tone in ``{"green","yellow","red"}``.

    Order matters: RED beats YELLOW beats GREEN.  Thresholds match the
    runbook section 21 documentation so what the operator reads in the
    report is exactly what they read on the runbook page.
    """
    why: list[str] = []
    is_red = False
    is_yellow = False

    status = (drift_status or "").strip().lower()
    if status in ("rolled_back", "fitting_failed", "drift_breach"):
        is_red = True
        why.append(f"drift_status={status}")
    elif status == "drift_warning":
        is_yellow = True
        why.append("drift_status=drift_warning")

    if brier_after is not None:
        if brier_after > 0.25:
            is_red = True
            why.append(f"brier_after={brier_after:.4f}>0.25")
        elif brier_after > 0.20:
            is_yellow = True
            why.append(f"brier_after={brier_after:.4f}>0.20")

    if age_hours is not None:
        if age_hours > 14 * 24:
            is_red = True
            why.append(f"age_hours={age_hours:.0f}>336")
        elif age_hours > 72:
            is_yellow = True
            why.append(f"age_hours={age_hours:.0f}>72")

    if is_red:
        return "red", "; ".join(why) or "red threshold tripped"
    if is_yellow:
        return "yellow", "; ".join(why) or "yellow threshold tripped"
    return "green", "ok"


async def collect_calibrator_drift_status() -> dict[str, Any]:
    """One row per supported per-league calibrator with green/yellow/red tone."""
    out: dict[str, Any] = {"items": [], "error": None}
    try:
        rows = await db.fetch(
            """
            SELECT DISTINCT ON (sub_category)
                   sub_category,
                   role,
                   sample_count,
                   brier_score::float                   AS brier_score,
                   config,
                   drift_status,
                   to_char(created_at,
                           'YYYY-MM-DD"T"HH24:MI:SSZ') AS fit_at,
                   extract(
                     epoch from (now() - created_at)
                   )::float                              AS age_seconds
              FROM calibration_snapshots
             WHERE sub_category LIKE 'per_league:%'
               AND COALESCE(role, 'champion') = 'champion'
             ORDER BY sub_category, created_at DESC
            """
        )
    except Exception as e:
        log.warning("calibrator_drift query failed: %s", e)
        out["error"] = f"{type(e).__name__}: {e}"[:240]
        return out

    def _parse_jsonb(v: Any) -> dict[str, Any]:
        if v is None:
            return {}
        if isinstance(v, dict):
            return v
        if isinstance(v, (str, bytes, bytearray)):
            try:
                p = _json.loads(v)
                return p if isinstance(p, dict) else {}
            except (ValueError, TypeError):
                return {}
        return {}

    items: list[dict[str, Any]] = []
    for r in rows:
        sub = str(r["sub_category"] or "")
        if not sub.startswith("per_league:"):
            continue
        league = sub.split(":", 1)[1].strip().upper()
        cfg = _parse_jsonb(r["config"])
        drift = _parse_jsonb(r["drift_status"])
        brier_after = cfg.get("brier_after")
        if brier_after is None and r["brier_score"] is not None:
            brier_after = float(r["brier_score"])
        try:
            brier_after = float(brier_after) if brier_after is not None else None
        except (TypeError, ValueError):
            brier_after = None
        age_hours = None
        if r["age_seconds"] is not None:
            age_hours = float(r["age_seconds"]) / 3600.0
        tone, why = _classify_drift(
            brier_after=brier_after,
            drift_status=str(drift.get("status") or "") or None,
            age_hours=age_hours,
        )
        items.append(
            {
                "league": league,
                "tone": tone,
                "why": why,
                "brier_after": (
                    round(brier_after, 6) if brier_after is not None else None
                ),
                "drift_status": str(drift.get("status") or "") or None,
                "sample_count": (
                    int(r["sample_count"])
                    if r["sample_count"] is not None
                    else None
                ),
                "fit_at": r["fit_at"],
                "age_hours": (
                    round(age_hours, 2) if age_hours is not None else None
                ),
            }
        )
    items.sort(key=lambda it: ({"red": 0, "yellow": 1, "green": 2}[it["tone"]], it["league"]))
    out["items"] = items
    return out


async def collect_tail_risk_summary() -> dict[str, Any]:
    """Headline numbers from the existing tail-risk dashboard tile.

    Now also surfaces the per-bucket totals so the PNG chart helper
    (G2) can draw a bar per bucket without a second DB round-trip.
    """
    out: dict[str, Any] = {
        "tail_max_loss_usd": None,
        "total_max_loss_usd": None,
        "pct_of_total": None,
        "tone": None,
        "threshold_price": None,
        "source": None,
        "buckets": [],
        "error": None,
    }
    try:
        from .. import dashboard as _dashboard  # local — avoid import cycles

        tile = await _dashboard.get_tail_risk_exposure(days=30)
    except Exception as e:
        log.warning("tail_risk_summary failed: %s", e)
        out["error"] = f"{type(e).__name__}: {e}"[:240]
        return out
    tc = (tile or {}).get("tail_concentration") or {}
    out["tail_max_loss_usd"] = tc.get("tail_max_loss_usd")
    out["total_max_loss_usd"] = tc.get("total_max_loss_usd")
    out["pct_of_total"] = tc.get("pct")
    out["tone"] = tc.get("tone")
    out["threshold_price"] = tc.get("threshold_price")
    out["source"] = (tile or {}).get("source")
    # Slim copy of the bucket rollup the chart needs — label, lo/hi
    # boundary, is_tail flag, and the open-position max-loss total.
    buckets = []
    for b in (tile or {}).get("buckets") or []:
        try:
            totals = b.get("totals") or {}
            buckets.append(
                {
                    "label": str(b.get("label") or ""),
                    "lo": float(b.get("lo") or 0.0),
                    "hi": float(b.get("hi") or 0.0),
                    "is_tail": bool(b.get("is_tail")),
                    "open_max_loss_usd": float(
                        totals.get("open_max_loss_usd") or 0.0
                    ),
                    "open_count": int(totals.get("open_count") or 0),
                }
            )
        except (TypeError, ValueError):
            continue
    out["buckets"] = buckets
    return out


async def collect_gate_trips_24h() -> dict[str, Any]:
    """Count of `risk_gate_trips` rows per gate over the last 24 hours."""
    out: dict[str, Any] = {"items": [], "total": 0, "error": None}
    try:
        rows = await db.fetch(
            """
            SELECT gate_name,
                   count(*)::int                                AS n,
                   to_char(max(created_at),
                           'YYYY-MM-DD"T"HH24:MI:SSZ')          AS last_at
              FROM risk_gate_trips
             WHERE created_at > now() - interval '24 hours'
             GROUP BY gate_name
             ORDER BY n DESC, gate_name ASC
            """
        )
    except Exception as e:
        log.warning("gate_trips_24h query failed: %s", e)
        out["error"] = f"{type(e).__name__}: {e}"[:240]
        return out
    items = [
        {
            "gate_name": r["gate_name"],
            "count": int(r["n"]),
            "last_at": r["last_at"],
        }
        for r in rows
    ]
    out["items"] = items
    out["total"] = sum(it["count"] for it in items)
    return out


# ---------------------------------------------------------------------------
# Top-level aggregator + dispatcher
# ---------------------------------------------------------------------------


async def collect_latest_investor_deck() -> dict[str, Any]:
    """Look up the most recent ``v1_backtest_gate_runs`` row whose
    ``notes`` JSON carries an ``investor_deck_path`` and return the
    deck location + the run timestamp.

    Wired by F762: the weekly cron auto-builds the deck and stamps the
    path into ``notes``; the daily honesty report surfaces a clickable
    link so the operator can grab a fresh PPTX without rummaging
    through the reports directory.
    """
    try:
        rows = await db.fetch(
            """
            SELECT to_char(run_at AT TIME ZONE 'UTC',
                           'YYYY-MM-DD"T"HH24:MI:SSZ') AS run_at_iso,
                   notes
              FROM v1_backtest_gate_runs
             WHERE notes IS NOT NULL
             ORDER BY run_at DESC
             LIMIT 5
            """,
        )
    except Exception as e:
        log.warning("investor deck lookup failed: %s", e)
        return {"error": str(e)[:240]}

    import json as _json

    for r in rows:
        notes_raw = r["notes"]
        if not notes_raw:
            continue
        try:
            payload = _json.loads(notes_raw) if isinstance(
                notes_raw, (str, bytes, bytearray)
            ) else notes_raw
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        path = payload.get("investor_deck_path")
        if not path:
            continue
        return {
            "investor_deck_path": str(path),
            "built_at": r["run_at_iso"],
        }
    return {"investor_deck_path": None, "built_at": None}


async def collect_report(date_utc: str) -> dict[str, Any]:
    """Build the four-section payload.  Sections that fail are marked
    with an ``error`` field but never raise."""
    div_t = asyncio.create_task(collect_shadow_vs_live_divergence())
    drift_t = asyncio.create_task(collect_calibrator_drift_status())
    tail_t = asyncio.create_task(collect_tail_risk_summary())
    gate_t = asyncio.create_task(collect_gate_trips_24h())
    deck_t = asyncio.create_task(collect_latest_investor_deck())
    divergence, drift_status, tail_risk, gate_trips, investor_deck = (
        await asyncio.gather(div_t, drift_t, tail_t, gate_t, deck_t)
    )
    return {
        "date_utc": date_utc,
        "divergence": divergence,
        "calibrator_drift": drift_status,
        "tail_risk": tail_risk,
        "gate_trips_24h": gate_trips,
        "investor_deck": investor_deck,
    }


def _fmt_usd(v: Any) -> str:
    if v is None:
        return "(n/a)"
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return str(v)


def _fmt_pct(v: Any) -> str:
    if v is None:
        return "(n/a)"
    try:
        return f"{float(v) * 100:.1f}%" if abs(float(v)) <= 1.0 else f"{float(v):.1f}%"
    except (TypeError, ValueError):
        return str(v)


def _divergence_bar(delta: float, *, width: int = 24, hi: float = 0.10) -> str:
    """Tiny ASCII bar for the divergence chart.  ``hi`` is full-scale."""
    if delta is None or delta != delta or delta < 0:
        return "·" * width
    filled = min(width, int(round((delta / hi) * width)))
    return "█" * filled + "·" * (width - filled)


def _dashboard_url(path: str) -> str | None:
    """Optional one-click deep-link to the live tile.

    Surfaced under the tail-risk section as a complement to the PNG
    snapshot we now ship as an attachment (see
    :func:`render_tail_risk_chart_png`).  Returns ``None`` when
    ``DASHBOARD_BASE_URL`` isn't configured.
    """
    base = os.environ.get("DASHBOARD_BASE_URL") or ""
    base = base.strip().rstrip("/")
    if not base:
        return None
    if not path.startswith("/"):
        path = "/" + path
    return f"{base}{path}"


# ---------------------------------------------------------------------------
# G2 — PNG tail-risk tile snapshot.
#
# We render with matplotlib's Agg backend (no headless browser, no
# X server) so the cron container stays lean.  The tile is a single
# horizontal bar chart of per-bucket open_max_loss_usd, with the tail
# bucket highlighted in red and a one-line caption summarising the
# headline tail %.
# ---------------------------------------------------------------------------


def _ensure_reports_dir() -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    return REPORTS_DIR


def render_tail_risk_chart_png(
    tail_summary: dict[str, Any],
    *,
    date_str: str,
    output_dir: Path | None = None,
) -> Path | None:
    """Render the tail-risk tile to a PNG file and return its path.

    Returns ``None`` (no exception) when:
      * matplotlib isn't installed,
      * the tail-risk section carries an ``error``, or
      * there are zero non-zero buckets to plot.
    """
    if not tail_summary or tail_summary.get("error"):
        return None
    buckets = list(tail_summary.get("buckets") or [])
    if not buckets:
        return None
    plottable = [
        b for b in buckets if (b.get("open_max_loss_usd") or 0.0) > 0.0
    ]
    if not plottable:
        return None

    try:
        import matplotlib  # noqa: WPS433 — local import keeps cold start cheap
        # Use ``force=False`` so we don't blow up when another caller in
        # the same process (a debug script, a different report) already
        # bound a backend — matplotlib raises ValueError on a second
        # ``.use()`` without ``force``. Agg is also the global default
        # in our containers, so the no-op-when-already-bound case is
        # the common path.
        if matplotlib.get_backend().lower() != "agg":
            matplotlib.use("Agg", force=False)
        import matplotlib.pyplot as plt
    except Exception as e:  # pragma: no cover — install-time issue
        log.warning("tail-risk chart skipped: matplotlib unavailable (%s)", e)
        return None

    out_dir = output_dir or _ensure_reports_dir()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:  # pragma: no cover — disk full / RO mount
        log.warning("tail-risk chart: cannot create %s (%s)", out_dir, e)
        return None
    out_path = out_dir / f"tail_risk_tile_{date_str}.png"

    labels = [str(b.get("label") or "?") for b in plottable]
    values = [float(b.get("open_max_loss_usd") or 0.0) for b in plottable]
    is_tail = [bool(b.get("is_tail")) for b in plottable]
    counts = [int(b.get("open_count") or 0) for b in plottable]
    colors = ["#b00000" if t else "#1565c0" for t in is_tail]

    tail_pct = tail_summary.get("pct_of_total")
    tone = (tail_summary.get("tone") or "").lower()
    tone_color = {
        "red": "#b00000",
        "yellow": "#b07e00",
        "green": "#0a7d0a",
    }.get(tone, "#444")

    try:
        fig, ax = plt.subplots(figsize=(8.5, 4.6), dpi=150)
        y_pos = list(range(len(labels)))
        ax.barh(y_pos, values, color=colors, edgecolor="#333", linewidth=0.5)
        ax.set_yticks(y_pos)
        ax.set_yticklabels(labels)
        ax.invert_yaxis()
        ax.set_xlabel("open max loss (USD)")
        for i, (v, n) in enumerate(zip(values, counts)):
            ax.text(
                v,
                i,
                f"  ${v:,.0f}  (n={n})",
                va="center",
                ha="left",
                fontsize=8,
                color="#222",
            )
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        # Headline + sub-caption.
        tail_usd = tail_summary.get("tail_max_loss_usd") or 0.0
        total_usd = tail_summary.get("total_max_loss_usd") or 0.0
        pct_str = (
            f"{(float(tail_pct) * 100):.1f}%"
            if isinstance(tail_pct, (int, float))
            else "(n/a)"
        )
        title = (
            f"Tail-risk concentration — {date_str}   "
            f"tail=${tail_usd:,.0f} / total=${total_usd:,.0f}  "
            f"({pct_str} in tail)"
        )
        ax.set_title(title, fontsize=11, color=tone_color, loc="left")
        thr = tail_summary.get("threshold_price")
        if thr is not None:
            try:
                fig.text(
                    0.99,
                    0.01,
                    f"tail bucket = price < {float(thr):.3f}   "
                    f"tone = {tone or '(n/a)'}",
                    ha="right",
                    fontsize=8,
                    color="#666",
                )
            except (TypeError, ValueError):
                pass
        fig.tight_layout()
        fig.savefig(out_path, format="png", bbox_inches="tight")
        plt.close(fig)
    except Exception as e:
        log.warning("tail-risk chart render failed: %s", e)
        return None
    return out_path


# ---------------------------------------------------------------------------
# G3 — PDF artifact.
#
# Built with reportlab.  We keep the layout deliberately spartan
# (heading + paragraphs + tables + the embedded PNG) so the cron
# container doesn't need any extra fonts or templates installed.
# ---------------------------------------------------------------------------


def render_daily_report_pdf(
    payload: dict[str, Any],
    *,
    text_body: str,
    chart_path: Path | None = None,
    output_dir: Path | None = None,
) -> Path | None:
    """Build a multi-page PDF of the daily honesty report.

    Returns ``None`` when reportlab isn't installed (logged once).
    Never raises — the email still goes out with the text body even if
    the PDF fails to render.
    """
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import inch
        from reportlab.platypus import (
            Image,
            PageBreak,
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )
    except Exception as e:  # pragma: no cover — install-time issue
        log.warning("daily-report PDF skipped: reportlab unavailable (%s)", e)
        return None

    date_str = str(payload.get("date_utc") or "unknown")
    out_dir = output_dir or _ensure_reports_dir()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:  # pragma: no cover — disk full / RO mount
        log.warning("daily-report PDF: cannot create %s (%s)", out_dir, e)
        return None
    out_path = out_dir / f"daily_honesty_{date_str}.pdf"

    try:
        styles = getSampleStyleSheet()
        h1 = styles["Heading1"]
        h2 = styles["Heading2"]
        body_style = ParagraphStyle(
            "body",
            parent=styles["BodyText"],
            fontName="Helvetica",
            fontSize=9,
            leading=12,
        )
        mono = ParagraphStyle(
            "mono",
            parent=styles["BodyText"],
            fontName="Courier",
            fontSize=8,
            leading=10,
        )
        small = ParagraphStyle(
            "small",
            parent=styles["BodyText"],
            fontName="Helvetica",
            fontSize=8,
            leading=10,
            textColor=colors.HexColor("#666666"),
        )

        story: list[Any] = []
        story.append(Paragraph(f"Axiom daily honesty report — {date_str}", h1))
        story.append(
            Paragraph(
                "Operator-visible signals — divergence, calibrator drift, "
                "tail-risk concentration, gate trips, and the latest "
                "investor deck.  See runbook §21 for what each red signal "
                "means.",
                body_style,
            )
        )
        story.append(Spacer(1, 0.2 * inch))

        # Section 1: divergence
        div = payload.get("divergence") or {}
        story.append(
            Paragraph(
                f"1. Shadow-vs-live divergence (last "
                f"{div.get('days', '?')}d, |Brier delta| per league)",
                h2,
            )
        )
        if div.get("error"):
            story.append(
                Paragraph(f"<b>error:</b> {div['error']}", body_style)
            )
        else:
            items = list(div.get("items") or [])
            if items:
                tbl_data = [["League", "|Δ|", "Brier (live)", "n live", "Brier (shadow)", "n shadow"]]
                for it in items:
                    tbl_data.append(
                        [
                            it.get("league", "?"),
                            f"{it.get('abs_delta', 0):.4f}",
                            f"{it.get('brier_live', 0):.4f}",
                            str(it.get("n_live", 0)),
                            f"{it.get('brier_shadow', 0):.4f}",
                            str(it.get("n_shadow", 0)),
                        ]
                    )
                tbl = Table(tbl_data, hAlign="LEFT")
                tbl.setStyle(
                    TableStyle(
                        [
                            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f0f0f0")),
                            ("FONTNAME", (0, 0), (-1, -1), "Courier"),
                            ("FONTSIZE", (0, 0), (-1, -1), 8),
                            ("BOX", (0, 0), (-1, -1), 0.25, colors.grey),
                            ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                            ("LEFTPADDING", (0, 0), (-1, -1), 4),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                        ]
                    )
                )
                story.append(tbl)
            ts = div.get("samples_too_small") or []
            if ts:
                story.append(Spacer(1, 0.1 * inch))
                story.append(
                    Paragraph(
                        "<i>Insufficient samples (skipped):</i> "
                        + ", ".join(it["league"] for it in ts),
                        body_style,
                    )
                )
            if not items and not ts:
                story.append(
                    Paragraph(
                        "no settled rows on either side yet", body_style
                    )
                )
        story.append(Spacer(1, 0.2 * inch))

        # Section 2: calibrator drift
        cd = payload.get("calibrator_drift") or {}
        story.append(Paragraph("2. Calibrator drift status", h2))
        if cd.get("error"):
            story.append(
                Paragraph(f"<b>error:</b> {cd['error']}", body_style)
            )
        else:
            items = list(cd.get("items") or [])
            if items:
                tbl_data = [["Tone", "League", "Brier", "Age (h)", "Why"]]
                tone_color_hex = {
                    "green": colors.HexColor("#0a7d0a"),
                    "yellow": colors.HexColor("#b07e00"),
                    "red": colors.HexColor("#b00000"),
                }
                style_cmds: list[Any] = [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f0f0f0")),
                    ("FONTNAME", (0, 0), (-1, -1), "Courier"),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("BOX", (0, 0), (-1, -1), 0.25, colors.grey),
                    ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ]
                for i, it in enumerate(items, start=1):
                    tone = (it.get("tone") or "").lower()
                    color = tone_color_hex.get(tone)
                    if color is not None:
                        style_cmds.append(("TEXTCOLOR", (0, i), (0, i), color))
                    brier_str = (
                        f"{it['brier_after']:.4f}"
                        if it.get("brier_after") is not None
                        else "(n/a)"
                    )
                    age_str = (
                        f"{it['age_hours']:.1f}"
                        if it.get("age_hours") is not None
                        else "(n/a)"
                    )
                    tbl_data.append(
                        [
                            tone.upper() or "?",
                            it.get("league", "?"),
                            brier_str,
                            age_str,
                            it.get("why", ""),
                        ]
                    )
                tbl = Table(tbl_data, hAlign="LEFT")
                tbl.setStyle(TableStyle(style_cmds))
                story.append(tbl)
            else:
                story.append(
                    Paragraph(
                        "no per-league calibrator snapshots yet",
                        body_style,
                    )
                )
        story.append(Spacer(1, 0.2 * inch))

        # Section 3: tail-risk + embedded chart
        tr = payload.get("tail_risk") or {}
        story.append(Paragraph("3. Tail-risk concentration (last 30d)", h2))
        if tr.get("error"):
            story.append(
                Paragraph(f"<b>error:</b> {tr['error']}", body_style)
            )
        else:
            facts = [
                ("tail_max_loss", _fmt_usd(tr.get("tail_max_loss_usd"))),
                ("total_max_loss", _fmt_usd(tr.get("total_max_loss_usd"))),
                ("pct_in_tail", _fmt_pct(tr.get("pct_of_total"))),
                ("tone", str(tr.get("tone") or "(n/a)")),
                (
                    "threshold_price",
                    str(tr.get("threshold_price") or "(n/a)"),
                ),
                ("source", str(tr.get("source") or "(n/a)")),
            ]
            tbl = Table(facts, hAlign="LEFT")
            tbl.setStyle(
                TableStyle(
                    [
                        ("FONTNAME", (0, 0), (-1, -1), "Courier"),
                        ("FONTSIZE", (0, 0), (-1, -1), 9),
                        ("FONTNAME", (0, 0), (0, -1), "Courier-Bold"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 2),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ]
                )
            )
            story.append(tbl)
            if chart_path is not None and Path(chart_path).is_file():
                try:
                    story.append(Spacer(1, 0.15 * inch))
                    story.append(
                        Image(str(chart_path), width=6.5 * inch, height=3.5 * inch)
                    )
                except Exception as e:  # pragma: no cover
                    log.warning("PDF chart embed failed: %s", e)
        story.append(PageBreak())

        # Section 4: gate trips
        gt = payload.get("gate_trips_24h") or {}
        story.append(Paragraph("4. Risk-gate trips (last 24h)", h2))
        if gt.get("error"):
            story.append(
                Paragraph(f"<b>error:</b> {gt['error']}", body_style)
            )
        elif not gt.get("items"):
            story.append(
                Paragraph(
                    "no gate trips in the last 24 hours", body_style
                )
            )
        else:
            story.append(
                Paragraph(
                    f"<b>total trips:</b> {gt.get('total', 0)}",
                    body_style,
                )
            )
            tbl_data = [["Gate", "Count", "Last at"]]
            for it in gt["items"]:
                tbl_data.append(
                    [
                        it.get("gate_name", "?"),
                        str(it.get("count", 0)),
                        str(it.get("last_at") or "(n/a)"),
                    ]
                )
            tbl = Table(tbl_data, hAlign="LEFT")
            tbl.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f0f0f0")),
                        ("FONTNAME", (0, 0), (-1, -1), "Courier"),
                        ("FONTSIZE", (0, 0), (-1, -1), 8),
                        ("BOX", (0, 0), (-1, -1), 0.25, colors.grey),
                        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                        ("LEFTPADDING", (0, 0), (-1, -1), 4),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                    ]
                )
            )
            story.append(tbl)
        story.append(Spacer(1, 0.2 * inch))

        # Investor deck deep-link
        deck = payload.get("investor_deck") or {}
        story.append(Paragraph("Latest investor deck", h2))
        if deck.get("error"):
            story.append(
                Paragraph(
                    f"lookup failed: {deck['error']}", body_style
                )
            )
        elif deck.get("investor_deck_path"):
            built = str(deck.get("built_at") or "(unknown)")
            path_s = str(deck["investor_deck_path"])
            story.append(Paragraph(f"<b>{path_s}</b>", mono))
            story.append(Paragraph(f"built {built}", small))
        else:
            story.append(
                Paragraph(
                    "not built yet — weekly cron will produce one on the "
                    "next run.",
                    small,
                )
            )

        story.append(Spacer(1, 0.3 * inch))
        story.append(
            Paragraph(
                "Read the runbook §21 for what each red signal means. "
                "Generated by app.notifications.daily_honesty_report.",
                small,
            )
        )

        doc = SimpleDocTemplate(
            str(out_path),
            pagesize=letter,
            leftMargin=0.6 * inch,
            rightMargin=0.6 * inch,
            topMargin=0.6 * inch,
            bottomMargin=0.6 * inch,
            title=f"Axiom daily honesty report — {date_str}",
            author="axiom.notifications.daily_honesty_report",
        )
        doc.build(story)
    except Exception as e:
        log.warning("daily-report PDF render failed: %s", e)
        return None
    return out_path


def render_text(payload: dict[str, Any]) -> tuple[str, str]:
    """Render ``(subject, body)`` for the operator broadcast."""
    date_utc = payload.get("date_utc") or "(unknown)"
    subject = f"[axiom] Daily honesty report {date_utc}"

    lines: list[str] = []

    # Section 1 — divergence (with ASCII bar chart per league)
    div = payload.get("divergence") or {}
    lines.append("== 1. Shadow-vs-live divergence (last "
                 f"{div.get('days', '?')}d, |Brier delta| per league) ==")
    if div.get("error"):
        lines.append(f"  ERROR: {div['error']}")
    elif not div.get("items") and not div.get("samples_too_small"):
        lines.append("  no settled rows on either side yet")
    else:
        items = list(div.get("items") or [])
        if items:
            lines.append("  chart (full scale = 0.10 |Δbrier|):")
            for it in items:
                lines.append(
                    f"    {it['league']:>8s} "
                    f"|{_divergence_bar(it['abs_delta'])}| "
                    f"Δ={it['abs_delta']:.4f}"
                )
            lines.append("")
            lines.append("  detail:")
        for it in items:
            lines.append(
                f"  {it['league']:>8s}  Δ={it['abs_delta']:.4f}  "
                f"live={it['brier_live']:.4f} (n={it['n_live']})  "
                f"shadow={it['brier_shadow']:.4f} (n={it['n_shadow']})"
            )
        for it in div.get("samples_too_small") or []:
            lines.append(
                f"  {it['league']:>8s}  insufficient samples "
                f"(live={it['n_live']}, shadow={it['n_shadow']}, "
                f"min={it['min_samples_per_side']})"
            )
    lines.append("")

    # Section 2 — calibrator drift
    cd = payload.get("calibrator_drift") or {}
    lines.append("== 2. Calibrator drift status (per league) ==")
    if cd.get("error"):
        lines.append(f"  ERROR: {cd['error']}")
    elif not cd.get("items"):
        lines.append("  no per-league calibrator snapshots yet")
    else:
        for it in cd["items"]:
            tone_marker = {"green": "[GREEN] ", "yellow": "[YELLOW]", "red": "[RED]   "}.get(
                it["tone"], "[?]     "
            )
            brier_str = (
                f"{it['brier_after']:.4f}"
                if it.get("brier_after") is not None
                else "(n/a)"
            )
            age_str = (
                f"{it['age_hours']:.1f}h"
                if it.get("age_hours") is not None
                else "(n/a)"
            )
            lines.append(
                f"  {tone_marker}  {it['league']:>8s}  "
                f"brier={brier_str}  age={age_str}  why={it['why']}"
            )
    lines.append("")

    # Section 3 — tail-risk
    tr = payload.get("tail_risk") or {}
    lines.append("== 3. Tail-risk concentration (last 30d) ==")
    if tr.get("error"):
        lines.append(f"  ERROR: {tr['error']}")
    else:
        lines.append(
            f"  tail_max_loss     = {_fmt_usd(tr.get('tail_max_loss_usd'))}"
        )
        lines.append(
            f"  total_max_loss    = {_fmt_usd(tr.get('total_max_loss_usd'))}"
        )
        lines.append(
            f"  pct_in_tail       = {_fmt_pct(tr.get('pct_of_total'))}"
        )
        lines.append(f"  tone              = {tr.get('tone') or '(n/a)'}")
        lines.append(
            f"  threshold_price   = {tr.get('threshold_price') or '(n/a)'}"
        )
        lines.append(f"  source            = {tr.get('source') or '(n/a)'}")
        dash_url = _dashboard_url("/dashboard/risk")
        if dash_url:
            lines.append(f"  live tile         = {dash_url}")
    lines.append("")

    # Section 4 — gate trips
    gt = payload.get("gate_trips_24h") or {}
    lines.append("== 4. Risk-gate trips (last 24h) ==")
    if gt.get("error"):
        lines.append(f"  ERROR: {gt['error']}")
    elif not gt.get("items"):
        lines.append("  no gate trips in the last 24 hours")
    else:
        lines.append(f"  total trips: {gt.get('total', 0)}")
        for it in gt["items"]:
            lines.append(
                f"  {it['gate_name']:<30s}  count={it['count']:>4d}  "
                f"last={it.get('last_at') or '(n/a)'}"
            )
    lines.append("")

    # Latest investor deck (F762)
    deck = payload.get("investor_deck") or {}
    if deck.get("error"):
        lines.append(
            f"Latest investor deck: lookup failed — {deck['error']}"
        )
    elif deck.get("investor_deck_path"):
        built = deck.get("built_at") or "(unknown)"
        lines.append(
            f"Latest investor deck: {deck['investor_deck_path']}  "
            f"(built {built})"
        )
    else:
        lines.append(
            "Latest investor deck: not built yet — weekly cron will "
            "produce one on the next run."
        )

    lines.append("")
    lines.append(
        "Read the runbook section 21 for what each red signal means."
    )
    body = "\n".join(lines) + "\n"
    return subject, body


def render_html(payload: dict[str, Any]) -> str:
    """Minimal-but-readable HTML body for inboxes that prefer rich text."""
    import html

    date_utc = html.escape(str(payload.get("date_utc") or ""))
    parts: list[str] = [
        "<html><body style=\"font-family:-apple-system,BlinkMacSystemFont,"
        "'Segoe UI',Roboto,Helvetica,Arial,sans-serif;max-width:760px;\">",
        f"<h2>Axiom daily honesty report — {date_utc}</h2>",
    ]

    def _section_h(title: str) -> None:
        parts.append(
            f"<h3 style=\"margin-top:24px;border-bottom:1px solid #ddd;"
            f"padding-bottom:4px;\">{html.escape(title)}</h3>"
        )

    def _err(s: dict[str, Any]) -> bool:
        if s.get("error"):
            parts.append(
                f"<p style=\"color:#b00;\">error: "
                f"{html.escape(str(s['error']))}</p>"
            )
            return True
        return False

    div = payload.get("divergence") or {}
    _section_h(f"Shadow-vs-live divergence (last {div.get('days', '?')}d)")
    if not _err(div):
        items = div.get("items") or []
        if items:
            parts.append(
                "<table cellpadding=4 style=\"border-collapse:collapse;"
                "font-family:ui-monospace,SFMono-Regular,Menlo,monospace;"
                "font-size:12px;\">"
            )
            parts.append(
                "<tr style=\"background:#f0f0f0;\">"
                "<th>League</th><th>|Δ|</th><th>Brier (live)</th>"
                "<th>n live</th><th>Brier (shadow)</th><th>n shadow</th></tr>"
            )
            for it in items:
                parts.append(
                    f"<tr><td>{html.escape(it['league'])}</td>"
                    f"<td>{it['abs_delta']:.4f}</td>"
                    f"<td>{it['brier_live']:.4f}</td>"
                    f"<td>{it['n_live']}</td>"
                    f"<td>{it['brier_shadow']:.4f}</td>"
                    f"<td>{it['n_shadow']}</td></tr>"
                )
            parts.append("</table>")
        ts = div.get("samples_too_small") or []
        if ts:
            parts.append(
                f"<p><i>Insufficient samples (skipped):</i> "
                f"{html.escape(', '.join(it['league'] for it in ts))}</p>"
            )
        if not items and not ts:
            parts.append("<p>no settled rows on either side yet</p>")

    cd = payload.get("calibrator_drift") or {}
    _section_h("Calibrator drift status")
    if not _err(cd):
        items = cd.get("items") or []
        if items:
            parts.append(
                "<table cellpadding=4 style=\"border-collapse:collapse;"
                "font-family:ui-monospace,SFMono-Regular,Menlo,monospace;"
                "font-size:12px;\">"
            )
            parts.append(
                "<tr style=\"background:#f0f0f0;\">"
                "<th>Tone</th><th>League</th><th>Brier</th><th>Age (h)</th>"
                "<th>Why</th></tr>"
            )
            tone_color = {
                "green": "#0a7d0a",
                "yellow": "#b07e00",
                "red": "#b00000",
            }
            for it in items:
                color = tone_color.get(it["tone"], "#444")
                brier_str = (
                    f"{it['brier_after']:.4f}"
                    if it.get("brier_after") is not None
                    else "(n/a)"
                )
                age_str = (
                    f"{it['age_hours']:.1f}"
                    if it.get("age_hours") is not None
                    else "(n/a)"
                )
                parts.append(
                    f"<tr><td style=\"color:{color};font-weight:600;\">"
                    f"{html.escape(it['tone'].upper())}</td>"
                    f"<td>{html.escape(it['league'])}</td>"
                    f"<td>{brier_str}</td>"
                    f"<td>{age_str}</td>"
                    f"<td>{html.escape(it['why'])}</td></tr>"
                )
            parts.append("</table>")
        else:
            parts.append("<p>no per-league calibrator snapshots yet</p>")

    tr = payload.get("tail_risk") or {}
    _section_h("Tail-risk concentration (last 30d)")
    if not _err(tr):
        rows = [
            ("tail_max_loss", _fmt_usd(tr.get("tail_max_loss_usd"))),
            ("total_max_loss", _fmt_usd(tr.get("total_max_loss_usd"))),
            ("pct_in_tail", _fmt_pct(tr.get("pct_of_total"))),
            ("tone", str(tr.get("tone") or "(n/a)")),
            ("threshold_price", str(tr.get("threshold_price") or "(n/a)")),
            ("source", str(tr.get("source") or "(n/a)")),
        ]
        parts.append(
            "<table cellpadding=4 style=\"border-collapse:collapse;"
            "font-family:ui-monospace,SFMono-Regular,Menlo,monospace;"
            "font-size:12px;\">"
        )
        for k, v in rows:
            parts.append(
                f"<tr><td style=\"font-weight:600;\">{html.escape(k)}</td>"
                f"<td>{html.escape(v)}</td></tr>"
            )
        parts.append("</table>")

    gt = payload.get("gate_trips_24h") or {}
    _section_h("Risk-gate trips (last 24h)")
    if not _err(gt):
        items = gt.get("items") or []
        if items:
            parts.append(
                f"<p>total trips: <b>{gt.get('total', 0)}</b></p>"
            )
            parts.append(
                "<table cellpadding=4 style=\"border-collapse:collapse;"
                "font-family:ui-monospace,SFMono-Regular,Menlo,monospace;"
                "font-size:12px;\">"
            )
            parts.append(
                "<tr style=\"background:#f0f0f0;\">"
                "<th>Gate</th><th>Count</th><th>Last at</th></tr>"
            )
            for it in items:
                parts.append(
                    f"<tr><td>{html.escape(it['gate_name'])}</td>"
                    f"<td>{it['count']}</td>"
                    f"<td>{html.escape(str(it.get('last_at') or '(n/a)'))}</td></tr>"
                )
            parts.append("</table>")
        else:
            parts.append("<p>no gate trips in the last 24 hours</p>")

    deck = payload.get("investor_deck") or {}
    _section_h("Latest investor deck")
    if deck.get("error"):
        parts.append(
            f"<p style=\"color:#b00;\">lookup failed: "
            f"{html.escape(str(deck['error']))}</p>"
        )
    elif deck.get("investor_deck_path"):
        built = html.escape(str(deck.get("built_at") or "(unknown)"))
        path = html.escape(str(deck["investor_deck_path"]))
        parts.append(
            f"<p style=\"font-family:ui-monospace,SFMono-Regular,Menlo,"
            f"monospace;font-size:12px;\">"
            f"<b>{path}</b><br/>"
            f"<span style=\"color:#666;\">built {built}</span></p>"
        )
    else:
        parts.append(
            "<p style=\"color:#666;font-size:12px;\">"
            "not built yet — weekly cron will produce one on the "
            "next run.</p>"
        )

    parts.append(
        "<p style=\"color:#666;font-size:12px;margin-top:24px;\">"
        "Read the runbook section 21 for what each red signal means.</p>"
    )
    parts.append("</body></html>")
    return "".join(parts)


async def fire_now(*, dry_run: bool = False) -> dict[str, Any]:
    """Build today's report and dispatch via Postmark + Telegram.

    Returns the rendered payload + dispatch result so the admin
    fire-now endpoint can echo it back to the operator for inspection.

    Side effects:
      * a PNG of the tail-risk tile is written to
        ``artifacts/api-server/reports/tail_risk_tile_<date>.png``
        when there's at least one non-zero bucket — **always**,
        including ``dry_run`` runs, so the operator can inspect the
        generated artifact alongside the rendered text body;
      * a multi-page PDF artifact is written to
        ``artifacts/api-server/reports/daily_honesty_<date>.pdf`` —
        also always, for the same reason.

    What ``dry_run`` actually gates:
      * Postmark email send (and the user-pref fanout).
      * Telegram text broadcast and PDF document push.

    Both attachment paths are echoed back in the return value so an
    admin fire-now operator can read them off disk for sanity-checking.
    """
    # Date stamp uses the configured local TZ so the artifact filename
    # matches the day the operator reads the report on.  (Backcompat:
    # the dispatch payload still surfaces a ``date_utc`` field with
    # the same value to avoid breaking any downstream consumer.)
    today = _local_today_str()
    payload = await collect_report(today)
    subject, body = render_text(payload)
    html_body = render_html(payload)

    # G2 — render the PNG tile snapshot (best-effort).
    chart_path: Path | None = None
    try:
        chart_path = render_tail_risk_chart_png(
            payload.get("tail_risk") or {}, date_str=today
        )
    except Exception as e:  # pragma: no cover — render is fail-soft
        log.warning("honesty-report chart render failed: %s", e)
        chart_path = None

    # G3 — render the PDF artifact (best-effort).
    pdf_path: Path | None = None
    try:
        pdf_path = render_daily_report_pdf(
            payload, text_body=body, chart_path=chart_path
        )
    except Exception as e:  # pragma: no cover — render is fail-soft
        log.warning("honesty-report PDF render failed: %s", e)
        pdf_path = None

    attachments: list[Path] = [p for p in (pdf_path, chart_path) if p]

    dispatch_result: dict[str, Any] = {}
    try:
        dispatch_result = alerts._dispatch(
            subject,
            body,
            tag=_DISPATCH_TAG,
            dry_run=dry_run,
            html_body=html_body,
            attachments=attachments or None,
        )
    except TypeError:
        # Older _dispatch signature (no `attachments` kwarg) — keep
        # working when this module is loaded ahead of the alerts
        # rebuild during a partial deploy.
        try:
            dispatch_result = alerts._dispatch(
                subject,
                body,
                tag=_DISPATCH_TAG,
                dry_run=dry_run,
                html_body=html_body,
            )
        except Exception as e:  # pragma: no cover
            log.warning("honesty-report dispatch failed: %s", e)
            dispatch_result = {"error": f"{type(e).__name__}: {e}"[:240]}
    except Exception as e:  # pragma: no cover — _dispatch already logs
        log.warning("honesty-report dispatch failed: %s", e)
        dispatch_result = {"error": f"{type(e).__name__}: {e}"[:240]}

    telegram_result: dict[str, Any] = {"text_broadcast": False, "pdf_sent": 0}
    if not dry_run:
        try:
            from ..telegram import notify as _tg_notify

            _tg_notify.broadcast_in_background(f"{subject}\n\n{body}")
            telegram_result["text_broadcast"] = True
        except Exception as e:  # pragma: no cover
            log.warning("honesty-report telegram broadcast failed: %s", e)
        if pdf_path is not None:
            try:
                telegram_result["pdf_sent"] = await _broadcast_document(
                    pdf_path, caption=subject
                )
            except Exception as e:  # pragma: no cover
                log.warning(
                    "honesty-report telegram document push failed: %s", e
                )

    return {
        "date_utc": today,
        "subject": subject,
        "body": body,
        "html_body": html_body,
        "payload": payload,
        "dispatch": dispatch_result,
        "dry_run": bool(dry_run),
        "artifacts": {
            "pdf_path": str(pdf_path) if pdf_path else None,
            "chart_path": str(chart_path) if chart_path else None,
        },
        "telegram": telegram_result,
    }


async def _broadcast_document(file_path: Path, *, caption: str) -> int:
    """Send a single file to every opted-in Telegram chat.

    Returns the count of chats the upload succeeded for.  Best-effort:
    a per-chat failure logs and is counted as 0, but never raises.
    """
    try:
        from ..telegram import notify as _tg_notify
        from ..telegram import bot as _tg_bot
    except Exception as e:  # pragma: no cover
        log.warning("telegram modules unavailable for document push: %s", e)
        return 0
    try:
        chat_ids = await _tg_notify.push_enabled_chat_ids()
    except Exception as e:  # pragma: no cover
        log.warning("push_enabled_chat_ids failed in document push: %s", e)
        return 0
    if not chat_ids:
        return 0
    results = await asyncio.gather(
        *(
            _tg_bot.send_document_to_chat(c, file_path, caption=caption)
            for c in chat_ids
        ),
        return_exceptions=True,
    )
    sent = 0
    for r in results:
        if r is True:
            sent += 1
    return sent


# ---------------------------------------------------------------------------
# Scheduler loop — minute-tick, fires once past the configured UTC time.
# ---------------------------------------------------------------------------


_task: asyncio.Task | None = None
_stop = asyncio.Event()
_last_sent_date: str | None = None


async def _loop() -> None:
    global _last_sent_date
    tz = _local_tz()
    log.info(
        "honesty-report scheduler started: %02d:%02d %s",
        _send_hour(),
        _send_minute(),
        getattr(tz, "key", str(tz)),
    )
    _last_sent_date = await _get_last_sent_date()
    while not _stop.is_set():
        try:
            now = _now_local()
            today = now.date().isoformat()
            target_hour = _send_hour()
            target_minute = _send_minute()
            past_target = (
                now.hour > target_hour
                or (now.hour == target_hour and now.minute >= target_minute)
            )
            if past_target and _last_sent_date != today:
                claimed = await _mark_sent(today)
                if not claimed:
                    _last_sent_date = today
                else:
                    log.info("firing daily honesty report for %s", today)
                    try:
                        await fire_now()
                        _last_sent_date = today
                    except Exception as e:
                        log.exception(
                            "honesty-report send failed: %s", e
                        )
                        error_log(
                            "daily_honesty_report",
                            f"{type(e).__name__}: {e}",
                        )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("honesty-report loop error: %s", e)
        try:
            await asyncio.wait_for(_stop.wait(), timeout=60.0)
        except asyncio.TimeoutError:
            pass
        else:
            return


async def start() -> None:
    global _task
    if not _enabled():
        log.info("honesty-report scheduler disabled")
        return
    if _task and not _task.done():
        return
    _stop.clear()
    _task = asyncio.create_task(_loop(), name="daily-honesty-report")


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
