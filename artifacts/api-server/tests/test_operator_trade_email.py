"""Tests for the branded HTML operator per-trade email (task #712).

Covers:
  * Subject-line shape (vertical + ticker present for fast filtering).
  * HTML body carries position, vertical PnL, and gate context.
  * Per-recipient dedupe collapses partial-fill bursts to one email.
  * Per-recipient opt-out via ``operator_alert_preferences`` skips
    the muted operator while still delivering to opted-in operators.
  * Default-when-missing is opted-in (a fresh install with no prefs
    sends to every configured operator).
  * Plain-text body covers all the same fields (no HTML-only data).

We never open the asyncpg pool: ``app.db.fetchrow`` / ``fetchval`` /
``fetch`` are stubbed in-memory so the dedupe + preferences + context
queries all hit a tiny shim. ``postmark.send`` is stubbed too so no
network calls fire.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app import db as _db
from app.notifications import alerts as _alerts
from app.notifications import postmark as _postmark


# ---------------------------------------------------------------------------
# Tiny shim that simulates the three DB tables the helper touches:
#   * notification_dedupe (per-recipient claim)
#   * operator_alert_preferences (opt-out)
#   * positions / trades / order_lifecycle (context fetchers)
#
# The matching is done by substring on the SQL because we want the
# tests to be resilient to small whitespace tweaks in the helper SQL.
# ---------------------------------------------------------------------------


class _Shim:
    def __init__(self) -> None:
        self.dedupe_rows: dict[str, datetime] = {}
        self.prefs: dict[tuple[str, str], bool] = {}
        # Context the fetchers will return. Tests override per-case.
        self.position_rows: list[dict[str, Any]] = []
        self.realized_today: float = 0.0
        self.open_exposure: float = 0.0
        self.lifecycle_metadata: dict[str, Any] | None = None

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "notification_dedupe" in query:
            key = args[0]
            ttl = int(args[1])
            now = datetime.now(timezone.utc)
            existing = self.dedupe_rows.get(key)
            if existing is None or existing <= now:
                self.dedupe_rows[key] = now + timedelta(seconds=ttl)
                return {"ok": 1}
            return None
        if "operator_alert_preferences" in query and "INSERT" in query.upper():
            email, alert_type, enabled = args[0], args[1], bool(args[2])
            self.prefs[(email, alert_type)] = enabled
            return {
                "email": email,
                "alert_type": alert_type,
                "enabled": enabled,
                "updated_at": datetime.now(timezone.utc),
            }
        if "operator_alert_preferences" in query and "SELECT" in query.upper():
            email, alert_type = args[0], args[1]
            if (email, alert_type) not in self.prefs:
                return None
            return {"enabled": self.prefs[(email, alert_type)]}
        if "order_lifecycle" in query:
            if self.lifecycle_metadata is None:
                return None
            return {"metadata": self.lifecycle_metadata}
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "FROM positions" in query and "side_lc" in query:
            return list(self.position_rows)
        if "operator_alert_preferences" in query:
            out = []
            for (email, alert_type), enabled in self.prefs.items():
                out.append({
                    "email": email,
                    "alert_type": alert_type,
                    "enabled": enabled,
                    "updated_at": datetime.now(timezone.utc),
                })
            return out
        return []

    async def fetchval(self, query: str, *args: Any) -> Any:
        if "FROM trades" in query and "executed_at" in query:
            return float(self.realized_today)
        if "FROM positions" in query and "notional" in query:
            return float(self.open_exposure)
        return None

    async def execute(self, query: str = "", *args: Any, **_kw: Any) -> str:
        # Honour DELETEs against notification_dedupe so that the
        # "release on send failure" path actually frees the in-memory
        # claim and a follow-up retry can re-claim it.
        if "DELETE FROM notification_dedupe" in query and args:
            self.dedupe_rows.pop(args[0], None)
        return "OK"


@pytest.fixture
def shim(monkeypatch: pytest.MonkeyPatch) -> _Shim:
    s = _Shim()
    monkeypatch.setattr(_db, "fetchrow", s.fetchrow)
    monkeypatch.setattr(_db, "fetch", s.fetch)
    monkeypatch.setattr(_db, "fetchval", s.fetchval)
    monkeypatch.setattr(_db, "execute", s.execute)
    return s


@pytest.fixture
def captured_sends(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def _fake_send(**kwargs):
        sent.append(kwargs)
        return [{"to": kwargs.get("to"), "ok": True}]

    monkeypatch.setattr(_postmark, "send", _fake_send)
    monkeypatch.setattr(_postmark, "is_configured", lambda: True)
    # Avoid touching market_titles cache in tests.
    async def _fake_get_title(_t: str) -> str | None:
        return "Test Market Title"
    from app.kalshi import market_titles as _mt
    monkeypatch.setattr(_mt, "get_title", _fake_get_title)
    return sent


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_subject_includes_vertical_ticker_action(shim, captured_sends):
    """Subject must include vertical + ticker so on-call can filter
    quickly without opening the body."""
    out = asyncio.run(_alerts.operator_trade_alert(
        market_ticker="KX-MKT-1",
        side="yes", action="buy", contracts=12, price_cents=42,
        order_id="ord-1", fill_id="fill-1", vertical="sports",
        recipients=["alice@example.com"],
    ))
    assert out["queued"] is True
    subj = out["subject"]
    assert "[axiom][operator]" in subj
    assert "[sports]" in subj
    assert "KX-MKT-1" in subj
    assert "BUY" in subj
    assert "YES" in subj
    assert "12x" in subj
    assert "42¢" in subj


def test_html_carries_position_pnl_and_gates(shim, captured_sends):
    """The HTML body must include the position-after-fill, vertical
    PnL block, and gate context — these are the whole point of the
    branded copy vs the firehose."""
    shim.position_rows = [
        {"side_lc": "yes", "qty": 30},
        {"side_lc": "no",  "qty": 5},
    ]
    shim.realized_today = 12.5
    shim.open_exposure = 250.0
    shim.lifecycle_metadata = {
        "context": {
            "model_prob": 0.62,
            "model_edge_cents": 4.0,
            "swarm_consensus": "YES",
            "kelly_fraction": 0.05,
        }
    }
    out = asyncio.run(_alerts.operator_trade_alert(
        market_ticker="KX-MKT-2",
        side="yes", action="buy", contracts=12, price_cents=42,
        order_id="ord-2", fill_id="fill-2", vertical="sports",
        recipients=["alice@example.com"],
    ))
    assert len(captured_sends) == 1
    html = captured_sends[0]["html_body"]
    text = captured_sends[0]["text_body"]
    # Position rendered correctly (long 30 / short 5).
    assert "long 30" in html
    assert "short 5" in html
    assert "long 30" in text
    # PnL block.
    assert "$12.50" in html  # realized today
    assert "$250.00" in html  # open exposure
    # Vertical surfaced in the heading.
    assert "sports" in html
    # Gate context surfaced.
    assert "Model prob" in html
    assert "0.62" in html
    assert "Swarm consensus" in html
    # CTA link present.
    assert "/trades/ord-2" in html
    # Plain-text covers the same data.
    assert "ord-2" in text
    assert "fill-2" in text


def test_per_recipient_dedupe_collapses_partial_fill_burst(shim, captured_sends):
    """Two calls with the same (operator, order, fill) must produce
    exactly one outbound email — partial-fill webhooks must NOT spam
    the operator's inbox."""
    args = dict(
        market_ticker="KX-MKT-3",
        side="yes", action="buy", contracts=10, price_cents=33,
        order_id="ord-3", fill_id="fill-3", vertical="sports",
        recipients=["alice@example.com"],
    )
    out1 = asyncio.run(_alerts.operator_trade_alert(**args))
    out2 = asyncio.run(_alerts.operator_trade_alert(**args))
    assert out1["sends"][0]["sent"] is True
    assert out2["sends"][0]["sent"] is False
    assert out2["sends"][0]["skipped_reason"] == "dedupe"
    assert len(captured_sends) == 1


def test_dedupe_is_per_recipient(shim, captured_sends):
    """Operator A's claim must NOT block operator B's email for the
    same fill — the rich email is per-recipient by design."""
    args = dict(
        market_ticker="KX-MKT-4",
        side="yes", action="buy", contracts=10, price_cents=33,
        order_id="ord-4", fill_id="fill-4", vertical="sports",
    )
    out = asyncio.run(_alerts.operator_trade_alert(
        **args, recipients=["alice@example.com", "bob@example.com"]
    ))
    sent_to = [s["to"] for s in out["sends"] if s["sent"]]
    assert sorted(sent_to) == ["alice@example.com", "bob@example.com"]
    assert len(captured_sends) == 2


def test_opt_out_skips_just_that_operator(shim, captured_sends):
    """A muted operator gets no email; opted-in operators still do."""
    shim.prefs[("alice@example.com", _alerts.OPERATOR_TRADE_ALERT_TYPE)] = False
    out = asyncio.run(_alerts.operator_trade_alert(
        market_ticker="KX-MKT-5",
        side="yes", action="buy", contracts=10, price_cents=33,
        order_id="ord-5", fill_id="fill-5", vertical="sports",
        recipients=["alice@example.com", "bob@example.com"],
    ))
    assert out["queued"] is True
    assert out["skipped_optout"] == ["alice@example.com"]
    sent_to = [s["to"] for s in out["sends"] if s["sent"]]
    assert sent_to == ["bob@example.com"]
    assert len(captured_sends) == 1
    assert captured_sends[0]["to"] == ["bob@example.com"]


def test_default_when_missing_is_opted_in(shim, captured_sends):
    """A fresh operator with NO row in operator_alert_preferences
    must still receive the alert (default opted-in)."""
    assert shim.prefs == {}
    out = asyncio.run(_alerts.operator_trade_alert(
        market_ticker="KX-MKT-6",
        side="no", action="sell", contracts=4, price_cents=58,
        order_id="ord-6", fill_id="fill-6", vertical="weather",
        recipients=["new-operator@example.com"],
    ))
    assert out["queued"] is True
    assert out["skipped_optout"] == []
    assert len(captured_sends) == 1


def test_no_recipients_skips_cleanly(shim, captured_sends):
    """Empty recipient list returns a structured skip rather than
    raising — alert dispatch errors must never propagate to the WS
    fill loop."""
    out = asyncio.run(_alerts.operator_trade_alert(
        market_ticker="KX-MKT-7",
        side="yes", action="buy", contracts=1, price_cents=99,
        order_id="ord-7", fill_id="fill-7", vertical="sports",
        recipients=[],
    ))
    assert out["queued"] is False
    assert out["skipped_reason"] == "no_recipients"
    assert captured_sends == []


def test_set_and_list_operator_preferences_roundtrip(shim):
    """Admin upsert + list should round-trip the row."""
    res = asyncio.run(_alerts.set_operator_preference(
        email="alice@example.com",
        alert_type=_alerts.OPERATOR_TRADE_ALERT_TYPE,
        enabled=False,
    ))
    assert "error" not in res
    assert res["enabled"] is False
    rows = asyncio.run(_alerts.list_operator_preferences())
    assert any(
        r["email"] == "alice@example.com" and r["enabled"] is False
        for r in rows
    )


def test_set_operator_preference_rejects_unknown_alert_type(shim):
    """Typo-protection: unknown alert types are rejected so a stray
    POST can't silently create dead rows."""
    res = asyncio.run(_alerts.set_operator_preference(
        email="alice@example.com",
        alert_type="not_a_real_alert",
        enabled=True,
    ))
    assert res.get("error") == "unknown_alert_type"


def test_set_operator_preference_rejects_invalid_email(shim):
    res = asyncio.run(_alerts.set_operator_preference(
        email="not-an-email",
        alert_type=_alerts.OPERATOR_TRADE_ALERT_TYPE,
        enabled=True,
    ))
    assert res.get("error") == "invalid_email"


def test_dry_run_does_not_send(shim, captured_sends):
    """``dry_run=True`` returns the rendered preview but never calls
    postmark.send — used by the admin "test send" flow."""
    out = asyncio.run(_alerts.operator_trade_alert(
        market_ticker="KX-MKT-DRY",
        side="yes", action="buy", contracts=2, price_cents=50,
        order_id="ord-dry", fill_id="fill-dry", vertical="sports",
        recipients=["alice@example.com"],
        dry_run=True,
    ))
    assert out["dry_run"] is True
    assert "would_send_to" in out
    assert captured_sends == []


def test_dedupe_released_on_send_exception_allows_retry(shim, monkeypatch):
    """If Postmark send raises (defensive path — the SDK is contractually
    never-raises, but a future bug could change that), the dedupe row
    must be released so a follow-up retry of the same (operator, order,
    fill) can land — otherwise a single transient Postmark error would
    silently suppress the alert for 7 days."""
    sent_calls = {"n": 0}

    async def _flaky_send(**_kwargs):
        sent_calls["n"] += 1
        if sent_calls["n"] == 1:
            raise RuntimeError("postmark down")
        return [{"to": _kwargs.get("to"), "ok": True}]

    monkeypatch.setattr(_postmark, "send", _flaky_send)
    monkeypatch.setattr(_postmark, "is_configured", lambda: True)
    from app.kalshi import market_titles as _mt
    async def _t(_t): return "Title"
    monkeypatch.setattr(_mt, "get_title", _t)

    args = dict(
        market_ticker="KX-MKT-RTY",
        side="yes", action="buy", contracts=3, price_cents=51,
        order_id="ord-rty", fill_id="fill-rty", vertical="sports",
        recipients=["alice@example.com"],
    )
    out1 = asyncio.run(_alerts.operator_trade_alert(**args))
    assert out1["sends"][0]["sent"] is False
    assert "error" in out1["sends"][0]
    # Retry with the same (operator, order, fill) — the dedupe row was
    # released, so the second attempt should claim it cleanly and send.
    out2 = asyncio.run(_alerts.operator_trade_alert(**args))
    assert out2["sends"][0]["sent"] is True


def test_dedupe_released_on_postmark_ok_false_allows_retry(shim, monkeypatch):
    """``postmark.send`` is contractually never-raises and instead
    returns ``[{"ok": False, "error": "..."}]`` on real failures
    (missing token, HTTP 5xx, suppression hit). Without inspecting the
    ``ok`` field, the alert would be marked sent and the dedupe row
    would silence the same (operator, order, fill) tuple for 7 days
    despite Postmark having rejected the message — the architect-flagged
    bug in code review.

    This test pins the real-world shape: first call returns ok=False,
    retry returns ok=True, the dedupe row must have been released so
    the retry actually goes out."""
    call_idx = {"n": 0}

    async def _send(**kwargs):
        call_idx["n"] += 1
        to = kwargs.get("to") or []
        if call_idx["n"] == 1:
            return [
                {"to": to[0] if to else None, "ok": False,
                 "error": "postmark 502: bad gateway"}
            ]
        return [{"to": to[0] if to else None, "ok": True}]

    monkeypatch.setattr(_postmark, "send", _send)
    monkeypatch.setattr(_postmark, "is_configured", lambda: True)
    from app.kalshi import market_titles as _mt
    async def _t(_t): return "Title"
    monkeypatch.setattr(_mt, "get_title", _t)

    args = dict(
        market_ticker="KX-MKT-OKF",
        side="yes", action="buy", contracts=4, price_cents=27,
        order_id="ord-okf", fill_id="fill-okf", vertical="sports",
        recipients=["alice@example.com"],
    )
    out1 = asyncio.run(_alerts.operator_trade_alert(**args))
    # Critical: the helper must inspect ok=False and report not-sent,
    # NOT silently mark it delivered.
    assert out1["sends"][0]["sent"] is False, (
        "ok=False from postmark.send must be treated as a send failure "
        "— otherwise the dedupe row silences this fill for 7 days "
        "despite it never being delivered."
    )
    assert "error" in out1["sends"][0]
    # Retry with the same (operator, order, fill): the released dedupe
    # row lets the second attempt actually land.
    out2 = asyncio.run(_alerts.operator_trade_alert(**args))
    assert out2["sends"][0]["sent"] is True
    assert call_idx["n"] == 2, (
        f"expected exactly two send attempts (1 fail, 1 retry), got "
        f"{call_idx['n']} — dedupe was likely not released after ok=False"
    )


def test_fees_cents_renders_in_subject_and_body(shim, captured_sends):
    """Task #712 explicitly asked for 'fees if available' in the
    operator email. When the lifecycle snapshot carries `fees_cents`
    the renderer must surface it in both HTML and plain-text bodies.
    When absent (the common WS-fill case), no phantom $0.00 row
    should appear that would mislead on-call triage."""
    # With fees: row appears in both bodies.
    asyncio.run(_alerts.operator_trade_alert(
        market_ticker="KX-MKT-FEE",
        side="yes", action="buy", contracts=10, price_cents=42,
        order_id="ord-fee", fill_id="fill-fee", vertical="sports",
        recipients=["alice@example.com"],
        fees_cents=37,
    ))
    assert len(captured_sends) == 1
    sent = captured_sends[0]
    assert "$0.37" in sent["text_body"], (
        "fees_cents=37 should render as $0.37 in the plain-text body"
    )
    assert "$0.37" in sent["html_body"], (
        "fees_cents=37 should render in the HTML body too"
    )
    # The 'Fees' label should be present so triage can scan for it.
    assert "Fees" in sent["html_body"]
    assert "fees" in sent["text_body"].lower()

    # Without fees: no Fees row at all (different order/fill so the
    # dedupe doesn't suppress).
    captured_sends.clear()
    asyncio.run(_alerts.operator_trade_alert(
        market_ticker="KX-MKT-NOFEE",
        side="yes", action="buy", contracts=10, price_cents=42,
        order_id="ord-nofee", fill_id="fill-nofee", vertical="sports",
        recipients=["alice@example.com"],
        # fees_cents omitted
    ))
    assert len(captured_sends) == 1
    sent = captured_sends[0]
    assert ">Fees<" not in sent["html_body"], (
        "no Fees row should appear when fees_cents is omitted"
    )
    # Text body uses lowercase key 'fees:' — guard explicitly.
    assert "\nfees" not in sent["text_body"].lower()


def test_lifecycle_operator_alert_failure_does_not_block_live_trade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Architect-flagged coverage gap: even if the branded HTML
    operator alert worker raises (render bug, DB hiccup, postmark
    misconfiguration), the plain-text firehose ``live_trade`` alert
    MUST still go out. They're independent failure boundaries — a
    bug in the rich email path must never silence the on-call
    firehose that operators actually rely on for paging.

    We exercise the full lifecycle path (handle_ws_fill → both
    background tasks) and assert: live_trade was invoked exactly
    once, even though operator_trade_alert raised."""
    from app.kalshi import lifecycle as _lc
    from app.notifications import alerts as _al

    live_trade_calls: list[dict[str, Any]] = []

    def _capture_live_trade(**kwargs: Any) -> dict[str, Any]:
        live_trade_calls.append(kwargs)
        return {"ok": True}

    async def _operator_alert_explodes(**_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("simulated render bug in operator alert")

    monkeypatch.setattr(_al, "live_trade", _capture_live_trade)
    monkeypatch.setattr(_al, "operator_trade_alert", _operator_alert_explodes)

    # Stub everything else lifecycle touches so we can run offline.
    async def _noop(*_a: Any, **_kw: Any) -> None:
        return None

    async def _always_fresh(**_kw: Any) -> bool:
        return True

    monkeypatch.setattr(_lc, "_record", _noop)
    monkeypatch.setattr(_lc, "_emit", _noop)
    monkeypatch.setattr(_lc, "_record_fill_idempotent", _always_fresh)
    monkeypatch.setattr(_lc, "_upsert_position_rollup", _noop)

    # Stub the dedupe DB lookup used by the live_trade background
    # worker so it actually invokes the captured live_trade fn.
    async def _claim_ok(*_a, **_kw) -> bool:
        return True

    monkeypatch.setattr(_al, "dedupe_or_skip", _claim_ok)

    req = _lc.OrderRequest(
        market_ticker="KXISO-FAIL-TEST",
        side="yes", action="buy",
        count=5, limit_price_cents=42,
        vertical="sports",
        client_order_id="coid_iso_fail",
        context={"user_id": "user_iso_fail"},
    )
    rec = _lc.OrderRecord(
        order_id="ord_iso_fail",
        request=req,
        paper=False,
        state="resting",
        exchange_order_id="exch_iso_fail",
    )

    async def go() -> None:
        async with _lc._resting_lock:
            _lc._resting[req.client_order_id] = rec
        msg = {
            "msg": {
                "type": "fill",
                "msg": {
                    "client_order_id": req.client_order_id,
                    "count": 5, "yes_price": 42,
                    "trade_id": "f_iso_fail",
                    "created_time": "2026-04-23T12:00:00Z",
                },
            }
        }
        await _lc.handle_ws_fill(msg)
        # Background tasks need a tick to settle; the operator path
        # raises immediately, the live_trade path gates on a 1s
        # dedupe sleep.
        for _ in range(40):
            await asyncio.sleep(0.05)
            if live_trade_calls:
                break

    asyncio.run(go())

    assert len(live_trade_calls) == 1, (
        f"live_trade firehose must fire exactly once even when "
        f"operator_trade_alert raises; got {len(live_trade_calls)} calls"
    )
    assert live_trade_calls[0].get("market_ticker") == "KXISO-FAIL-TEST"


def test_lifecycle_fees_from_ws_fill_reach_rendered_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a `fee` field on the WS fill payload must flow
    through the lifecycle snapshot, the background worker, and the
    operator_trade_alert renderer into the actual rendered email
    body. Direct unit tests on operator_trade_alert prove the
    renderer plumbing; this test pins the production wiring so a
    future refactor of the lifecycle dispatch can't silently drop
    the fee context the task spec required."""
    from app.kalshi import lifecycle as _lc
    from app.notifications import alerts as _al
    from app.notifications import postmark as _pm

    captured_alert_calls: list[dict[str, Any]] = []
    captured_sends: list[dict[str, Any]] = []

    # Wrap real operator_trade_alert so we can capture both the
    # kwargs it receives AND let it run end-to-end into postmark.send.
    real_operator_alert = _al.operator_trade_alert

    async def _capturing_operator_alert(**kwargs: Any) -> dict[str, Any]:
        captured_alert_calls.append(kwargs)
        return await real_operator_alert(**kwargs)

    monkeypatch.setattr(_al, "operator_trade_alert", _capturing_operator_alert)

    async def _fake_send(**kwargs: Any) -> list[dict[str, Any]]:
        captured_sends.append(kwargs)
        to = kwargs.get("to") or []
        return [{"to": to[0] if to else None, "ok": True}]

    monkeypatch.setattr(_pm, "send", _fake_send)
    monkeypatch.setattr(
        _pm, "alert_recipients", lambda: ["alice@example.com"]
    )
    monkeypatch.setattr(_pm, "is_configured", lambda: True)

    # Stub live_trade so the firehose path is silent in this test.
    monkeypatch.setattr(_al, "live_trade", lambda **_kw: {"stub": True})

    # Stub everything else lifecycle / alerts touches so we run offline.
    async def _noop(*_a: Any, **_kw: Any) -> None:
        return None

    async def _always_fresh(**_kw: Any) -> bool:
        return True

    async def _claim_ok(*_a, **_kw) -> bool:
        return True

    monkeypatch.setattr(_lc, "_record", _noop)
    monkeypatch.setattr(_lc, "_emit", _noop)
    monkeypatch.setattr(_lc, "_record_fill_idempotent", _always_fresh)
    monkeypatch.setattr(_lc, "_upsert_position_rollup", _noop)
    monkeypatch.setattr(_al, "dedupe_or_skip", _claim_ok)

    # The operator alert helper consults operator opt-out + context
    # tables; stub the DB layer so all of those return "no rows".
    from app import db as _db_mod

    async def _fr_none(*_a, **_kw): return None
    async def _f_empty(*_a, **_kw): return []
    async def _fv_none(*_a, **_kw): return None
    async def _exec_ok(*_a, **_kw): return "OK"

    monkeypatch.setattr(_db_mod, "fetchrow", _fr_none)
    monkeypatch.setattr(_db_mod, "fetch", _f_empty)
    monkeypatch.setattr(_db_mod, "fetchval", _fv_none)
    monkeypatch.setattr(_db_mod, "execute", _exec_ok)

    from app.kalshi import market_titles as _mt
    async def _t(_t): return "Test Title"
    monkeypatch.setattr(_mt, "get_title", _t)

    req = _lc.OrderRequest(
        market_ticker="KX-FEE-WIRE",
        side="yes", action="buy",
        count=10, limit_price_cents=42,
        vertical="sports",
        client_order_id="coid_fee_wire",
        context={"user_id": "user_fee_wire"},
    )
    rec = _lc.OrderRecord(
        order_id="ord_fee_wire", request=req,
        paper=False, state="resting",
        exchange_order_id="exch_fee_wire",
    )

    async def go() -> None:
        async with _lc._resting_lock:
            _lc._resting[req.client_order_id] = rec
        msg = {
            "msg": {
                "type": "fill",
                "msg": {
                    "client_order_id": req.client_order_id,
                    "count": 10, "yes_price": 42,
                    "trade_id": "f_fee_wire",
                    "created_time": "2026-04-23T12:00:00Z",
                    # Kalshi WS fill carries an exchange fee on this
                    # variant — must reach the rendered email.
                    "fee": 23,
                },
            }
        }
        await _lc.handle_ws_fill(msg)
        # Background tasks need a tick to settle.
        for _ in range(40):
            await asyncio.sleep(0.05)
            if captured_sends:
                break

    asyncio.run(go())

    assert len(captured_alert_calls) == 1, (
        f"operator_trade_alert must be called exactly once; got "
        f"{len(captured_alert_calls)}"
    )
    assert captured_alert_calls[0].get("fees_cents") == 23, (
        "fees_cents from WS fill payload must propagate through the "
        "lifecycle snapshot into operator_trade_alert kwargs"
    )
    assert len(captured_sends) == 1, (
        f"expected exactly one rendered email, got {len(captured_sends)}"
    )
    sent = captured_sends[0]
    assert "$0.23" in sent["text_body"], (
        "fee from WS fill payload (23¢) must render as $0.23 in the "
        "operator email plain-text body"
    )
    assert "$0.23" in sent["html_body"], (
        "fee from WS fill payload (23¢) must render as $0.23 in the "
        "operator email HTML body"
    )


def test_snapshot_contracts_from_helper_match_input(shim, captured_sends):
    """`operator_trade_alert(contracts=...)` must propagate verbatim
    into the rendered email — proves the lifecycle wiring (which now
    passes the ACCEPTED fill quantity, not `rec.request.count`)
    surfaces correctly in the operator's inbox.

    Regression guard for the case where `rec.request.count` (the
    requested order size, e.g. 100) was previously emailed instead
    of the actual partial-fill quantity (e.g. 25)."""
    # Simulate a partial fill of 7 contracts on a 100-contract order.
    out = asyncio.run(_alerts.operator_trade_alert(
        market_ticker="KX-MKT-PRT",
        side="yes", action="buy", contracts=7, price_cents=33,
        order_id="ord-prt", fill_id="fill-prt-1", vertical="sports",
        recipients=["alice@example.com"],
    ))
    assert out["queued"] is True
    assert "7x" in out["subject"]
    html = captured_sends[0]["html_body"]
    text = captured_sends[0]["text_body"]
    # Body shows 7 contracts and the matching $2.31 (7 × 33¢) total.
    assert "7x" in html
    assert "$2.31" in html
    assert "7" in text


def test_context_fetch_failure_degrades_gracefully(monkeypatch, captured_sends):
    """If every DB call (context fetch, opt-in lookup, dedupe claim)
    raises, the alert must STILL fire with empty context blocks
    rather than blow up the WS fill handler.

    Mirrors the existing "fail open on dedupe DB error" behaviour
    of the firehose alerts: we'd rather risk a duplicate than swallow
    a per-trade operator alert."""
    async def _boom(*_a, **_k):
        raise RuntimeError("db down")
    monkeypatch.setattr(_db, "fetchrow", _boom)
    monkeypatch.setattr(_db, "fetch", _boom)
    monkeypatch.setattr(_db, "fetchval", _boom)
    out = asyncio.run(_alerts.operator_trade_alert(
        market_ticker="KX-MKT-X",
        side="yes", action="buy", contracts=1, price_cents=10,
        order_id="ord-x", fill_id="fill-x", vertical="sports",
        recipients=["alice@example.com"],
    ))
    assert out["queued"] is True
    # Email STILL goes out (fail-open), with whatever context blocks
    # could be salvaged. Position should be the safe zero default.
    assert any(s["sent"] for s in out["sends"])
    assert len(captured_sends) == 1
    html = captured_sends[0]["html_body"]
    assert "/trades/ord-x" in html
    # Empty position block degrades to "flat".
    assert "flat" in html
    # Empty gates block degrades to the empty-state message.
    assert "No pre-trade gate metadata" in html
