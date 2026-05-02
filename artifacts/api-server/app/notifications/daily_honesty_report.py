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
from datetime import datetime, timezone
from typing import Any

from .. import db
from ..health import error_log
from . import alerts


log = logging.getLogger("axiom.notifications.daily_honesty_report")


# ---------------------------------------------------------------------------
# Env knobs (mirrors daily_summary.py for muscle-memory).
# ---------------------------------------------------------------------------


def _enabled() -> bool:
    raw = (os.environ.get("DAILY_HONESTY_REPORT_ENABLED") or "1").strip().lower()
    return raw in ("1", "true", "yes", "on", "y")


def _send_hour() -> int:
    try:
        return max(
            0, min(23, int(os.environ.get("DAILY_HONESTY_REPORT_HOUR_UTC") or "13"))
        )
    except (TypeError, ValueError):
        return 13


def _send_minute() -> int:
    try:
        return max(
            0, min(59, int(os.environ.get("DAILY_HONESTY_REPORT_MINUTE_UTC") or "0"))
        )
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
    """Headline numbers from the existing tail-risk dashboard tile."""
    out: dict[str, Any] = {
        "tail_max_loss_usd": None,
        "total_max_loss_usd": None,
        "pct_of_total": None,
        "tone": None,
        "threshold_price": None,
        "source": None,
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
    """Stand-in for the operator's screenshot — link to the live tile.

    A true PNG screenshot would require a headless-browser dependency we
    don't carry in the cron container; an authenticated link to the live
    dashboard delivers the same operator value with one click and stays
    correct as the tile evolves.
    """
    base = os.environ.get("DASHBOARD_BASE_URL") or ""
    base = base.strip().rstrip("/")
    if not base:
        return None
    if not path.startswith("/"):
        path = "/" + path
    return f"{base}{path}"


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
    """
    today = datetime.now(tz=timezone.utc).date().isoformat()
    payload = await collect_report(today)
    subject, body = render_text(payload)
    html_body = render_html(payload)
    dispatch_result: dict[str, Any] = {}
    try:
        dispatch_result = alerts._dispatch(
            subject,
            body,
            tag=_DISPATCH_TAG,
            dry_run=dry_run,
            html_body=html_body,
        )
    except Exception as e:  # pragma: no cover — _dispatch already logs
        log.warning("honesty-report dispatch failed: %s", e)
        dispatch_result = {"error": f"{type(e).__name__}: {e}"[:240]}
    if not dry_run:
        try:
            from ..telegram import notify as _tg_notify

            _tg_notify.broadcast_in_background(f"{subject}\n\n{body}")
        except Exception as e:  # pragma: no cover
            log.warning("honesty-report telegram broadcast failed: %s", e)
    return {
        "date_utc": today,
        "subject": subject,
        "body": body,
        "html_body": html_body,
        "payload": payload,
        "dispatch": dispatch_result,
        "dry_run": bool(dry_run),
    }


# ---------------------------------------------------------------------------
# Scheduler loop — minute-tick, fires once past the configured UTC time.
# ---------------------------------------------------------------------------


_task: asyncio.Task | None = None
_stop = asyncio.Event()
_last_sent_date: str | None = None


async def _loop() -> None:
    global _last_sent_date
    log.info(
        "honesty-report scheduler started: hour=%02d:%02d UTC",
        _send_hour(),
        _send_minute(),
    )
    _last_sent_date = await _get_last_sent_date()
    while not _stop.is_set():
        try:
            now = datetime.now(tz=timezone.utc)
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
