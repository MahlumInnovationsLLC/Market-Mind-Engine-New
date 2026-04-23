"""Focused tests for the Clerk webhook signature verification.

Covers the security-critical paths called out in code review:

  * **valid signature** -> 200 OK + user upsert side-effect runs
  * **bad signature** -> 401, NO upsert side-effect
  * **stale timestamp** -> 400 (replay window enforcement)
  * **missing svix headers** -> 400
  * **secret unset** -> 503 (fail-closed posture)

The HMAC scheme matches Svix v1: secret = base64 of bytes after the
``whsec_`` prefix; signed payload = ``f"{id}.{ts}.{raw_body}"`` as
raw bytes; signature header carries one or more ``v1,<b64sig>``
entries.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import admin as admin_module
from app.main import app


_SECRET_RAW = b"super-secret-random-bytes-for-tests"
_SECRET_HEADER = "whsec_" + base64.b64encode(_SECRET_RAW).decode()


def _sign(svix_id: str, ts: int, body: bytes) -> str:
    payload = svix_id.encode("ascii") + b"." + str(ts).encode("ascii") + b"." + body
    sig = base64.b64encode(
        hmac.new(_SECRET_RAW, payload, hashlib.sha256).digest()
    ).decode()
    return f"v1,{sig}"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CLERK_WEBHOOK_SECRET", _SECRET_HEADER)
    return TestClient(app)


@pytest.fixture
def upsert_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []

    async def fake_upsert(uid: str, email: str | None, name: str | None) -> None:
        calls.append((uid, email, name))

    monkeypatch.setattr(admin_module, "upsert_seen_user", fake_upsert)
    return calls


def _payload(event_type: str = "user.updated") -> bytes:
    return json.dumps({
        "type": event_type,
        "data": {
            "id": "user_test_123",
            "primary_email_address_id": "ema_1",
            "email_addresses": [
                {"id": "ema_1", "email_address": "PILOT@example.com"},
            ],
            "first_name": "Pi",
            "last_name": "Lot",
        },
    }).encode("utf-8")


def test_clerk_webhook_valid_signature_runs_upsert(
    client: TestClient, upsert_calls: list[tuple[Any, ...]]
) -> None:
    body = _payload()
    ts = int(time.time())
    sig = _sign("msg_1", ts, body)
    r = client.post(
        "/api/webhooks/clerk",
        content=body,
        headers={
            "svix-id": "msg_1",
            "svix-timestamp": str(ts),
            "svix-signature": sig,
            "content-type": "application/json",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json().get("type") == "user.updated"
    assert len(upsert_calls) == 1
    uid, email, name = upsert_calls[0]
    assert uid == "user_test_123"
    # email is normalised to lower-case by the handler.
    assert email == "pilot@example.com"
    assert name == "Pi Lot"


def test_clerk_webhook_bad_signature_rejects(
    client: TestClient, upsert_calls: list[tuple[Any, ...]]
) -> None:
    body = _payload()
    ts = int(time.time())
    r = client.post(
        "/api/webhooks/clerk",
        content=body,
        headers={
            "svix-id": "msg_2",
            "svix-timestamp": str(ts),
            "svix-signature": "v1,AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            "content-type": "application/json",
        },
    )
    assert r.status_code == 401
    assert upsert_calls == []  # signature failure must short-circuit


def test_clerk_webhook_stale_timestamp_rejects(
    client: TestClient, upsert_calls: list[tuple[Any, ...]]
) -> None:
    body = _payload()
    ts = int(time.time()) - 60 * 60  # 1 hour old
    sig = _sign("msg_3", ts, body)
    r = client.post(
        "/api/webhooks/clerk",
        content=body,
        headers={
            "svix-id": "msg_3",
            "svix-timestamp": str(ts),
            "svix-signature": sig,
            "content-type": "application/json",
        },
    )
    assert r.status_code == 400
    assert "out of window" in r.text
    assert upsert_calls == []


def test_clerk_webhook_missing_headers_rejects(
    client: TestClient, upsert_calls: list[tuple[Any, ...]]
) -> None:
    r = client.post(
        "/api/webhooks/clerk",
        content=_payload(),
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400
    assert upsert_calls == []


def test_clerk_webhook_unconfigured_returns_503(
    monkeypatch: pytest.MonkeyPatch,
    upsert_calls: list[tuple[Any, ...]],
) -> None:
    """Fail-closed: an unconfigured deployment cannot be tricked by a forged event."""
    monkeypatch.delenv("CLERK_WEBHOOK_SECRET", raising=False)
    c = TestClient(app)
    r = c.post(
        "/api/webhooks/clerk",
        content=_payload(),
        headers={
            "svix-id": "x",
            "svix-timestamp": str(int(time.time())),
            "svix-signature": "v1,xxx",
        },
    )
    assert r.status_code == 503
    assert upsert_calls == []


def test_clerk_webhook_unicode_payload_signature_holds(
    client: TestClient, upsert_calls: list[tuple[Any, ...]]
) -> None:
    """Multi-byte (emoji) display names must not break the signature.

    The handler concatenates the raw request bytes when computing the
    HMAC; a UTF-8 round-trip would change byte length and break the
    signature for any payload with characters outside ASCII.
    """
    body = json.dumps({
        "type": "user.updated",
        "data": {
            "id": "user_emoji",
            "primary_email_address_id": "ema_1",
            "email_addresses": [
                {"id": "ema_1", "email_address": "u@example.com"},
            ],
            "first_name": "🚀",
            "last_name": "Pilot",
        },
    }).encode("utf-8")
    ts = int(time.time())
    sig = _sign("msg_uni", ts, body)
    r = client.post(
        "/api/webhooks/clerk",
        content=body,
        headers={
            "svix-id": "msg_uni",
            "svix-timestamp": str(ts),
            "svix-signature": sig,
            "content-type": "application/json",
        },
    )
    assert r.status_code == 200, r.text
    assert len(upsert_calls) == 1
    assert upsert_calls[0][2].startswith("🚀")
