"""Churn-event log + daily operator digest.

Every user-visible billing-failure event (payment failed, cancellation
scheduled, subscription cancelled) is appended to ``billing_churn_events``
by ``email.py`` regardless of whether the user-facing email actually went
out (suppression / dedupe).  Once a day a digest of the last 24h is
mailed to operators so they can see what's happening without grepping
logs.

Configuration:
  - CHURN_DIGEST_ENABLED      (default: 1)
  - CHURN_DIGEST_HOUR_UTC     (default: 14 — ~10am ET, an hour after the
                                P&L summary so the two emails don't
                                arrive at the same minute)
  - CHURN_DIGEST_MINUTE_UTC   (default: 0)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from . import postmark


log = logging.getLogger("axiom.notifications.churn")


_table_ready = False
_lock = asyncio.Lock()


async def ensure_table() -> None:
    """Schema is now owned by the Alembic baseline migration; no-op."""
    global _table_ready
    _table_ready = True


async def record(
    *,
    event_type: str,
    user_id: str | None,
    email: str | None,
    details: dict[str, Any] | None = None,
    email_sent: bool = False,
    skipped_reason: str | None = None,
) -> None:
    await ensure_table()
    from .. import db
    try:
        await db.execute(
            """
            INSERT INTO billing_churn_events (
                event_type, user_id, email, email_sent, skipped_reason, details
            ) VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            """,
            event_type,
            user_id,
            (email or "").strip().lower() or None,
            bool(email_sent),
            skipped_reason,
            json.dumps(details or {}),
        )
    except Exception as e:
        log.warning("churn record failed (%s): %s", event_type, e)


# ---------------------------------------------------------------------------
# Digest
# ---------------------------------------------------------------------------


async def collect_digest(hours: int = 24) -> dict[str, Any]:
    """Aggregate the last `hours` of churn events."""
    await ensure_table()
    from .. import db
    out: dict[str, Any] = {
        "window_hours": hours,
        "totals": {"payment_failed": 0, "will_cancel": 0, "canceled": 0},
        "events": [],
    }
    try:
        rows = await db.fetch(
            """
            SELECT id, event_type, user_id, email, email_sent,
                   skipped_reason, details, created_at
              FROM billing_churn_events
             WHERE created_at >= now() - make_interval(hours => $1)
             ORDER BY created_at DESC
             LIMIT 200
            """,
            int(max(1, min(hours, 168))),
        )
        for r in rows:
            et = r["event_type"]
            if et in out["totals"]:
                out["totals"][et] += 1
            details = r["details"]
            if isinstance(details, str):
                try:
                    details = json.loads(details)
                except Exception:
                    details = {}
            out["events"].append({
                "id": int(r["id"]),
                "event_type": et,
                "user_id": r["user_id"],
                "email": r["email"],
                "email_sent": bool(r["email_sent"]),
                "skipped_reason": r["skipped_reason"],
                "details": details or {},
                "created_at": r["created_at"].isoformat()
                if r["created_at"] else None,
            })
    except Exception as e:
        log.warning("churn digest collect failed: %s", e)
    return out


def _format_digest(d: dict[str, Any]) -> tuple[str, str]:
    totals = d.get("totals") or {}
    events = d.get("events") or []
    n_pay = int(totals.get("payment_failed") or 0)
    n_will = int(totals.get("will_cancel") or 0)
    n_can = int(totals.get("canceled") or 0)
    subject = (
        f"[axiom] Daily churn digest :: "
        f"{n_pay} pay-fail / {n_will} cancel-scheduled / {n_can} cancelled"
    )
    if not events:
        body = (
            "Last 24h: no payment failures or cancellations. "
            "All quiet on the churn front.\n"
        )
        return subject, body
    lines = [
        "Last 24h subscription churn events.",
        "",
        f"  payment_failed     {n_pay}",
        f"  will_cancel        {n_will}",
        f"  canceled           {n_can}",
        "",
        "Detail (most recent first):",
    ]
    for e in events[:50]:
        ts = (e.get("created_at") or "")[:19]
        sent = "sent" if e.get("email_sent") else (
            f"skipped:{e.get('skipped_reason')}" if e.get("skipped_reason")
            else "skipped"
        )
        lines.append(
            f"  {ts}  {e.get('event_type','?'):<14} "
            f"{(e.get('email') or e.get('user_id') or '?'):<32}  {sent}"
        )
    return subject, "\n".join(lines) + "\n"


async def fire_now(*, dry_run: bool = False) -> dict[str, Any]:
    digest = await collect_digest(24)
    subject, body = _format_digest(digest)
    if dry_run:
        return {
            "dry_run": True,
            "subject": subject,
            "body_preview": body[:4000],
            "totals": digest["totals"],
            "would_send_to": postmark.alert_recipients(),
        }
    results = await postmark.send(
        subject=subject, text_body=body, tag="churn-digest"
    )
    return {
        "dry_run": False,
        "subject": subject,
        "totals": digest["totals"],
        "results": results,
    }


# ---------------------------------------------------------------------------
# Scheduler — same shape as daily_summary
# ---------------------------------------------------------------------------


_DEDUPE_KEY = "billing_churn_digest"
_task: asyncio.Task | None = None
_stop = asyncio.Event()
_last_sent_date: str | None = None


def _enabled() -> bool:
    raw = (os.environ.get("CHURN_DIGEST_ENABLED") or "1").strip().lower()
    return raw in ("1", "true", "yes", "on", "y")


def _send_hour() -> int:
    try:
        return max(0, min(23, int(os.environ.get("CHURN_DIGEST_HOUR_UTC") or "14")))
    except Exception:
        return 14


def _send_minute() -> int:
    try:
        return max(0, min(59, int(os.environ.get("CHURN_DIGEST_MINUTE_UTC") or "0")))
    except Exception:
        return 0


async def _claim_today(date_utc: str) -> bool:
    """Atomic per-day claim using the shared notification_dedupe table."""
    from . import alerts
    return await alerts.dedupe_or_skip(
        "churn_digest", date_utc, ttl_seconds=23 * 3600,
    )


async def _last_sent_from_db() -> str | None:
    from .. import db
    try:
        row = await db.fetchrow(
            "SELECT date_utc::text AS d FROM notification_dedupe WHERE key=$1",
            f"churn_digest:{datetime.now(tz=timezone.utc).date().isoformat()}",
        )
        return row["d"] if row else None
    except Exception:
        return None


async def _loop() -> None:
    global _last_sent_date
    log.info(
        "churn digest scheduler started: hour=%02d:%02d UTC",
        _send_hour(), _send_minute(),
    )
    while not _stop.is_set():
        try:
            now = datetime.now(tz=timezone.utc)
            today = now.date().isoformat()
            past_target = (
                now.hour > _send_hour()
                or (now.hour == _send_hour() and now.minute >= _send_minute())
            )
            if past_target and _last_sent_date != today:
                if await _claim_today(today):
                    log.info("firing churn digest for %s", today)
                    try:
                        await fire_now()
                    except Exception as e:
                        log.exception("churn digest send failed: %s", e)
                _last_sent_date = today
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("churn digest loop error: %s", e)
        try:
            await asyncio.wait_for(_stop.wait(), timeout=60.0)
        except asyncio.TimeoutError:
            pass
        else:
            return


async def start() -> None:
    global _task
    if not _enabled():
        log.info("churn digest scheduler disabled")
        return
    if _task and not _task.done():
        return
    await ensure_table()
    _stop.clear()
    _task = asyncio.create_task(_loop(), name="churn-digest")


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
