"""Outlier.bet positive-EV scan ingest.

Outlier.bet exposes a session-cookie-authenticated JSON endpoint
(``/api/v3/positive-ev/scan``) that returns ~1,000-1,500 markets per
poll with implied edge versus a consensus fair line.  We persist
each scan into ``intangibles_reports`` so the orchestrator can fade
or follow them downstream.

Auth:
  * ``OUTLIERBET_EMAIL`` + ``OUTLIERBET_PASSWORD`` — required.  When
    either is unset the feed registers as ``skipped`` and never
    issues a request.
  * On 401/expired cookie the next poll retries the login flow.

This is intentionally a thin ingest path — interpretation lives in
the orchestrator.  When the API endpoint shape drifts (it has, in
the past) the per-row exception logging will surface the change in
``feeds_health`` without dropping the entire scan.
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
import os
import time
from typing import Any

import httpx

from .. import db
from .registry import Feed, registry
from .scheduler import RateLimited


log = logging.getLogger("axiom.feeds.outlier_bet")


OUTLIER_BASE = (
    os.environ.get("AXIOM_OUTLIERBET_BASE_URL")
    or "https://app.outlier.bet"
).rstrip("/")
DEFAULT_CADENCE = max(60, int(os.environ.get("AXIOM_OUTLIERBET_CADENCE") or "300"))


# Cached session cookie: the login round-trip is cheap but not free,
# and outlier.bet rate-limits aggressive re-auth.  We hold the cookie
# for ~30 minutes and refresh on 401.
_SESSION_LOCK = asyncio.Lock()
_session_cookie: str | None = None
_session_expires_at: float = 0.0


async def _login(client: httpx.AsyncClient) -> str | None:
    email = os.environ.get("OUTLIERBET_EMAIL")
    password = os.environ.get("OUTLIERBET_PASSWORD")
    if not email or not password:
        return None
    try:
        r = await client.post(
            f"{OUTLIER_BASE}/api/auth/login",
            json={"email": email, "password": password},
            timeout=15.0,
        )
    except httpx.HTTPError as e:
        log.warning("outlier.bet login transport error: %s", e)
        return None
    if r.status_code == 429:
        raise RateLimited("outlier.bet login rate-limited")
    if r.status_code >= 400:
        log.warning("outlier.bet login %s: %s", r.status_code, r.text[:240])
        return None
    cookie = r.cookies.get("session") or r.cookies.get("auth_token")
    if not cookie:
        # Some deployments return JSON {token: "..."} instead of a cookie.
        try:
            body = r.json()
            cookie = (body or {}).get("token") or (body or {}).get("access_token")
        except Exception:
            cookie = None
    return cookie


async def _ensure_session(client: httpx.AsyncClient, *, force: bool = False) -> str | None:
    global _session_cookie, _session_expires_at
    async with _SESSION_LOCK:
        if (
            not force
            and _session_cookie
            and time.time() < _session_expires_at
        ):
            return _session_cookie
        cookie = await _login(client)
        if cookie:
            _session_cookie = cookie
            _session_expires_at = time.time() + 30 * 60.0
            return cookie
        _session_cookie = None
        _session_expires_at = 0.0
        return None


async def _persist_scan(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    written = 0
    for r in rows:
        try:
            await db.execute(
                """
                INSERT INTO intangibles_reports
                    (provider, sport, external_id, market_type, side,
                     edge_pct, fair_prob, book_prob, sportsbook, teams,
                     payload, scanned_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                        $11::jsonb, to_timestamp($12))
                ON CONFLICT (provider, external_id, scanned_at)
                DO NOTHING
                """,
                "outlier.bet",
                str(r.get("sport") or "unknown").lower(),
                str(r.get("external_id") or r.get("id") or ""),
                r.get("market_type"),
                r.get("side"),
                _flt(r.get("edge_pct")),
                _flt(r.get("fair_prob")),
                _flt(r.get("book_prob")),
                r.get("sportsbook"),
                r.get("teams"),
                _json.dumps(r.get("payload") or {}),
                float(r.get("scanned_at") or time.time()),
            )
            written += 1
        except Exception as e:
            log.warning("outlier.bet persist row failed: %s", e)
    return written


def _flt(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _normalize(raw: list[dict[str, Any]] | dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        items = (
            raw.get("results")
            or raw.get("data")
            or raw.get("items")
            or []
        )
    else:
        items = raw
    out: list[dict[str, Any]] = []
    now = time.time()
    for it in items or []:
        if not isinstance(it, dict):
            continue
        out.append({
            "external_id": it.get("id") or it.get("market_id") or it.get("uuid"),
            "sport": it.get("sport") or it.get("league") or "unknown",
            "market_type": it.get("market") or it.get("market_type"),
            "side": it.get("side") or it.get("selection"),
            "edge_pct": it.get("edge") or it.get("edge_pct"),
            "fair_prob": it.get("fair_prob") or it.get("true_prob"),
            "book_prob": it.get("book_prob") or it.get("implied_prob"),
            "sportsbook": it.get("book") or it.get("sportsbook"),
            "teams": it.get("teams") or it.get("event"),
            "payload": it,
            "scanned_at": now,
        })
    return out


async def _poll_outlier_bet() -> int:
    if not (os.environ.get("OUTLIERBET_EMAIL") and os.environ.get("OUTLIERBET_PASSWORD")):
        return 0
    async with httpx.AsyncClient(follow_redirects=True) as client:
        cookie = await _ensure_session(client)
        if not cookie:
            log.warning("outlier.bet: no session cookie; skipping scan")
            return 0
        for attempt in (0, 1):
            try:
                r = await client.get(
                    f"{OUTLIER_BASE}/api/v3/positive-ev/scan",
                    headers={
                        "Cookie": f"session={cookie}",
                        "Authorization": f"Bearer {cookie}",
                        "Accept": "application/json",
                    },
                    timeout=30.0,
                )
            except httpx.HTTPError as e:
                log.warning("outlier.bet scan transport error: %s", e)
                return 0
            if r.status_code == 401 and attempt == 0:
                cookie = await _ensure_session(client, force=True)
                if not cookie:
                    return 0
                continue
            if r.status_code == 429:
                retry = int(r.headers.get("Retry-After") or "60")
                raise RateLimited(f"outlier.bet rate-limited; retry in {retry}s")
            if r.status_code >= 400:
                log.warning(
                    "outlier.bet scan %s: %s", r.status_code, r.text[:240]
                )
                return 0
            try:
                body = r.json()
            except Exception as e:
                log.warning("outlier.bet scan body parse failed: %s", e)
                return 0
            rows = _normalize(body)
            return await _persist_scan(rows)
        return 0


def register_outlier_bet_feed() -> None:
    if registry.status_of("outlier_bet"):
        return
    creds_set = bool(
        os.environ.get("OUTLIERBET_EMAIL") and os.environ.get("OUTLIERBET_PASSWORD")
    )
    registry.register(Feed(
        name="outlier_bet",
        cadence_seconds=float(DEFAULT_CADENCE),
        poll=_poll_outlier_bet,
        enabled=creds_set,
        skipped_reason=(
            None
            if creds_set
            else "OUTLIERBET_EMAIL / OUTLIERBET_PASSWORD not set"
        ),
    ))
