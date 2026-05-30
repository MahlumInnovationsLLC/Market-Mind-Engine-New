"""Soccer market-resolution coverage + LLM team-overlap guard.

Kalshi lists soccer per competition (``KXEPL*``, ``KXUCL*``, …) with full
GAME / SPREAD / TOTAL series — there is no live generic ``KXSOCCER*``
series.  These tests pin:

  * the vertical/pick_type map fans soccer spreads & totals across the
    real competition series (so a Champions League spread can resolve);
  * the LLM fallback is rejected when its pick names neither team — the
    failure mode that would otherwise bet a totals line on an unrelated
    fixture (``UNDER 2.5`` for ``India @ Zimbabwe`` latching onto a
    Ligue 1 Nantes–Toulouse total).
"""
from __future__ import annotations

import asyncio
from typing import Any

from app.edge_terminal import market_resolver


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_soccer_spread_and_total_map_to_competition_series():
    m = market_resolver._SERIES_BY_VERTICAL_PICK
    assert ("soccer", "spread") in m
    assert ("soccer", "total") in m
    # The European-cup series must be present — that's what carries a
    # cross-league fixture like PSG vs Arsenal.
    assert "KXUCLSPREAD" in m[("soccer", "spread")]
    assert "KXUCLTOTAL" in m[("soccer", "total")]
    assert "KXEPLSPREAD" in m[("soccer", "spread")]
    # The empty generic series must NOT be the only mapping anymore.
    assert "KXSOCCERTOTAL" not in m[("soccer", "total")]


def test_has_team_overlap_detects_token_and_code():
    picked = {"arsenal"}
    other = {"paris", "saint", "germain"}
    codes = {"ars", "psg"}
    # Title names a team token.
    assert market_resolver._has_team_overlap(
        {"ticker": "KXUCLSPREAD-26MAY30PSGARS-ARS1", "title": "PSG vs Arsenal"},
        picked=picked, other=other, picked_codes=codes, other_codes=set(),
    )
    # Ticker code substring only.
    assert market_resolver._has_team_overlap(
        {"ticker": "KXUCLSPREAD-26MAY30PSGARS-ARS1", "title": ""},
        picked=set(), other=set(), picked_codes={"ars"}, other_codes=set(),
    )
    # Neither team present → no overlap.
    assert not market_resolver._has_team_overlap(
        {"ticker": "KXLIGUE1TOTAL-26MAY17FCNTFC-2", "title": "Nantes vs Toulouse"},
        picked={"india", "zimbabwe"}, other=set(),
        picked_codes=set(), other_codes=set(),
    )


def _wire_no_live(monkeypatch, *, cands, llm_ticker, llm_reason="llm matched"):
    async def _cache(prefixes):
        return list(cands)

    async def _live(prefixes):
        return []

    async def _llm(signal, scored):
        return (llm_ticker, llm_reason)

    monkeypatch.setattr(market_resolver, "_cache_candidates", _cache)
    monkeypatch.setattr(market_resolver, "_live_candidates", _live)
    monkeypatch.setattr(market_resolver.llm_resolver, "llm_pick", _llm)


def test_resolve_match_rejects_llm_pick_with_no_team_overlap(monkeypatch):
    cand = {
        "ticker": "KXLIGUE1TOTAL-26MAY17FCNTFC-2",
        "title": "Nantes vs Toulouse",
    }
    _wire_no_live(monkeypatch, cands=[cand], llm_ticker=cand["ticker"])

    rr = _run(market_resolver.resolve_match({
        "vertical": "soccer", "pick_type": "total",
        "matchup": "India @ Zimbabwe", "selection": "UNDER 2.5",
    }))

    assert rr.kind == "no_match"
    assert rr.ticker is None
    assert "rejected" in (rr.reason or "")


def test_resolve_match_accepts_llm_pick_that_names_a_team(monkeypatch):
    cand = {
        "ticker": "KXUCLTOTAL-26MAY30PSGARS-2",
        "title": "PSG vs Arsenal — total goals",
    }
    _wire_no_live(monkeypatch, cands=[cand], llm_ticker=cand["ticker"])

    rr = _run(market_resolver.resolve_match({
        "vertical": "soccer", "pick_type": "total",
        "matchup": "Arsenal @ Paris Saint-Germain", "selection": "UNDER 2.5",
    }))

    assert rr.kind == "matched"
    assert rr.ticker == cand["ticker"]
