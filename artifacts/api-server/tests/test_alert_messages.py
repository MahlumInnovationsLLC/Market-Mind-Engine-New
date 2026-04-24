"""Subject/body assembly + dry-run tests for the operator alert helpers.

Covers each of the six task-#11 alert types listed in the spec:
  - tier_change
  - live_trade
  - halt
  - drift
  - daily_pnl_summary
  - pending_approval

These tests intentionally never reach the network — they exercise both
the `dry_run=True` path (returns subject/body_preview without queuing)
and the live path (monkeypatches `postmark.send_in_background` so we
can assert the same subject/body are handed off without needing a real
Postmark token).

Also re-verifies the per-`(alert_type, key)` TTL semantics of
`dedupe_or_skip` against an in-memory shim, so a regression in the
TTL window would surface in this file directly.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app import db as _db
from app.notifications import alerts as _alerts
from app.notifications import postmark as _postmark


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_postmark_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make sure these tests never accidentally hit a real Postmark
    token that happens to be set in the runner's environment."""
    monkeypatch.delenv("POSTMARK_SERVER_TOKEN", raising=False)
    monkeypatch.delenv("OPERATOR_NOTIFY_EMAIL", raising=False)
    monkeypatch.setenv("OPERATOR_ALERT_EMAILS", "ops@example.com")


@pytest.fixture
def captured_sends(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture every `postmark.send_in_background(**kwargs)` call.

    Replaces the helper with a sync stub so the live alert path doesn't
    need a running event loop and can't accidentally fan out to push /
    SMS / Telegram channels (those imports are exercised in their own
    test modules)."""
    calls: list[dict[str, Any]] = []

    def _capture(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(_postmark, "send_in_background", _capture)

    # Block live-trade premium-channel fan-out: those code paths import
    # the SMS + Telegram modules and try to spawn tasks on a running
    # loop. They have their own dedicated tests; here we only care
    # about the email assembly.
    monkeypatch.setattr(
        _alerts, "_schedule_premium_live_trade", lambda **_kw: None
    )
    # Block the user-pref email fanout for the same reason — it spawns
    # tasks on the loop and queries the DB.
    monkeypatch.setattr(
        _alerts, "_schedule_user_email_fanout", lambda **_kw: None
    )
    return calls


# ---------------------------------------------------------------------------
# tier_change
# ---------------------------------------------------------------------------


def test_tier_change_dry_run_subject_and_body() -> None:
    out = _alerts.tier_change(
        user_email="alice@example.com",
        user_id="user_alice",
        old_tier="pro",
        new_tier="elite",
        status="active",
        stripe_subscription_id="sub_123",
        dry_run=True,
    )
    assert out["dry_run"] is True
    assert out["tag"] == "tier-change"
    assert "upgraded" in out["subject"]
    assert "pro -> elite" in out["subject"]
    assert "alice@example.com" in out["subject"]
    body = out["body_preview"]
    assert "user_email" in body and "alice@example.com" in body
    assert "user_id" in body and "user_alice" in body
    assert "old_tier" in body and "pro" in body
    assert "new_tier" in body and "elite" in body
    assert "stripe_subscription_id" in body and "sub_123" in body
    assert "active" in body
    # Recipient list is materialised so the operator can sanity-check it.
    assert out["would_send_to"] == ["ops@example.com"]
    assert out["configured"] is False


def test_tier_change_downgrade_direction_label() -> None:
    out = _alerts.tier_change(
        user_email=None, user_id="u1",
        old_tier="elite", new_tier="pro",
        dry_run=True,
    )
    assert "downgraded" in out["subject"]
    # Falls back to user_id when no email supplied.
    assert "u1" in out["subject"]
    assert "(unknown)" in out["body_preview"]


def test_tier_change_live_path_queues_send(
    captured_sends: list[dict[str, Any]],
) -> None:
    out = _alerts.tier_change(
        user_email="alice@example.com", user_id="user_alice",
        old_tier="pro", new_tier="elite",
    )
    assert out["dry_run"] is False
    assert out["queued"] is True
    assert len(captured_sends) == 1
    sent = captured_sends[0]
    assert sent["tag"] == "tier-change"
    assert "upgraded" in sent["subject"]
    assert "user_alice" in sent["text_body"]


# ---------------------------------------------------------------------------
# live_trade
# ---------------------------------------------------------------------------


def test_live_trade_dry_run_subject_and_body() -> None:
    out = _alerts.live_trade(
        market_ticker="MKT-X",
        side="yes",
        action="buy",
        contracts=12,
        price_cents=42,
        order_id="ord_99",
        fill_id="fill_1",
        vertical="sports",
        dry_run=True,
    )
    assert out["dry_run"] is True
    assert out["tag"] == "live-trade"
    # Default exchange is kalshi — venue tag appears in the subject
    # bracket and the human-readable body preamble.
    assert "[LIVE kalshi]" in out["subject"]
    assert "BUY YES 12x MKT-X @ 42¢" in out["subject"]
    body = out["body_preview"]
    assert "A LIVE Kalshi order filled" in body
    for token in (
        "market", "MKT-X", "ord_99", "fill_1", "sports", "42¢",
        "exchange", "kalshi",
    ):
        assert token in body, token


def test_live_trade_polymarket_exchange_in_subject_and_body() -> None:
    out = _alerts.live_trade(
        market_ticker="0xCOND",
        side="yes",
        action="buy",
        contracts=5,
        price_cents=45,
        order_id="cli-1",
        fill_id="trade-A",
        vertical="sports",
        exchange="polymarket",
        dry_run=True,
    )
    assert "[LIVE polymarket]" in out["subject"]
    body = out["body_preview"]
    # The Polymarket-flavoured preamble replaces the old hard-coded
    # "A LIVE Kalshi order filled." copy so on-call operators paged on
    # a Polymarket fill see the right venue immediately.
    assert "A LIVE Polymarket order filled" in body
    assert "Kalshi" not in body
    assert "exchange" in body and "polymarket" in body


def test_live_trade_market_price_when_no_price_cents() -> None:
    out = _alerts.live_trade(
        market_ticker="MKT-Y", side="no", action="sell",
        contracts=3, price_cents=None, dry_run=True,
    )
    assert "(market)" in out["subject"]
    assert "(market)" in out["body_preview"]


def test_live_trade_live_path_queues_send(
    captured_sends: list[dict[str, Any]],
) -> None:
    out = _alerts.live_trade(
        market_ticker="MKT-Z", side="yes", action="buy",
        contracts=1, price_cents=10,
        order_id="ord_1", fill_id="fill_1",
    )
    assert out["queued"] is True
    assert len(captured_sends) == 1
    assert captured_sends[0]["tag"] == "live-trade"
    assert "[LIVE kalshi]" in captured_sends[0]["subject"]
    assert "BUY YES 1x MKT-Z @ 10¢" in captured_sends[0]["subject"]
    assert "A LIVE Kalshi order filled" in captured_sends[0]["text_body"]


def test_live_trade_live_path_queues_polymarket_send(
    captured_sends: list[dict[str, Any]],
) -> None:
    out = _alerts.live_trade(
        market_ticker="0xCOND", side="no", action="sell",
        contracts=2, price_cents=55,
        order_id="cli-2", fill_id="trade-B",
        exchange="polymarket",
    )
    assert out["queued"] is True
    assert len(captured_sends) == 1
    sent = captured_sends[0]
    assert sent["tag"] == "live-trade"
    assert "[LIVE polymarket]" in sent["subject"]
    assert "A LIVE Polymarket order filled" in sent["text_body"]
    assert "Kalshi" not in sent["text_body"]


# ---------------------------------------------------------------------------
# halt
# ---------------------------------------------------------------------------


def test_halt_dry_run_subject_and_body() -> None:
    out = _alerts.halt(
        reason="data feed stale", actor="watchdog", dry_run=True,
    )
    assert out["dry_run"] is True
    assert out["tag"] == "halt"
    assert "[HALT]" in out["subject"]
    body = out["body_preview"]
    assert "data feed stale" in body
    assert "watchdog" in body


def test_halt_defaults_when_reason_and_actor_missing() -> None:
    out = _alerts.halt(reason=None, dry_run=True)
    body = out["body_preview"]
    assert "(unspecified)" in body
    assert "(api)" in body


def test_halt_live_path_queues_send(
    captured_sends: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # halt() also calls push.push_halt — stub the push module so the
    # email-side test stays focused.
    from app.notifications import push as _push
    monkeypatch.setattr(_push, "push_halt", lambda **_kw: None)
    out = _alerts.halt(reason="kill switch", actor="ops")
    assert out["queued"] is True
    assert len(captured_sends) == 1
    assert captured_sends[0]["tag"] == "halt"
    assert "kill switch" in captured_sends[0]["text_body"]


# ---------------------------------------------------------------------------
# drift
# ---------------------------------------------------------------------------


def test_drift_dry_run_subject_and_body() -> None:
    out = _alerts.drift(
        vertical="sports",
        metric="brier_score_24h",
        value=0.27,
        threshold=0.25,
        note="warning band",
        dry_run=True,
    )
    assert out["dry_run"] is True
    assert out["tag"] == "drift"
    assert "[DRIFT]" in out["subject"]
    assert "sports" in out["subject"]
    assert "brier_score_24h" in out["subject"]
    body = out["body_preview"]
    assert "0.27" in body
    assert "0.25" in body
    assert "warning band" in body


def test_drift_handles_missing_value_threshold() -> None:
    out = _alerts.drift(
        vertical="weather", metric="brier_score_24h",
        value=None, threshold=None, dry_run=True,
    )
    body = out["body_preview"]
    assert "(n/a)" in body


def test_drift_live_path_queues_send(
    captured_sends: list[dict[str, Any]],
) -> None:
    out = _alerts.drift(
        vertical="sports", metric="brier_score_24h",
        value=0.30, threshold=0.25,
    )
    assert out["queued"] is True
    assert len(captured_sends) == 1
    assert captured_sends[0]["tag"] == "drift"


# ---------------------------------------------------------------------------
# daily_pnl_summary
# ---------------------------------------------------------------------------


def test_daily_pnl_summary_dry_run_subject_and_body() -> None:
    out = _alerts.daily_pnl_summary(
        date_utc="2026-04-22",
        realized_pnl_cents=12345,    # +$123.45
        unrealized_pnl_cents=-678,   # -$6.78
        trades_settled=7,
        fills_today=14,
        open_positions=3,
        cycles_completed=42,
        halts_today=0,
        drift_alerts_today=1,
        top_movers=[
            {"market_ticker": "MKT-A", "realized_pnl_cents": 500, "position": 5},
        ],
        by_vertical=[{
            "vertical": "sports", "trades": 3, "wins": 2, "losses": 1,
            "pnl_cents": 1000, "win_rate_pct": 66.7, "avg_edge": 0.05,
            "brier": 0.18,
        }],
        by_persona=[{
            "persona_id": "skeptic", "debates": 4, "brier": 0.21,
            "accuracy_pct": 75.0, "avg_confidence": 0.6,
            "lean_yes": 2, "lean_no": 1, "lean_pass": 1,
        }],
        dry_run=True,
    )
    assert out["dry_run"] is True
    assert out["tag"] == "daily-summary"
    assert "2026-04-22" in out["subject"]
    assert "+$123.45" in out["subject"]
    assert "(7 settled)" in out["subject"]
    body = out["body_preview"]
    assert "+$123.45" in body
    assert "-$6.78" in body
    assert "MKT-A" in body
    assert "sports" in body and "skeptic" in body
    # An HTML preview is provided for HTML-rendering inboxes.
    assert out["html_preview"] is not None
    assert "Daily summary" in out["html_preview"]
    assert "2026-04-22" in out["html_preview"]


def test_daily_pnl_summary_live_path_queues_html(
    captured_sends: list[dict[str, Any]],
) -> None:
    out = _alerts.daily_pnl_summary(
        date_utc="2026-04-22",
        realized_pnl_cents=0, unrealized_pnl_cents=0,
        trades_settled=0, fills_today=0, open_positions=0,
        cycles_completed=0, halts_today=0, drift_alerts_today=0,
    )
    assert out["queued"] is True
    assert len(captured_sends) == 1
    sent = captured_sends[0]
    assert sent["tag"] == "daily-summary"
    assert sent["html_body"] is not None
    assert "2026-04-22" in sent["text_body"]


# ---------------------------------------------------------------------------
# pending_approval
# ---------------------------------------------------------------------------


def test_pending_approval_dry_run_subject_and_body() -> None:
    out = _alerts.pending_approval(
        user_id="user_42", user_email="bob@example.com", dry_run=True,
    )
    assert out["dry_run"] is True
    assert out["tag"] == "pending-approval"
    assert "Teammate awaiting approval" in out["subject"]
    assert "bob@example.com" in out["subject"]
    body = out["body_preview"]
    assert "bob@example.com" in body
    assert "user_42" in body
    # No OPERATOR_NOTIFY_EMAIL set in the autouse fixture, so it falls
    # back to the standard alert recipient list.
    assert out["would_send_to"] == ["ops@example.com"]


def test_pending_approval_uses_operator_notify_email_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "OPERATOR_NOTIFY_EMAIL", "founder@example.com, second@example.com"
    )
    out = _alerts.pending_approval(
        user_id="u1", user_email=None, dry_run=True,
    )
    assert out["would_send_to"] == [
        "founder@example.com", "second@example.com",
    ]
    assert "(unknown)" in out["body_preview"]


def test_pending_approval_live_path_queues_send(
    captured_sends: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPERATOR_NOTIFY_EMAIL", "approval@example.com")
    out = _alerts.pending_approval(
        user_id="user_42", user_email="bob@example.com",
    )
    assert out["queued"] is True
    assert out["recipients"] == ["approval@example.com"]
    assert len(captured_sends) == 1
    sent = captured_sends[0]
    assert sent["tag"] == "pending-approval"
    assert sent["to"] == ["approval@example.com"]
    assert "bob@example.com" in sent["subject"]


# ---------------------------------------------------------------------------
# dedupe_or_skip TTL window per (alert_type, key)
# ---------------------------------------------------------------------------


class _FakeDedupeStore:
    """Minimal in-memory shim that mimics the
    `INSERT ... ON CONFLICT ... WHERE expires_at <= now()` semantics
    we rely on for `dedupe_or_skip`."""

    def __init__(self) -> None:
        self.rows: dict[str, datetime] = {}

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        assert "notification_dedupe" in query
        key, ttl = args[0], int(args[1])
        now = datetime.now(timezone.utc)
        existing = self.rows.get(key)
        if existing is None or existing <= now:
            self.rows[key] = now + timedelta(seconds=ttl)
            return {"ok": 1}
        return None


@pytest.fixture
def dedupe_store(monkeypatch: pytest.MonkeyPatch) -> _FakeDedupeStore:
    store = _FakeDedupeStore()
    monkeypatch.setattr(_db, "fetchrow", store.fetchrow)
    _alerts.reset_suppression_counts()
    return store


def test_dedupe_or_skip_only_returns_true_once_per_ttl_per_key(
    dedupe_store: _FakeDedupeStore,
) -> None:
    """Two replays inside the TTL window: only the first claim wins.
    Different alert_type or different key claims independently."""

    async def go() -> None:
        # First (halt, k1) inside TTL window — wins.
        assert await _alerts.dedupe_or_skip(
            "halt", "k1", ttl_seconds=3600
        ) is True
        # Replay (halt, k1) — suppressed.
        assert await _alerts.dedupe_or_skip(
            "halt", "k1", ttl_seconds=3600
        ) is False
        assert await _alerts.dedupe_or_skip(
            "halt", "k1", ttl_seconds=3600
        ) is False

        # Same key but different alert_type — separate namespace, wins.
        assert await _alerts.dedupe_or_skip(
            "drift", "k1", ttl_seconds=3600
        ) is True
        assert await _alerts.dedupe_or_skip(
            "drift", "k1", ttl_seconds=3600
        ) is False

        # Same alert_type + different key — wins.
        assert await _alerts.dedupe_or_skip(
            "halt", "k2", ttl_seconds=3600
        ) is True

        # Force the (halt, k1) row past its TTL — next call wins again.
        dedupe_store.rows["halt:k1"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        )
        assert await _alerts.dedupe_or_skip(
            "halt", "k1", ttl_seconds=3600
        ) is True

    asyncio.run(go())

    # Suppression counter tracks the dropped replays per alert_type.
    counts = _alerts.suppression_counts()
    assert counts.get("halt") == 2
    assert counts.get("drift") == 1
