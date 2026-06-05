"""Fixture-driven tests for the daily P&L summary email body builder.

Verifies that the new "By vertical" and "By persona" sections render
correctly, degrade gracefully when their data sources return no rows,
and that `collect_summary` correctly aggregates per-persona Brier /
accuracy / lean counts from `swarm_debates.analyst_verdicts`.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.notifications import alerts, daily_summary


# ---------------------------------------------------------------------------
# alerts.daily_pnl_summary — body rendering
# ---------------------------------------------------------------------------


_BASE_KW = dict(
    date_utc="2026-04-21",
    realized_pnl_cents=1234,
    unrealized_pnl_cents=-500,
    trades_settled=4,
    fills_today=6,
    open_positions=2,
    cycles_completed=12,
    halts_today=0,
    drift_alerts_today=1,
    dry_run=True,
)


def _body(**extra: Any) -> str:
    res = alerts.daily_pnl_summary(**{**_BASE_KW, **extra})
    assert res["dry_run"] is True
    return res["body_preview"]


def test_renders_full_by_vertical_table() -> None:
    body = _body(
        by_vertical=[
            {
                "vertical": "sports",
                "trades": 5,
                "wins": 3,
                "losses": 2,
                "pnl_cents": 1500,
                "cumulative_pnl_cents": 9000,
                "win_rate_pct": 60.0,
                "avg_edge": 0.045,
                "brier": 0.213,
            },
            {
                "vertical": "politics",
                "trades": 1,
                "wins": 0,
                "losses": 1,
                "pnl_cents": -300,
                "cumulative_pnl_cents": -300,
                "win_rate_pct": 0.0,
                "avg_edge": 0.012,
                "brier": None,
            },
        ],
    )
    assert "By vertical:" in body
    assert "sports" in body
    assert "politics" in body
    assert "3/2" in body          # W/L for sports
    assert "0/1" in body          # W/L for politics
    assert "+$15.00" in body      # sports pnl_cents = 1500
    assert "-$3.00" in body       # politics pnl_cents = -300
    assert "0.213" in body        # sports brier
    # Missing brier degrades to dash, not "0.000".
    assert "  -  " in body


def test_by_vertical_degrades_when_empty() -> None:
    body = _body(by_vertical=[])
    assert "By vertical:" in body
    assert "(no settled trades by vertical today)" in body


def test_renders_full_by_persona_table() -> None:
    body = _body(
        by_persona=[
            {
                "persona_id": "sharp_edge",
                "debates": 4,
                "brier": 0.182,
                "accuracy_pct": 75.0,
                "avg_confidence": 0.71,
                "lean_yes": 3,
                "lean_no": 1,
                "lean_pass": 0,
            },
            {
                "persona_id": "risk_guardian",
                "debates": 4,
                "brier": 0.305,
                "accuracy_pct": 50.0,
                "avg_confidence": None,
                "lean_yes": 1,
                "lean_no": 0,
                "lean_pass": 3,
            },
        ],
    )
    assert "By persona" in body
    assert "sharp_edge" in body
    assert "risk_guardian" in body
    assert "0.182" in body
    assert "75.0" in body
    assert "3/1/0" in body         # sharp_edge leans
    assert "1/0/3" in body         # risk_guardian leans
    # Missing avg_confidence degrades to dash placeholder.
    assert body.count("  -  ") >= 1


def test_by_persona_degrades_when_empty() -> None:
    body = _body(by_persona=None)
    assert "By persona" in body
    assert "(no settled swarm debates from today)" in body


def test_renders_handles_non_numeric_brier_and_confidence() -> None:
    """Lock in graceful rendering when DB returns non-numeric (e.g.
    string, None) for fields the formatter expects to be floats."""
    body = _body(
        by_vertical=[
            {
                "vertical": "weather",
                "trades": 1,
                "wins": 1,
                "losses": 0,
                "pnl_cents": 100,
                "cumulative_pnl_cents": 100,
                "win_rate_pct": 100.0,
                "avg_edge": 0.01,
                "brier": "not-a-number",   # bad data must not crash
            },
        ],
        by_persona=[
            {
                "persona_id": "matchup_historian",
                "debates": 1,
                "brier": 0.10,
                "accuracy_pct": 100.0,
                "avg_confidence": "n/a",   # bad data must render as dash
                "lean_yes": 1,
                "lean_no": 0,
                "lean_pass": 0,
            },
        ],
    )
    assert "weather" in body
    assert "matchup_historian" in body
    # Bad brier / confidence values fall through to the dash placeholder
    # rather than blowing up the format string.
    assert "  -  " in body


def test_renders_html_body_with_semantic_tables() -> None:
    res = alerts.daily_pnl_summary(
        **{
            **_BASE_KW,
            "by_vertical": [
                {
                    "vertical": "sports",
                    "trades": 2,
                    "wins": 1,
                    "losses": 1,
                    "pnl_cents": 250,
                    "win_rate_pct": 50.0,
                    "avg_edge": 0.02,
                    "brier": 0.21,
                },
            ],
            "by_persona": [
                {
                    "persona_id": "sharp_edge",
                    "debates": 1,
                    "brier": 0.18,
                    "accuracy_pct": 100.0,
                    "avg_confidence": 0.7,
                    "lean_yes": 1,
                    "lean_no": 0,
                    "lean_pass": 0,
                },
            ],
            "top_movers": [
                {"market_ticker": "KXNFL-FOO", "realized_pnl_cents": 500, "position": 3},
            ],
        }
    )
    html = res["html_preview"]
    assert html is not None
    assert "<!DOCTYPE html>" in html
    assert "<table" in html
    # All three section headers render in HTML.
    assert "Daily summary" in html and "2026-04-21" in html
    assert "By vertical" in html
    assert "By persona" in html
    assert "Top position movers" in html
    # Row data flows through.
    assert "sports" in html and "sharp_edge" in html and "KXNFL-FOO" in html
    # Plain-text body still produced for fallback.
    assert "By vertical:" in res["body_preview"]


def test_html_body_degrades_when_sections_empty() -> None:
    res = alerts.daily_pnl_summary(**{**_BASE_KW, "by_vertical": [], "by_persona": []})
    html = res["html_preview"]
    assert html is not None
    assert "No settled trades by vertical today." in html
    assert "No settled swarm debates from today." in html


def test_html_body_escapes_user_content() -> None:
    res = alerts.daily_pnl_summary(
        **{
            **_BASE_KW,
            "by_vertical": [
                {
                    "vertical": "<script>x</script>",
                    "trades": 1,
                    "wins": 1,
                    "losses": 0,
                    "pnl_cents": 100,
                    "win_rate_pct": 100.0,
                    "avg_edge": 0.01,
                    "brier": 0.10,
                },
            ],
        }
    )
    html = res["html_preview"]
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;x&lt;/script&gt;" in html


def test_renders_slippage_guard_offenders_text_and_html() -> None:
    res = alerts.daily_pnl_summary(
        **{
            **_BASE_KW,
            "slippage_guard_rejects_today": 7,
            "slippage_guard_offenders": [
                {
                    "market_ticker": "KXNFL-THIN",
                    "rejects": 4,
                    "max_slippage_cents": 12,
                    "max_edge_erased_pct": 250.5,
                },
                {
                    "market_ticker": "KXMLB-LITE",
                    "rejects": 1,
                    "max_slippage_cents": None,
                    "max_edge_erased_pct": None,
                },
            ],
        }
    )
    body = res["body_preview"]
    html = res["html_preview"]
    assert "slippage_guard_rejects_today" in body
    assert "Slippage-guard repeat offenders" in body
    assert "KXNFL-THIN" in body and "KXMLB-LITE" in body
    assert "12¢" in body
    assert "250.5" in body
    assert html is not None
    assert "Slippage-guard offenders" in html
    assert "KXNFL-THIN" in html
    assert "250.5%" in html


def test_slippage_guard_section_degrades_when_empty() -> None:
    res = alerts.daily_pnl_summary(
        **{**_BASE_KW, "slippage_guard_offenders": []}
    )
    assert "(no slippage-guard rejections today)" in res["body_preview"]
    assert "No slippage-guard rejections today." in res["html_preview"]


def test_subject_unchanged_by_new_sections() -> None:
    res = alerts.daily_pnl_summary(
        **{**_BASE_KW, "by_vertical": [], "by_persona": []}
    )
    assert res["subject"].startswith("[axiom] Daily summary 2026-04-21")
    assert "realized +$12.34" in res["subject"]


def test_renders_placed_trades_text_and_html() -> None:
    res = alerts.daily_pnl_summary(
        **{
            **_BASE_KW,
            "trades_placed_today": 2,
            "placed_trades": [
                {
                    "market_ticker": "KXMLBGAME-25-NYY",
                    "market_title": "Yankees win",
                    "vertical": "mlb",
                    "side": "YES",
                    "action": "buy",
                    "quantity": 10.0,
                    "price_cents": 62,
                    "trading_mode": "live",
                    "placed_by": "edge_terminal",
                    "model_prob": 0.71,
                },
            ],
        }
    )
    # overview count
    assert "trades_placed_today" in res["body_preview"]
    # text section
    assert "Trades placed today" in res["body_preview"]
    assert "KXMLBGAME-25-NYY" in res["body_preview"]
    assert "edge_terminal" in res["body_preview"]
    assert "62¢" in res["body_preview"]
    # html section
    assert "Trades placed today" in res["html_preview"]
    assert "KXMLBGAME-25-NYY" in res["html_preview"]
    assert "edge_terminal" in res["html_preview"]


def test_placed_trades_section_degrades_when_empty() -> None:
    res = alerts.daily_pnl_summary(**{**_BASE_KW, "placed_trades": []})
    assert "(no trades placed today)" in res["body_preview"]
    assert "No trades placed today." in res["html_preview"]


# ---------------------------------------------------------------------------
# collect_summary — per-persona aggregation from analyst_verdicts JSON
# ---------------------------------------------------------------------------


class _FakeDB:
    """Minimal stand-in for app.db that returns canned rows per query
    fragment.  Each entry maps a substring that uniquely identifies the
    SQL statement to either a list (for fetch) or a single row/value
    (for fetchrow / fetchval).  Anything unknown returns empty so the
    underlying try/except branches in collect_summary produce a useful
    "graceful degrade" outcome rather than blowing up the test."""

    def __init__(self, mapping: dict[str, Any]):
        self._mapping = mapping

    def _match(self, query: str) -> Any:
        for needle, value in self._mapping.items():
            if needle in query:
                return value
        return None

    async def fetch(self, query: str, *args):
        v = self._match(query)
        return v if isinstance(v, list) else []

    async def fetchrow(self, query: str, *args):
        v = self._match(query)
        if isinstance(v, dict):
            return v
        return None

    async def fetchval(self, query: str, *args):
        v = self._match(query)
        if isinstance(v, (int, float)):
            return v
        return 0


def _run(coro):
    # asyncio.get_event_loop() raises in Python 3.12+ when no loop is set
    # in the main thread (DeprecationWarning -> RuntimeError on newer
    # interpreters). Use asyncio.run() so each test gets a fresh loop.
    return asyncio.run(coro)


def test_collect_summary_aggregates_personas(monkeypatch: pytest.MonkeyPatch) -> None:
    debates = [
        {
            "id": "d1",
            "outcome": "WIN",                      # y = 1
            "analyst_verdicts": [
                {"persona": "sharp_edge",     "probability": 0.80, "confidence": 0.7, "lean": "YES"},
                {"persona": "risk_guardian",  "probability": 0.40, "confidence": 0.5, "lean": "PASS"},
            ],
        },
        {
            "id": "d2",
            "outcome": "LOSS",                     # y = 0
            # JSON-string form must also be parsed.
            "analyst_verdicts": (
                '[{"analyst_id": "sharp_edge", "probability": 0.6, "lean": "YES"},'
                ' {"persona": "risk_guardian", "probability": 0.3, "lean": "NO"}]'
            ),
        },
    ]
    fake = _FakeDB(
        {
            "FROM swarm_debates": debates,
            "FROM vertical_daily_pnl v": [
                {
                    "vertical": "sports",
                    "n": 2,
                    "wins": 1,
                    "losses": 1,
                    "pnl": 1.50,
                    "cum_pnl": 3.00,
                    "win_rate": 50.0,
                    "avg_edge": 0.04,
                    "brier": 0.22,
                },
            ],
        }
    )
    monkeypatch.setattr(daily_summary, "db", fake)

    summary = _run(daily_summary.collect_summary("2026-04-21"))

    # by_vertical surfaces from vertical_daily_pnl.
    assert summary["by_vertical"] and summary["by_vertical"][0]["vertical"] == "sports"
    assert summary["by_vertical"][0]["pnl_cents"] == 150
    assert summary["by_vertical"][0]["brier"] == 0.22

    # by_persona aggregated across both debates.
    by_persona = {p["persona_id"]: p for p in summary["by_persona"]}
    assert set(by_persona) == {"sharp_edge", "risk_guardian"}

    sharp = by_persona["sharp_edge"]
    assert sharp["debates"] == 2
    # (0.80-1)^2 + (0.6-0)^2 = 0.04 + 0.36 = 0.40 ; /2 = 0.20
    assert sharp["brier"] == pytest.approx(0.20, abs=1e-4)
    # WIN: 0.80>=0.5 == YES → correct.  LOSS: 0.6>=0.5 != NO → wrong.
    assert sharp["accuracy_pct"] == 50.0
    assert sharp["lean_yes"] == 2 and sharp["lean_no"] == 0 and sharp["lean_pass"] == 0

    rg = by_persona["risk_guardian"]
    assert rg["debates"] == 2
    # (0.40-1)^2 + (0.3-0)^2 = 0.36 + 0.09 = 0.45 ; /2 = 0.225
    assert rg["brier"] == pytest.approx(0.225, abs=1e-4)
    # WIN: 0.40<0.5 → predicted NO → wrong.  LOSS: 0.3<0.5 → predicted NO → correct.
    assert rg["accuracy_pct"] == 50.0
    assert rg["lean_pass"] == 1 and rg["lean_no"] == 1 and rg["lean_yes"] == 0

    # Sort order: lower Brier first (best persona of the day on top).
    assert summary["by_persona"][0]["persona_id"] == "sharp_edge"


def test_dashboard_swarm_personas_today_uses_shared_aggregator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dashboard endpoint must surface the same per-persona numbers
    the daily-summary email uses, so operators can spot a misbehaving
    persona before the morning digest arrives."""
    from app import dashboard

    debates = [
        {
            "id": "d1",
            "outcome": "WIN",
            "analyst_verdicts": [
                {"persona": "sharp_edge",    "probability": 0.80, "lean": "YES"},
                {"persona": "risk_guardian", "probability": 0.40, "lean": "PASS"},
            ],
        },
    ]
    fake = _FakeDB({"FROM swarm_debates": debates})
    monkeypatch.setattr(daily_summary, "db", fake)

    out = _run(dashboard.get_swarm_personas_today("2026-04-21"))
    assert out["date_utc"] == "2026-04-21"
    by_id = {p["persona_id"]: p for p in out["items"]}
    assert set(by_id) == {"sharp_edge", "risk_guardian"}
    assert by_id["sharp_edge"]["debates"] == 1
    # (0.80 - 1)^2 = 0.04
    assert by_id["sharp_edge"]["brier"] == pytest.approx(0.04, abs=1e-4)
    assert by_id["sharp_edge"]["accuracy_pct"] == 100.0
    assert by_id["sharp_edge"]["lean_yes"] == 1
    assert by_id["risk_guardian"]["lean_pass"] == 1
    # Sort: lowest Brier first.
    assert out["items"][0]["persona_id"] == "sharp_edge"


def test_dashboard_swarm_fallbacks_today_aggregates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fallback panel surfaces a per-persona count + the most-recent
    primary/fallback model + reason so operators can see at a glance
    which configured providers are degrading."""
    from app import dashboard

    debates = [
        {
            "analyst_verdicts": [
                {
                    "persona": "sharp_edge",
                    "provider": "anthropic",
                    "model": "claude-sonnet",
                    "primary_provider": "openai",
                    "primary_model": "gpt-5",
                    "fallback_used": True,
                    "fallback_reason": "RateLimitError: 429",
                },
                {"persona": "risk_guardian", "fallback_used": False},
            ],
        },
        {
            # JSON-string form must also be parsed.
            "analyst_verdicts": (
                '[{"persona": "sharp_edge", "provider": "anthropic",'
                ' "model": "claude-sonnet", "primary_provider": "openai",'
                ' "primary_model": "gpt-5", "fallback_used": true,'
                ' "fallback_reason": "ServerError: 503"}]'
            ),
        },
    ]
    fake = _FakeDB({"FROM swarm_debates": debates})
    monkeypatch.setattr(dashboard, "db", fake)

    out = _run(dashboard.get_swarm_fallbacks_today("2026-04-21"))
    assert out["date_utc"] == "2026-04-21"
    assert len(out["items"]) == 1
    row = out["items"][0]
    assert row["persona_id"] == "sharp_edge"
    assert row["fallbacks"] == 2
    assert row["primary_provider"] == "openai"
    assert row["primary_model"] == "gpt-5"
    assert row["fallback_provider"] == "anthropic"
    assert row["fallback_model"] == "claude-sonnet"
    # Most-recent reason wins (the JSON-string row is processed last).
    assert row["last_reason"] == "ServerError: 503"


def test_dashboard_swarm_fallbacks_today_no_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import dashboard

    monkeypatch.setattr(dashboard, "db", _FakeDB({"FROM swarm_debates": []}))
    out = _run(dashboard.get_swarm_fallbacks_today("2026-04-21"))
    assert out == {"date_utc": "2026-04-21", "items": []}


def test_dashboard_swarm_personas_today_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import dashboard

    monkeypatch.setattr(daily_summary, "db", _FakeDB({}))
    out = _run(dashboard.get_swarm_personas_today("2026-04-21"))
    assert out == {"date_utc": "2026-04-21", "items": []}


def test_collect_summary_degrades_when_no_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeDB({})
    monkeypatch.setattr(daily_summary, "db", fake)
    summary = _run(daily_summary.collect_summary("2026-04-21"))
    assert summary["by_vertical"] == []
    assert summary["by_persona"] == []
    # Sanity: rendering the empty summary still produces both section
    # headers with their graceful-degrade placeholders.
    body = alerts.daily_pnl_summary(**{**_BASE_KW, **summary})["body_preview"]
    assert "(no settled trades by vertical today)" in body
    assert "(no settled swarm debates from today)" in body


# ---------------------------------------------------------------------------
# /api/notifications/daily-summary/preview — operator console preview pane.
# ---------------------------------------------------------------------------


def test_daily_summary_preview_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """The preview endpoint composes collect_summary + dry_run dispatch
    so the operator console can render the exact email subscribers
    would receive, without sending. Past dates work; an unknown date
    still returns a usable HTML body via the graceful-degrade path."""
    from fastapi.testclient import TestClient

    from app import main as _main
    from app.auth import require_approved_user

    monkeypatch.setattr(daily_summary, "db", _FakeDB({}))

    _main.app.dependency_overrides[require_approved_user] = lambda: {
        "clerk_user_id": "user_op",
        "is_admin": True,
    }
    try:
        client = TestClient(_main.app)
        # Past date — should re-aggregate without error.
        resp = client.get(
            "/api/notifications/daily-summary/preview?date=2026-04-21"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["date_utc"] == "2026-04-21"
        assert body["is_today"] is False
        assert isinstance(body["html_preview"], str)
        assert body["html_preview"].startswith("<!DOCTYPE html>")
        # The subject embeds the rendered date so operators can sanity-check
        # the iframe is showing the day they asked for.
        assert "2026-04-21" in (body["subject"] or "")
        # Plain-text preview is always populated alongside the HTML body.
        assert "Daily Axiom-Predict summary" in (body["body_preview"] or "")

        # No date = today UTC (is_today=True).
        resp = client.get("/api/notifications/daily-summary/preview")
        assert resp.status_code == 200
        assert resp.json()["is_today"] is True

        # Bad date format -> 400 with a helpful detail.
        resp = client.get(
            "/api/notifications/daily-summary/preview?date=not-a-date"
        )
        assert resp.status_code == 400
        assert "YYYY-MM-DD" in resp.json()["detail"]

        # Future date -> 400 (we only render today / historical).
        resp = client.get(
            "/api/notifications/daily-summary/preview?date=2099-01-01"
        )
        assert resp.status_code == 400
        assert "future" in resp.json()["detail"]
    finally:
        _main.app.dependency_overrides.pop(require_approved_user, None)
