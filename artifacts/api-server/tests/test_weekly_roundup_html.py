"""Tests for the HTML rendering of the weekly P&L roundup email.

The weekly roundup ships an HTML body alongside the existing plain-text
fallback so mobile mail clients keep table alignment. These tests lock
in:
  * Both bodies are produced (text fallback + HTML).
  * The HTML uses the same semantic <table> styling as the daily summary.
  * Optional sections (notes, win rate) only appear when supplied.
  * User-supplied content is escaped, not rendered as markup.
"""
from __future__ import annotations

from typing import Any

from app.notifications import alerts


_BASE_KW = dict(
    week_ending_utc="2026-04-24",
    realized_pnl_cents=4321,
    unrealized_pnl_cents=-1500,
    trades_settled=12,
    dry_run=True,
)


def _result(**extra: Any) -> dict[str, Any]:
    res = alerts.weekly_roundup(**{**_BASE_KW, **extra})
    assert res["dry_run"] is True
    return res


def test_renders_html_body_with_semantic_table() -> None:
    res = _result(win_rate_pct=58.3)
    html = res["html_preview"]
    assert html is not None
    assert "<!DOCTYPE html>" in html
    assert "<table" in html
    # Header carries the week-ending date.
    assert "Weekly roundup" in html
    assert "2026-04-24" in html
    # Overview rows surface in the table.
    assert "realized_pnl" in html
    assert "unrealized_pnl" in html
    assert "trades_settled" in html
    assert "win_rate_pct" in html
    # Realized P&L is formatted and signed.
    assert "+$43.21" in html
    assert "-$15.00" in html
    # Plain-text fallback is still produced alongside the HTML.
    assert "Weekly Axiom-Predict P&L roundup." in res["body_preview"]
    assert "realized_pnl" in res["body_preview"]


def test_html_body_omits_optional_sections_when_absent() -> None:
    res = _result()
    html = res["html_preview"]
    assert html is not None
    # Win rate is optional and not in the base kwargs.
    assert "win_rate_pct" not in html
    # Notes block only renders when notes are supplied.
    assert "Notes" not in html


def test_html_body_renders_notes_when_supplied() -> None:
    res = _result(notes="Friday close was choppy across NFL slates.")
    html = res["html_preview"]
    assert html is not None
    assert "Notes" in html
    assert "Friday close was choppy across NFL slates." in html
    # And the text body keeps the notes section as well.
    assert "Notes:" in res["body_preview"]


def test_html_body_escapes_user_content() -> None:
    res = _result(notes="<script>alert(1)</script>")
    html = res["html_preview"]
    assert html is not None
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_html_body_uses_same_table_styling_as_daily_summary() -> None:
    """The semantic <table> styling should match the daily summary so
    mobile mail clients render both digests consistently."""
    weekly = _result()["html_preview"]
    daily = alerts.daily_pnl_summary(
        date_utc="2026-04-24",
        realized_pnl_cents=100,
        unrealized_pnl_cents=0,
        trades_settled=1,
        fills_today=1,
        open_positions=0,
        cycles_completed=1,
        halts_today=0,
        drift_alerts_today=0,
        dry_run=True,
    )["html_preview"]
    assert weekly is not None and daily is not None
    # Shared envelope + per-cell styling helpers from `_html_table`.
    for needle in (
        "border-collapse:collapse",
        "background:#F9FAFB",
        "border:1px solid #E5E7EB",
        "Axiom Predict",
    ):
        assert needle in weekly
        assert needle in daily


def test_dispatch_passes_html_body_to_postmark(monkeypatch) -> None:
    """Live (non-dry-run) path must hand both TextBody and HtmlBody
    off to Postmark so the dispatched email carries the new HTML."""
    captured: dict[str, Any] = {}

    def _fake_send(*, subject: str, text_body: str, html_body=None, tag: str):
        captured["subject"] = subject
        captured["text_body"] = text_body
        captured["html_body"] = html_body
        captured["tag"] = tag

    monkeypatch.setattr(alerts.postmark, "send_in_background", _fake_send)
    # Squash the user-fanout side effect so this test stays focused on
    # the operator dispatch path.
    monkeypatch.setattr(alerts, "_schedule_user_email_fanout", lambda **_: None)

    res = alerts.weekly_roundup(
        week_ending_utc="2026-04-24",
        realized_pnl_cents=200,
        unrealized_pnl_cents=0,
        trades_settled=3,
        dry_run=False,
    )
    assert res["queued"] is True
    assert captured["tag"] == "weekly-roundup"
    assert captured["text_body"].startswith("Weekly Axiom-Predict P&L roundup.")
    assert captured["html_body"] is not None
    assert "<table" in captured["html_body"]
    assert "Weekly roundup" in captured["html_body"]
