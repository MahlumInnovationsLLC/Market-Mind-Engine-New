"""Tests for the operator-facing crypto realised-accuracy rollup
(task #644).

Exercises the dashboard helper and the
``/api/dashboard/crypto-accuracy`` endpoint with stubbed
``db.fetchrow`` so the test does not need a live Postgres or the
Kalshi crypto producer.

The rollup powers the operator console panel that joins predicted
``predictions.kalshi_price`` against the realised
``predictions.settle_price`` the
``app.feeds.kalshi_crypto_markets`` settler stamps (migration
``0022_predictions_settlement``).
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest


def _run(coro):
    return asyncio.run(coro)


def _install_window_stub(
    monkeypatch,
    by_window: dict[int, dict[str, Any]],
) -> None:
    """Stub ``db.fetchrow`` to answer per-window crypto-accuracy queries.

    ``by_window`` maps the requested ``window_hours`` to the row the
    fake should return. The fake validates the SQL targets the
    expected predictions slice so a regression that swaps tables
    fails fast.
    """
    from app import db as db_module

    async def fake_fetchrow(sql: str, *args: Any) -> dict[str, Any] | None:
        assert "FROM predictions" in sql
        assert "league = 'CRYPTO'" in sql
        assert "settled_at" in sql
        assert args and isinstance(args[0], int)
        window = int(args[0])
        return by_window.get(window)

    monkeypatch.setattr(db_module, "fetchrow", fake_fetchrow)


def test_helper_returns_per_window_brier_and_hit_rate(monkeypatch):
    """Each requested window round-trips brier / hit_rate / counters
    from the underlying SELECT into the operator-facing payload."""
    from app import dashboard

    _install_window_stub(monkeypatch, {
        # Last 24h: 10 settled, 1 void, 9 scored. Mock brier 0.18,
        # hit rate 0.6.
        24: {
            "n_settled": 10,
            "n_void": 1,
            "n_scored": 9,
            "brier": 0.18,
            "hit_rate": 0.6,
        },
        # Last 7d: 80 settled, 5 void, 75 scored. Brier 0.22, hit
        # rate 0.55.
        24 * 7: {
            "n_settled": 80,
            "n_void": 5,
            "n_scored": 75,
            "brier": 0.22,
            "hit_rate": 0.5466666,
        },
    })

    out = _run(dashboard.get_crypto_accuracy_rollup())

    assert out["league"] == "CRYPTO"
    windows = out["windows"]
    assert [w["window_hours"] for w in windows] == [24, 24 * 7]

    day = windows[0]
    assert day["n_settled"] == 10
    assert day["n_void"] == 1
    assert day["n_scored"] == 9
    assert day["brier"] == pytest.approx(0.18)
    assert day["hit_rate"] == pytest.approx(0.6)

    week = windows[1]
    assert week["n_settled"] == 80
    assert week["n_void"] == 5
    assert week["n_scored"] == 75
    assert week["brier"] == pytest.approx(0.22)
    assert week["hit_rate"] == pytest.approx(0.5466666)


def test_helper_renders_no_data_when_window_is_empty(monkeypatch):
    """A window with no scored rows surfaces ``brier`` / ``hit_rate``
    as ``None`` (not 0.0) so the operator panel renders an
    unambiguous "no data" state instead of a misleading perfect
    Brier of zero."""
    from app import dashboard

    _install_window_stub(monkeypatch, {
        24: {
            "n_settled": 0, "n_void": 0, "n_scored": 0,
            "brier": None, "hit_rate": None,
        },
        24 * 7: {
            "n_settled": 4, "n_void": 4, "n_scored": 0,
            "brier": None, "hit_rate": None,
        },
    })

    out = _run(dashboard.get_crypto_accuracy_rollup())
    day, week = out["windows"]
    assert day["brier"] is None
    assert day["hit_rate"] is None
    assert day["n_scored"] == 0
    # All four week-window rows voided → still scoreless.
    assert week["n_settled"] == 4
    assert week["n_void"] == 4
    assert week["n_scored"] == 0
    assert week["brier"] is None
    assert week["hit_rate"] is None


def test_helper_soft_fails_when_predictions_table_missing(monkeypatch):
    """A fork without the settlement migration (or a transient DB
    error) must degrade each window to its empty shape rather than
    blanking the whole panel — same convention as the sibling
    ``/api/feeds/kalshi-crypto-candidates`` endpoint."""
    from app import db as db_module
    from app import dashboard

    async def boom(*_a, **_k):
        raise RuntimeError('relation "predictions" does not exist')

    monkeypatch.setattr(db_module, "fetchrow", boom)

    out = _run(dashboard.get_crypto_accuracy_rollup())
    assert out["league"] == "CRYPTO"
    assert [w["window_hours"] for w in out["windows"]] == [24, 24 * 7]
    for w in out["windows"]:
        assert w["n_settled"] == 0
        assert w["n_void"] == 0
        assert w["n_scored"] == 0
        assert w["brier"] is None
        assert w["hit_rate"] is None


def test_helper_isolates_per_window_failure(monkeypatch):
    """A failing 7d query must not blank the 24h card — the operator
    still sees the short-window rollup it was given, plus an empty
    7d card in place of the failing one."""
    from app import db as db_module
    from app import dashboard

    async def fake_fetchrow(sql: str, *args: Any) -> dict[str, Any] | None:
        assert args and isinstance(args[0], int)
        window = int(args[0])
        if window == 24:
            return {
                "n_settled": 5, "n_void": 0, "n_scored": 5,
                "brier": 0.12, "hit_rate": 0.8,
            }
        raise RuntimeError("temporary asyncpg error on 7d window")

    monkeypatch.setattr(db_module, "fetchrow", fake_fetchrow)

    out = _run(dashboard.get_crypto_accuracy_rollup())
    day, week = out["windows"]
    # 24h card preserved.
    assert day["window_hours"] == 24
    assert day["n_scored"] == 5
    assert day["brier"] == pytest.approx(0.12)
    assert day["hit_rate"] == pytest.approx(0.8)
    # 7d card degrades to empty rather than 500ing the panel.
    assert week["window_hours"] == 24 * 7
    assert week["n_settled"] == 0
    assert week["brier"] is None
    assert week["hit_rate"] is None


def test_endpoint_delegates_to_helper(monkeypatch):
    """The route handler returns whatever the helper produces — no
    massaging — so the helper-level tests above also lock the
    public API contract."""
    from app import main as main_module

    expected = {
        "league": "CRYPTO",
        "windows": [
            {
                "window_hours": 24,
                "n_settled": 3, "n_void": 0, "n_scored": 3,
                "brier": 0.1, "hit_rate": 0.667,
            },
        ],
    }

    async def fake_rollup() -> dict[str, Any]:
        return expected

    monkeypatch.setattr(
        main_module.dashboard, "get_crypto_accuracy_rollup", fake_rollup,
    )

    out = _run(main_module.dashboard_crypto_accuracy())
    assert out == expected
