"""Soccer settlement drift watcher (task #611).

Covers:
  * the ``soccer_settlement_drift`` operator email helper
    (subject/body/dry-run, optional fields)
  * ``check_now()`` end-to-end against an in-memory ``db`` shim:
    - inserts a `model_health_alerts` row keyed on the kalshi ticker
    - dispatches one operator email per stuck market
    - same-(ticker, game_id) re-runs are suppressed by the dedupe layer
    - dispatch is skipped when the SQL returns no rows (clean state)
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app import db as _db
from app.notifications import alerts as _alerts
from app.notifications import postmark as _postmark
from app.notifications import soccer_settlement_drift as _ssd


# ---------------------------------------------------------------------------
# Operator email helper
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_postmark_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POSTMARK_SERVER_TOKEN", raising=False)
    monkeypatch.delenv("OPERATOR_NOTIFY_EMAIL", raising=False)
    monkeypatch.setenv("OPERATOR_ALERT_EMAILS", "ops@example.com")


def test_soccer_settlement_drift_dry_run_subject_and_body() -> None:
    out = _alerts.soccer_settlement_drift(
        market_ticker="KX-EPL-LIV-CHE",
        game_id="sr:match:12345",
        league="EPL",
        final_for_seconds=3 * 3600 + 600,  # 3h10m
        threshold_seconds=2 * 3600,
        open_trades=4,
        sportradar_status="closed",
        first_final_at="2026-04-26T12:34:56+00:00",
        note="missing swarm_debates row",
        dry_run=True,
    )
    assert out["dry_run"] is True
    assert out["tag"] == "soccer-settlement-drift"
    assert "[SOCCER-SETTLEMENT]" in out["subject"]
    assert "KX-EPL-LIV-CHE" in out["subject"]
    body = out["body_preview"]
    assert "KX-EPL-LIV-CHE" in body
    assert "sr:match:12345" in body
    assert "EPL" in body
    assert "closed" in body
    # 3h10m = 190 minutes
    assert "190" in body
    # threshold 2h = 120 minutes
    assert "120" in body
    assert "open_trades" in body
    assert "4" in body
    assert "missing swarm_debates row" in body
    assert "2026-04-26T12:34:56" in body


def test_soccer_settlement_drift_handles_missing_optional_fields() -> None:
    out = _alerts.soccer_settlement_drift(
        market_ticker="KX-MLS-LAFC-LAG",
        game_id="sr:match:99",
        league=None,
        final_for_seconds=2.5 * 3600,
        threshold_seconds=2 * 3600,
        open_trades=1,
        dry_run=True,
    )
    body = out["body_preview"]
    assert "(unknown)" in body  # league + sportradar_status both n/a
    assert "KX-MLS-LAFC-LAG" in body


# ---------------------------------------------------------------------------
# check_now() end-to-end
# ---------------------------------------------------------------------------


class _FakeDb:
    """Minimum surface needed by ``soccer_settlement_drift.check_now()``.

    Records every INSERT / dedupe interaction so the assertions below can
    pin down both the persistence and the dispatch behaviour.
    """

    def __init__(self) -> None:
        self.dedupe_rows: dict[str, datetime] = {}
        self.model_health_inserts: list[dict[str, Any]] = []
        self.recent_lookups: list[tuple[str, str]] = []
        self.stuck_rows: list[dict[str, Any]] = []
        self.stuck_query_count = 0

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "notification_dedupe" in query:
            key, ttl = args[0], int(args[1])
            now = datetime.now(timezone.utc)
            existing = self.dedupe_rows.get(key)
            if existing is None or existing <= now:
                self.dedupe_rows[key] = now + timedelta(seconds=ttl)
                return {"ok": 1}
            return None
        raise AssertionError(f"unexpected fetchrow query: {query[:120]!r}")

    async def fetchval(self, query: str, *args: Any) -> Any:
        if (
            "model_health_alerts" in query
            and "alert_type = 'soccer_settlement_drift'" in query
        ):
            self.recent_lookups.append((str(args[0]), str(args[1])))
            for row in self.model_health_inserts:
                if (
                    row["vertical"] == args[0]
                    and row["metric_name"] == args[1]
                    and row["created_at"]
                    > datetime.now(timezone.utc) - timedelta(hours=24)
                ):
                    return 1
            return None
        raise AssertionError(f"unexpected fetchval query: {query[:120]!r}")

    async def fetch(self, query: str, *_args: Any) -> list[dict[str, Any]]:
        if "soccer_unsettled" in query and "sportradar_live_state" in query:
            self.stuck_query_count += 1
            return list(self.stuck_rows)
        raise AssertionError(f"unexpected fetch query: {query[:120]!r}")

    async def execute(self, query: str, *args: Any) -> str:
        if "INSERT INTO model_health_alerts" in query:
            self.model_health_inserts.append(
                {
                    "vertical": args[0],
                    "severity": args[1],
                    "metric_name": args[2],
                    "metric_value": args[3],
                    "threshold": args[4],
                    "message": args[5],
                    "created_at": datetime.now(timezone.utc),
                }
            )
        return "OK"


@pytest.fixture
def fake_db_and_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[_FakeDb, list[dict[str, Any]]]:
    fake = _FakeDb()
    monkeypatch.setattr(_db, "fetchrow", fake.fetchrow)
    monkeypatch.setattr(_db, "fetchval", fake.fetchval)
    monkeypatch.setattr(_db, "fetch", fake.fetch)
    monkeypatch.setattr(_db, "execute", fake.execute)

    sent: list[dict[str, Any]] = []

    def _capture(**kwargs: Any) -> None:
        sent.append(kwargs)

    monkeypatch.setattr(_postmark, "send_in_background", _capture)
    monkeypatch.setattr(
        _alerts, "_schedule_user_email_fanout", lambda **_kw: None
    )
    fake.sent = sent  # type: ignore[attr-defined]
    return fake, fake.stuck_rows


def _stuck_row(
    *,
    ticker: str,
    game_id: str,
    league: str = "EPL",
    open_trades: int = 2,
    minutes_final: int = 180,
    last_status: str = "closed",
) -> dict[str, Any]:
    first_final_at = datetime.now(timezone.utc) - timedelta(minutes=minutes_final)
    return {
        "ticker": ticker,
        "game_id": game_id,
        "league": league,
        "open_trades": open_trades,
        "first_final_at": first_final_at,
        "last_status": last_status,
    }


def test_check_now_inserts_row_and_dispatches_email(
    fake_db_and_rows: tuple[_FakeDb, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOCCER_SETTLEMENT_DRIFT_FINAL_HOURS", "2")
    fake, rows = fake_db_and_rows
    rows.append(
        _stuck_row(
            ticker="KX-EPL-LIV-CHE",
            game_id="sr:match:12345",
            league="EPL",
            open_trades=3,
            minutes_final=180,
        )
    )

    out = asyncio.run(_ssd.check_now())

    assert out["alerts_raised"] == 1
    assert len(out["findings"]) == 1
    f0 = out["findings"][0]
    assert f0["ticker"] == "KX-EPL-LIV-CHE"
    assert f0["game_id"] == "sr:match:12345"
    assert f0["open_trades"] == 3

    # Exactly one model_health_alerts row, vertical=ticker.
    assert len(fake.model_health_inserts) == 1
    row = fake.model_health_inserts[0]
    assert row["vertical"] == "KX-EPL-LIV-CHE"
    assert row["severity"] == "warn"
    assert row["metric_name"] == "soccer_settlement_drift:sr:match:12345"
    assert row["threshold"] == pytest.approx(2 * 3600.0)
    assert "KX-EPL-LIV-CHE" in row["message"]
    assert "sr:match:12345" in row["message"]

    # Exactly one Postmark dispatch, tagged appropriately.
    sends = [
        s for s in fake.sent  # type: ignore[attr-defined]
        if s.get("tag") == "soccer-settlement-drift"
    ]
    assert len(sends) == 1
    assert "KX-EPL-LIV-CHE" in sends[0]["subject"]


def test_check_now_dedupes_within_ttl(
    fake_db_and_rows: tuple[_FakeDb, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOCCER_SETTLEMENT_DRIFT_FINAL_HOURS", "2")
    fake, rows = fake_db_and_rows
    rows.append(
        _stuck_row(
            ticker="KX-EPL-MUN-ARS",
            game_id="sr:match:777",
            minutes_final=200,
        )
    )

    asyncio.run(_ssd.check_now())
    asyncio.run(_ssd.check_now())

    # Second pass must NOT insert a second row (suppressed by the
    # 24h same-(ticker, metric_name) lookup).
    assert len(fake.model_health_inserts) == 1
    sends = [
        s for s in fake.sent  # type: ignore[attr-defined]
        if s.get("tag") == "soccer-settlement-drift"
    ]
    assert len(sends) == 1


def test_check_now_no_findings_when_clean(
    fake_db_and_rows: tuple[_FakeDb, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOCCER_SETTLEMENT_DRIFT_FINAL_HOURS", "2")
    fake, _rows = fake_db_and_rows

    out = asyncio.run(_ssd.check_now())

    assert out["alerts_raised"] == 0
    assert out["findings"] == []
    assert fake.model_health_inserts == []
    assert all(
        s.get("tag") != "soccer-settlement-drift"
        for s in fake.sent  # type: ignore[attr-defined]
    )


def test_check_now_dispatches_one_alert_per_market(
    fake_db_and_rows: tuple[_FakeDb, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOCCER_SETTLEMENT_DRIFT_FINAL_HOURS", "2")
    fake, rows = fake_db_and_rows
    rows.extend(
        [
            _stuck_row(
                ticker="KX-EPL-A",
                game_id="sr:match:A",
                minutes_final=140,
            ),
            _stuck_row(
                ticker="KX-MLS-B",
                game_id="sr:match:B",
                league="MLS",
                minutes_final=300,
                open_trades=1,
            ),
        ]
    )

    out = asyncio.run(_ssd.check_now())

    assert out["alerts_raised"] == 2
    assert {f["ticker"] for f in out["findings"]} == {"KX-EPL-A", "KX-MLS-B"}
    assert len(fake.model_health_inserts) == 2
    sends = [
        s for s in fake.sent  # type: ignore[attr-defined]
        if s.get("tag") == "soccer-settlement-drift"
    ]
    assert len(sends) == 2
    subjects = " ".join(s["subject"] for s in sends)
    assert "KX-EPL-A" in subjects
    assert "KX-MLS-B" in subjects


def test_check_now_survives_query_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(_db, "fetch", _boom)
    monkeypatch.setattr(_db, "fetchval", _boom)
    monkeypatch.setattr(_db, "fetchrow", _boom)
    monkeypatch.setattr(_db, "execute", _boom)

    out = asyncio.run(_ssd.check_now())
    assert out["alerts_raised"] == 0
    assert out["findings"] == []


# ---------------------------------------------------------------------------
# Soccer-leagues helper
# ---------------------------------------------------------------------------


def test_soccer_leagues_includes_known_three_way_leagues() -> None:
    leagues = set(_ssd._soccer_leagues())
    # Sanity: every known soccer league config (three_way_market=True)
    # is included so the SQL filter is auto-extending.
    assert {"EPL", "MLS", "LALIGA", "SERIEA", "BUNDES", "LIGUE1", "UCL"} <= leagues
