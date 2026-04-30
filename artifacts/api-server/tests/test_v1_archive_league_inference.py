"""Unit tests for the V1-archive UNKNOWN-league inference cascade.

Task #740. Covers the full rule order in
``app.v1_archive.league_inference.infer_league``:

  1. Kalshi series prefix on top-level ``market_id``
  2. ``analysis_json.league`` (with alias normalization)
  3. ``analysis_json.<ticker_key>`` Kalshi prefix
  4. ``vertical`` single-league fallback
  5. NO RULE FIRES → ``None``

Also covers the ``is_unresolved`` predicate that the backtest harness
uses to decide which rows to skip.
"""
from __future__ import annotations

import pytest

from app.v1_archive.league_inference import (
    SUPPORTED_LEAGUES,
    infer_league,
    is_unresolved,
)


# --- is_unresolved ----------------------------------------------------------


@pytest.mark.parametrize("v", [
    None, "", "   ", "unknown", "UNKNOWN", "Unknown", " unknown ",
    "none", "NONE", "null", "NULL",
])
def test_is_unresolved_true_for_v1_sentinels(v):
    assert is_unresolved(v) is True


@pytest.mark.parametrize("v", ["NBA", "nba", "MLB", "EPL", "TECH_AI"])
def test_is_unresolved_false_for_real_leagues(v):
    assert is_unresolved(v) is False


def test_is_unresolved_false_for_non_string_garbage():
    # Defensive: a non-string non-None value (e.g. an int the V1 dump
    # could conceivably store after a bad cast) is NOT treated as
    # unresolved — the caller will hit a separate validation path.
    assert is_unresolved(0) is False
    assert is_unresolved(["nba"]) is False


# --- Rule 1: Kalshi prefix on market_id -------------------------------------


@pytest.mark.parametrize("market_id,expected_league", [
    # Sports (matches kalshi_sports_markets producer)
    ("KXNFLGAME-26APR29DETMIN-DET", "NFL"),
    ("KXNBAGAME-26APR29ORLDET-ORL", "NBA"),
    ("KXMLBGAME-26APR301240COLCIN-COL", "MLB"),
    ("KXNHLGAME-26APR29BOSNYR-BOS", "NHL"),
    ("KXMLSGAME-26APR29ATXMIA-ATX", "MLS"),
    ("KXEPLGAME-26APR29ARSCHE-ARS", "EPL"),
    ("KXLALIGAGAME-26APR29RMABAR-RMA", "LALIGA"),
    ("KXSERIEAGAME-26APR29JUVMIL-JUV", "SERIEA"),
    ("KXBUNDESGAME-26APR29BAYDOR-BAY", "BUNDES"),
    ("KXLIGUE1GAME-26APR29PSGOM-PSG", "LIGUE1"),
    ("KXUCLGAME-26APR29RMACITY-RMA", "UCL"),
    ("KXNCAABGAME-26APR29DUKEUNC-DUKE", "NCAAB"),
    # Combat / individual — longest-prefix-first ordering matters here
    ("KXATPMATCH-26FRENCH-NADAL", "ATP"),
    ("KXWTAMATCH-26WIMB-SWIATEK", "WTA"),
    ("KXUFCFIGHT-26UFC300-JONES", "UFC"),
    ("KXF1RACE-26MONACO-VER", "F1"),
    ("KXPGATOURNEY-26MASTERS-RAHM", "PGA"),
    # Commodities
    ("KXOIL-26MAY-80", "COMMODITIES"),
    ("KXGOLD-26MAY-2400", "COMMODITIES"),
    ("KXNATGAS-26MAY-3", "COMMODITIES"),
    # Tech / AI
    ("KXAI-OPENAI-GPT5-2026", "TECH_AI"),
    ("KXNVDA-26Q1EARNINGS-BEAT", "TECH_AI"),
    # Elections
    ("KXPRES2028-DEM-NOMINEE", "ELECTIONS"),
    ("KXSENATE26-PA-DEM", "ELECTIONS"),
    # Events / macro
    ("KXFED-26JUN-CUT25", "EVENTS_ECON"),
    ("KXCPI-26MAY-32", "EVENTS_ECON"),
    # Entertainment
    ("KXOSCAR2026-BESTPIC-OPP", "ENTERTAINMENT"),
])
def test_rule4_kalshi_prefix_on_market_id(market_id, expected_league):
    # Spec rule 4 — top-level market_id Kalshi prefix is the LAST-RESORT
    # rule, fired when no analysis_json blob is present (rules 1+3 miss)
    # and the vertical is not single-league (rule 2 misses).  The audit
    # key reflects the originating column.
    result = infer_league(
        market_id=market_id, vertical=None, analysis_json=None,
    )
    assert result is not None
    league, rule = result
    assert league == expected_league
    assert rule == "market_id:kalshi_prefix"


def test_rule4_kalshi_prefix_handles_lowercase():
    result = infer_league(
        market_id="kxnflgame-26apr29detmin-det",
        vertical=None, analysis_json=None,
    )
    assert result is not None
    assert result[0] == "NFL"


def test_rule4_kalshi_prefix_strips_leading_quotes():
    # Defensive: a JSON-string-of-a-string might leak through with a
    # leading quote.  The leading-non-alpha strip should still match.
    result = infer_league(
        market_id='"KXNBAGAME-foo"', vertical=None, analysis_json=None,
    )
    assert result is not None
    assert result[0] == "NBA"


# ---------------------------------------------------------------------------
# Rule 5 — V1 ``SPORT-<LEAGUE>-<SUB>-<HASH>`` synthetic-placeholder schema.
# Discovered live (2026-04-30) on Supabase: ~40,088 unresolved rows have a
# market_id like ``SPORT-NCAAB-NCAAB-873982...``; the second segment is the
# canonical league.  Distinct audit key keeps it separate from rule 4.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("market_id, expected_league", [
    ("SPORT-NBA-NBA-873982815bce991e0e334990ca56e228",   "NBA"),
    ("SPORT-NCAAB-NCAAB-873982815bce991e0e334990ca56e228", "NCAAB"),
    ("SPORT-NHL-NHL-deadbeef",                            "NHL"),
    ("SPORT-MLB-MLB-cafe",                                "MLB"),
    # case-insensitive top-level
    ("sport-nba-nba-abc",                                 "NBA"),
])
def test_rule5_v1_sport_schema(market_id, expected_league):
    result = infer_league(
        market_id=market_id, vertical="sports", analysis_json=None,
    )
    assert result is not None
    league, rule = result
    assert league == expected_league
    assert rule == "market_id:v1_sport_schema"


def test_rule5_unsupported_league_segment_returns_none():
    # FIBA / international basketball isn't in SUPPORTED_LEAGUES — rule 5
    # MUST refuse rather than mis-bucket as NBA.
    assert infer_league(
        market_id="SPORT-FIBA-KORJPN-abc",
        vertical="sports", analysis_json=None,
    ) is None


def test_rule5_does_not_fire_for_crypto_placeholder():
    # CRYPTO-BTC-5m must NOT match the SPORT-<X>- regex.
    assert infer_league(
        market_id="CRYPTO-BTC-5m", vertical="crypto", analysis_json=None,
    ) is None


def test_rule5_runs_after_rule4_kalshi_prefix():
    # If both rule 4 (Kalshi prefix) and rule 5 (V1 SPORT- schema) could
    # fire, rule 4 must win because it represents real Kalshi data.
    # ``KXNBAGAME-...`` doesn't start with SPORT- so rule 5 can't fire,
    # but this test pins the precedence intent for future maintainers.
    result = infer_league(
        market_id="KXNBAGAME-26APR29DETMIN-DET",
        vertical="sports", analysis_json=None,
    )
    assert result == ("NBA", "market_id:kalshi_prefix")


def test_rule4_does_not_match_crypto_prefixes():
    # KXBTC / KXETH are intentionally NOT in the rule table — they
    # route through the crypto vertical's own pricing stack, not the
    # per-league calibrator.
    assert infer_league(
        market_id="KXBTC-26DEC31-100K", vertical=None, analysis_json=None,
    ) is None
    assert infer_league(
        market_id="KXETH-26DEC31-5K", vertical=None, analysis_json=None,
    ) is None


def test_rule4_does_not_match_unknown_prefixes():
    # NCAAF is a real Kalshi series but not in SUPPORTED_LEAGUES, so
    # leave it unmapped rather than create a synthetic bucket.
    assert infer_league(
        market_id="KXNCAAFGAME-26SEP01-OSU", vertical=None, analysis_json=None,
    ) is None


# --- Rule 2: analysis_json.league -------------------------------------------


def test_rule3_analysis_json_direct_league():
    result = infer_league(
        market_id=None, vertical=None,
        analysis_json={"league": "nba", "extra": "ignored"},
    )
    assert result == ("NBA", "analysis_json.league")


def test_rule3_analysis_json_alias_normalization():
    """V1 stored some leagues loosely (``serie_a`` etc); the
    normalizer should map them onto SUPPORTED_LEAGUES."""
    cases = [
        ("serie_a", "SERIEA"),
        ("Serie A", "SERIEA"),
        ("bundesliga", "BUNDES"),
        ("ligue_1", "LIGUE1"),
        ("La Liga", "LALIGA"),
        ("champions league", "UCL"),
        ("tech-ai", "TECH_AI"),
        ("events-econ", "EVENTS_ECON"),
    ]
    for raw, expected in cases:
        result = infer_league(
            market_id=None, vertical=None,
            analysis_json={"league": raw},
        )
        assert result is not None, f"failed to map {raw!r}"
        assert result[0] == expected, f"expected {expected} got {result[0]} for {raw!r}"


def test_rule3_skipped_when_value_unknown():
    # If analysis_json.league is itself an unresolved sentinel,
    # rule 2 must NOT fire — fall through to the next rule.
    result = infer_league(
        market_id=None, vertical="commodities",
        analysis_json={"league": "unknown"},
    )
    assert result == ("COMMODITIES", "vertical:commodities")


def test_rule3_alternate_key_names():
    # V1 used a few different key names for the league field.
    for key in ["league", "League", "LEAGUE", "league_name", "leagueName"]:
        result = infer_league(
            market_id=None, vertical=None,
            analysis_json={key: "MLB"},
        )
        assert result is not None, f"failed for key {key!r}"
        assert result[0] == "MLB"


# --- Rule 3: Kalshi prefix in analysis_json ticker fields -------------------


def test_rule1_kalshi_ticker_in_analysis_json():
    result = infer_league(
        market_id=None, vertical=None,
        analysis_json={"kalshi_market_ticker": "KXEPLGAME-foo-bar"},
    )
    assert result == ("EPL", "analysis_json.kalshi_market_ticker:kalshi_prefix")


def test_rule1_alternate_ticker_keys():
    for key in ["kalshi_market_ticker", "kalshi_ticker", "market_ticker",
                "ticker", "market_id", "marketId"]:
        result = infer_league(
            market_id=None, vertical=None,
            analysis_json={key: "KXMLBGAME-foo"},
        )
        assert result is not None, f"failed for key {key!r}"
        assert result[0] == "MLB"


# --- Rule 4: Single-league vertical fallback --------------------------------


@pytest.mark.parametrize("vertical,expected", [
    ("commodities", "COMMODITIES"),
    ("tech_ai", "TECH_AI"),
    ("techai", "TECH_AI"),
    ("elections", "ELECTIONS"),
    ("events_econ", "EVENTS_ECON"),
    ("eventsecon", "EVENTS_ECON"),
    ("events", "EVENTS_ECON"),
    ("macro", "EVENTS_ECON"),
    ("entertainment", "ENTERTAINMENT"),
    ("ENTERTAINMENT", "ENTERTAINMENT"),  # case-insensitive
])
def test_rule2_vertical_fallback(vertical, expected):
    result = infer_league(
        market_id=None, vertical=vertical, analysis_json=None,
    )
    assert result is not None
    league, rule = result
    assert league == expected
    assert rule.startswith("vertical:")


@pytest.mark.parametrize("vertical", ["sports", "soccer", "crypto", "unknown", ""])
def test_rule2_does_not_fire_for_multi_league_or_unknown_verticals(vertical):
    # ``sports`` and ``soccer`` fan out to many leagues; we must NOT
    # mis-bucket them.  Crypto is intentionally unmapped (no per-league
    # calibrator).  An unknown / empty vertical naturally falls through.
    result = infer_league(
        market_id=None, vertical=vertical, analysis_json=None,
    )
    assert result is None


# --- Rule precedence --------------------------------------------------------


def test_rule1_analysis_ticker_wins_over_everything_else():
    # Rule 1 (analysis_json ticker prefix) is the highest-precedence
    # rule.  Even when the row also has a vertical that would map and
    # a market_id that would map to a DIFFERENT league, the explicit
    # ticker reference wins.
    result = infer_league(
        market_id="KXMLBGAME-foo",        # rule 4 would say MLB
        vertical="elections",              # rule 2 would say ELECTIONS
        analysis_json={"kalshi_ticker": "KXNBAGAME-bar",  # rule 1: NBA
                       "league": "PGA"},   # rule 3 would say PGA
    )
    assert result == ("NBA", "analysis_json.kalshi_ticker:kalshi_prefix")


def test_rule2_vertical_wins_over_analysis_field_and_market_id():
    # When rule 1 misses (no ticker key in analysis_json), a single-
    # league vertical beats both the analysis_json explicit league
    # field (rule 3) AND the top-level market_id Kalshi prefix (rule
    # 4).  This is intentional: vertical=commodities is deterministic;
    # the analysis_json league string might be a copy-paste artifact.
    result = infer_league(
        market_id="KXNBAGAME-baz",         # rule 4 would say NBA
        vertical="commodities",            # rule 2 fires: COMMODITIES
        analysis_json={"league": "PGA"},   # rule 3 would say PGA
    )
    assert result == ("COMMODITIES", "vertical:commodities")


def test_rule3_analysis_field_wins_over_market_id_fallback():
    # When rules 1+2 miss, the explicit analysis_json league/sport
    # field beats the last-resort market_id Kalshi prefix.
    result = infer_league(
        market_id="KXMLBGAME-foo",         # rule 4 would say MLB
        vertical="sports",                 # rule 2 misses (multi-league)
        analysis_json={"league": "NBA"},   # rule 3 fires
    )
    assert result == ("NBA", "analysis_json.league")


def test_rule3_accepts_sport_key_when_value_is_supported_league():
    # The Task #740 spec adds ``analysis_json.sport`` as an explicit
    # field source.  It only fires when the value normalises to a
    # SUPPORTED_LEAGUE — a generic sport name like ``"basketball"``
    # must NOT mis-bucket as NBA.
    hit = infer_league(
        market_id=None, vertical="sports",
        analysis_json={"sport": "MLB"},
    )
    assert hit == ("MLB", "analysis_json.sport")
    miss = infer_league(
        market_id=None, vertical="sports",
        analysis_json={"sport": "basketball"},
    )
    assert miss is None


def test_returns_none_when_no_rule_fires():
    # All inputs blank/non-matching -> None (caller leaves row alone).
    assert infer_league(
        market_id=None, vertical=None, analysis_json=None,
    ) is None
    assert infer_league(
        market_id="", vertical="", analysis_json={},
    ) is None
    assert infer_league(
        market_id="UNKNOWN-XYZ", vertical="sports", analysis_json={"foo": "bar"},
    ) is None


# --- Defensive input handling -----------------------------------------------


def test_handles_non_dict_analysis_json():
    # asyncpg returns jsonb as parsed Python; if a row stored it as
    # text the caller passes a string — must not crash.
    assert infer_league(
        market_id=None, vertical=None, analysis_json="not a dict",
    ) is None
    assert infer_league(
        market_id=None, vertical=None, analysis_json=["league", "NBA"],
    ) is None


def test_handles_non_string_market_id():
    # Defensive: if the dump somehow stores market_id as int.
    assert infer_league(
        market_id=12345, vertical=None, analysis_json=None,
    ) is None


def test_supported_leagues_matches_calibrator_set():
    # Pin the union: if app.calibrators.SUPPORTED_LEAGUES gains a new
    # league, this test fails so the inference rule table is updated
    # in lockstep.
    from app.calibrators import SUPPORTED_LEAGUES as CAL_SUPPORTED
    assert SUPPORTED_LEAGUES == frozenset(CAL_SUPPORTED), (
        "league_inference.SUPPORTED_LEAGUES diverged from "
        "app.calibrators.SUPPORTED_LEAGUES — update the inference "
        "rule table to cover the new league."
    )
