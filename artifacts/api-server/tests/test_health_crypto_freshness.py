"""Tests for the crypto OHLCV per-symbol freshness health probe."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from app import health as health_mod


class _StubDB:
    def __init__(self, rows):
        self._rows = rows
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, query, *args):
        self.calls.append((query, args))
        return list(self._rows)


def _run(coro):
    return asyncio.run(coro)


def test_unconfigured_when_no_provider_keys(monkeypatch):
    monkeypatch.delenv("COINAPI_API_KEY", raising=False)
    monkeypatch.delenv("COINAPI_KEY", raising=False)
    monkeypatch.delenv("COINGECKO_API_KEY", raising=False)
    card = _run(health_mod._probe_crypto_freshness())
    assert card["status"] == "unconfigured"
    assert card["key"] == "crypto_feed_freshness"


def test_all_pairs_fresh_returns_ok(monkeypatch):
    monkeypatch.setenv("COINAPI_API_KEY", "x")
    now = time.time()
    rows = []
    from app.feeds import crypto as crypto_feeds
    for sym in crypto_feeds.DEFAULT_SYMBOLS:
        for gran in crypto_feeds.GRANULARITIES:
            rows.append({
                "symbol": sym,
                "granularity": gran,
                "max_ts": datetime.fromtimestamp(now - 5, tz=timezone.utc),
            })
    stub = _StubDB(rows)
    monkeypatch.setattr(health_mod, "db", stub)
    card = _run(health_mod._probe_crypto_freshness())
    assert card["status"] == "ok"
    assert card["extras"]["fresh_count"] == card["extras"]["total_count"]


def test_stale_1m_bar_marks_card_down(monkeypatch):
    monkeypatch.setenv("COINAPI_API_KEY", "x")
    now = time.time()
    from app.feeds import crypto as crypto_feeds
    rows = []
    for sym in crypto_feeds.DEFAULT_SYMBOLS:
        for gran in crypto_feeds.GRANULARITIES:
            # Make the 1m bar very stale (1h old) for the first symbol;
            # everything else is fresh.
            age = 3600 if (sym == crypto_feeds.DEFAULT_SYMBOLS[0] and gran == "1m") else 5
            rows.append({
                "symbol": sym,
                "granularity": gran,
                "max_ts": datetime.fromtimestamp(now - age, tz=timezone.utc),
            })
    monkeypatch.setattr(health_mod, "db", _StubDB(rows))
    card = _run(health_mod._probe_crypto_freshness())
    assert card["status"] == "down"
    bad = [p for p in card["extras"]["pairs"] if p["status"] == "down"]
    assert any(p["granularity"] == "1m" for p in bad)


def test_missing_pair_marks_card_down(monkeypatch):
    monkeypatch.setenv("COINAPI_API_KEY", "x")
    monkeypatch.setattr(health_mod, "db", _StubDB([]))
    card = _run(health_mod._probe_crypto_freshness())
    assert card["status"] == "down"
    # Every expected pair shows up as missing.
    assert all(p["status"] == "down" for p in card["extras"]["pairs"])
    assert card["extras"]["fresh_count"] == 0


def test_moderately_stale_marks_card_degraded(monkeypatch):
    monkeypatch.setenv("COINAPI_API_KEY", "x")
    now = time.time()
    from app.feeds import crypto as crypto_feeds
    rows = []
    for sym in crypto_feeds.DEFAULT_SYMBOLS:
        for gran in crypto_feeds.GRANULARITIES:
            # 1m bar 5 minutes old: > yellow (max(180, 120)=180) but
            # < red (max(600, 600)=600). Should be "degraded".
            age = 300 if (sym == crypto_feeds.DEFAULT_SYMBOLS[0] and gran == "1m") else 5
            rows.append({
                "symbol": sym,
                "granularity": gran,
                "max_ts": datetime.fromtimestamp(now - age, tz=timezone.utc),
            })
    monkeypatch.setattr(health_mod, "db", _StubDB(rows))
    card = _run(health_mod._probe_crypto_freshness())
    assert card["status"] == "degraded"


def test_db_error_marks_card_down(monkeypatch):
    monkeypatch.setenv("COINAPI_API_KEY", "x")

    class _Boom:
        async def fetch(self, *a, **kw):
            raise RuntimeError("boom")

    monkeypatch.setattr(health_mod, "db", _Boom())
    card = _run(health_mod._probe_crypto_freshness())
    assert card["status"] == "down"
    assert "boom" in card["detail"]
