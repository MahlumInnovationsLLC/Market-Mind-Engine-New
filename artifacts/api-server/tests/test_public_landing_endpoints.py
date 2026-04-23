"""Tests for the public landing endpoints.

Covers:

* ``GET /api/public/recent-signals`` — returns ``{"signals": []}`` (not 500)
  when the DB query raises or when there are no settled trades, and
  surfaces the rows when they exist.
* ``POST /api/public/leads`` — validates the email, inserts a row via
  ``dashboard.insert_landing_lead``, and tolerates Postmark being
  unconfigured (returns ``confirmation_sent=False`` instead of failing).

No network, DB, or Postmark connectivity required — every external
collaborator is monkeypatched in-process.
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import dashboard as dashboard_module
from app import main as main_module
from app.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# /api/public/recent-signals
# ---------------------------------------------------------------------------


def test_recent_signals_empty_when_no_settled_trades(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh DB (no settled rows) must yield an empty list, not a 500."""

    async def fake_fetch(_query: str, *_args: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(dashboard_module.db, "fetch", fake_fetch)

    r = client.get("/api/public/recent-signals")
    assert r.status_code == 200, r.text
    assert r.json() == {"signals": []}


def test_recent_signals_swallows_db_errors(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the DB blows up (e.g. trade_postmortems missing on a fresh DB),
    the endpoint still returns 200 with an empty list."""

    async def boom(_query: str, *_args: Any) -> list[dict[str, Any]]:
        raise RuntimeError("relation \"trade_postmortems\" does not exist")

    monkeypatch.setattr(dashboard_module.db, "fetch", boom)

    r = client.get("/api/public/recent-signals")
    assert r.status_code == 200, r.text
    assert r.json() == {"signals": []}


def test_recent_signals_returns_rows_and_clamps_limit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    fake_rows = [
        {
            "vertical": "sports",
            "league": "NFL",
            "side": "yes",
            "model_prob": 0.62,
            "entry_price": 0.55,
            "actual_outcome": 1,
            "settle_price": 1.0,
            "settled_at": "2026-04-22T18:30:00Z",
        }
    ]

    async def fake_fetch(_query: str, *args: Any) -> list[dict[str, Any]]:
        captured["limit"] = args[0]
        return fake_rows

    monkeypatch.setattr(dashboard_module.db, "fetch", fake_fetch)

    # Above the cap (12) — should clamp.
    r = client.get("/api/public/recent-signals?limit=99")
    assert r.status_code == 200, r.text
    assert r.json() == {"signals": fake_rows}
    assert captured["limit"] == 12

    # Below the floor (1) — should clamp up.
    r = client.get("/api/public/recent-signals?limit=0")
    assert r.status_code == 200, r.text
    assert captured["limit"] == 1


# ---------------------------------------------------------------------------
# /api/public/leads
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_lead_store(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace ``insert_landing_lead`` / ``mark_lead_confirmation_sent`` with
    in-memory recorders so we can assert what the endpoint did."""
    store: dict[str, Any] = {"inserts": [], "marked": []}

    async def fake_insert(**kwargs: Any) -> int:
        store["inserts"].append(kwargs)
        return 1234 + len(store["inserts"])

    async def fake_mark(lead_id: int) -> None:
        store["marked"].append(lead_id)

    monkeypatch.setattr(dashboard_module, "insert_landing_lead", fake_insert)
    monkeypatch.setattr(dashboard_module, "mark_lead_confirmation_sent", fake_mark)
    return store


def _stub_postmark(monkeypatch: pytest.MonkeyPatch, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stub ``notif_postmark.send`` to return the supplied result list and
    record each invocation."""
    calls: list[dict[str, Any]] = []

    async def fake_send(**kwargs: Any) -> list[dict[str, Any]]:
        calls.append(kwargs)
        return results

    monkeypatch.setattr(main_module.notif_postmark, "send", fake_send)
    return calls


def test_leads_happy_path_postmark_delivered(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fake_lead_store: dict[str, Any],
) -> None:
    sends = _stub_postmark(
        monkeypatch,
        [{"to": "user@example.com", "ok": True, "error": None, "skipped_reason": None}],
    )

    r = client.post(
        "/api/public/leads",
        json={"email": " user@example.com ", "interest": "NFL", "source": "hero"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["confirmation_sent"] is True
    assert isinstance(body["lead_id"], int)

    # Insert was called with normalized email + supplied interest/source.
    assert len(fake_lead_store["inserts"]) == 1
    inserted = fake_lead_store["inserts"][0]
    assert inserted["email"] == "user@example.com"
    assert inserted["interest"] == "NFL"
    assert inserted["source"] == "hero"
    # ip_hash should be a hex digest prefix when client host is known.
    assert inserted["ip_hash"] is None or len(inserted["ip_hash"]) == 32

    # Postmark was called once, addressed to the lead.
    assert len(sends) == 1
    assert sends[0]["to"] == ["user@example.com"]
    assert sends[0]["tag"] == "landing-lead-confirmation"

    # Confirmation flag was persisted.
    assert fake_lead_store["marked"] == [body["lead_id"]]


def test_leads_tolerates_postmark_unconfigured(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fake_lead_store: dict[str, Any],
) -> None:
    """When Postmark has no recipients/token configured, ``send`` returns a
    skipped result with ``ok=False``.  The endpoint must still 200, persist
    the lead, and report ``confirmation_sent=False`` without marking the
    confirmation row."""
    sends = _stub_postmark(
        monkeypatch,
        [{"to": "user@example.com", "ok": False, "error": None, "skipped_reason": "no postmark token"}],
    )

    r = client.post("/api/public/leads", json={"email": "user@example.com"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["confirmation_sent"] is False
    assert isinstance(body["lead_id"], int)

    # Insert ran, but no confirmation_sent_at update.
    assert len(fake_lead_store["inserts"]) == 1
    assert fake_lead_store["marked"] == []
    # Defaulted source/interest.
    assert fake_lead_store["inserts"][0]["source"] == "landing"
    assert fake_lead_store["inserts"][0]["interest"] is None
    # Postmark was still attempted.
    assert len(sends) == 1


def test_leads_tolerates_postmark_raising(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fake_lead_store: dict[str, Any],
) -> None:
    """If the Postmark client itself raises, the endpoint still succeeds."""

    async def boom(**_kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("postmark http 500")

    monkeypatch.setattr(main_module.notif_postmark, "send", boom)

    r = client.post("/api/public/leads", json={"email": "user@example.com"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["confirmation_sent"] is False
    assert len(fake_lead_store["inserts"]) == 1
    assert fake_lead_store["marked"] == []


@pytest.mark.parametrize(
    "bad_email",
    [
        "no-at-sign",
        "missing-domain@",
        "missing-tld@example",
    ],
)
def test_leads_rejects_invalid_emails(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fake_lead_store: dict[str, Any],
    bad_email: str,
) -> None:
    sends = _stub_postmark(monkeypatch, [])

    r = client.post("/api/public/leads", json={"email": bad_email})
    assert r.status_code == 400, r.text
    assert "invalid email" in r.text

    # Nothing inserted, no email sent.
    assert fake_lead_store["inserts"] == []
    assert fake_lead_store["marked"] == []
    assert sends == []


def test_leads_returns_503_when_insert_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the `landing_leads` insert raises (e.g. DB unavailable) the
    endpoint must surface a 503, not a 500, and must not attempt Postmark."""

    async def fake_insert(**_kwargs: Any) -> int:
        raise RuntimeError("connection refused")

    sends = _stub_postmark(monkeypatch, [])
    monkeypatch.setattr(dashboard_module, "insert_landing_lead", fake_insert)

    r = client.post("/api/public/leads", json={"email": "user@example.com"})
    assert r.status_code == 503, r.text
    assert "lead store unavailable" in r.text
    assert sends == []
