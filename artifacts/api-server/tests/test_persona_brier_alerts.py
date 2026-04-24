"""Per-persona daily Brier degradation alert (task #305).

Covers:
  * pure-decision logic in `compute_findings`
    - absolute-threshold breaches with the min-debate floor
    - jump-vs-baseline breaches
    - severity escalation when both rules fire
    - personas with too few debates are ignored
  * the `persona_brier_drift` operator email helper (subject/body/dry-run)
  * `check_now()` end-to-end against an in-memory `db` shim:
    - inserts a `model_health_alerts` row with vertical=persona_id
    - dispatches one operator email per finding
    - same-day re-runs are suppressed by the dedupe layer
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app import db as _db
from app.notifications import alerts as _alerts
from app.notifications import persona_brier_alerts as _pba
from app.notifications import postmark as _postmark


# ---------------------------------------------------------------------------
# Pure decision logic
# ---------------------------------------------------------------------------


def test_compute_findings_absolute_breach() -> None:
    findings = _pba.compute_findings(
        personas=[
            {"persona_id": "skeptic", "debates": 8, "brier": 0.34},
            {"persona_id": "calm", "debates": 9, "brier": 0.20},
        ],
        baselines={"skeptic": 0.22, "calm": 0.20},
        min_debates=5,
        abs_threshold=0.30,
        jump_threshold=0.10,
    )
    assert len(findings) == 1
    f = findings[0]
    assert f["persona_id"] == "skeptic"
    assert f["reason"] == "absolute+jump"  # 0.34>0.30 AND +0.12>0.10
    assert f["severity"] == "high"
    assert f["baseline"] == 0.22
    assert f["jump"] == pytest.approx(0.12, abs=1e-6)


def test_compute_findings_jump_only() -> None:
    findings = _pba.compute_findings(
        personas=[
            {"persona_id": "hawk", "debates": 7, "brier": 0.21},
        ],
        baselines={"hawk": 0.08},
        min_debates=5,
        abs_threshold=0.30,
        jump_threshold=0.10,
    )
    assert len(findings) == 1
    assert findings[0]["reason"] == "jump"
    assert findings[0]["severity"] == "warn"
    assert findings[0]["jump"] == pytest.approx(0.13, abs=1e-6)


def test_compute_findings_skips_low_volume() -> None:
    findings = _pba.compute_findings(
        personas=[
            {"persona_id": "skeptic", "debates": 4, "brier": 0.50},
        ],
        baselines={},
        min_debates=5,
        abs_threshold=0.30,
        jump_threshold=0.10,
    )
    assert findings == []


def test_compute_findings_no_baseline_falls_back_to_absolute() -> None:
    findings = _pba.compute_findings(
        personas=[
            {"persona_id": "newbie", "debates": 6, "brier": 0.45},
        ],
        baselines={},
        min_debates=5,
        abs_threshold=0.30,
        jump_threshold=0.10,
    )
    assert len(findings) == 1
    f = findings[0]
    assert f["reason"] == "absolute"
    assert f["baseline"] is None
    assert f["jump"] is None
    # 0.45 >= max(0.40, 0.30+0.10) -> high severity even without jump
    assert f["severity"] == "high"


def test_compute_findings_handles_malformed_rows() -> None:
    findings = _pba.compute_findings(
        personas=[
            {"persona_id": None, "debates": 8, "brier": 0.5},
            {"persona_id": "ok", "debates": "not-a-number", "brier": 0.5},
            {"persona_id": "ok2", "debates": 8, "brier": None},
            {"persona_id": "good", "debates": 8, "brier": 0.40},
        ],
        baselines={},
        min_debates=5,
        abs_threshold=0.30,
        jump_threshold=0.10,
    )
    assert [f["persona_id"] for f in findings] == ["good"]


# ---------------------------------------------------------------------------
# Operator email helper
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_postmark_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POSTMARK_SERVER_TOKEN", raising=False)
    monkeypatch.delenv("OPERATOR_NOTIFY_EMAIL", raising=False)
    monkeypatch.setenv("OPERATOR_ALERT_EMAILS", "ops@example.com")


def test_persona_brier_drift_dry_run_subject_and_body() -> None:
    out = _alerts.persona_brier_drift(
        persona_id="skeptic",
        date_utc="2026-04-22",
        brier=0.345,
        debates=8,
        abs_threshold=0.30,
        jump_threshold=0.10,
        severity="high",
        reason="absolute+jump",
        baseline=0.22,
        jump=0.125,
        note="warning band",
        dry_run=True,
    )
    assert out["dry_run"] is True
    assert out["tag"] == "persona-brier"
    assert "[PERSONA-BRIER]" in out["subject"]
    assert "[HIGH]" in out["subject"]
    assert "skeptic" in out["subject"]
    assert "0.3450" in out["subject"]
    body = out["body_preview"]
    assert "skeptic" in body
    assert "2026-04-22" in body
    assert "0.3450" in body
    assert "0.2200" in body  # baseline
    assert "+0.1250" in body  # jump
    assert "absolute+jump" in body
    assert "warning band" in body


def test_persona_brier_drift_handles_missing_baseline() -> None:
    out = _alerts.persona_brier_drift(
        persona_id="newbie",
        date_utc="2026-04-22",
        brier=0.42,
        debates=6,
        abs_threshold=0.30,
        jump_threshold=0.10,
        severity="warn",
        reason="absolute",
        baseline=None,
        jump=None,
        dry_run=True,
    )
    body = out["body_preview"]
    assert "(n/a)" in body  # baseline + jump both rendered as n/a
    assert "[WARN]" in out["subject"]


# ---------------------------------------------------------------------------
# check_now() end-to-end against an in-memory shim
# ---------------------------------------------------------------------------


class _FakeDb:
    """Minimum surface needed by `persona_brier_alerts.check_now()`.

    Routes queries based on a substring match — keeps the shim tight
    enough to catch silent regressions in the SQL we actually depend
    on.
    """

    def __init__(self) -> None:
        self.dedupe_rows: dict[str, datetime] = {}
        self.model_health_inserts: list[dict[str, Any]] = []
        self.recent_lookups: list[tuple[str, str]] = []

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
        if "model_health_alerts" in query and "alert_type = 'persona_daily_brier'" in query:
            self.recent_lookups.append((str(args[0]), str(args[1])))
            for row in self.model_health_inserts:
                if (
                    row["vertical"] == args[0]
                    and row["severity"] == args[1]
                    and row["created_at"] > datetime.now(timezone.utc) - timedelta(hours=24)
                ):
                    return 1
            return None
        raise AssertionError(f"unexpected fetchval query: {query[:120]!r}")

    async def fetch(self, query: str, *_args: Any) -> list[dict[str, Any]]:
        if "FROM swarm_analyst_metrics" in query:
            # `all_time_baselines` query — return weighted-average rows
            # for the personas the test scenarios reference.
            return [
                {"analyst_id": "skeptic", "num": 220.0, "denom": 1000.0},  # 0.22
                {"analyst_id": "hawk", "num": 80.0, "denom": 1000.0},      # 0.08
            ]
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
def fake_db_and_personas(monkeypatch: pytest.MonkeyPatch) -> tuple[_FakeDb, list[dict[str, Any]]]:
    fake = _FakeDb()
    monkeypatch.setattr(_db, "fetchrow", fake.fetchrow)
    monkeypatch.setattr(_db, "fetchval", fake.fetchval)
    monkeypatch.setattr(_db, "fetch", fake.fetch)
    monkeypatch.setattr(_db, "execute", fake.execute)

    personas: list[dict[str, Any]] = []

    async def _fake_aggregate(_date: str) -> list[dict[str, Any]]:
        return list(personas)

    monkeypatch.setattr(_pba, "aggregate_persona_day", _fake_aggregate)

    # Block real Postmark sends — capture per-tag dispatch counts.
    sent: list[dict[str, Any]] = []

    def _capture(**kwargs: Any) -> None:
        sent.append(kwargs)

    monkeypatch.setattr(_postmark, "send_in_background", _capture)
    monkeypatch.setattr(
        _alerts, "_schedule_user_email_fanout", lambda **_kw: None
    )
    fake.sent = sent  # type: ignore[attr-defined]
    return fake, personas


def test_check_now_inserts_row_and_dispatches_email(
    fake_db_and_personas: tuple[_FakeDb, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PERSONA_BRIER_ALERT_MIN_DEBATES", "5")
    monkeypatch.setenv("PERSONA_BRIER_ALERT_ABS_THRESHOLD", "0.30")
    monkeypatch.setenv("PERSONA_BRIER_ALERT_JUMP_THRESHOLD", "0.10")
    fake, personas = fake_db_and_personas
    personas.extend(
        [
            {"persona_id": "skeptic", "debates": 8, "brier": 0.34},
            {"persona_id": "calm",    "debates": 9, "brier": 0.20},
        ]
    )

    out = asyncio.run(_pba.check_now(date_utc="2026-04-22"))

    assert out["alerts_raised"] == 1
    assert out["personas_checked"] == 2
    assert len(out["findings"]) == 1
    assert out["findings"][0]["persona_id"] == "skeptic"

    # Exactly one model_health_alerts row inserted, with vertical=persona_id.
    assert len(fake.model_health_inserts) == 1
    row = fake.model_health_inserts[0]
    assert row["vertical"] == "skeptic"
    assert row["severity"] == "high"
    assert row["metric_value"] == pytest.approx(0.34)
    assert row["threshold"] == pytest.approx(0.30)
    assert "skeptic" in row["message"]
    assert "0.3400" in row["message"]

    # Exactly one Postmark dispatch for the persona-brier tag.
    persona_sends = [
        s for s in fake.sent  # type: ignore[attr-defined]
        if s.get("tag") == "persona-brier"
    ]
    assert len(persona_sends) == 1
    assert "skeptic" in persona_sends[0]["subject"]


def test_check_now_dedupes_within_24h(
    fake_db_and_personas: tuple[_FakeDb, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PERSONA_BRIER_ALERT_MIN_DEBATES", "5")
    monkeypatch.setenv("PERSONA_BRIER_ALERT_ABS_THRESHOLD", "0.30")
    monkeypatch.setenv("PERSONA_BRIER_ALERT_JUMP_THRESHOLD", "0.10")
    fake, personas = fake_db_and_personas
    personas.append({"persona_id": "skeptic", "debates": 8, "brier": 0.34})

    asyncio.run(_pba.check_now(date_utc="2026-04-22"))
    asyncio.run(_pba.check_now(date_utc="2026-04-22"))

    # Second pass must NOT insert a second row (suppressed by the
    # 24h same-(persona, severity) lookup).
    assert len(fake.model_health_inserts) == 1
    # And only one operator email for the day.
    persona_sends = [
        s for s in fake.sent  # type: ignore[attr-defined]
        if s.get("tag") == "persona-brier"
    ]
    assert len(persona_sends) == 1


def test_check_now_no_findings_when_personas_clean(
    fake_db_and_personas: tuple[_FakeDb, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PERSONA_BRIER_ALERT_MIN_DEBATES", "5")
    monkeypatch.setenv("PERSONA_BRIER_ALERT_ABS_THRESHOLD", "0.30")
    monkeypatch.setenv("PERSONA_BRIER_ALERT_JUMP_THRESHOLD", "0.10")
    fake, personas = fake_db_and_personas
    personas.extend(
        [
            {"persona_id": "skeptic", "debates": 10, "brier": 0.18},
            {"persona_id": "calm",    "debates": 9,  "brier": 0.20},
            {"persona_id": "low_n",   "debates": 2,  "brier": 0.99},
        ]
    )

    out = asyncio.run(_pba.check_now(date_utc="2026-04-22"))
    assert out["alerts_raised"] == 0
    assert out["findings"] == []
    assert fake.model_health_inserts == []
