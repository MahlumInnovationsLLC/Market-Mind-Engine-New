"""Tiny Postmark email client.

We hit the Postmark HTTP API directly with httpx — Postmark's official
SDK is sync-only and we don't need its retry/queueing features.

Configuration (env vars):
  - POSTMARK_SERVER_TOKEN   (required to actually send; without it
                             every send is a logged no-op).
  - POSTMARK_FROM_EMAIL     ("from" address, must be a verified
                             sender on the Postmark server).
  - POSTMARK_MESSAGE_STREAM (defaults to "outbound").
  - OPERATOR_ALERT_EMAILS   (comma-sep list).  Falls back to
                             OPERATOR_ALLOWED_EMAILS so a fresh
                             install doesn't need a second list.

Every public function is a no-op when the token is missing — alert
call sites should never crash because email isn't wired yet.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Iterable

import httpx

from ..http_retry import retry_request


log = logging.getLogger("axiom.notifications.postmark")

POSTMARK_API = "https://api.postmarkapp.com/email"
DEFAULT_FROM = "alerts@axiom-predict.app"


# ---------------------------------------------------------------------------
# In-memory delivery log so the operator can inspect what fired.
# ---------------------------------------------------------------------------


@dataclass
class _SendRecord:
    sent_at: float
    to: str
    subject: str
    ok: bool
    error: str | None = None
    skipped_reason: str | None = None


_sends: list[_SendRecord] = []
_SENDS_MAX = 200


def _record(rec: _SendRecord) -> None:
    _sends.append(rec)
    if len(_sends) > _SENDS_MAX:
        del _sends[: len(_sends) - _SENDS_MAX]


def recent_sends(limit: int = 50) -> list[dict]:
    """Last N Postmark attempts (success / failure / skipped)."""
    items = _sends[-limit:][::-1]
    return [
        {
            "sent_at": r.sent_at,
            "to": r.to,
            "subject": r.subject,
            "ok": r.ok,
            "error": r.error,
            "skipped_reason": r.skipped_reason,
        }
        for r in items
    ]


# ---------------------------------------------------------------------------
# Recipients + token resolution
# ---------------------------------------------------------------------------


def alert_recipients() -> list[str]:
    raw = (
        os.environ.get("OPERATOR_ALERT_EMAILS")
        or os.environ.get("OPERATOR_ALLOWED_EMAILS")
        or ""
    )
    out: list[str] = []
    for tok in raw.split(","):
        tok = tok.strip()
        if tok and "@" in tok:
            out.append(tok)
    # de-dupe preserving order
    seen: set[str] = set()
    deduped = []
    for e in out:
        if e.lower() in seen:
            continue
        seen.add(e.lower())
        deduped.append(e)
    return deduped


def is_configured() -> bool:
    return bool(os.environ.get("POSTMARK_SERVER_TOKEN"))


def status() -> dict:
    return {
        "configured": is_configured(),
        "from_email": os.environ.get("POSTMARK_FROM_EMAIL") or DEFAULT_FROM,
        "message_stream": os.environ.get("POSTMARK_MESSAGE_STREAM") or "outbound",
        "recipients": alert_recipients(),
    }


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------


async def _send_one(
    *,
    to: str,
    subject: str,
    text_body: str,
    html_body: str | None,
    tag: str | None,
    dry_run: bool = False,
) -> _SendRecord:
    token = os.environ.get("POSTMARK_SERVER_TOKEN")
    from_email = os.environ.get("POSTMARK_FROM_EMAIL") or DEFAULT_FROM
    stream = os.environ.get("POSTMARK_MESSAGE_STREAM") or "outbound"
    now = time.time()

    if dry_run:
        rec = _SendRecord(
            sent_at=now,
            to=to,
            subject=subject,
            ok=False,
            skipped_reason="dry_run",
        )
        log.info("postmark dry-run: to=%s subject=%r", to, subject)
        _record(rec)
        return rec

    if not token:
        rec = _SendRecord(
            sent_at=now,
            to=to,
            subject=subject,
            ok=False,
            skipped_reason="POSTMARK_SERVER_TOKEN not set",
        )
        log.info("postmark skipped (no token): to=%s subject=%r", to, subject)
        _record(rec)
        return rec

    payload = {
        "From": from_email,
        "To": to,
        "Subject": subject,
        "TextBody": text_body,
        "MessageStream": stream,
    }
    if html_body:
        payload["HtmlBody"] = html_body
    if tag:
        payload["Tag"] = tag

    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            async def _do() -> httpx.Response:
                return await c.post(
                    POSTMARK_API,
                    json=payload,
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "X-Postmark-Server-Token": token,
                    },
                )

            # Postmark POSTs are technically non-idempotent, but we
            # only retry on transient 5xx / network failures: the
            # Postmark server cannot have queued the message in those
            # cases, so a duplicate is impossible.  4xx responses
            # (validation, suppression, bad token) are returned to the
            # caller untouched.
            r = await retry_request(
                _do,
                client_name="postmark",
                max_attempts=3,
                retryable_statuses=frozenset({500, 502, 503, 504}),
            )
        if r.status_code >= 300:
            err = f"HTTP {r.status_code}: {r.text[:300]}"
            rec = _SendRecord(sent_at=now, to=to, subject=subject, ok=False, error=err)
            log.warning("postmark send failed: %s", err)
        else:
            rec = _SendRecord(sent_at=now, to=to, subject=subject, ok=True)
            log.info("postmark sent: to=%s subject=%r tag=%s", to, subject, tag)
    except Exception as e:
        rec = _SendRecord(
            sent_at=now,
            to=to,
            subject=subject,
            ok=False,
            error=f"{type(e).__name__}: {e}",
        )
        log.warning("postmark send error: %s", e)
    _record(rec)
    return rec


async def send(
    *,
    subject: str,
    text_body: str,
    html_body: str | None = None,
    to: Iterable[str] | None = None,
    tag: str | None = None,
    dry_run: bool = False,
) -> list[dict]:
    """Send `subject`/`text_body` to every operator (or the explicit
    `to` list when provided).  Returns a list of per-recipient result
    dicts.  Never raises — failures are logged + recorded."""
    recipients = list(to) if to is not None else alert_recipients()
    if not recipients:
        rec = _SendRecord(
            sent_at=time.time(),
            to="(no recipients)",
            subject=subject,
            ok=False,
            skipped_reason="no operator recipients configured",
        )
        _record(rec)
        log.info("postmark skipped: no recipients for subject=%r", subject)
        return [
            {
                "to": rec.to,
                "ok": False,
                "skipped_reason": rec.skipped_reason,
            }
        ]
    results = await asyncio.gather(
        *[
            _send_one(
                to=r,
                subject=subject,
                text_body=text_body,
                html_body=html_body,
                tag=tag,
                dry_run=dry_run,
            )
            for r in recipients
        ],
        return_exceptions=False,
    )
    return [
        {
            "to": rec.to,
            "ok": rec.ok,
            "error": rec.error,
            "skipped_reason": rec.skipped_reason,
        }
        for rec in results
    ]


def send_in_background(**kwargs) -> None:
    """Fire-and-forget wrapper for use from sync code paths.

    Schedules `send(**kwargs)` on the running event loop and ignores the
    result.  Safe to call from webhook handlers / background tasks."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No loop — log and drop. Postmark is best-effort.
        log.warning("send_in_background called with no running loop; dropping")
        return
    loop.create_task(send(**kwargs))
