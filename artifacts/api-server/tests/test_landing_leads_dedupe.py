"""Tests for the public landing-leads endpoint dedupe + per-IP rate limit.

Covers task #126:
  * Repeat submissions from the same email within 24h return success
    without re-emailing or inserting a duplicate row.
  * Per-IP rate limit returns 429 once the per-minute quota is exceeded.
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import dashboard, main as main_module
from app.main import app
from app.notifications import postmark as notif_postmark


@pytest.fixture(autouse=True)
def _reset_rate_buckets():
    main_module._leads_rate_buckets.clear()
    yield
    main_module._leads_rate_buckets.clear()


@pytest.fixture
def stub_dashboard(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {
        "inserted": [],
        "existing_id": None,
        "marks": [],
    }

    async def _insert(**kwargs: Any) -> int:
        state["inserted"].append(kwargs)
        return 100 + len(state["inserted"])

    async def _find(email: str, hours: int = 24) -> int | None:
        return state["existing_id"]

    async def _mark(lead_id: int) -> None:
        state["marks"].append(lead_id)

    monkeypatch.setattr(dashboard, "insert_landing_lead", _insert)
    monkeypatch.setattr(dashboard, "find_recent_lead_by_email", _find)
    monkeypatch.setattr(dashboard, "mark_lead_confirmation_sent", _mark)
    return state


@pytest.fixture
def stub_postmark(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    counter = {"sends": 0}

    async def _send(**kwargs: Any) -> list[dict[str, Any]]:
        counter["sends"] += 1
        return [{"ok": True, "to": kwargs.get("to", [None])[0]}]

    monkeypatch.setattr(notif_postmark, "send", _send)
    return counter


def test_first_signup_inserts_and_emails(stub_dashboard, stub_postmark) -> None:
    client = TestClient(app)
    res = client.post(
        "/api/public/leads",
        json={"email": "alice@example.com", "interest": "sports"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body.get("deduped") is not True
    assert body["confirmation_sent"] is True
    assert len(stub_dashboard["inserted"]) == 1
    assert stub_postmark["sends"] == 1


def test_duplicate_within_window_returns_success_without_email(
    stub_dashboard, stub_postmark
) -> None:
    stub_dashboard["existing_id"] = 42
    client = TestClient(app)
    res = client.post(
        "/api/public/leads",
        json={"email": "Alice@Example.com"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["deduped"] is True
    assert body["confirmation_sent"] is False
    assert body["lead_id"] == 42
    # Crucially: no insert and no Postmark send.
    assert stub_dashboard["inserted"] == []
    assert stub_postmark["sends"] == 0


def test_per_ip_rate_limit_returns_429(stub_dashboard, stub_postmark) -> None:
    client = TestClient(app)
    ok_count = 0
    last_status = 200
    # 6 calls in quick succession from the same TestClient (same client host).
    for i in range(6):
        res = client.post(
            "/api/public/leads",
            json={"email": f"user{i}@example.com"},
        )
        last_status = res.status_code
        if res.status_code == 200:
            ok_count += 1
        elif res.status_code == 429:
            break
    assert ok_count == main_module._LEADS_RATE_LIMIT_PER_MIN
    assert last_status == 429


def test_invalid_email_rejected(stub_dashboard, stub_postmark) -> None:
    client = TestClient(app)
    res = client.post("/api/public/leads", json={"email": "not-an-email"})
    assert res.status_code == 400
    assert stub_dashboard["inserted"] == []
    assert stub_postmark["sends"] == 0
