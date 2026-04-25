"""Scheduled crypto-binary backtest runner + drift watcher (task #421).

Covers:
  * pure-decision logic in ``compute_drift_findings``
    - too few evaluations -> no findings
    - clean run inside both bands -> no findings
    - absolute-only Brier breach
    - jump-only log-loss breach
    - both metrics flagged in a single run
  * the ``crypto_backtest_drift`` operator email helper
    (subject/body/dry-run, baseline + jump rendering, missing-baseline path)
  * ``persist_run`` writes the JSON-serialised reliability table
  * ``run_pair`` end-to-end against an in-memory ``db`` shim:
    - persists one ``crypto_backtest_runs`` row
    - inserts one ``model_health_alerts`` row per drift finding
    - dispatches one operator email per finding
    - same-day re-runs are suppressed by the dedupe layer
  * scheduler ``start()`` is a no-op when disabled by env
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app import db as _db
from app.crypto import backtest as _backtest
from app.crypto import backtest_scheduler as _bts
from app.crypto.backtest import BacktestResult
from app.notifications import alerts as _alerts
from app.notifications import postmark as _postmark


# ---------------------------------------------------------------------------
# Pure decision logic
# ---------------------------------------------------------------------------


def test_compute_drift_findings_skips_when_below_min_evaluated() -> None:
    findings = _bts.compute_drift_findings(
        symbol="BTC",
        timeframe="1h",
        n_evaluated=10,
        brier=0.50,
        log_loss=2.0,
        baseline=None,
        min_evaluated=30,
        brier_abs_threshold=0.30,
        logloss_abs_threshold=0.85,
        brier_jump_threshold=0.05,
        logloss_jump_threshold=0.10,
    )
    assert findings == []


def test_compute_drift_findings_clean_run_no_findings() -> None:
    findings = _bts.compute_drift_findings(
        symbol="BTC",
        timeframe="1h",
        n_evaluated=120,
        brier=0.20,
        log_loss=0.55,
        baseline={"count": 5, "brier": 0.21, "log_loss": 0.56},
        min_evaluated=30,
        brier_abs_threshold=0.30,
        logloss_abs_threshold=0.85,
        brier_jump_threshold=0.05,
        logloss_jump_threshold=0.10,
    )
    assert findings == []


def test_compute_drift_findings_absolute_brier_breach_only() -> None:
    findings = _bts.compute_drift_findings(
        symbol="ETH",
        timeframe="15m",
        n_evaluated=120,
        brier=0.34,
        log_loss=0.60,
        baseline={"count": 4, "brier": 0.32, "log_loss": 0.58},
        min_evaluated=30,
        brier_abs_threshold=0.30,
        logloss_abs_threshold=0.85,
        brier_jump_threshold=0.05,
        logloss_jump_threshold=0.10,
    )
    assert len(findings) == 1
    f = findings[0]
    assert f["metric"] == "brier"
    # Absolute breach (0.34 > 0.30) but jump (+0.02) is below 0.05.
    assert f["reason"] == "absolute"
    assert f["baseline"] == 0.32
    assert f["baseline_runs"] == 4
    assert f["jump"] == pytest.approx(0.02, abs=1e-6)


def test_compute_drift_findings_jump_only_log_loss() -> None:
    findings = _bts.compute_drift_findings(
        symbol="BTC",
        timeframe="5m",
        n_evaluated=200,
        brier=0.22,
        log_loss=0.78,
        baseline={"count": 6, "brier": 0.21, "log_loss": 0.60},
        min_evaluated=30,
        brier_abs_threshold=0.30,
        logloss_abs_threshold=0.85,
        brier_jump_threshold=0.05,
        logloss_jump_threshold=0.10,
    )
    assert len(findings) == 1
    f = findings[0]
    assert f["metric"] == "log_loss"
    # Inside abs band (0.78 < 0.85), jumped +0.18 vs baseline (>0.10).
    assert f["reason"] == "jump"
    assert f["baseline"] == 0.60
    assert f["jump"] == pytest.approx(0.18, abs=1e-6)


def test_compute_drift_findings_both_metrics() -> None:
    findings = _bts.compute_drift_findings(
        symbol="BTC",
        timeframe="1h",
        n_evaluated=80,
        brier=0.40,
        log_loss=1.10,
        baseline={"count": 5, "brier": 0.20, "log_loss": 0.55},
        min_evaluated=30,
        brier_abs_threshold=0.30,
        logloss_abs_threshold=0.85,
        brier_jump_threshold=0.05,
        logloss_jump_threshold=0.10,
    )
    assert {f["metric"] for f in findings} == {"brier", "log_loss"}
    for f in findings:
        # Both rules trip on both metrics in this scenario.
        assert f["reason"] == "absolute+jump"
        assert f["symbol"] == "BTC"
        assert f["timeframe"] == "1h"


def test_compute_drift_findings_no_baseline_falls_back_to_absolute() -> None:
    findings = _bts.compute_drift_findings(
        symbol="ETH",
        timeframe="1h",
        n_evaluated=60,
        brier=0.45,
        log_loss=0.90,
        baseline={"count": 0, "brier": 0.0, "log_loss": 0.0},
        min_evaluated=30,
        brier_abs_threshold=0.30,
        logloss_abs_threshold=0.85,
        brier_jump_threshold=0.05,
        logloss_jump_threshold=0.10,
    )
    assert {f["metric"] for f in findings} == {"brier", "log_loss"}
    for f in findings:
        # No baseline -> only the absolute rule can fire.
        assert f["reason"] == "absolute"
        assert f["baseline"] is None
        assert f["jump"] is None


# ---------------------------------------------------------------------------
# Operator email helper
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_postmark_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POSTMARK_SERVER_TOKEN", raising=False)
    monkeypatch.delenv("OPERATOR_NOTIFY_EMAIL", raising=False)
    monkeypatch.setenv("OPERATOR_ALERT_EMAILS", "ops@example.com")


def test_crypto_backtest_drift_dry_run_renders_baseline_and_jump() -> None:
    out = _alerts.crypto_backtest_drift(
        symbol="BTC",
        timeframe="1h",
        metric="brier",
        value=0.345,
        threshold=0.30,
        baseline=0.22,
        baseline_runs=5,
        jump=0.125,
        jump_threshold=0.05,
        reason="absolute+jump",
        n_evaluated=120,
        note="warning band",
        dry_run=True,
    )
    assert out["dry_run"] is True
    assert out["tag"] == "crypto-backtest-drift"
    assert "[CRYPTO-BACKTEST-DRIFT]" in out["subject"]
    assert "BTC/1h" in out["subject"]
    assert "0.3450" in out["subject"]
    body = out["body_preview"]
    assert "BTC" in body and "1h" in body
    assert "0.3450" in body
    assert "0.2200" in body  # baseline
    assert "+0.1250" in body  # jump
    assert "absolute+jump" in body
    assert "warning band" in body
    # Drift alerts must opt-in via email_drift, same as persona-brier.
    assert out["user_pref_column"] == "email_drift"


def test_crypto_backtest_drift_handles_missing_baseline() -> None:
    out = _alerts.crypto_backtest_drift(
        symbol="ETH",
        timeframe="15m",
        metric="log_loss",
        value=1.20,
        threshold=0.85,
        baseline=None,
        baseline_runs=0,
        jump=None,
        jump_threshold=0.10,
        reason="absolute",
        n_evaluated=64,
        dry_run=True,
    )
    body = out["body_preview"]
    assert "(n/a)" in body  # baseline + jump both rendered as n/a
    assert "1.2000" in body
    assert "log_loss" in body or "log loss" in body


# ---------------------------------------------------------------------------
# In-memory DB shim
# ---------------------------------------------------------------------------


class _FakeDb:
    """Minimum surface needed by `backtest_scheduler.run_pair`."""

    def __init__(self) -> None:
        self.runs: list[dict[str, Any]] = []
        self.dedupe: dict[str, datetime] = {}
        self.model_health_inserts: list[dict[str, Any]] = []
        self._next_id = 1

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "notification_dedupe" in query:
            key, ttl = args[0], int(args[1])
            now = datetime.now(timezone.utc)
            existing = self.dedupe.get(key)
            if existing is None or existing <= now:
                self.dedupe[key] = now + timedelta(seconds=ttl)
                return {"ok": 1}
            return None
        raise AssertionError(f"unexpected fetchrow query: {query[:120]!r}")

    async def fetchval(self, query: str, *args: Any) -> Any:
        if "INSERT INTO crypto_backtest_runs" in query:
            row = {
                "id": self._next_id,
                "symbol": args[0],
                "timeframe": args[1],
                "days": args[2],
                "lookback": args[3],
                "n_evaluated": args[4],
                "accuracy": args[5],
                "brier_score": args[6],
                "log_loss": args[7],
                "reliability": args[8],
                "started_at": args[9],
                "completed_at": args[10],
                "row_created_at": datetime.now(timezone.utc),
            }
            self.runs.append(row)
            self._next_id += 1
            return row["id"]
        if "model_health_alerts" in query and "alert_type = 'crypto_backtest_drift'" in query:
            for row in self.model_health_inserts:
                if (
                    row["vertical"] == args[0]
                    and row["metric_name"] == args[1]
                    and row["created_at"] > datetime.now(timezone.utc) - timedelta(hours=24)
                ):
                    return 1
            return None
        raise AssertionError(f"unexpected fetchval query: {query[:120]!r}")

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "FROM crypto_backtest_runs" in query:
            symbol, timeframe, exclude_id, limit = args
            rows = [
                r for r in self.runs
                if r["symbol"] == symbol and r["timeframe"] == timeframe
                and (exclude_id is None or r["id"] != exclude_id)
            ]
            rows.sort(key=lambda r: r["completed_at"], reverse=True)
            return rows[: int(limit)]
        return []

    async def execute(self, query: str, *args: Any) -> str:
        if "INSERT INTO model_health_alerts" in query:
            self.model_health_inserts.append({
                "vertical": args[0],
                "severity": args[1],
                "metric_name": args[2],
                "metric_value": args[3],
                "threshold": args[4],
                "message": args[5],
                "created_at": datetime.now(timezone.utc),
            })
        return "OK"


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch) -> _FakeDb:
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
    return fake


# ---------------------------------------------------------------------------
# persist_run
# ---------------------------------------------------------------------------


def test_persist_run_writes_reliability_as_jsonb_payload(
    fake_db: _FakeDb,
) -> None:
    result = BacktestResult(
        symbol="BTC",
        timeframe="1h",
        n_evaluated=120,
        accuracy=0.62,
        brier_score=0.21,
        log_loss=0.58,
        reliability=[(0.0, 0.1, 0.05, 0.10, 12)],
        started_at="2026-04-23T00:00:00+00:00",
        completed_at="2026-04-23T00:00:05+00:00",
    )
    row_id = asyncio.run(_bts.persist_run(result, days=14, lookback=50))
    assert row_id == 1
    assert len(fake_db.runs) == 1
    row = fake_db.runs[0]
    assert row["symbol"] == "BTC"
    assert row["timeframe"] == "1h"
    assert row["n_evaluated"] == 120
    assert row["brier_score"] == pytest.approx(0.21)
    # Reliability was JSON-serialised so the JSONB cast accepts it.
    parsed = json.loads(row["reliability"])
    assert isinstance(parsed, list) and parsed[0]["count"] == 12


# ---------------------------------------------------------------------------
# run_pair end-to-end
# ---------------------------------------------------------------------------


def _stub_run_backtest(
    monkeypatch: pytest.MonkeyPatch,
    *,
    n_evaluated: int = 120,
    brier: float = 0.21,
    log_loss: float = 0.58,
    accuracy: float = 0.60,
) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def _fake(
        *,
        symbol: str,
        timeframe: str,
        days: int,
        lookback: int,
        model: Any | None = None,
    ) -> BacktestResult:
        captured["symbol"] = symbol
        captured["timeframe"] = timeframe
        captured["days"] = days
        captured["lookback"] = lookback
        return BacktestResult(
            symbol=symbol,
            timeframe=timeframe,
            n_evaluated=n_evaluated,
            accuracy=accuracy,
            brier_score=brier,
            log_loss=log_loss,
            reliability=[(0.0, 0.1, 0.05, 0.10, 12)],
            started_at="2026-04-23T00:00:00+00:00",
            completed_at="2026-04-23T00:00:05+00:00",
        )

    monkeypatch.setattr(_backtest, "run_backtest", _fake)
    return captured


def test_run_pair_clean_persists_no_alert(
    fake_db: _FakeDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CRYPTO_BACKTEST_MIN_EVALUATED", "30")
    monkeypatch.setenv("CRYPTO_BACKTEST_BRIER_ABS_THRESHOLD", "0.30")
    monkeypatch.setenv("CRYPTO_BACKTEST_LOGLOSS_ABS_THRESHOLD", "0.85")
    monkeypatch.setenv("CRYPTO_BACKTEST_BRIER_JUMP_THRESHOLD", "0.05")
    monkeypatch.setenv("CRYPTO_BACKTEST_LOGLOSS_JUMP_THRESHOLD", "0.10")
    _stub_run_backtest(monkeypatch, brier=0.21, log_loss=0.55)

    out = asyncio.run(_bts.run_pair(symbol="BTC", timeframe="1h"))

    assert out["ok"] is True
    assert out["row_id"] == 1
    assert out["alerts_raised"] == 0
    assert out["findings"] == []
    assert len(fake_db.runs) == 1
    assert fake_db.model_health_inserts == []


def test_run_pair_drift_persists_row_and_dispatches_email(
    fake_db: _FakeDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CRYPTO_BACKTEST_MIN_EVALUATED", "30")
    monkeypatch.setenv("CRYPTO_BACKTEST_BRIER_ABS_THRESHOLD", "0.30")
    monkeypatch.setenv("CRYPTO_BACKTEST_LOGLOSS_ABS_THRESHOLD", "0.85")
    monkeypatch.setenv("CRYPTO_BACKTEST_BRIER_JUMP_THRESHOLD", "0.05")
    monkeypatch.setenv("CRYPTO_BACKTEST_LOGLOSS_JUMP_THRESHOLD", "0.10")
    _stub_run_backtest(monkeypatch, brier=0.40, log_loss=0.60)

    out = asyncio.run(_bts.run_pair(symbol="BTC", timeframe="1h"))

    # Persistence: one crypto_backtest_runs row.
    assert len(fake_db.runs) == 1
    # Drift evaluation found the Brier breach (no log-loss alert).
    assert out["alerts_raised"] == 1
    assert len(out["findings"]) == 1
    assert out["findings"][0]["metric"] == "brier"

    # Exactly one model_health_alerts row inserted with vertical=BTC/1h.
    assert len(fake_db.model_health_inserts) == 1
    row = fake_db.model_health_inserts[0]
    assert row["vertical"] == "BTC/1h"
    assert row["metric_name"] == "crypto_backtest_brier"
    assert row["metric_value"] == pytest.approx(0.40)
    assert row["threshold"] == pytest.approx(0.30)

    # Exactly one Postmark dispatch for the crypto-backtest-drift tag.
    sends = [
        s for s in fake_db.sent  # type: ignore[attr-defined]
        if s.get("tag") == "crypto-backtest-drift"
    ]
    assert len(sends) == 1
    assert "BTC/1h" in sends[0]["subject"]


def test_run_pair_dedupes_within_ttl(
    fake_db: _FakeDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CRYPTO_BACKTEST_MIN_EVALUATED", "30")
    monkeypatch.setenv("CRYPTO_BACKTEST_BRIER_ABS_THRESHOLD", "0.30")
    monkeypatch.setenv("CRYPTO_BACKTEST_LOGLOSS_ABS_THRESHOLD", "0.85")
    monkeypatch.setenv("CRYPTO_BACKTEST_ALERT_TTL_SECONDS", "3600")
    _stub_run_backtest(monkeypatch, brier=0.45, log_loss=0.55)

    asyncio.run(_bts.run_pair(symbol="BTC", timeframe="1h"))
    asyncio.run(_bts.run_pair(symbol="BTC", timeframe="1h"))

    # Both runs persist a fresh row in the trend table.
    assert len(fake_db.runs) == 2
    # But the second run is suppressed by the same-day model_health_alerts
    # lookup AND by the dedupe layer for emails.
    assert len(fake_db.model_health_inserts) == 1
    sends = [
        s for s in fake_db.sent  # type: ignore[attr-defined]
        if s.get("tag") == "crypto-backtest-drift"
    ]
    assert len(sends) == 1


def test_run_pair_skips_drift_when_too_few_evaluations(
    fake_db: _FakeDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CRYPTO_BACKTEST_MIN_EVALUATED", "100")
    _stub_run_backtest(monkeypatch, n_evaluated=10, brier=0.99, log_loss=5.0)

    out = asyncio.run(_bts.run_pair(symbol="BTC", timeframe="1h"))

    # Persistence still happens — the trend table records every run.
    assert len(fake_db.runs) == 1
    # No drift evaluation, no alerts, even though the metrics are bad.
    assert out["alerts_raised"] == 0
    assert out["findings"] == []
    assert fake_db.model_health_inserts == []


# ---------------------------------------------------------------------------
# check_now sweeps every supported pair
# ---------------------------------------------------------------------------


def test_check_now_iterates_supplied_pairs(
    fake_db: _FakeDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CRYPTO_BACKTEST_MIN_EVALUATED", "30")
    monkeypatch.setenv("CRYPTO_BACKTEST_BRIER_ABS_THRESHOLD", "0.30")
    monkeypatch.setenv("CRYPTO_BACKTEST_LOGLOSS_ABS_THRESHOLD", "0.85")
    _stub_run_backtest(monkeypatch, brier=0.20, log_loss=0.55)

    out = asyncio.run(_bts.check_now(pairs=[("BTC", "1h"), ("ETH", "15m")]))
    assert len(out["pairs"]) == 2
    assert {p["symbol"] for p in out["pairs"]} == {"BTC", "ETH"}
    assert out["errors"] == 0
    assert out["alerts_raised"] == 0
    assert len(fake_db.runs) == 2


# ---------------------------------------------------------------------------
# Background loop start/stop
# ---------------------------------------------------------------------------


def test_start_is_noop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRYPTO_BACKTEST_SCHEDULER_ENABLED", "0")
    asyncio.run(_bts.start())
    assert _bts._task is None


def test_supported_pairs_covers_full_grid() -> None:
    pairs = _bts.supported_pairs()
    # Must include at least one pair so the cadence loop has work to do.
    assert pairs, "supported_pairs() returned empty"
    # Sanity: BTC/1h is always supported.
    assert ("BTC", "1h") in pairs
