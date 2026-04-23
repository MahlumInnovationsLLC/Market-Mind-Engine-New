"""Tests for `/api/me/notification-history` (task #192).

Covers `alerts.list_user_history`:
  * Broadcast alerts (halt/drift/etc.) appear for every user.
  * User-scoped alerts (`trade_confirmation`, `tier_change`) only appear
    for the matching `user_id` prefix.
  * Channel breakdown reflects the caller's current opt-in state.
  * Unknown dedupe keys are ignored.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app import db as _db
from app.notifications import alerts as _alerts
from app.notifications import push as _push


class _FakeRows:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def fake_fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        assert "notification_dedupe" in query
        # The real SQL alternates `starts_with(key, $prefix)` with
        # `key = $exact` predicates. Mirror that here exactly — using
        # plain string `startswith` (NOT SQL `LIKE`) — so the test
        # fixture stays honest about wildcard semantics.
        limit = int(args[-1])
        params = list(args[:-1])
        # Even-indexed args are starts_with prefixes; odd-indexed are
        # exact matches. Both lists may include trailing colons.
        prefixes = params[0::2]
        exacts = params[1::2]

        def matches(key: str) -> bool:
            for p in prefixes:
                if key.startswith(str(p)):
                    return True
            for e in exacts:
                if key == str(e):
                    return True
            return False

        rows = [r for r in self.rows if matches(r.get("key") or "")]
        rows.sort(
            key=lambda r: r.get("sent_at") or datetime.min, reverse=True
        )
        return rows[:limit]


@pytest.fixture
def fake_prefs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    prefs: dict[str, Any] = {
        "email_daily_summary": True,
        "email_drift": False,
        "email_halt": True,
        "notify_halt": True,
        "email_live_trade": False,
        "notify_live_trade": True,
        "sms_live_trade": False,
        "telegram_live_trade": False,
        "email_weekly_roundup": True,
        "email_trade_confirmation": True,
    }

    async def _get_prefs(_uid: str) -> dict[str, Any]:
        return dict(prefs)

    monkeypatch.setattr(_push, "get_prefs", _get_prefs)
    return prefs


def test_list_user_history_filters_user_scoped_rows(
    monkeypatch: pytest.MonkeyPatch, fake_prefs: dict[str, Any]
) -> None:
    now = datetime.now(timezone.utc)
    me = "user_me"
    other = "user_other"
    rows = [
        {"key": "halt:abcdef", "sent_at": now - timedelta(minutes=1),
         "expires_at": None, "date_utc": None},
        {"key": "drift:nfl:warn", "sent_at": now - timedelta(minutes=2),
         "expires_at": None, "date_utc": None},
        {"key": f"trade_confirmation:{me}:order1:fill1",
         "sent_at": now - timedelta(minutes=3),
         "expires_at": None, "date_utc": None},
        {"key": f"trade_confirmation:{other}:order2:fill2",
         "sent_at": now - timedelta(minutes=4),
         "expires_at": None, "date_utc": None},
        {"key": f"tier_change:{me}:elite",
         "sent_at": now - timedelta(minutes=5),
         "expires_at": None, "date_utc": None},
        {"key": "totally-unknown-tag", "sent_at": now - timedelta(minutes=6),
         "expires_at": None, "date_utc": None},
        {"key": "daily_pnl_summary", "sent_at": now - timedelta(minutes=7),
         "expires_at": None, "date_utc": None},
    ]
    store = _FakeRows(rows)
    monkeypatch.setattr(_db, "fetch", store.fake_fetch)

    items = asyncio.run(_alerts.list_user_history(user_id=me, limit=30))
    keys = [it["dedupe_key"] for it in items]

    assert "halt:abcdef" in keys
    assert "drift:nfl:warn" in keys
    assert "daily_pnl_summary" in keys
    assert f"trade_confirmation:{me}:order1:fill1" in keys
    assert f"tier_change:{me}:elite" in keys

    # Other users' user-scoped rows are filtered out.
    assert f"trade_confirmation:{other}:order2:fill2" not in keys
    # Unknown keys are dropped.
    assert "totally-unknown-tag" not in keys

    # Ordering is newest-first.
    assert keys[0] == "halt:abcdef"


def test_list_user_history_channel_breakdown_uses_prefs(
    monkeypatch: pytest.MonkeyPatch, fake_prefs: dict[str, Any]
) -> None:
    now = datetime.now(timezone.utc)
    rows = [
        {"key": "live_trade:order:fill", "sent_at": now,
         "expires_at": None, "date_utc": None},
    ]
    monkeypatch.setattr(_db, "fetch", _FakeRows(rows).fake_fetch)

    items = asyncio.run(_alerts.list_user_history(user_id="u", limit=10))
    assert len(items) == 1
    by_kind = {c["kind"]: c for c in items[0]["channels"]}
    # email_live_trade is False in the fixture; notify_live_trade is True.
    assert by_kind["email"]["opted_in"] is False
    assert by_kind["push"]["opted_in"] is True
    assert by_kind["sms"]["opted_in"] is False
    assert by_kind["telegram"]["opted_in"] is False
    assert items[0]["attempted"] is True
    assert items[0]["alert_type"] == "live_trade"


def test_list_user_history_underscored_user_ids_dont_leak_across_users(
    monkeypatch: pytest.MonkeyPatch, fake_prefs: dict[str, Any]
) -> None:
    """Regression: SQL `LIKE` treats `_` as a single-char wildcard, so a
    naive `key LIKE 'trade_confirmation:user_abc:%'` would also match
    `trade_confirmation:user_xbc:...`. Clerk IDs always contain
    underscores, so this is a real cross-user leak risk. The helper
    must use `starts_with` (or equivalent exact-prefix matching) to
    avoid it.
    """
    now = datetime.now(timezone.utc)
    me = "user_abc"
    sibling = "user_xbc"   # differs from `me` only in one wildcard slot
    rows = [
        {"key": f"trade_confirmation:{me}:o1:f1",
         "sent_at": now - timedelta(minutes=1),
         "expires_at": None, "date_utc": None},
        {"key": f"trade_confirmation:{sibling}:o2:f2",
         "sent_at": now - timedelta(minutes=2),
         "expires_at": None, "date_utc": None},
        {"key": f"tier_change:{sibling}:elite",
         "sent_at": now - timedelta(minutes=3),
         "expires_at": None, "date_utc": None},
    ]
    monkeypatch.setattr(_db, "fetch", _FakeRows(rows).fake_fetch)

    items = asyncio.run(_alerts.list_user_history(user_id=me, limit=30))
    keys = [it["dedupe_key"] for it in items]
    assert keys == [f"trade_confirmation:{me}:o1:f1"], keys


def test_list_user_history_swallows_db_errors(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("db down")

    monkeypatch.setattr(_db, "fetch", boom)
    out = asyncio.run(_alerts.list_user_history(user_id="u", limit=30))
    assert out == []
