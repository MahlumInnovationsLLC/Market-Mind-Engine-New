"""Task #950: categorized Edge Terminal scrape outcomes.

Proves every poll tick / self-check resolves to exactly one category
(ok | not_configured | browser_missing | login_failed | no_rows |
error), that the precise category survives into the poller status row,
and that the health probe maps each category to the right card status.
"""
from __future__ import annotations

import asyncio
from typing import Any


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


# ---------------------------------------------------------------------
# Exception → category contract
# ---------------------------------------------------------------------

def test_exception_categories_are_pinned():
    from app.edge_terminal import scraper

    assert scraper.ScraperNotConfigured.category == scraper.SCRAPE_NOT_CONFIGURED
    assert scraper.ScraperUnavailable.category == scraper.SCRAPE_BROWSER_MISSING
    assert scraper.ScraperLoginFailed.category == scraper.SCRAPE_LOGIN_FAILED
    assert scraper.ScraperDOMChanged.category == scraper.SCRAPE_NO_ROWS
    # Subclass relationship keeps legacy ``except ScraperUnavailable``
    # callers working while the poller catches the precise type first.
    assert issubclass(scraper.ScraperLoginFailed, scraper.ScraperUnavailable)


# ---------------------------------------------------------------------
# self_check() categorization
# ---------------------------------------------------------------------

def test_self_check_maps_each_exception_to_its_category(monkeypatch):
    from app.edge_terminal import scraper

    cases = [
        (scraper.ScraperNotConfigured("no creds"), scraper.SCRAPE_NOT_CONFIGURED),
        (scraper.ScraperLoginFailed("bad creds"), scraper.SCRAPE_LOGIN_FAILED),
        (scraper.ScraperUnavailable("no chromium"), scraper.SCRAPE_BROWSER_MISSING),
        (scraper.ScraperDOMChanged("0 rows"), scraper.SCRAPE_NO_ROWS),
        (RuntimeError("boom"), scraper.SCRAPE_ERROR),
    ]
    for exc, expected in cases:
        async def _raise(_exc=exc):
            raise _exc

        monkeypatch.setattr(scraper, "scrape_signals", _raise)
        out = _run(scraper.self_check())
        assert out["ok"] is False
        assert out["category"] == expected
        assert out["signal_count"] == 0
        assert out["detail"]


def test_self_check_ok_with_and_without_signals(monkeypatch):
    from app.edge_terminal import scraper

    async def _empty():
        return []

    monkeypatch.setattr(scraper, "scrape_signals", _empty)
    out = _run(scraper.self_check())
    assert out == {
        "ok": True,
        "category": scraper.SCRAPE_OK,
        "detail": out["detail"],
        "signal_count": 0,
    }
    assert "0 badged signals" in out["detail"]

    async def _two():
        return [{"selection": "a"}, {"selection": "b"}]

    monkeypatch.setattr(scraper, "scrape_signals", _two)
    out = _run(scraper.self_check())
    assert out["ok"] is True
    assert out["category"] == scraper.SCRAPE_OK
    assert out["signal_count"] == 2


# ---------------------------------------------------------------------
# Poller persists the precise category as $3
# ---------------------------------------------------------------------

class _StatusCapture:
    def __init__(self) -> None:
        self.updates: list[tuple[Any, ...]] = []

    async def fetchval(self, q: str, *args):
        return None

    async def execute(self, q: str, *args):
        if "UPDATE edge_terminal_poller_status" in q:
            self.updates.append(args)


def _run_tick_with(monkeypatch, scrape_coro):
    from app.edge_terminal import poller

    cap = _StatusCapture()
    monkeypatch.setattr(poller.db, "fetchval", cap.fetchval)
    monkeypatch.setattr(poller.db, "execute", cap.execute)
    monkeypatch.setattr(poller.scraper, "scrape_signals", scrape_coro)

    async def _drain():
        return {}

    monkeypatch.setattr(poller.executor, "drain_pending", _drain)
    _run(poller._one_tick())
    assert len(cap.updates) == 1
    return cap.updates[0]


def test_one_tick_records_login_failed_category(monkeypatch):
    from app.edge_terminal import poller

    async def _raise():
        raise poller.scraper.ScraperLoginFailed("bad creds")

    error, count, category = _run_tick_with(monkeypatch, _raise)
    assert category == poller.scraper.SCRAPE_LOGIN_FAILED
    assert error.startswith("login_failed:")
    assert count == 0


def test_one_tick_records_no_rows_category(monkeypatch):
    from app.edge_terminal import poller

    async def _raise():
        raise poller.scraper.ScraperDOMChanged("0 rows")

    error, count, category = _run_tick_with(monkeypatch, _raise)
    assert category == poller.scraper.SCRAPE_NO_ROWS
    assert error.startswith("no_rows:")


def test_one_tick_records_browser_missing_category(monkeypatch):
    from app.edge_terminal import poller

    async def _raise():
        raise poller.scraper.ScraperUnavailable("no chromium")

    error, count, category = _run_tick_with(monkeypatch, _raise)
    assert category == poller.scraper.SCRAPE_BROWSER_MISSING
    assert error.startswith("browser_missing:")


def test_one_tick_ok_category_when_clean(monkeypatch):
    from app.edge_terminal import poller

    async def _empty():
        return []

    error, count, category = _run_tick_with(monkeypatch, _empty)
    assert category == poller.scraper.SCRAPE_OK
    assert error is None
    assert count == 0


# ---------------------------------------------------------------------
# Health probe category → card status mapping
# ---------------------------------------------------------------------

def _probe_with_status(monkeypatch, status_dict):
    from app import health
    from app.edge_terminal import poller

    async def _status():
        return status_dict

    monkeypatch.setattr(poller, "get_status", _status)
    return _run(health._probe_edge_terminal_poller())


def test_probe_unconfigured_when_not_edge_terminal_mode(monkeypatch):
    card = _probe_with_status(monkeypatch, {"input_mode": "pipeline"})
    assert card["status"] == "unconfigured"


def test_probe_down_on_browser_missing(monkeypatch):
    card = _probe_with_status(
        monkeypatch,
        {
            "input_mode": "edge_terminal",
            "last_error_category": "browser_missing",
            "last_error": "no chromium",
            "stale": True,
        },
    )
    assert card["status"] == "down"


def test_probe_degraded_on_login_failed(monkeypatch):
    card = _probe_with_status(
        monkeypatch,
        {
            "input_mode": "edge_terminal",
            "last_error_category": "login_failed",
            "last_error": "bad creds",
            "stale": False,
        },
    )
    assert card["status"] == "degraded"


def test_probe_ok_when_clean(monkeypatch):
    card = _probe_with_status(
        monkeypatch,
        {
            "input_mode": "edge_terminal",
            "last_error_category": "ok",
            "last_error": None,
            "stale": False,
            "age_seconds": 12.0,
            "last_signal_count": 3,
        },
    )
    assert card["status"] == "ok"
    assert "signals=3" in card["detail"]


# ---------------------------------------------------------------------
# POST /api/edge-terminal/self-check end-to-end (regression: the route
# must reach the exported scraper.self_check, not 500 on a missing
# attribute).
# ---------------------------------------------------------------------

def test_self_check_route_returns_scraper_result(monkeypatch):
    from fastapi.testclient import TestClient

    from app import edge_terminal
    from app.auth import AuthedUser, require_approved_user
    from app.main import app

    async def _fake_self_check():
        return {
            "ok": True,
            "category": "ok",
            "detail": "Login OK; saw 4 badged signals.",
            "signal_count": 4,
        }

    monkeypatch.setattr(edge_terminal, "self_check", _fake_self_check)

    fake_user = AuthedUser(
        user_id="u_test",
        session_id="s_test",
        claims={},
    )
    app.dependency_overrides[require_approved_user] = lambda: fake_user
    try:
        client = TestClient(app)
        resp = client.post("/api/edge-terminal/self-check")
    finally:
        app.dependency_overrides.pop(require_approved_user, None)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "ok": True,
        "category": "ok",
        "detail": "Login OK; saw 4 badged signals.",
        "signal_count": 4,
    }
