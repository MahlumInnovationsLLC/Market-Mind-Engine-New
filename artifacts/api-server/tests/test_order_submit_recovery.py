"""Unit tests for the submit-order recovery path.

POST /portfolio/orders is intentionally single-shot (no retry). When it
returns a timeout / 5xx / network error the local lifecycle code uses
the ``client_order_id`` to ask Kalshi whether the order was actually
accepted, and promotes the local row to the venue's truth. These tests
drive every branch of that path through a mocked KalshiClient.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app.kalshi import client as kc
from app.kalshi import lifecycle
from app.kalshi.bus import bus


# ---------------------------------------------------------------------------
# Test helpers (mirror the style in test_kalshi_lifecycle.py)
# ---------------------------------------------------------------------------

def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _make_settings(trading_mode: str = "live") -> SimpleNamespace:
    return SimpleNamespace(
        kalshi_api_key_id="kid",
        kalshi_private_key="pem",
        kalshi_api_base_url="https://demo-api.kalshi.co/trade-api/v2",
        trading_mode=trading_mode,
    )


class _FakeClient:
    """Configurable stand-in for ``KalshiClient``.

    ``submit_raises`` fires from ``submit_order`` on the first submit
    call. ``lookup_result`` is the dict returned from
    ``get_order_by_client_id``; ``lookup_raises`` fires instead when
    set. The same instance handles both the submit and the lookup so
    tests observe a single object's call log.
    """

    def __init__(
        self,
        *,
        submit_raises: Exception | None = None,
        submit_ack: dict[str, Any] | None = None,
        lookup_result: dict[str, Any] | None = None,
        lookup_raises: Exception | None = None,
    ) -> None:
        self.submit_raises = submit_raises
        self.submit_ack = submit_ack or {"order": {"order_id": "exo-ok"}}
        self.lookup_result = lookup_result
        self.lookup_raises = lookup_raises
        self.submit_calls: list[dict[str, Any]] = []
        self.lookup_calls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def submit_order(self, payload):
        self.submit_calls.append(payload)
        if self.submit_raises is not None:
            raise self.submit_raises
        return self.submit_ack

    async def get_order_by_client_id(self, coid):
        self.lookup_calls.append(coid)
        if self.lookup_raises is not None:
            raise self.lookup_raises
        return self.lookup_result or {"found": False, "order": None}


@pytest.fixture(autouse=True)
def _reset_lifecycle(monkeypatch):
    """Same clean-slate harness used by test_kalshi_lifecycle.py."""
    lifecycle._resting.clear()
    lifecycle._kill._halted = False
    lifecycle._kill._reason = None
    lifecycle._kill._at = None
    lifecycle._resting_lock = asyncio.Lock()
    lifecycle._kill._lock = asyncio.Lock()
    bus._lock = asyncio.Lock()
    bus._subs.clear()
    lifecycle._reset_submit_recovery_state()

    written: list[dict[str, Any]] = []

    async def _fake_write_lifecycle(**kwargs):
        written.append(kwargs)
        return len(written)

    async def _fake_record_fill(**kwargs):
        return True

    async def _fake_upsert_position(**kwargs):
        return None

    monkeypatch.setattr(lifecycle, "_write_lifecycle", _fake_write_lifecycle)
    monkeypatch.setattr(lifecycle, "_record_fill_idempotent", _fake_record_fill)
    monkeypatch.setattr(lifecycle, "_upsert_position_rollup", _fake_upsert_position)
    monkeypatch.setattr(lifecycle, "get_settings", lambda: _make_settings("live"))
    yield written


def _install_client(monkeypatch, fake: _FakeClient) -> None:
    """Route both the submit and the recovery-lookup KalshiClient to the same fake."""
    monkeypatch.setattr(
        lifecycle.kalshi_client, "KalshiClient", lambda **kw: fake
    )


def _mk_req(coid: str = "coid-1") -> lifecycle.OrderRequest:
    return lifecycle.OrderRequest(
        market_ticker="MKT-1", side="yes", action="buy", count=3,
        limit_price_cents=55, client_order_id=coid,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_timeout_then_found_resting_promotes_to_resting(monkeypatch, _reset_lifecycle):
    fake = _FakeClient(
        submit_raises=kc.KalshiAPIError(0, None, "Kalshi network error: timeout"),
        lookup_result={
            "found": True,
            "order": {"order_id": "exo-REST", "status": "resting",
                      "client_order_id": "coid-rest"},
        },
    )
    _install_client(monkeypatch, fake)

    rec = _run(lifecycle.submit_order(_mk_req("coid-rest")))

    assert rec.state == "resting"
    assert rec.exchange_order_id == "exo-REST"
    assert "coid-rest" in lifecycle._resting
    assert fake.lookup_calls == ["coid-rest"]

    event_types = [w["event_type"] for w in _reset_lifecycle]
    assert event_types == ["submitted", "submit_failed", "resting"]

    # Recovery metadata on the resting row.
    resting_row = next(w for w in _reset_lifecycle if w["event_type"] == "resting")
    assert resting_row["metadata"]["reason"] == "submit_recovered"
    assert resting_row["metadata"]["exchange_order_id"] == "exo-REST"

    state = lifecycle.submit_recovery_state()
    assert state["attempts"] == 1
    assert state["recovered_total"] == 1
    assert state["recovered_resting"] == 1
    assert state["last_outcome"] == "recovered_resting"


def test_timeout_then_found_filled_records_filled(monkeypatch, _reset_lifecycle):
    fake = _FakeClient(
        submit_raises=kc.KalshiAPIError(0, None, "Kalshi network error: read timeout"),
        lookup_result={
            "found": True,
            "order": {
                "order_id": "exo-FILL", "status": "executed",
                "client_order_id": "coid-fill",
                "filled_count": 3, "avg_fill_price": 55,
            },
        },
    )
    _install_client(monkeypatch, fake)

    rec = _run(lifecycle.submit_order(_mk_req("coid-fill")))

    assert rec.state == "filled"
    assert rec.filled_count == 3
    assert rec.avg_fill_price_cents == 55
    # Filled orders should NOT be added to the local resting book.
    assert "coid-fill" not in lifecycle._resting

    event_types = [w["event_type"] for w in _reset_lifecycle]
    assert event_types == ["submitted", "submit_failed", "filled"]

    state = lifecycle.submit_recovery_state()
    assert state["recovered_filled"] == 1
    assert state["recovered_total"] == 1
    assert state["last_outcome"] == "recovered_filled"


def test_timeout_then_not_found_keeps_submit_failed(monkeypatch, _reset_lifecycle):
    fake = _FakeClient(
        submit_raises=kc.KalshiAPIError(0, None, "Kalshi network error"),
        lookup_result={"found": False, "order": None},
    )
    _install_client(monkeypatch, fake)

    with pytest.raises(lifecycle.OrderRejected) as excinfo:
        _run(lifecycle.submit_order(_mk_req("coid-missing")))
    assert "unresolved" in str(excinfo.value)

    # No resting row was created.
    assert "coid-missing" not in lifecycle._resting

    # Only the submitted + submit_failed lifecycle rows exist; nothing
    # was promoted because the venue didn't have the order.
    event_types = [w["event_type"] for w in _reset_lifecycle]
    assert event_types == ["submitted", "submit_failed"]
    failed_row = next(w for w in _reset_lifecycle if w["event_type"] == "submit_failed")
    assert failed_row["metadata"]["reason"] == "submit_ambiguous"

    state = lifecycle.submit_recovery_state()
    assert state["attempts"] == 1
    assert state["not_found"] == 1
    assert state["recovered_total"] == 0
    assert state["last_outcome"] == "not_found"


def test_503_is_recoverable_and_promotes(monkeypatch, _reset_lifecycle):
    fake = _FakeClient(
        submit_raises=kc.KalshiAPIError(503, {"error": "service unavailable"}),
        lookup_result={
            "found": True,
            "order": {"order_id": "exo-503", "status": "resting",
                      "client_order_id": "coid-503"},
        },
    )
    _install_client(monkeypatch, fake)

    rec = _run(lifecycle.submit_order(_mk_req("coid-503")))

    assert rec.state == "resting"
    assert rec.exchange_order_id == "exo-503"
    assert lifecycle.submit_recovery_state()["recovered_resting"] == 1


def test_400_bad_request_is_terminal_no_recovery(monkeypatch, _reset_lifecycle):
    """4xx responses are terminal — the venue definitively rejected us."""
    fake = _FakeClient(
        submit_raises=kc.KalshiAPIError(400, {"error": "invalid ticker"}),
    )
    _install_client(monkeypatch, fake)

    with pytest.raises(lifecycle.OrderRejected):
        _run(lifecycle.submit_order(_mk_req("coid-400")))

    # Critically: the lookup helper must NOT be invoked — 4xx is terminal.
    assert fake.lookup_calls == []

    event_types = [w["event_type"] for w in _reset_lifecycle]
    assert event_types == ["submitted", "rejected"]
    rej = next(w for w in _reset_lifecycle if w["event_type"] == "rejected")
    assert rej["metadata"]["reason"] == "exchange_reject"

    state = lifecycle.submit_recovery_state()
    assert state["attempts"] == 0
    assert state["recovered_total"] == 0


def test_409_duplicate_client_order_id_is_terminal(monkeypatch, _reset_lifecycle):
    """409 (e.g. duplicate client_order_id) must NOT drive recovery."""
    fake = _FakeClient(
        submit_raises=kc.KalshiAPIError(409, {"error": "duplicate"}),
    )
    _install_client(monkeypatch, fake)

    with pytest.raises(lifecycle.OrderRejected):
        _run(lifecycle.submit_order(_mk_req("coid-dup")))

    assert fake.lookup_calls == []
    assert lifecycle.submit_recovery_state()["attempts"] == 0


def test_recovery_lookup_itself_fails_safe_retry(monkeypatch, _reset_lifecycle):
    """If the recovery lookup raises, we keep submit_failed and raise
    OrderRejected so the caller retries next cycle.
    """
    fake = _FakeClient(
        submit_raises=kc.KalshiAPIError(0, None, "Kalshi network error"),
        lookup_raises=kc.KalshiAPIError(0, None, "Kalshi network error: still down"),
    )
    _install_client(monkeypatch, fake)

    with pytest.raises(lifecycle.OrderRejected):
        _run(lifecycle.submit_order(_mk_req("coid-broken")))

    # No local state was promoted.
    assert "coid-broken" not in lifecycle._resting

    # submit_failed row was recorded (audit trail), nothing else.
    event_types = [w["event_type"] for w in _reset_lifecycle]
    assert event_types == ["submitted", "submit_failed"]

    state = lifecycle.submit_recovery_state()
    assert state["attempts"] == 1
    assert state["lookup_failed"] == 1
    assert state["recovered_total"] == 0
    assert state["last_outcome"] == "lookup_failed"
    assert state["last_error"] and "still down" in state["last_error"]


def test_timeout_then_found_canceled(monkeypatch, _reset_lifecycle):
    """If the venue already canceled the order, mirror that locally."""
    fake = _FakeClient(
        submit_raises=kc.KalshiAPIError(504, {"error": "gateway timeout"}),
        lookup_result={
            "found": True,
            "order": {"order_id": "exo-CXL", "status": "canceled",
                      "client_order_id": "coid-cxl"},
        },
    )
    _install_client(monkeypatch, fake)

    rec = _run(lifecycle.submit_order(_mk_req("coid-cxl")))
    assert rec.state == "canceled"
    assert "coid-cxl" not in lifecycle._resting

    event_types = [w["event_type"] for w in _reset_lifecycle]
    assert event_types == ["submitted", "submit_failed", "canceled"]

    state = lifecycle.submit_recovery_state()
    assert state["recovered_canceled"] == 1
    assert state["recovered_total"] == 1


def test_health_probe_surfaces_recovered_counter(monkeypatch, _reset_lifecycle):
    """The /api/health/snapshot card exposes ``order_submit_recovered``."""
    # Drive one successful recovery to bump the counters.
    fake = _FakeClient(
        submit_raises=kc.KalshiAPIError(0, None, "timeout"),
        lookup_result={
            "found": True,
            "order": {"order_id": "exo-RC", "status": "resting",
                      "client_order_id": "coid-rc"},
        },
    )
    _install_client(monkeypatch, fake)
    _run(lifecycle.submit_order(_mk_req("coid-rc")))

    from app import health

    card = health._probe_submit_recovery()
    assert card["key"] == "order_submit_recovery"
    assert card["status"] == "ok"
    assert card["extras"]["order_submit_recovered"] == 1
    assert card["extras"]["attempts"] == 1
    assert card["extras"]["last_outcome"] == "recovered_resting"


def test_mismatched_client_order_id_is_treated_as_not_found(monkeypatch, _reset_lifecycle):
    """Defense-in-depth: if the lookup returns a single row whose
    client_order_id doesn't match ours (e.g. venue filter bug), the
    client-side helper must report "not found" — we never promote an
    unrelated venue order to our local row.
    """
    # Simulate the client.get_order_by_client_id() path by exercising it
    # directly via the fake — here we just confirm that when found=False
    # comes out of the client, recovery treats it as not_found.
    fake = _FakeClient(
        submit_raises=kc.KalshiAPIError(0, None, "timeout"),
        # Note: found=False because the client helper filters strictly.
        lookup_result={"found": False, "order": None},
    )
    _install_client(monkeypatch, fake)

    with pytest.raises(lifecycle.OrderRejected):
        _run(lifecycle.submit_order(_mk_req("coid-mismatch")))

    assert "coid-mismatch" not in lifecycle._resting
    state = lifecycle.submit_recovery_state()
    assert state["not_found"] == 1
    assert state["recovered_total"] == 0


def test_client_helper_strictly_matches_client_order_id(monkeypatch):
    """Unit-level check on KalshiClient.get_order_by_client_id:
    a response containing a single unrelated order must NOT be returned
    as found. Protects against a venue-side filter regression.
    """
    calls: list[dict[str, Any]] = []

    class _FakeClientInternals:
        async def _request(self, method, sub_path, *, params=None, json=None):
            calls.append({"method": method, "sub_path": sub_path, "params": params})
            # Venue returns a single, unrelated order (wrong client_order_id).
            return {
                "orders": [
                    {"order_id": "exo-other", "client_order_id": "someone-else",
                     "status": "resting"},
                ]
            }

    fake = _FakeClientInternals()
    # Bind the real method to the fake so we exercise the real filter logic.
    result = _run(
        kc.KalshiClient.get_order_by_client_id(fake, "my-coid")  # type: ignore[arg-type]
    )
    assert result == {"found": False, "order": None}
    assert calls and calls[0]["params"]["client_order_id"] == "my-coid"


def test_client_helper_accepts_exact_match(monkeypatch):
    """Positive side of the strict-match guard: when client_order_id
    matches, the order is returned.
    """
    class _FakeClientInternals:
        async def _request(self, method, sub_path, *, params=None, json=None):
            return {
                "orders": [
                    {"order_id": "exo-me", "client_order_id": "my-coid",
                     "status": "resting"},
                ]
            }

    fake = _FakeClientInternals()
    result = _run(
        kc.KalshiClient.get_order_by_client_id(fake, "my-coid")  # type: ignore[arg-type]
    )
    assert result["found"] is True
    assert result["order"]["order_id"] == "exo-me"


def test_malformed_venue_response_falls_back_to_not_found(monkeypatch, _reset_lifecycle):
    """If the lookup helper reports found=True but the order payload has
    no exchange id and an unknown status, the recovery path must refuse
    to promote local state.
    """
    fake = _FakeClient(
        submit_raises=kc.KalshiAPIError(0, None, "timeout"),
        lookup_result={
            "found": True,
            "order": {"client_order_id": "coid-bad", "status": "whoknows"},
        },
    )
    _install_client(monkeypatch, fake)

    with pytest.raises(lifecycle.OrderRejected):
        _run(lifecycle.submit_order(_mk_req("coid-bad")))

    assert "coid-bad" not in lifecycle._resting
    state = lifecycle.submit_recovery_state()
    assert state["recovered_total"] == 0
    assert state["not_found"] == 1
    assert state["last_outcome"] == "not_found_malformed"

    event_types = [w["event_type"] for w in _reset_lifecycle]
    assert event_types == ["submitted", "submit_failed"]


def test_health_probe_degraded_when_last_lookup_failed(monkeypatch, _reset_lifecycle):
    fake = _FakeClient(
        submit_raises=kc.KalshiAPIError(0, None, "timeout"),
        lookup_raises=kc.KalshiAPIError(0, None, "lookup down"),
    )
    _install_client(monkeypatch, fake)
    with pytest.raises(lifecycle.OrderRejected):
        _run(lifecycle.submit_order(_mk_req("coid-deg")))

    from app import health

    card = health._probe_submit_recovery()
    assert card["status"] == "degraded"
    assert "lookup down" in card["detail"]
