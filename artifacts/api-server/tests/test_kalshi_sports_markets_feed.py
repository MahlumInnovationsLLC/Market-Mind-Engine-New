"""Tests for the Kalshi sports market discovery feed.

Mirrors the structure of test_kalshi_crypto_markets_feed.py — same
helper-by-helper unit tests plus a small integration test of the
upsert path with a fake DB executor.  Keeps the two feeds' regressions
visible side-by-side on CI.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest

from app.feeds import kalshi_sports_markets as ksm


# --- Pricing -----------------------------------------------------------------


def test_mid_price_uses_bid_ask_midpoint_in_dollars():
    assert ksm._mid_price_dollars({"yes_bid": 40, "yes_ask": 44}) == pytest.approx(0.42)


def test_mid_price_falls_back_to_last_then_one_sided():
    assert ksm._mid_price_dollars({"last_price": 55}) == pytest.approx(0.55)
    assert ksm._mid_price_dollars({"yes_ask": 60}) == pytest.approx(0.60)
    assert ksm._mid_price_dollars({"yes_bid": 30}) == pytest.approx(0.30)
    assert ksm._mid_price_dollars({}) is None


def test_mid_price_drops_negative_side_and_falls_back():
    # A negative ask is a Kalshi data glitch — discard that side and
    # fall back to the one-sided bid rather than skewing the mid into
    # a nonsense value.  Matches the crypto producer's behavior so
    # both feeds degrade identically on bad quotes.
    assert ksm._mid_price_dollars({"yes_bid": 40, "yes_ask": -5}) == pytest.approx(0.40)
    # If BOTH sides are bad, we genuinely have no price signal and
    # must return None (so the upsert leaves kalshi_price NULL and
    # the cycle skips the row).
    assert ksm._mid_price_dollars({"yes_bid": -1, "yes_ask": -5}) is None


def test_mid_price_uses_dollars_fields_directly_without_dividing_by_100():
    # The live Kalshi GET /markets response now ships YES book under the
    # ``*_dollars`` suffixed field names, already on the 0..1 dollar
    # scale.  Don't divide by 100 — these are the exact final price.
    assert ksm._mid_price_dollars(
        {"yes_bid_dollars": 0.40, "yes_ask_dollars": 0.44}
    ) == pytest.approx(0.42)
    assert ksm._mid_price_dollars(
        {"last_price_dollars": 0.55}
    ) == pytest.approx(0.55)
    assert ksm._mid_price_dollars(
        {"yes_ask_dollars": 0.60}
    ) == pytest.approx(0.60)
    assert ksm._mid_price_dollars(
        {"yes_bid_dollars": 0.30}
    ) == pytest.approx(0.30)


def test_mid_price_prefers_dollars_field_when_both_keys_present():
    # If a fixture happens to carry both names (e.g. transitional
    # API), the new ``*_dollars`` field wins so the live path stays
    # correct even if some intermediary added a legacy alias.
    assert ksm._mid_price_dollars(
        {"yes_bid_dollars": 0.40, "yes_ask_dollars": 0.44,
         "yes_bid": 99, "yes_ask": 99}
    ) == pytest.approx(0.42)


# --- Series → league mapping -------------------------------------------------


def test_series_map_aligns_with_verticals_registry():
    """The names this feed writes into predictions.league must exactly
    match the verticals registry's soccer filter names; otherwise the
    soccer cycle would skip every row we produce."""
    m = ksm._DEFAULT_SERIES_TO_LEAGUE
    # Verticals registry uses these specific tokens (see
    # app/verticals/builtins.py: soccer leagues filter):
    expected_soccer = {"EPL", "LALIGA", "SERIEA", "BUNDES", "LIGUE1", "MLS", "UCL"}
    soccer_in_map = {
        m[k] for k in (
            "KXEPLGAME", "KXLALIGAGAME", "KXSERIEAGAME",
            "KXBUNDESGAME", "KXLIGUE1GAME", "KXMLSGAME", "KXUCLGAME",
        )
    }
    assert soccer_in_map == expected_soccer
    # Sports vertical itself is league-agnostic (leagues=()), so the
    # team-sports labels just need to be stable strings the orchestrator
    # can display.
    for league in ("NFL", "NBA", "MLB", "NHL", "NCAAF", "NCAAB"):
        assert league in m.values()


def test_series_map_env_override(monkeypatch):
    monkeypatch.setenv(
        "AXIOM_KALSHI_SPORTS_MARKETS_SERIES", "KXNFLGAME=NFL,KXNBAGAME=NBA",
    )
    m = ksm._series_to_league()
    assert m == {"KXNFLGAME": "NFL", "KXNBAGAME": "NBA"}


def test_series_map_env_garbage_falls_back_to_defaults(monkeypatch):
    monkeypatch.setenv("AXIOM_KALSHI_SPORTS_MARKETS_SERIES", "no equals here")
    m = ksm._series_to_league()
    assert m == ksm._DEFAULT_SERIES_TO_LEAGUE


# --- Cadence -----------------------------------------------------------------


def test_cadence_default(monkeypatch):
    monkeypatch.delenv("AXIOM_KALSHI_SPORTS_MARKETS_CADENCE_SECS", raising=False)
    assert ksm._cadence_seconds() == 60.0


def test_cadence_floors_at_30s(monkeypatch):
    # Operators can't melt the per-series endpoint with a 1-second poll.
    monkeypatch.setenv("AXIOM_KALSHI_SPORTS_MARKETS_CADENCE_SECS", "5")
    assert ksm._cadence_seconds() == 30.0


# --- Candidate construction --------------------------------------------------


def test_candidate_from_market_skips_missing_ticker():
    assert ksm._candidate_from_market(
        {"title": "Eagles vs Bills"}, league="NFL", series="KXNFLGAME",
    ) is None


def test_candidate_from_market_skips_unparseable_title():
    # Exotic / prop markets ("Will the MVP be...") shouldn't create
    # candidates because the orchestrator can't price them against a
    # standard moneyline.
    cand = ksm._candidate_from_market(
        {"ticker": "KXNBAMVP-26-LJ", "title": "Will LeBron win MVP?"},
        league="NBA", series="KXNBAMVP",
    )
    assert cand is None


def test_candidate_from_market_builds_full_payload():
    market = {
        "ticker": "KXNBAGAME-26APR15CLEMIA-CLE",
        "event_ticker": "KXNBAGAME-26APR15CLEMIA",
        "status": "open",
        "yes_bid": 55,
        "yes_ask": 59,
        "last_price": 57,
        "close_time": "2026-04-15T23:00:00Z",
        "title": "Cleveland @ Miami",
    }
    cand = ksm._candidate_from_market(
        market, league="NBA", series="KXNBAGAME",
    )
    assert cand is not None
    assert cand["league"] == "NBA"
    assert cand["kalshi_market_id"] == "KXNBAGAME-26APR15CLEMIA-CLE"
    # game_id keys off the parent event so multiple markets in the
    # same matchup (moneyline yes/no, spread, total) collapse to the
    # same swarm-debate group.
    assert cand["game_id"] == "kalshi:KXNBAGAME-26APR15CLEMIA"
    assert cand["kalshi_price"] == pytest.approx(0.57)
    assert cand["game_time"] == datetime(2026, 4, 15, 23, 0, tzinfo=timezone.utc)
    # Title parser puts away on the left, home on the right.
    assert cand["away_team"] == "Cleveland"
    assert cand["home_team"] == "Miami"
    assert cand["matchup"] == "Cleveland @ Miami"
    # Provenance is keyed under "metadata" so it serializes 1:1 into
    # the predictions.metadata jsonb column (the row dict and the
    # column name match by design — see the UPSERT SQL above).
    ctx = cand["metadata"]
    assert ctx["source"] == "kalshi_sports_markets"
    assert ctx["kalshi_event_ticker"] == "KXNBAGAME-26APR15CLEMIA"
    assert ctx["kalshi_series"] == "KXNBAGAME"
    assert ctx["title"] == "Cleveland @ Miami"


def test_candidate_falls_back_to_ticker_when_event_missing():
    """Some single-market events ship without an explicit
    ``event_ticker`` field; the candidate must still upsert (using
    the market ticker as the event surrogate) rather than getting
    dropped on the floor."""
    market = {
        "ticker": "KXMLSGAME-26MAY01-LAFGAL-LAF",
        "title": "LAFC vs LA Galaxy",
        "yes_bid": 48,
        "yes_ask": 52,
        "close_time": "2026-05-01T02:30:00Z",
    }
    cand = ksm._candidate_from_market(
        market, league="MLS", series="KXMLSGAME",
    )
    assert cand is not None
    # Event surrogate = market ticker when no explicit event field.
    assert cand["game_id"] == "kalshi:KXMLSGAME-26MAY01-LAFGAL-LAF"
    assert cand["kalshi_market_id"] == "KXMLSGAME-26MAY01-LAFGAL-LAF"


def test_candidate_allows_missing_price():
    """Freshly-listed markets often have no quotes yet — we still want
    a candidate row so the cycle can pick them up the moment Kalshi
    seeds a quote.  The orchestrator's own NULL-price guard handles
    the don't-trade-yet case."""
    market = {
        "ticker": "KXNFLGAME-26SEP08DALPHI-DAL",
        "title": "Dallas at Philadelphia",
        "close_time": "2026-09-08T20:25:00Z",
    }
    cand = ksm._candidate_from_market(
        market, league="NFL", series="KXNFLGAME",
    )
    assert cand is not None
    assert cand["kalshi_price"] is None


# --- Upsert path -------------------------------------------------------------


def test_upsert_candidates_runs_one_statement_per_row(monkeypatch):
    """Project convention: async helpers are exercised via
    ``asyncio.run()`` rather than the pytest-asyncio marker (matches
    test_kalshi_crypto_markets_feed.py)."""
    calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fake_execute(sql: str, *args: Any) -> None:
        calls.append((sql, args))

    monkeypatch.setattr(ksm.db, "execute", fake_execute)

    rows = [
        {
            "game_id": "kalshi:KXNBAGAME-1",
            "league": "NBA",
            "matchup": "A @ B",
            "home_team": "B",
            "away_team": "A",
            "kalshi_market_id": "KXNBAGAME-1-A",
            "kalshi_price": 0.42,
            "game_time": datetime(2026, 4, 15, 20, 0, tzinfo=timezone.utc),
            "metadata": {"source": "kalshi_sports_markets"},
        },
        {
            "game_id": "kalshi:KXMLBGAME-2",
            "league": "MLB",
            "matchup": "C @ D",
            "home_team": "D",
            "away_team": "C",
            "kalshi_market_id": "KXMLBGAME-2-C",
            "kalshi_price": None,
            "game_time": None,
            "metadata": {"source": "kalshi_sports_markets"},
        },
    ]
    n = asyncio.run(ksm._upsert_candidates(rows))
    assert n == 2
    assert len(calls) == 2
    # metadata is always JSON-serialized (ninth positional arg) into
    # the predictions.metadata jsonb column.
    for _sql, args in calls:
        assert isinstance(args[8], str)
        assert "kalshi_sports_markets" in args[8]


def test_upsert_candidates_swallows_per_row_failure(monkeypatch):
    """A poison row must not take the rest of the page down with it —
    the cycle is far better off with N-1 valid candidates than zero,
    matching the crypto producer's defensive posture."""
    state = {"calls": 0}

    async def fake_execute(sql: str, *args: Any) -> None:
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError("simulated DB blip")

    monkeypatch.setattr(ksm.db, "execute", fake_execute)

    rows = [
        {
            "game_id": "g1", "league": "NBA", "matchup": "A @ B",
            "home_team": "B", "away_team": "A",
            "kalshi_market_id": "KX-1", "kalshi_price": None,
            "game_time": None, "metadata": {},
        },
        {
            "game_id": "g2", "league": "NBA", "matchup": "C @ D",
            "home_team": "D", "away_team": "C",
            "kalshi_market_id": "KX-2", "kalshi_price": None,
            "game_time": None, "metadata": {},
        },
    ]
    n = asyncio.run(ksm._upsert_candidates(rows))
    # Only the second row counted — the first raised but was swallowed.
    assert n == 1
    assert state["calls"] == 2
