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


def test_spread_sign_parses_plus_minus_and_absent():
    # The handicap sign decides BOTH the target market and the side, so it
    # must be parsed independently of the line magnitude.
    assert market_resolver._spread_sign("Giants +1.5") == 1
    assert market_resolver._spread_sign("Giants -1.5") == -1
    assert market_resolver._spread_sign("Giants 1.5") is None
    assert market_resolver._spread_sign("Giants") is None


def test_resolve_match_plus_spread_targets_opponent_market_no_side(monkeypatch):
    # "Giants +1.5" (SF underdog cover) must resolve to the OPPONENT's
    # "wins by over 1.5" market and recommend NO — i.e. "Brewers do NOT win
    # by over 1.5" — NOT YES on SF's own over-1.5 market.  Both teams' over-
    # 1.5 markets are offered; the resolver must pick MIL2 + side NO.
    sf = {
        "ticker": "KXMLBSPREAD-26MAY30SFMIL-SF2",
        "title": "San Francisco Giants vs Milwaukee Brewers",
    }
    mil = {
        "ticker": "KXMLBSPREAD-26MAY30SFMIL-MIL2",
        "title": "San Francisco Giants vs Milwaukee Brewers",
    }
    _wire_no_live(monkeypatch, cands=[sf, mil], llm_ticker=None)

    rr = _run(market_resolver.resolve_match({
        "vertical": "mlb", "pick_type": "spread",
        "matchup": "San Francisco Giants @ Milwaukee Brewers",
        "selection": "Giants +1.5", "signal_date": "2026-05-30",
    }))

    assert rr.kind == "matched"
    assert rr.ticker == "KXMLBSPREAD-26MAY30SFMIL-MIL2"
    assert rr.side == "NO"


def test_resolve_match_minus_spread_targets_pick_market_yes_side(monkeypatch):
    # "Giants -1.5" (SF favourite) keeps the PICK team's "wins by over 1.5"
    # market and recommends YES — "SF wins by over 1.5".
    sf = {
        "ticker": "KXMLBSPREAD-26MAY30SFMIL-SF2",
        "title": "San Francisco Giants vs Milwaukee Brewers",
    }
    mil = {
        "ticker": "KXMLBSPREAD-26MAY30SFMIL-MIL2",
        "title": "San Francisco Giants vs Milwaukee Brewers",
    }
    _wire_no_live(monkeypatch, cands=[sf, mil], llm_ticker=None)

    rr = _run(market_resolver.resolve_match({
        "vertical": "mlb", "pick_type": "spread",
        "matchup": "San Francisco Giants @ Milwaukee Brewers",
        "selection": "Giants -1.5", "signal_date": "2026-05-30",
    }))

    assert rr.kind == "matched"
    assert rr.ticker == "KXMLBSPREAD-26MAY30SFMIL-SF2"
    assert rr.side == "YES"


def test_resolve_match_signed_spread_ignores_scraper_ticker(monkeypatch):
    # A scraper-supplied kalshi_ticker is normally authoritative, BUT for a
    # signed spread the scraper resolves "+1.5" to the favourite's own market
    # (the wrong-market bug).  The resolver must IGNORE the direct ticker and
    # run sign-aware resolution: "Giants +1.5" → opponent's MIL2 market + NO.
    sf = {
        "ticker": "KXMLBSPREAD-26MAY30SFMIL-SF2",
        "title": "San Francisco Giants vs Milwaukee Brewers",
    }
    mil = {
        "ticker": "KXMLBSPREAD-26MAY30SFMIL-MIL2",
        "title": "San Francisco Giants vs Milwaukee Brewers",
    }
    _wire_no_live(monkeypatch, cands=[sf, mil], llm_ticker=None)

    rr = _run(market_resolver.resolve_match({
        "vertical": "mlb", "pick_type": "spread",
        "matchup": "San Francisco Giants @ Milwaukee Brewers",
        "selection": "Giants +1.5", "signal_date": "2026-05-30",
        # Scraper points at the FAVOURITE's own market — must be discarded.
        "kalshi_ticker": "KXMLBSPREAD-26MAY30SFMIL-SF2",
    }))

    assert rr.kind == "matched"
    assert rr.ticker == "KXMLBSPREAD-26MAY30SFMIL-MIL2"
    assert rr.side == "NO"


def test_resolve_match_unsigned_spread_honours_scraper_ticker(monkeypatch):
    # An UNSIGNED spread selection carries no handicap sign, so there is no
    # side to derive — the scraper-supplied ticker stays authoritative and the
    # direct short-circuit is honoured (side left unset).
    async def _hit(*a, **k):
        return 1

    monkeypatch.setattr(market_resolver.db, "fetchval", _hit)

    rr = _run(market_resolver.resolve_match({
        "vertical": "mlb", "pick_type": "spread",
        "matchup": "San Francisco Giants @ Milwaukee Brewers",
        "selection": "Giants 1.5", "signal_date": "2026-05-30",
        "kalshi_ticker": "KXMLBSPREAD-26MAY30SFMIL-SF2",
    }))

    assert rr.kind == "matched"
    assert rr.ticker == "KXMLBSPREAD-26MAY30SFMIL-SF2"
    assert rr.side is None


def test_resolve_match_non_spread_leaves_side_unset(monkeypatch):
    # A moneyline pick must not carry a resolver-dictated side — the scraper's
    # side is honoured downstream.
    cand = {
        "ticker": "KXMLBGAME-26MAY30SFMIL-SF",
        "title": "San Francisco Giants vs Milwaukee Brewers",
    }
    _wire_no_live(monkeypatch, cands=[cand], llm_ticker=None)

    rr = _run(market_resolver.resolve_match({
        "vertical": "mlb", "pick_type": "moneyline",
        "matchup": "San Francisco Giants @ Milwaukee Brewers",
        "selection": "Giants", "signal_date": "2026-05-30",
    }))

    assert rr.kind == "matched"
    assert rr.side is None


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


# --- dynamic tag-based series discovery -----------------------------------

_FAKE_SPORTS_SERIES = [
    {"ticker": "KXINTLFRIENDLYGAME", "tags": ["Soccer"]},
    {"ticker": "KXINTLFRIENDLYSPREAD", "tags": ["Soccer"]},
    {"ticker": "KXINTLFRIENDLYTOTAL", "tags": ["Soccer"]},
    {"ticker": "KXUEFANLGAME", "tags": ["Soccer"]},
    {"ticker": "KXEPLGAME", "tags": ["Soccer"]},
    # Excluded bet-type variants that share our GAME/SPREAD/TOTAL suffix:
    {"ticker": "KXEPL1HSPREAD", "tags": ["Soccer"]},     # half-time
    {"ticker": "KXEPLTEAMTOTAL", "tags": ["Soccer"]},    # team total
    {"ticker": "KXUCLBTTS", "tags": ["Soccer"]},         # both-teams-to-score
    {"ticker": "KXNBASERIESSPREAD", "tags": ["Basketball"]},  # playoff series
    # Wrong sport — must never appear for a soccer pick:
    {"ticker": "KXMLBGAME", "tags": ["Baseball"]},
    {"ticker": "KXATPGAME", "tags": ["Tennis"]},
]


def _wire_discovery(monkeypatch, series=_FAKE_SPORTS_SERIES):
    async def _all(*, allow_network):
        return list(series)

    monkeypatch.setattr(market_resolver, "_all_sports_series", _all)


def test_discover_series_filters_by_tag_and_suffix(monkeypatch):
    _wire_discovery(monkeypatch)
    game = _run(market_resolver._discover_series(
        "soccer", "moneyline", allow_network=True))
    assert "KXINTLFRIENDLYGAME" in game
    assert "KXUEFANLGAME" in game
    assert "KXEPLGAME" in game
    # Wrong sport / wrong suffix excluded.
    assert "KXMLBGAME" not in game
    assert "KXATPGAME" not in game
    assert "KXINTLFRIENDLYSPREAD" not in game


def test_discover_series_excludes_non_full_game_variants(monkeypatch):
    _wire_discovery(monkeypatch)
    spread = _run(market_resolver._discover_series(
        "soccer", "spread", allow_network=True))
    assert "KXINTLFRIENDLYSPREAD" in spread
    assert "KXEPL1HSPREAD" not in spread        # half-time
    assert "KXNBASERIESSPREAD" not in spread    # playoff-series + wrong sport
    total = _run(market_resolver._discover_series(
        "soccer", "total", allow_network=True))
    assert "KXINTLFRIENDLYTOTAL" in total
    assert "KXEPLTEAMTOTAL" not in total        # team total


def test_discover_series_only_enabled_verticals(monkeypatch):
    _wire_discovery(monkeypatch)
    # MLB/tennis are deliberately not auto-discovered (single precise series
    # / MATCH-not-GAME), so discovery returns nothing for them.
    assert _run(market_resolver._discover_series(
        "mlb", "moneyline", allow_network=True)) == []
    assert _run(market_resolver._discover_series(
        "tennis", "moneyline", allow_network=True)) == []


# ---------------------------------------------------------------------------
# Soccer half-goal handicap == moneyline + FIFA nation codes.
# ---------------------------------------------------------------------------

def test_nation_codes_use_fifa_not_first_three_letters():
    # Kalshi abbreviates national sides with the FIFA code, which the generic
    # first-three-letters heuristic gets wrong for several nations.
    assert market_resolver._nation_codes({"romania"}) == {"rou"}
    assert market_resolver._nation_codes({"netherlands"}) == {"ned"}
    assert market_resolver._nation_codes({"spain"}) == {"esp"}
    # Collisions resolve to distinct codes.
    assert market_resolver._nation_codes({"chile"}) == {"chi"}
    assert market_resolver._nation_codes({"china"}) == {"chn"}
    assert market_resolver._nation_codes({"india"}) == {"ind"}
    assert market_resolver._nation_codes({"indonesia"}) == {"idn"}


def test_side_evidence_includes_fifa_nation_code():
    # The FIFA code must be in the code set so a market whose ticker uses it
    # (GEOROU) is recognised as naming Romania.
    _, codes = market_resolver._side_evidence(["romania"])
    assert "rou" in codes
    _, codes = market_resolver._side_evidence(["georgia"])
    assert "geo" in codes


# The real Kalshi market set for Romania @ Georgia: a 3-way winner market and
# 1.5/2.5 spread lines — there is NO 0.5 spread market.
_GEOROU_GAME = [
    {"ticker": "KXINTLFRIENDLYGAME-26JUN02GEOROU-GEO",
     "title": "Georgia vs Romania Winner?"},
    {"ticker": "KXINTLFRIENDLYGAME-26JUN02GEOROU-ROU",
     "title": "Georgia vs Romania Winner?"},
    {"ticker": "KXINTLFRIENDLYGAME-26JUN02GEOROU-TIE",
     "title": "Georgia vs Romania Winner?"},
]
_GEOROU_SPREAD = [
    {"ticker": "KXINTLFRIENDLYSPREAD-26JUN02GEOROU-GEO1",
     "title": "Georgia wins by over 1.5 goals?"},
    {"ticker": "KXINTLFRIENDLYSPREAD-26JUN02GEOROU-GEO2",
     "title": "Georgia wins by over 2.5 goals?"},
    {"ticker": "KXINTLFRIENDLYSPREAD-26JUN02GEOROU-ROU1",
     "title": "Romania wins by over 1.5 goals?"},
    {"ticker": "KXINTLFRIENDLYSPREAD-26JUN02GEOROU-ROU2",
     "title": "Romania wins by over 2.5 goals?"},
]


def test_soccer_plus_half_spread_routes_to_winner_market_no_side(monkeypatch):
    # "Romania +0.5" (underdog win-or-draw) has no 0.5 spread market.  It is
    # mathematically NO on the OPPONENT's winner: "Georgia does NOT win" ==
    # Romania wins or draws.  Must resolve to the Georgia winner market + NO.
    _wire_no_live(monkeypatch, cands=list(_GEOROU_GAME), llm_ticker=None)
    rr = _run(market_resolver.resolve_match({
        "vertical": "soccer", "pick_type": "spread",
        "matchup": "Romania @ Georgia", "selection": "Romania +0.5",
        "signal_date": "2026-06-02",
    }))
    assert rr.kind == "matched"
    assert rr.ticker == "KXINTLFRIENDLYGAME-26JUN02GEOROU-GEO"
    assert rr.side == "NO"


def test_soccer_minus_half_spread_routes_to_pick_winner_yes_side(monkeypatch):
    # "Romania -0.5" (must win) is YES on the PICK team's winner market.
    _wire_no_live(monkeypatch, cands=list(_GEOROU_GAME), llm_ticker=None)
    rr = _run(market_resolver.resolve_match({
        "vertical": "soccer", "pick_type": "spread",
        "matchup": "Romania @ Georgia", "selection": "Romania -0.5",
        "signal_date": "2026-06-02",
    }))
    assert rr.kind == "matched"
    assert rr.ticker == "KXINTLFRIENDLYGAME-26JUN02GEOROU-ROU"
    assert rr.side == "YES"


def test_soccer_non_half_spread_still_uses_spread_series(monkeypatch):
    # A 1.5 handicap is a genuine spread and must NOT be rerouted to the
    # winner market — "Romania -1.5" stays YES on Romania's over-1.5 line.
    _wire_no_live(monkeypatch, cands=list(_GEOROU_SPREAD), llm_ticker=None)
    rr = _run(market_resolver.resolve_match({
        "vertical": "soccer", "pick_type": "spread",
        "matchup": "Romania @ Georgia", "selection": "Romania -1.5",
        "signal_date": "2026-06-02",
    }))
    assert rr.kind == "matched"
    assert rr.ticker == "KXINTLFRIENDLYSPREAD-26JUN02GEOROU-ROU1"
    assert rr.side == "YES"


def test_soccer_plus_half_spread_scores_over_tie(monkeypatch):
    # The winner market set shares one title across GEO/ROU/TIE, so the tail
    # code is the only differentiator: targeting Georgia (for "Romania +0.5")
    # must out-score the TIE leg, never matching the draw by accident.
    _wire_no_live(monkeypatch, cands=list(_GEOROU_GAME), llm_ticker=None)
    rr = _run(market_resolver.resolve_match({
        "vertical": "soccer", "pick_type": "spread",
        "matchup": "Romania @ Georgia", "selection": "Romania +0.5",
        "signal_date": "2026-06-02",
    }))
    by_tkr = {c["ticker"]: c["score"] for c in rr.candidates}
    geo = by_tkr["KXINTLFRIENDLYGAME-26JUN02GEOROU-GEO"]
    tie = by_tkr["KXINTLFRIENDLYGAME-26JUN02GEOROU-TIE"]
    rou = by_tkr["KXINTLFRIENDLYGAME-26JUN02GEOROU-ROU"]
    assert geo > tie and geo > rou


def test_resolve_match_unions_discovered_series(monkeypatch):
    # An international friendly (never in the static soccer list) resolves
    # because discovery surfaces KXINTLFRIENDLYGAME and the cache carries
    # the matching market.
    _wire_discovery(monkeypatch)
    friendly = {
        "ticker": "KXINTLFRIENDLYGAME-26JUN01AUTTUN-TUN",
        "title": "Austria vs Tunisia Winner?",
    }
    _wire_no_live(monkeypatch, cands=[friendly], llm_ticker=None)

    rr = _run(market_resolver.resolve_match({
        "vertical": "soccer", "pick_type": "moneyline",
        "matchup": "Tunisia @ Austria",
        "selection": "Tunisia", "signal_date": "2026-06-01",
    }))

    assert rr.kind == "matched"
    assert rr.ticker == friendly["ticker"]
