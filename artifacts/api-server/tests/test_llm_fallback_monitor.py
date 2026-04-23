"""Tests for the per-provider LLM fallback-rate spike monitor (task #145).

The monitor reads the `fallback_used` / `primary_provider` flags written
into ``llm_predictions.metadata`` by ``swarm.runner._call_persona`` and
raises an operator alert whenever any provider's trailing fallback rate
crosses the configured threshold. We stub the DB query and the alert
dispatch / dedupe layer so the tests exercise pure monitor logic.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app import db as _db
from app.notifications import alerts as _alerts
from app.swarm import fallback_monitor as _monitor


@pytest.fixture
def fake_pipe(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub DB + dedupe + dispatch so the monitor runs without real IO."""
    state: dict[str, Any] = {
        "rows": [],
        "dispatched": [],
        "dedupe_rows": {},
    }

    async def _fake_fetch(query: str, *args: Any) -> list[dict[str, Any]]:
        assert "llm_predictions" in query
        return list(state["rows"])

    monkeypatch.setattr(_db, "fetch", _fake_fetch)

    async def _fake_dedupe_query(query: str, *args: Any) -> dict | None:
        key, ttl = args[0], int(args[1])
        now = datetime.now(timezone.utc)
        rows = state["dedupe_rows"]
        if key not in rows or rows[key] <= now:
            rows[key] = now + timedelta(seconds=ttl)
            return {"ok": 1}
        return None

    monkeypatch.setattr(_db, "fetchrow", _fake_dedupe_query)

    def _fake_dispatch(subject: str, text_body: str, *, tag: str, dry_run: bool):
        state["dispatched"].append(
            {"subject": subject, "body": text_body, "tag": tag}
        )
        return {"tag": tag, "dispatched": True}

    monkeypatch.setattr(_alerts, "_dispatch", _fake_dispatch)
    return state


def _row(provider: str, total: int, fallback: int,
         reasons: list[str] | None = None) -> dict[str, Any]:
    return {
        "provider": provider,
        "total": total,
        "fallback": fallback,
        "reasons": list(reasons or []),
    }


def test_alert_fires_when_rate_exceeds_threshold(
    fake_pipe: dict[str, Any]
) -> None:
    fake_pipe["rows"] = [
        _row("openai", total=10, fallback=6, reasons=["RateLimitError: slow down"])
    ]
    stats = asyncio.run(_monitor.check_once())
    assert len(fake_pipe["dispatched"]) == 1
    msg = fake_pipe["dispatched"][0]
    assert msg["tag"] == "llm-fallback-spike"
    assert "openai" in msg["subject"]
    assert "60" in msg["subject"]
    assert "RateLimitError" in msg["body"]
    assert stats[0]["alerted"] is True
    assert stats[0]["rate"] == pytest.approx(0.6)


def test_no_alert_below_threshold(fake_pipe: dict[str, Any]) -> None:
    fake_pipe["rows"] = [_row("openai", total=10, fallback=1)]
    stats = asyncio.run(_monitor.check_once())
    assert fake_pipe["dispatched"] == []
    assert stats[0]["alerted"] is False


def test_no_alert_below_min_samples(fake_pipe: dict[str, Any]) -> None:
    # 3/4 = 75% but only 4 samples (< default 8) — guard against false
    # positives from a single transient blip.
    fake_pipe["rows"] = [_row("openai", total=4, fallback=3)]
    stats = asyncio.run(_monitor.check_once())
    assert fake_pipe["dispatched"] == []
    assert stats[0]["alerted"] is False


def test_dedupes_within_window(fake_pipe: dict[str, Any]) -> None:
    fake_pipe["rows"] = [_row("openai", total=10, fallback=6)]
    asyncio.run(_monitor.check_once())
    asyncio.run(_monitor.check_once())
    asyncio.run(_monitor.check_once())
    assert len(fake_pipe["dispatched"]) == 1


def test_independent_providers_alert_independently(
    fake_pipe: dict[str, Any]
) -> None:
    fake_pipe["rows"] = [
        _row("openai", total=10, fallback=6),
        _row("anthropic", total=10, fallback=1),
    ]
    asyncio.run(_monitor.check_once())
    subjects = [d["subject"] for d in fake_pipe["dispatched"]]
    assert len(subjects) == 1
    assert "openai" in subjects[0]
    assert "anthropic" not in subjects[0]


def test_disabled_monitor_is_noop(
    fake_pipe: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import config as _config

    _config.get_settings.cache_clear()
    monkeypatch.setenv("AXIOM_LLM_FALLBACK_ALERT_ENABLED", "0")
    try:
        fake_pipe["rows"] = [_row("openai", total=100, fallback=99)]
        stats = asyncio.run(_monitor.check_once())
        assert stats == []
        assert fake_pipe["dispatched"] == []
    finally:
        monkeypatch.delenv("AXIOM_LLM_FALLBACK_ALERT_ENABLED", raising=False)
        _config.get_settings.cache_clear()
