"""Per-user trade-confirmation email tests (task #269).

Covers:
  * 7-day dedupe blocks WS fill re-broadcasts for the same
    (user_id, order_id, fill_id).
  * Different `fill_id` on the same order fires a separate email.
  * `digest_partials=true` defers partial fills and consolidates on
    the terminal `filled` state, summing contracts and computing a
    weighted-average price across all `order_fills` rows.
  * `digest_partials=true` + terminal state dedupes its synthetic
    `fill_id="digest"` sentinel so a replay of the final fill does
    not re-email the consolidated receipt.
  * `email_trade_confirmation=false` suppresses entirely.
  * Missing `user_id` on `OrderRequest.context` is a silent no-op.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app import db as _db
from app.kalshi import lifecycle as _lc
from app.notifications import alerts as _alerts


# ---------------------------------------------------------------------------
# Shared in-memory shims
# ---------------------------------------------------------------------------


class _Harness:
    """Minimal Postgres + Postmark stand-in scoped to one test."""

    def __init__(self) -> None:
        self.dedupe: dict[str, datetime] = {}
        self.sent: list[dict[str, Any]] = []
        # keyed by clerk_user_id
        self.users: dict[str, dict[str, Any]] = {}
        # order_id -> list[(count, price_cents)]
        self.fills: dict[str, list[tuple[int, int]]] = {}

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "notification_dedupe" in query:
            key, ttl = args[0], int(args[1])
            now = datetime.now(timezone.utc)
            existing = self.dedupe.get(key)
            if existing is None or existing <= now:
                self.dedupe[key] = now + timedelta(seconds=ttl)
                return {"ok": 1}
            return None
        if "FROM app_users" in query:
            user = self.users.get(args[0])
            if not user:
                return None
            return {
                "email": user.get("email"),
                "conf": user.get("email_trade_confirmation", True),
                "digest": user.get("digest_partials", False),
            }
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "FROM order_fills" in query:
            rows = self.fills.get(args[0], [])
            return [{"count": c, "price_cents": p} for c, p in rows]
        return []

    async def execute(self, *_a: Any, **_kw: Any) -> str:
        return "OK"


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> _Harness:
    h = _Harness()

    monkeypatch.setattr(_db, "fetchrow", h.fetchrow)
    monkeypatch.setattr(_db, "fetch", h.fetch)
    monkeypatch.setattr(_db, "execute", h.execute)

    # Capture every postmark send instead of really emailing.
    async def _fake_send(**kwargs: Any) -> list[dict[str, Any]]:
        h.sent.append(kwargs)
        return [{"MessageID": f"m-{len(h.sent)}"}]

    from app.notifications import postmark as _pm
    monkeypatch.setattr(_pm, "send", _fake_send)

    # Suppression list: nothing is suppressed.
    from app.notifications import suppression as _supp

    async def _not_suppressed(_email: str) -> bool:
        return False

    monkeypatch.setattr(_supp, "is_suppressed", _not_suppressed)

    # Market title resolver — keep offline.
    from app.kalshi import market_titles as _titles

    async def _title(_ticker: str) -> str | None:
        return "Will Bitcoin close above $50,000 on Dec 12?"

    monkeypatch.setattr(_titles, "get_title", _title)

    # Lifecycle side effects we don't exercise here.
    async def _noop(*_a: Any, **_kw: Any) -> None:
        return None

    async def _always_fresh(**_kw: Any) -> bool:
        return True

    monkeypatch.setattr(_lc, "_record", _noop)
    monkeypatch.setattr(_lc, "_emit", _noop)
    monkeypatch.setattr(_lc, "_record_fill_idempotent", _always_fresh)
    monkeypatch.setattr(_lc, "_upsert_position_rollup", _noop)

    # Operator live_trade alert is a separate concern — stub it.
    monkeypatch.setattr(_alerts, "live_trade", lambda **_kw: {"stub": True})

    # Operator branded HTML alert (task #712) is also a separate
    # concern in this suite — stub so it doesn't pull in extra
    # postmark.send calls that would inflate `call_count` in
    # test_trade_confirmation_postmark_failure_is_non_fatal.
    async def _stub_operator_alert(**_kw: Any) -> dict[str, Any]:
        return {"stub": True}

    monkeypatch.setattr(_alerts, "operator_trade_alert", _stub_operator_alert)

    return h


def _mk_record(user_id: str | None = "user_42", count: int = 20) -> _lc.OrderRecord:
    ctx: dict[str, Any] = {}
    if user_id is not None:
        ctx["user_id"] = user_id
    req = _lc.OrderRequest(
        market_ticker="KXBTC-24DEC12-50000",
        side="yes", action="buy",
        count=count, limit_price_cents=42,
        vertical="crypto",
        client_order_id=f"coid_{user_id or 'anon'}",
        context=ctx,
    )
    return _lc.OrderRecord(
        order_id=f"ord_{user_id or 'anon'}",
        request=req, paper=False, state="resting",
        exchange_order_id="exch_1",
    )


async def _feed_fill(rec: _lc.OrderRecord, *, count: int, price: int,
                     trade_id: str | None) -> None:
    """Inject a single WS fill for ``rec``."""
    async with _lc._resting_lock:
        _lc._resting[rec.request.client_order_id] = rec
    rec.state = "resting"
    msg_body: dict[str, Any] = {
        "client_order_id": rec.request.client_order_id,
        "count": count, "yes_price": price,
        "created_time": "2026-04-23T12:00:00Z",
    }
    if trade_id is not None:
        msg_body["trade_id"] = trade_id
    await _lc.handle_ws_fill({"msg": {"type": "fill", "msg": msg_body}})
    # Drain any scheduled email task. The pipeline now has two
    # create_task hops (enrichment -> dispatch) plus several
    # asyncio.wait_for boundaries, so give the loop enough turns
    # to fully flush before we inspect `harness.sent`.
    for _ in range(20):
        await asyncio.sleep(0)


def _confirmations(h: _Harness) -> list[dict[str, Any]]:
    return [s for s in h.sent if s.get("tag") == "trade-confirmation"]


# ---------------------------------------------------------------------------
# 1. Replay dedupe
# ---------------------------------------------------------------------------


def test_trade_confirmation_dedupes_ws_replay(harness: _Harness) -> None:
    harness.users["user_42"] = {"email": "trader@example.com"}

    async def go() -> None:
        rec = _mk_record(count=20)
        # Terminal fill — full qty in one WS message.
        for _ in range(4):
            await _feed_fill(rec, count=20, price=42, trade_id="fill_99")

    asyncio.run(go())
    assert len(_confirmations(harness)) == 1
    sent = _confirmations(harness)[0]
    assert sent["to"] == ["trader@example.com"]
    assert "trader@example.com" in sent["to"]
    assert "KXBTC-24DEC12-50000" in sent["text_body"]
    assert "Bitcoin" in sent["html_body"]
    # Different fill on same order -> separate email.

    async def second() -> None:
        rec2 = _mk_record(count=5)
        rec2.order_id = "ord_user_42"  # same order as before
        await _feed_fill(rec2, count=5, price=43, trade_id="fill_100")

    asyncio.run(second())
    assert len(_confirmations(harness)) == 2


# ---------------------------------------------------------------------------
# 2. Opt-out
# ---------------------------------------------------------------------------


def test_trade_confirmation_respects_opt_out(harness: _Harness) -> None:
    harness.users["user_42"] = {
        "email": "trader@example.com",
        "email_trade_confirmation": False,
    }

    async def go() -> None:
        rec = _mk_record(count=10)
        await _feed_fill(rec, count=10, price=42, trade_id="fill_1")

    asyncio.run(go())
    assert _confirmations(harness) == []


# ---------------------------------------------------------------------------
# 3. Missing user_id -> silent no-op
# ---------------------------------------------------------------------------


def test_trade_confirmation_skips_when_no_user_context(harness: _Harness) -> None:
    # Populate an email for every user so a leak would be visible.
    harness.users["user_42"] = {"email": "trader@example.com"}

    async def go() -> None:
        rec = _mk_record(user_id=None, count=10)
        await _feed_fill(rec, count=10, price=42, trade_id="fill_1")

    asyncio.run(go())
    assert _confirmations(harness) == []


# ---------------------------------------------------------------------------
# 4. digest_partials: defer partials, consolidate on terminal
# ---------------------------------------------------------------------------


def test_trade_confirmation_digest_partials_consolidates(harness: _Harness) -> None:
    harness.users["user_42"] = {
        "email": "trader@example.com",
        "email_trade_confirmation": True,
        "digest_partials": True,
    }

    async def go() -> None:
        rec = _mk_record(count=30)  # 30 to fill total
        # Three partials at different prices.
        await _feed_fill(rec, count=10, price=40, trade_id="f1")
        harness.fills.setdefault(rec.order_id, []).append((10, 40))
        assert _confirmations(harness) == []  # deferred

        await _feed_fill(rec, count=10, price=42, trade_id="f2")
        harness.fills[rec.order_id].append((10, 42))
        assert _confirmations(harness) == []  # still deferred

        # Terminal fill — record the last partial BEFORE handle_ws_fill
        # so the aggregate query sees all three rows (order_fills insert
        # is stubbed out in this test harness).
        harness.fills[rec.order_id].append((10, 44))
        await _feed_fill(rec, count=10, price=44, trade_id="f3")

    asyncio.run(go())
    sent = _confirmations(harness)
    assert len(sent) == 1, f"expected 1 digest email, got {len(sent)}: {sent}"
    body = sent[0]["text_body"]
    # Weighted avg: (10*40 + 10*42 + 10*44) / 30 = 42
    assert "30" in body  # total contracts
    assert "42" in body  # weighted avg cents
    # Dedupe the terminal send: replaying the final fill shouldn't double-send.

    async def replay() -> None:
        rec = _mk_record(count=30)
        await _feed_fill(rec, count=10, price=44, trade_id="f3")

    asyncio.run(replay())
    assert len(_confirmations(harness)) == 1


# ---------------------------------------------------------------------------
# 5. digest off: every partial emails
# ---------------------------------------------------------------------------


def test_handle_ws_fill_returns_promptly_despite_slow_enrichment(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the WS fill hot path must not block on notification
    enrichment. We make the recipient lookup and title resolver sleep
    for longer than `handle_ws_fill` could tolerate; the fill must
    still complete in well under the slowest dependency window.
    """
    harness.users["user_42"] = {"email": "trader@example.com"}

    # Replace harness.fetchrow with a slow variant.
    original = harness.fetchrow

    async def _slow_fetchrow(query: str, *args: Any) -> dict[str, Any] | None:
        if "FROM app_users" in query:
            await asyncio.sleep(1.0)
        return await original(query, *args)

    monkeypatch.setattr(_db, "fetchrow", _slow_fetchrow)

    from app.kalshi import market_titles as _titles

    async def _slow_title(_t: str) -> str | None:
        await asyncio.sleep(1.0)
        return "Slow-resolved title"

    monkeypatch.setattr(_titles, "get_title", _slow_title)

    async def go() -> None:
        rec = _mk_record(count=5)
        async with _lc._resting_lock:
            _lc._resting[rec.request.client_order_id] = rec
        rec.state = "resting"
        msg = {
            "msg": {
                "type": "fill",
                "msg": {
                    "client_order_id": rec.request.client_order_id,
                    "count": 5, "yes_price": 42,
                    "trade_id": "f1",
                    "created_time": "2026-04-23T12:00:00Z",
                },
            }
        }
        start = asyncio.get_running_loop().time()
        await _lc.handle_ws_fill(msg)
        elapsed = asyncio.get_running_loop().time() - start
        # Hot path must return well under the slowest enrichment step
        # (1s). Allow a generous margin for CI jitter.
        assert elapsed < 0.3, f"handle_ws_fill blocked for {elapsed:.3f}s"
        # Wait for the background task to flush.
        for _ in range(30):
            await asyncio.sleep(0.1)
            if _confirmations(harness):
                break

    asyncio.run(go())
    assert len(_confirmations(harness)) == 1


def test_handle_ws_fill_returns_promptly_despite_slow_live_trade_dedupe(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for task #296: the WS fill hot path must not block on
    the operator `live_trade` dedupe DB lookup. Simulate a slow
    `notification_dedupe` UPSERT and assert `handle_ws_fill` still
    returns promptly while the alert eventually dispatches off the hot
    path.
    """
    harness.users["user_42"] = {
        "email": "trader@example.com",
        # Disable the per-user trade-confirmation path so this test
        # isolates the operator-broadcast `live_trade` dedupe latency.
        "email_trade_confirmation": False,
    }

    # Slow down ONLY the notification_dedupe UPSERT — every other
    # query goes through the harness fast path.
    original = harness.fetchrow

    async def _slow_fetchrow(query: str, *args: Any) -> dict[str, Any] | None:
        if "notification_dedupe" in query:
            await asyncio.sleep(1.0)
        return await original(query, *args)

    monkeypatch.setattr(_db, "fetchrow", _slow_fetchrow)

    # Capture every operator live_trade dispatch.
    sent_alerts: list[dict[str, Any]] = []

    def _capture_live_trade(**kwargs: Any) -> dict[str, Any]:
        sent_alerts.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(_alerts, "live_trade", _capture_live_trade)

    async def go() -> None:
        rec = _mk_record(count=5)
        async with _lc._resting_lock:
            _lc._resting[rec.request.client_order_id] = rec
        rec.state = "resting"
        msg = {
            "msg": {
                "type": "fill",
                "msg": {
                    "client_order_id": rec.request.client_order_id,
                    "count": 5, "yes_price": 42,
                    "trade_id": "f1",
                    "created_time": "2026-04-23T12:00:00Z",
                },
            }
        }
        start = asyncio.get_running_loop().time()
        await _lc.handle_ws_fill(msg)
        elapsed = asyncio.get_running_loop().time() - start
        # Hot path must return well under the slow dedupe step (1s).
        # Generous CI margin.
        assert elapsed < 0.3, (
            f"handle_ws_fill blocked for {elapsed:.3f}s on live_trade "
            "dedupe DB lookup"
        )
        # Wait for the background task to flush — the dedupe sleeps 1s
        # so we poll up to a few seconds.
        for _ in range(30):
            await asyncio.sleep(0.1)
            if sent_alerts:
                break

    asyncio.run(go())
    assert len(sent_alerts) == 1, (
        f"expected exactly one live_trade alert, got {len(sent_alerts)}"
    )
    assert sent_alerts[0]["market_ticker"] == "KXBTC-24DEC12-50000"
    assert sent_alerts[0]["fill_id"] == "f1"


def test_trade_confirmation_skips_when_email_missing(
    harness: _Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """User exists (opted in) but has no email on file -> skip with
    a warning log; no send attempted."""
    import logging as _logging

    harness.users["user_42"] = {"email": None}

    async def go() -> None:
        rec = _mk_record(count=5)
        with caplog.at_level(_logging.WARNING, logger="app.kalshi.lifecycle"):
            await _feed_fill(rec, count=5, price=42, trade_id="f1")

    asyncio.run(go())
    assert _confirmations(harness) == []
    assert any(
        "has no email on file" in rec.message for rec in caplog.records
    ), [r.message for r in caplog.records]


def test_trade_confirmation_postmark_failure_is_non_fatal(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A Postmark 5xx-equivalent exception during send must be logged
    and swallowed without breaking subsequent fills."""
    import logging as _logging

    harness.users["user_42"] = {"email": "trader@example.com"}

    from app.notifications import postmark as _pm
    call_count = {"n": 0}

    async def _flaky_send(**kwargs: Any) -> list[dict[str, Any]]:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("postmark 500: server error")
        harness.sent.append(kwargs)
        return [{"MessageID": "ok"}]

    monkeypatch.setattr(_pm, "send", _flaky_send)

    async def go() -> None:
        with caplog.at_level(_logging.WARNING, logger="app.notifications.alerts"):
            rec1 = _mk_record(count=5)
            await _feed_fill(rec1, count=5, price=42, trade_id="f1")
            # Different order -> next send succeeds; the first failure
            # must not have poisoned anything.
            rec2 = _mk_record(count=5)
            rec2.request.client_order_id = "coid_user_42_b"
            rec2.order_id = "ord_user_42_b"
            await _feed_fill(rec2, count=5, price=42, trade_id="f2")

    asyncio.run(go())
    # First send raised; second succeeded.
    assert call_count["n"] == 2
    assert len(_confirmations(harness)) == 1
    assert any(
        "trade_confirmation send failed" in rec.message
        for rec in caplog.records
    ), [r.message for r in caplog.records]


def test_render_trade_confirmation_html_snapshot() -> None:
    """Stable HTML snapshot: guards against accidental template
    regressions (layout, fields, dashboard link). Not a full DOM diff
    — we assert the important invariants a user would notice."""
    from app.notifications.alerts import _render_trade_confirmation_html

    html = _render_trade_confirmation_html(
        market_title="Will Bitcoin close above $50,000 on Dec 12?",
        market_ticker="KXBTC-24DEC12-50000",
        side="yes", action="buy",
        contracts=30,
        avg_price_cents=42,
        total_cost_cents=1260,
        current_position=30,
        dashboard_url="https://example.com/positions",
        vertical="crypto",
        order_id="ord_42",
        fill_id="fill_99",
        timestamp_iso="2026-04-23T12:00:00Z",
        partial=False,
        digest=False,
        fills_summed=None,
    )
    # Core visible fields.
    assert "Will Bitcoin close above $50,000 on Dec 12?" in html
    assert "KXBTC-24DEC12-50000" in html
    assert "BUY" in html or "Buy" in html or "buy" in html.lower()
    assert "YES" in html or "Yes" in html or "yes" in html.lower()
    assert "30" in html  # contracts / position
    # Price rendered in cents or dollars (either is fine — we just
    # guard against the field being dropped entirely).
    assert ("42" in html) or ("0.42" in html) or ("$12.60" in html)
    # Total cost somewhere.
    assert ("1260" in html) or ("12.60" in html)
    # Dashboard link.
    assert 'href="https://example.com/positions"' in html
    # Timestamp surfaced.
    assert "2026-04-23" in html
    # HTML shell.
    assert "<html" in html.lower()
    assert "</html>" in html.lower()


def test_trade_confirmation_per_fill_default(harness: _Harness) -> None:
    harness.users["user_42"] = {
        "email": "trader@example.com",
        "email_trade_confirmation": True,
        "digest_partials": False,
    }

    async def go() -> None:
        rec = _mk_record(count=30)
        await _feed_fill(rec, count=10, price=40, trade_id="f1")
        await _feed_fill(rec, count=10, price=42, trade_id="f2")
        await _feed_fill(rec, count=10, price=44, trade_id="f3")

    asyncio.run(go())
    assert len(_confirmations(harness)) == 3
