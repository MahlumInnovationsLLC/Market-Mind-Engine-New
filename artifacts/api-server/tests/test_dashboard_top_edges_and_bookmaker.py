"""Read-side tests for the Top-Edges-Today + Bookmaker-Metrics tiles.

Both helpers under test (``dashboard.get_top_edges_today`` and
``dashboard.get_bookmaker_metrics``) read existing tables only;
asyncpg I/O is monkey-patched so the tests run without Postgres.

Covers:
  * Top-Edges: empty pool, multi-row ranking, halt-vertical greyout,
    snooze-row exclusion (caller-supplied), error-passthrough.
  * Bookmaker: window mapping (7d/30d/ytd), profit-factor math, equiv-
    unit P&L denominator, win-rate edge-cases, picks-today rollup.
  * snooze_prediction writes a row and returns the cutoff timestamp.
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


# -----------------------------------------------------------------------
# Top Edges Today
# -----------------------------------------------------------------------

def _patch_top_edges(monkeypatch, rows: list[dict[str, Any]], *, halts=None):
    from app import dashboard as dash
    from app import runtime_settings as rs

    async def _fake_fetch(_q, *args, **kwargs):
        return rows

    async def _fake_halts():
        return halts or {}

    monkeypatch.setattr(dash.db, "fetch", _fake_fetch)
    monkeypatch.setattr(rs, "get_vertical_halts", _fake_halts)


def test_top_edges_empty(monkeypatch):
    from app import dashboard as dash

    _patch_top_edges(monkeypatch, [])
    out = _run(dash.get_top_edges_today())
    assert out["items"] == []
    assert out["error"] is None
    assert out["limit"] == 25
    assert out["halted_verticals"] == []
    assert isinstance(out["generated_at"], str)


def test_top_edges_ranking_and_halt_greyout(monkeypatch):
    from app import dashboard as dash

    rows = [
        {
            "id": 11,
            "game_id": "g1",
            "league": "NBA",
            "kalshi_market_id": "KX-NBA-1",
            "market_price": 0.50,
            "model_prob": 0.62,
            "home_win_prob": 0.62,
            "confidence": 0.75,
            "edge": 0.09,
            "game_time": None,
            "game_context": {"matchup": "LAL @ BOS"},
            "snoozed_until": None,
        },
        {
            "id": 22,
            "game_id": "g2",
            "league": "BTC",
            "kalshi_market_id": "KX-BTC-1",
            "market_price": 0.48,
            "model_prob": 0.55,
            "home_win_prob": 0.55,
            "confidence": 0.60,
            "edge": 0.07,
            "game_time": None,
            "game_context": None,
            "snoozed_until": None,
        },
    ]
    _patch_top_edges(monkeypatch, rows, halts={"crypto": {}})
    out = _run(dash.get_top_edges_today(limit=5))
    assert out["limit"] == 5
    assert [r["prediction_id"] for r in out["items"]] == [11, 22]
    nba, crypto = out["items"]
    assert nba["matchup"] == "LAL @ BOS"
    assert nba["vertical"] == "sports"
    assert nba["vertical_halted"] is False
    assert nba["edge_pct"] == 9.0
    assert crypto["vertical"] == "crypto"
    assert crypto["vertical_halted"] is True
    assert crypto["edge_pct"] == 7.0
    assert "crypto" in out["halted_verticals"]


def test_top_edges_error_passthrough(monkeypatch):
    from app import dashboard as dash
    from app import runtime_settings as rs

    async def _boom(_q, *a, **kw):
        raise RuntimeError("relation predictions does not exist")

    async def _no_halts():
        return {}

    monkeypatch.setattr(dash.db, "fetch", _boom)
    monkeypatch.setattr(rs, "get_vertical_halts", _no_halts)
    out = _run(dash.get_top_edges_today())
    assert out["items"] == []
    assert "relation predictions" in (out["error"] or "")


# -----------------------------------------------------------------------
# Bookmaker Metrics
# -----------------------------------------------------------------------

def _patch_bookmaker(
    monkeypatch,
    *,
    trades_row: dict[str, Any] | None,
    pending_row: dict[str, Any] | None,
    by_vertical_rows: list[dict[str, Any]] | None = None,
):
    from app import dashboard as dash

    calls: list[str] = []

    async def _fake_fetchrow(q, *a, **kw):
        # Pending query is sourced from predictions (with NOT EXISTS
        # over trades as a sub-filter), so discriminate on the main
        # FROM clause rather than substring-matching "FROM trades".
        if "FROM predictions" in q:
            calls.append("predictions")
            return pending_row
        calls.append("trades")
        return trades_row

    async def _fake_fetch(_q, *a, **kw):
        return by_vertical_rows or []

    monkeypatch.setattr(dash.db, "fetchrow", _fake_fetchrow)
    monkeypatch.setattr(dash.db, "fetch", _fake_fetch)
    return calls


def test_bookmaker_metrics_7d_math(monkeypatch):
    from app import dashboard as dash

    _patch_bookmaker(
        monkeypatch,
        trades_row={
            "gross_wins": 850.0,
            "gross_losses": 425.0,
            "net_pnl": 425.0,
            "n_settled": 20,
            "n_wins": 13,
        },
        pending_row={
            "top_conf": 0.81,
            "top_edge": 0.0912,
            "n_today": 5,
        },
        by_vertical_rows=[
            {"league": "NBA", "n": 3},
            {"league": "BTC", "n": 2},
        ],
    )
    out = _run(dash.get_bookmaker_metrics(window="7d"))
    assert out["window"] == "7d"
    assert out["profit_factor"] == 2.0
    assert out["equivalent_unit_pnl"] == 4.25  # 425/100
    assert out["unit_dollars"] == 100.0
    assert out["win_rate"] == round(13 / 20, 6)
    assert out["n_settled"] == 20
    assert out["top_confidence_today"] == 0.81
    assert out["highest_edge_today"] == 9.12
    assert out["picks_today"]["total"] == 5
    by_v = {b["vertical"]: b["n"] for b in out["picks_today"]["by_vertical"]}
    assert by_v == {"sports": 3, "crypto": 2}


def test_bookmaker_metrics_window_mapping(monkeypatch):
    from app import dashboard as dash

    seen_queries: list[str] = []

    async def _fake_fetchrow(q, *a, **kw):
        seen_queries.append(q)
        if "FROM predictions" in q:
            return {"top_conf": None, "top_edge": None, "n_today": 0}
        return {"gross_wins": 0.0, "gross_losses": 0.0,
                "net_pnl": 0.0, "n_settled": 0, "n_wins": 0}

    async def _fake_fetch(_q, *a, **kw):
        return []

    monkeypatch.setattr(dash.db, "fetchrow", _fake_fetchrow)
    monkeypatch.setattr(dash.db, "fetch", _fake_fetch)

    out30 = _run(dash.get_bookmaker_metrics(window="30d"))
    out_ytd = _run(dash.get_bookmaker_metrics(window="ytd"))
    out_bad = _run(dash.get_bookmaker_metrics(window="garbage"))
    assert out30["window"] == "30d"
    assert out_ytd["window"] == "ytd"
    assert out_bad["window"] == "7d"  # falls back to 7d default
    trades_qs = [q for q in seen_queries if "FROM trades" in q]
    assert any("interval '30 days'" in q for q in trades_qs)
    assert any("date_trunc('year', now())" in q for q in trades_qs)


def test_bookmaker_metrics_empty(monkeypatch):
    from app import dashboard as dash

    _patch_bookmaker(
        monkeypatch,
        trades_row={"gross_wins": 0.0, "gross_losses": 0.0,
                    "net_pnl": 0.0, "n_settled": 0, "n_wins": 0},
        pending_row={"top_conf": None, "top_edge": None, "n_today": 0},
        by_vertical_rows=[],
    )
    out = _run(dash.get_bookmaker_metrics(window="7d"))
    assert out["profit_factor"] is None
    assert out["equivalent_unit_pnl"] == 0.0
    assert out["win_rate"] is None
    assert out["n_settled"] == 0
    assert out["top_confidence_today"] is None
    assert out["highest_edge_today"] is None
    assert out["picks_today"] == {"total": 0, "by_vertical": []}


def test_bookmaker_metrics_all_winners_no_pf(monkeypatch):
    from app import dashboard as dash

    _patch_bookmaker(
        monkeypatch,
        trades_row={"gross_wins": 500.0, "gross_losses": 0.0,
                    "net_pnl": 500.0, "n_settled": 5, "n_wins": 5},
        pending_row={"top_conf": None, "top_edge": None, "n_today": 0},
    )
    out = _run(dash.get_bookmaker_metrics())
    assert out["profit_factor"] is None  # divide by zero → null, not ∞
    assert out["win_rate"] == 1.0
    assert out["equivalent_unit_pnl"] == 5.0


# -----------------------------------------------------------------------
# Snooze writer
# -----------------------------------------------------------------------

def test_snooze_prediction_writes_row(monkeypatch):
    from app import dashboard as dash

    captured: dict[str, Any] = {}

    async def _fake_fetchval(q, *args, **kwargs):
        return 1

    async def _fake_fetchrow(q, *args, **kwargs):
        captured["q"] = q
        captured["args"] = args
        return {"snoozed_until": "2026-05-22T13:00:00Z"}

    monkeypatch.setattr(dash.db, "fetchval", _fake_fetchval)
    monkeypatch.setattr(dash.db, "fetchrow", _fake_fetchrow)
    out = _run(
        dash.snooze_prediction(
            prediction_id=42, hours=2.0,
            by_user="u-1", by_email="op@example.com",
            reason="manual snooze",
        )
    )
    assert out["prediction_id"] == 42
    assert out["snoozed_until"] == "2026-05-22T13:00:00Z"
    assert out["hours"] == 2.0
    assert out["error"] is None
    assert "INSERT INTO prediction_snoozes" in captured["q"]
    assert captured["args"][0] == 42
    assert captured["args"][1] == 2.0


def test_snooze_prediction_caps_hours(monkeypatch):
    from app import dashboard as dash

    async def _fake_fetchval(q, *args, **kwargs):
        return 1

    async def _fake_fetchrow(q, *args, **kwargs):
        return {"snoozed_until": "2026-06-22T13:00:00Z"}

    monkeypatch.setattr(dash.db, "fetchval", _fake_fetchval)
    monkeypatch.setattr(dash.db, "fetchrow", _fake_fetchrow)
    out = _run(dash.snooze_prediction(prediction_id=1, hours=10000.0))
    # cap is 30 days = 720h
    assert out["hours"] == 720.0


def test_snooze_prediction_error_passthrough(monkeypatch):
    from app import dashboard as dash

    async def _fake_fetchval(q, *args, **kwargs):
        return 1

    async def _boom(*a, **kw):
        raise RuntimeError("write blocked")

    monkeypatch.setattr(dash.db, "fetchval", _fake_fetchval)
    monkeypatch.setattr(dash.db, "fetchrow", _boom)
    out = _run(dash.snooze_prediction(prediction_id=7, hours=1))
    assert out["snoozed_until"] is None
    assert "write blocked" in (out["error"] or "")


def test_snooze_prediction_missing_returns_error(monkeypatch):
    """Snoozing a non-existent prediction must not write a row."""
    from app import dashboard as dash

    insert_calls = {"n": 0}

    async def _fake_fetchval(q, *args, **kwargs):
        return None  # prediction not found

    async def _fake_fetchrow(q, *args, **kwargs):
        insert_calls["n"] += 1
        return {"snoozed_until": "should-not-happen"}

    monkeypatch.setattr(dash.db, "fetchval", _fake_fetchval)
    monkeypatch.setattr(dash.db, "fetchrow", _fake_fetchrow)
    out = _run(dash.snooze_prediction(prediction_id=9999, hours=1))
    assert out["snoozed_until"] is None
    assert "not found" in (out["error"] or "")
    assert insert_calls["n"] == 0
