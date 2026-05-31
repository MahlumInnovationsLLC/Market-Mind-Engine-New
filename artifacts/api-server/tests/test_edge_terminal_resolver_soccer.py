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


# ---------------------------------------------------------------------------
# Matchup-integrity guards: both-teams present + date sanity.
# ---------------------------------------------------------------------------

def test_ticker_date_parses_embedded_yymondd():
    assert market_resolver._ticker_date(
        "KXMLBSPREAD-26MAY261810WSHCLE-CLE2",
    ).isoformat() == "2026-05-26"
    assert market_resolver._ticker_date(
        "KXUCLTOTAL-26MAY30PSGARS-2",
    ).isoformat() == "2026-05-30"
    assert market_resolver._ticker_date("KXATPMATCH-NODATE-ABC") is None


def test_side_evidence_tolerates_kalshi_abbreviations():
    # Acronym of a multi-word side ("Paris Saint-Germain" -> "psg") and the
    # first-three-letters form ("arsenal" -> "ars") must both be generated,
    # so a market that only abbreviates a club still matches.
    _, codes = market_resolver._side_evidence(["paris", "saint", "germain"])
    assert "psg" in codes
    _, codes = market_resolver._side_evidence(["arsenal"])
    assert "ars" in codes


def test_resolve_match_rejects_wrong_opponent_same_series(monkeypatch):
    # WSH@CLE shares the picked team's code (CLE) but is the WRONG game for a
    # BOS@CLE signal — the opponent (Boston) appears nowhere, so it must be
    # rejected rather than traded.
    wrong = {
        "ticker": "KXMLBSPREAD-26MAY311810WSHCLE-CLE2",
        "title": "Washington Nationals vs Cleveland Guardians",
    }
    _wire_no_live(monkeypatch, cands=[wrong], llm_ticker=None)

    rr = _run(market_resolver.resolve_match({
        "vertical": "mlb", "pick_type": "spread",
        "matchup": "Boston Red Sox @ Cleveland Guardians",
        "selection": "Guardians +1.5", "signal_date": "2026-05-31",
    }))

    assert rr.kind == "no_match"
    assert rr.ticker is None
    assert "both_teams=0" in (rr.reason or "")


def test_resolve_match_rejects_stale_dated_market(monkeypatch):
    # Correct opponent but the market is dated five days before the signal's
    # game — a stale/closed fixture that traps on "no liquidity". Reject it.
    stale = {
        "ticker": "KXMLBSPREAD-26MAY261810BOSCLE-CLE2",
        "title": "Boston Red Sox vs Cleveland Guardians",
    }
    _wire_no_live(monkeypatch, cands=[stale], llm_ticker=None)

    rr = _run(market_resolver.resolve_match({
        "vertical": "mlb", "pick_type": "spread",
        "matchup": "Boston Red Sox @ Cleveland Guardians",
        "selection": "Guardians +1.5", "signal_date": "2026-05-31",
    }))

    assert rr.kind == "no_match"
    assert "date_ok=0" in (rr.reason or "")


def test_ticker_team_code_extracts_segment():
    # Team-code segment is the chunk after date(+time) and before the
    # outcome suffix — codes matched there can't collide with the series
    # prefix or outcome.
    assert market_resolver._ticker_team_code(
        "KXUCLTOTAL-26MAY30PSGARS-2") == "PSGARS"
    assert market_resolver._ticker_team_code(
        "KXMLBSPREAD-26MAY311810BOSCLE-CLE2") == "BOSCLE"
    assert market_resolver._ticker_team_code(
        "KXWTAMATCH-26MAY27LYSCIR-CIR") == "LYSCIR"


def test_resolve_match_llm_rejects_same_date_wrong_opponent(monkeypatch):
    # The LLM picks a market on the CORRECT date that names the picked team
    # but the WRONG opponent (Arsenal vs Tottenham for an Arsenal @ PSG
    # signal). The both-teams guard at the LLM tier must reject it.
    wrong = {
        "ticker": "KXEPLTOTAL-26MAY30ARSTOT-2",
        "title": "Arsenal vs Tottenham — total goals",
    }
    _wire_no_live(monkeypatch, cands=[wrong], llm_ticker=wrong["ticker"])

    rr = _run(market_resolver.resolve_match({
        "vertical": "soccer", "pick_type": "total",
        "matchup": "Arsenal @ Paris Saint-Germain", "selection": "UNDER 2.5",
        "signal_date": "2026-05-30",
    }))

    assert rr.kind == "no_match"
    assert rr.ticker is None
    assert "rejected" in (rr.reason or "")


def test_resolve_match_rejects_shared_city_wrong_opponent(monkeypatch):
    # A Yankees signal must NOT match a same-day Mets market: the shared
    # "New York" tokens and the broad "york" alias are ignored for side
    # identity, so the Mets ticker/title fails the Yankees side.
    mets = {
        "ticker": "KXMLBSPREAD-26MAY311810BOSNYM-NYM2",
        "title": "Boston Red Sox vs New York Mets",
    }
    _wire_no_live(monkeypatch, cands=[mets], llm_ticker=mets["ticker"])

    rr = _run(market_resolver.resolve_match({
        "vertical": "mlb", "pick_type": "spread",
        "matchup": "Boston Red Sox @ New York Yankees",
        "selection": "Yankees -1.5", "signal_date": "2026-05-31",
    }))

    assert rr.kind == "no_match"
    assert rr.ticker is None


def test_resolve_match_accepts_shared_city_correct_opponent(monkeypatch):
    # The genuine Yankees market (BOSNYY) still matches — tightening must not
    # over-reject the correct same-city game.
    yanks = {
        "ticker": "KXMLBSPREAD-26MAY311810BOSNYY-NYY2",
        "title": "Boston Red Sox vs New York Yankees",
    }
    _wire_no_live(monkeypatch, cands=[yanks], llm_ticker=yanks["ticker"])

    rr = _run(market_resolver.resolve_match({
        "vertical": "mlb", "pick_type": "spread",
        "matchup": "Boston Red Sox @ New York Yankees",
        "selection": "Yankees -1.5", "signal_date": "2026-05-31",
    }))

    assert rr.kind == "matched"
    assert rr.ticker == yanks["ticker"]


def test_resolve_match_rejects_shared_word_wrong_club(monkeypatch):
    # Manchester City and Manchester United share the "Manchester" token but
    # neither is a place word in the generic list. A single shared-token
    # overlap must NOT prove the side: an Arsenal @ Man City signal must not
    # match an Arsenal vs Man United market.
    united = {
        "ticker": "KXEPLSPREAD-26MAY30ARSMUN-MUN1",
        "title": "Arsenal vs Manchester United",
    }
    _wire_no_live(monkeypatch, cands=[united], llm_ticker=united["ticker"])

    rr = _run(market_resolver.resolve_match({
        "vertical": "soccer", "pick_type": "spread",
        "matchup": "Arsenal @ Manchester City",
        "selection": "Manchester City -0.5", "signal_date": "2026-05-30",
    }))

    assert rr.kind == "no_match"
    assert rr.ticker is None


def test_resolve_match_accepts_correct_game_and_date(monkeypatch):
    # Both teams present AND dated to the signal's game -> matched.
    right = {
        "ticker": "KXMLBSPREAD-26MAY311810BOSCLE-CLE2",
        "title": "Boston Red Sox vs Cleveland Guardians",
    }
    _wire_no_live(monkeypatch, cands=[right], llm_ticker=None)

    rr = _run(market_resolver.resolve_match({
        "vertical": "mlb", "pick_type": "spread",
        "matchup": "Boston Red Sox @ Cleveland Guardians",
        "selection": "Guardians +1.5", "signal_date": "2026-05-31",
    }))

    assert rr.kind == "matched"
    assert rr.ticker == right["ticker"]
