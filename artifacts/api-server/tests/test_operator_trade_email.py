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


def test_dedupe_released_on_send_failure_allows_retry(shim, monkeypatch):
    """If Postmark send raises, the dedupe row must be released so
    a follow-up retry of the same (operator, order, fill) can land —
    otherwise a single transient Postmark error would silently
    suppress the alert for 7 days."""
    # First call: postmark.send raises.
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
