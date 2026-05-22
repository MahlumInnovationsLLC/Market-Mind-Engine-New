"""Unit tests for the shared sport-key normalizer (task #874).

The dashboard Live-Now tile links to ``/live-games?league=<sport>``
using canonical lowercase sport keys (e.g. ``"nba"``, ``"soccer"``),
and the live-games endpoint tags each item with the same canonical
key so the frontend filter always lands on a populated list.  Both
sides resolve the canonical key through
:func:`app.learning.in_game_loop.sport_for_league`.
"""
from __future__ import annotations

from app.learning.in_game_loop import sport_for_league


def test_canonical_sport_keys_round_trip() -> None:
    assert sport_for_league("nba") == "nba"
    assert sport_for_league("soccer") == "soccer"


def test_case_insensitive_match_against_configured_leagues() -> None:
    # League codes as written by the orchestrator / sportradar feed.
    assert sport_for_league("NBA") == "nba"
    assert sport_for_league("nba") == "nba"
    assert sport_for_league("Nba") == "nba"
    # Soccer fans out across several league codes.
    assert sport_for_league("EPL") == "soccer"
    assert sport_for_league("epl") == "soccer"
    assert sport_for_league("MLS") == "soccer"
    assert sport_for_league("UCL") == "soccer"


def test_the_odds_api_style_prefix_is_resolved() -> None:
    # The-odds-api / odds-feeds sometimes emit ``<sport>_<league>``.
    assert sport_for_league("basketball_nba") == "nba"
    assert sport_for_league("soccer_epl") == "soccer"
    assert sport_for_league("americanfootball_nfl") == "nfl"


def test_unknown_and_empty_inputs_return_none() -> None:
    assert sport_for_league("") is None
    assert sport_for_league(None) is None
    assert sport_for_league("   ") is None
    assert sport_for_league("KENTUCKY_DERBY") is None
