"""Unit tests for app.orchestrator.venue_router.

Pure logic — no DB, no network. Covers the four routing modes and the
auto-mode signal-comparison rules.
"""
from __future__ import annotations

import pytest

from app.orchestrator.venue_router import (
    KALSHI,
    POLYMARKET,
    VALID_MODES,
    dedupe,
    normalize_mode,
    route_for_candidate,
)


def test_default_mode_routes_to_kalshi_only():
    # Empty candidate + default mode preserves prior Kalshi-only behavior.
    assert route_for_candidate({}, mode="kalshi") == [KALSHI]


def test_polymarket_mode_routes_to_polymarket_only():
    assert route_for_candidate({}, mode="polymarket") == [POLYMARKET]


def test_both_mode_fans_out_to_both_venues_when_both_have_signal():
    cand = {
        "kalshi_market_id": "K-1", "current_price": 0.5,
        "polymarket_market_id": "0xabc",
    }
    assert route_for_candidate(cand, mode="both") == [KALSHI, POLYMARKET]


def test_both_mode_drops_polymarket_when_no_signal():
    cand = {"kalshi_market_id": "K-1", "current_price": 0.5}
    assert route_for_candidate(cand, mode="both") == [KALSHI]


def test_both_mode_drops_kalshi_when_no_signal():
    cand = {"polymarket_market_id": "0xabc", "polymarket_price": 0.4}
    assert route_for_candidate(cand, mode="both") == [POLYMARKET]


def test_both_mode_falls_back_to_kalshi_when_neither_has_signal():
    # Never return an empty list — the cycle would treat it as a place_error.
    assert route_for_candidate({}, mode="both") == [KALSHI]


def test_auto_with_only_kalshi_signal_picks_kalshi():
    cand = {"kalshi_market_id": "K-1", "current_price": 0.55}
    assert route_for_candidate(cand, mode="auto", side="YES") == [KALSHI]


def test_auto_with_only_polymarket_signal_picks_polymarket():
    cand = {"polymarket_market_id": "0xabc", "polymarket_price": 0.42}
    assert route_for_candidate(cand, mode="auto", side="YES") == [POLYMARKET]


def test_auto_picks_cheaper_yes_buy():
    # Kalshi @ 0.55, Polymarket @ 0.50 → Polymarket wins for YES.
    cand = {
        "kalshi_market_id": "K-1", "current_price": 0.55,
        "polymarket_market_id": "0xabc", "polymarket_price": 0.50,
    }
    assert route_for_candidate(cand, mode="auto", side="YES") == [POLYMARKET]


def test_auto_picks_cheaper_no_buy():
    # NO buy is cheaper at the venue with the higher YES price.
    cand = {
        "kalshi_market_id": "K-1", "current_price": 0.55,
        "polymarket_market_id": "0xabc", "polymarket_price": 0.60,
    }
    assert route_for_candidate(cand, mode="auto", side="NO") == [POLYMARKET]


def test_auto_breaks_price_tie_on_liquidity():
    cand = {
        "kalshi_market_id": "K-1", "current_price": 0.50,
        "polymarket_market_id": "0xabc", "polymarket_price": 0.50,
        "kalshi_liquidity": 100.0,
        "polymarket_liquidity": 5000.0,
    }
    assert route_for_candidate(cand, mode="auto", side="YES") == [POLYMARKET]


def test_auto_falls_back_to_kalshi_when_no_signal():
    assert route_for_candidate({}, mode="auto") == [KALSHI]


def test_unknown_mode_raises():
    with pytest.raises(ValueError):
        route_for_candidate({}, mode="binance")


def test_normalize_mode_accepts_all_documented_modes():
    for m in VALID_MODES:
        assert normalize_mode(m) == m
    assert normalize_mode("  KALSHI ") == "kalshi"
    assert normalize_mode(None) == "kalshi"


def test_dedupe_preserves_order():
    assert dedupe(["kalshi", "polymarket", "kalshi"]) == ["kalshi", "polymarket"]


def test_route_uses_settings_when_mode_omitted(monkeypatch):
    # Force the settings cache to a known value to exercise the
    # "mode is None → read from settings" branch.
    from app.orchestrator import venue_router as vr

    class _Stub:
        venue_routing = "polymarket"

    monkeypatch.setattr(vr, "get_settings", lambda: _Stub())
    assert route_for_candidate({}) == [POLYMARKET]
