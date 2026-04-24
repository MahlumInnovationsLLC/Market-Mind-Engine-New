"""Tests for the crypto OHLCV per-pair freshness alert path.

Verifies that ``health._probe_crypto_freshness`` fans a Postmark alert
through the existing alert pipeline when a (symbol, granularity) pair
stays `down` (no fresh bar within the red threshold) for N consecutive
snapshots, and that the dedupe table suppresses follow-up alerts on
the same pair within the TTL.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app import db as _db, health as health_mod
from app.notifications import alerts as _alerts


class _FakeDedupe:
    def __init__(self) -> None:
        self.rows: dict[str, datetime] = {}
        self.dispatched: list[dict[str, Any]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        assert "notification_dedupe" in query
        key, ttl = args[0], int(args[1])
        now = datetime.now(timezone.utc)
        if key not in self.rows or self.rows[key] <= now:
            self.rows[key] = now + timedelta(seconds=ttl)
            return {"ok": 1}
        return None


class _StubDB:
    """Minimal asyncpg-shaped stub for the freshness probe.

    The probe imports ``db`` from the health module and only calls
    ``db.fetch(...)``; we record each call and return the configured
    rows so each test can drive the per-pair status independently.
    """

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, query: str, *args: Any) -> list[dict]:
        self.calls.append((query, args))
        return list(self.rows)


@pytest.fixture
def fake_pipe(monkeypatch: pytest.MonkeyPatch) -> _FakeDedupe:
    fake = _FakeDedupe()
    monkeypatch.setattr(_db, "fetchrow", fake.fetchrow)

    def _fake_dispatch(subject: str, text_body: str, *, tag: str, dry_run: bool):
        fake.dispatched.append({"subject": subject, "body": text_body, "tag": tag})
        return {"tag": tag, "dispatched": True}

    monkeypatch.setattr(_alerts, "_dispatch", _fake_dispatch)
    # Provider key so the probe doesn't short-circuit `unconfigured`.
    monkeypatch.setenv("COINAPI_API_KEY", "x")
    # Reset per-test counters so tests are decoupled.
    health_mod._crypto_freshness_bad_counts.clear()
    return fake


def _crypto_alerts(fake: _FakeDedupe) -> list[dict[str, Any]]:
    return [d for d in fake.dispatched if d["tag"] == "crypto-feed-silent"]


def _stale_rows(*, age_seconds: float | None) -> list[dict]:
    """Build a fetch-result list where every (symbol, granularity) pair
    is either missing (``age_seconds is None``) or pegged at a chosen
    age.  Anything older than ~``max(seconds * 10, 600)`` is `down`.
    """
    from app.feeds import crypto as crypto_feeds
    now = datetime.now(timezone.utc).timestamp()
    rows: list[dict] = []
    if age_seconds is None:
        return rows
    for sym in crypto_feeds.DEFAULT_SYMBOLS:
        for gran in crypto_feeds.GRANULARITIES:
            ts = datetime.fromtimestamp(now - age_seconds, tz=timezone.utc)
            rows.append({
                "symbol": sym,
                "granularity": gran,
                "max_ts": ts,
            })
    return rows


def _fresh_rows() -> list[dict]:
    from app.feeds import crypto as crypto_feeds
    now = datetime.now(timezone.utc).timestamp()
    rows: list[dict] = []
    for sym in crypto_feeds.DEFAULT_SYMBOLS:
        for gran in crypto_feeds.GRANULARITIES:
            rows.append({
                "symbol": sym,
                "granularity": gran,
                "max_ts": datetime.fromtimestamp(now - 5, tz=timezone.utc),
            })
    return rows


def _run(coro):
    return asyncio.run(coro)


def test_alert_fires_after_threshold_consecutive_down_snapshots(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_mod, "CRYPTO_FRESHNESS_ALERT_THRESHOLD", 3)
    # No bars at all -> every (symbol, granularity) pair is `down`.
    monkeypatch.setattr(health_mod, "db", _StubDB([]))

    # First two snapshots: counters increment, no alert dispatched yet.
    _run(health_mod._probe_crypto_freshness())
    _run(health_mod._probe_crypto_freshness())
    assert _crypto_alerts(fake_pipe) == []

    # Third snapshot crosses the threshold -> one alert per down pair.
    _run(health_mod._probe_crypto_freshness())
    msgs = _crypto_alerts(fake_pipe)
    from app.feeds import crypto as crypto_feeds
    expected_pairs = len(crypto_feeds.DEFAULT_SYMBOLS) * len(
        crypto_feeds.GRANULARITIES
    )
    assert len(msgs) == expected_pairs
    # Subjects identify the symbol / granularity so the on-call knows
    # which feed/provider to investigate.
    sample = msgs[0]
    assert "Crypto feed stale" in sample["subject"]
    assert "/" in sample["subject"]


def test_alert_dedupes_within_ttl(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_mod, "CRYPTO_FRESHNESS_ALERT_THRESHOLD", 2)
    monkeypatch.setattr(health_mod, "db", _StubDB([]))

    # Cross the threshold once.
    _run(health_mod._probe_crypto_freshness())
    _run(health_mod._probe_crypto_freshness())
    first_count = len(_crypto_alerts(fake_pipe))
    assert first_count > 0

    # The pairs recover (counters reset) then go down again. The
    # in-process counter rebuilds and re-crosses the threshold, but the
    # persistent dedupe table suppresses every per-pair page within the
    # TTL window.
    monkeypatch.setattr(health_mod, "db", _StubDB(_fresh_rows()))
    _run(health_mod._probe_crypto_freshness())
    assert health_mod._crypto_freshness_bad_counts == {}

    monkeypatch.setattr(health_mod, "db", _StubDB([]))
    _run(health_mod._probe_crypto_freshness())
    _run(health_mod._probe_crypto_freshness())
    assert len(_crypto_alerts(fake_pipe)) == first_count


def test_recovery_resets_counter(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_mod, "CRYPTO_FRESHNESS_ALERT_THRESHOLD", 3)
    monkeypatch.setattr(health_mod, "db", _StubDB([]))

    _run(health_mod._probe_crypto_freshness())
    _run(health_mod._probe_crypto_freshness())
    assert _crypto_alerts(fake_pipe) == []
    assert len(health_mod._crypto_freshness_bad_counts) > 0

    # Bars land again — the per-pair counter must drain, otherwise the
    # next bad snapshot would alert immediately instead of needing N
    # more consecutive bad snapshots.
    monkeypatch.setattr(health_mod, "db", _StubDB(_fresh_rows()))
    _run(health_mod._probe_crypto_freshness())
    assert health_mod._crypto_freshness_bad_counts == {}

    # Goes bad again — needs threshold more bad snapshots, not 1.
    monkeypatch.setattr(health_mod, "db", _StubDB([]))
    _run(health_mod._probe_crypto_freshness())
    _run(health_mod._probe_crypto_freshness())
    assert _crypto_alerts(fake_pipe) == []
    _run(health_mod._probe_crypto_freshness())
    assert len(_crypto_alerts(fake_pipe)) > 0


def test_healthy_pairs_never_alert(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_mod, "CRYPTO_FRESHNESS_ALERT_THRESHOLD", 1)
    monkeypatch.setattr(health_mod, "db", _StubDB(_fresh_rows()))
    for _ in range(5):
        _run(health_mod._probe_crypto_freshness())
    assert _crypto_alerts(fake_pipe) == []
    assert health_mod._crypto_freshness_bad_counts == {}


def test_only_down_pairs_alerted(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pairs in `degraded` (yellow-but-not-red) state must not page —
    only `down` pairs should accumulate consecutive-bad counters and
    cross the alert threshold.
    """
    from app.feeds import crypto as crypto_feeds
    monkeypatch.setattr(health_mod, "CRYPTO_FRESHNESS_ALERT_THRESHOLD", 1)
    now = datetime.now(timezone.utc).timestamp()

    # Degrade every pair to ~5 minutes old: > yellow (180s for 1m bar)
    # but < red (600s for 1m bar) -> "degraded", not "down".
    rows = []
    for sym in crypto_feeds.DEFAULT_SYMBOLS:
        for gran in crypto_feeds.GRANULARITIES:
            rows.append({
                "symbol": sym,
                "granularity": gran,
                "max_ts": datetime.fromtimestamp(now - 300, tz=timezone.utc),
            })
    monkeypatch.setattr(health_mod, "db", _StubDB(rows))
    card = _run(health_mod._probe_crypto_freshness())
    assert card["status"] == "degraded"
    assert _crypto_alerts(fake_pipe) == []
    # No down pairs => the bad-count map stays empty.
    assert health_mod._crypto_freshness_bad_counts == {}


def test_alert_body_includes_pair_diagnostics(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operator alert body must surface the symbol, granularity, age,
    and consecutive-snapshot count so the on-call can act on the page
    without opening the dashboard.
    """
    from app.feeds import crypto as crypto_feeds
    monkeypatch.setattr(health_mod, "CRYPTO_FRESHNESS_ALERT_THRESHOLD", 1)

    sym = crypto_feeds.DEFAULT_SYMBOLS[0]
    gran = next(iter(crypto_feeds.GRANULARITIES))
    rows: list[dict] = []
    now = datetime.now(timezone.utc).timestamp()
    for s in crypto_feeds.DEFAULT_SYMBOLS:
        for g in crypto_feeds.GRANULARITIES:
            # Make the targeted pair very stale; everything else fresh.
            age = 86_400 if (s == sym and g == gran) else 5
            rows.append({
                "symbol": s,
                "granularity": g,
                "max_ts": datetime.fromtimestamp(now - age, tz=timezone.utc),
            })

    monkeypatch.setattr(health_mod, "db", _StubDB(rows))
    _run(health_mod._probe_crypto_freshness())
    msgs = _crypto_alerts(fake_pipe)
    # Exactly one pair was down; exactly one alert should be dispatched.
    assert len(msgs) == 1
    body = msgs[0]["body"]
    assert sym in body
    assert gran in body
    assert "consecutive_snapshots" in body
