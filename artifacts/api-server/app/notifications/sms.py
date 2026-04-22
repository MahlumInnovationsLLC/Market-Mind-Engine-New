"""Tiny SMS sender — Twilio over plain HTTP.

Configuration (env):
  - TWILIO_ACCOUNT_SID
  - TWILIO_AUTH_TOKEN
  - TWILIO_FROM_NUMBER  (E.164, e.g. "+15551234567")

Without those three set, every send is a logged no-op so the backend
keeps running in dev / CI without provisioning a Twilio account. SMS
is currently only used for the Elite-tier `live_trade` alert; if we
add more channels in the future the dispatch tag is captured for log
correlation.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
from typing import Iterable

import httpx


log = logging.getLogger("axiom.notifications.sms")


_TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"
# Twilio caps SMS body at 1600 characters (concatenated multi-part).
# We keep alerts well under one segment so the whole message arrives
# as a single SMS.
_MAX_BODY = 320


def is_configured() -> bool:
    return all(
        os.environ.get(k)
        for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER")
    )


def _normalize(phone: str) -> str | None:
    p = (phone or "").strip()
    if not p:
        return None
    # Permissive validation — let Twilio reject obvious junk.
    if not p.startswith("+") or len(p) < 8 or len(p) > 20:
        return None
    return p


async def _send_one(
    to: str,
    body: str,
    *,
    tag: str | None,
) -> dict[str, object]:
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    tok = os.environ.get("TWILIO_AUTH_TOKEN")
    src = os.environ.get("TWILIO_FROM_NUMBER")
    norm_to = _normalize(to)
    if norm_to is None:
        return {"to": to, "ok": False, "skipped_reason": "invalid_phone"}
    if not (sid and tok and src):
        log.info("sms skipped (no twilio config): to=%s tag=%s", norm_to, tag)
        return {"to": norm_to, "ok": False, "skipped_reason": "twilio_not_configured"}
    url = f"{_TWILIO_API_BASE}/Accounts/{sid}/Messages.json"
    auth = base64.b64encode(f"{sid}:{tok}".encode()).decode()
    payload = {
        "From": src,
        "To": norm_to,
        "Body": body[:_MAX_BODY],
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.post(
                url,
                data=payload,
                headers={"Authorization": f"Basic {auth}"},
            )
        if r.status_code >= 300:
            err = f"HTTP {r.status_code}: {r.text[:200]}"
            log.warning("twilio send failed: %s", err)
            return {"to": norm_to, "ok": False, "error": err}
        log.info("sms sent: to=%s tag=%s", norm_to, tag)
        return {"to": norm_to, "ok": True}
    except Exception as e:  # pragma: no cover — network errors are best-effort
        log.warning("twilio send error: %s", e)
        return {"to": norm_to, "ok": False, "error": f"{type(e).__name__}: {e}"}


async def send(
    to: Iterable[str],
    body: str,
    *,
    tag: str | None = None,
) -> list[dict[str, object]]:
    """Fan-out one SMS body to every recipient. Never raises."""
    targets = [t for t in to if t]
    if not targets:
        return []
    return list(
        await asyncio.gather(*[_send_one(t, body, tag=tag) for t in targets])
    )


def send_in_background(
    to: Iterable[str],
    body: str,
    *,
    tag: str | None = None,
) -> None:
    """Fire-and-forget wrapper for sync call sites."""
    targets = list(to)
    if not targets:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.warning("sms.send_in_background called with no loop; dropping")
        return
    loop.create_task(send(targets, body, tag=tag))
