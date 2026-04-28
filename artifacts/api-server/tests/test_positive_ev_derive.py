"""Pure-math tests for the positive-EV derivation (outlier.bet substitute).

These tests run with NO network and NO database — they feed
The-Odds-API-shaped fixture payloads into ``derive_positive_ev`` and
assert on the math.  This is the safety net for the substitute we
shipped to replace outlier.bet's ``/api/v3/positive-ev/scan`` after
CloudFront started serving the SPA shell instead of the API.
"""
from __future__ import annotations

from app.feeds.positive_ev import (
    _devig,
    _outcome_key,
    derive_positive_ev,
)


def test_devig_rejects_degenerate_inputs():
    assert _devig([]) is None
    assert _devig([1.0, 2.0]) is None  # 1.0 odds = certain payout, garbage
    assert _devig([2.5, 2.5]) is None  # sum of raw implieds < 1 -> giveaway


def test_devig_proportional_h2h():
    # A symmetric -110/-110 market in decimal: 1.909 / 1.909.
    # Raw implieds = 0.5238 each, sum = 1.0476, vig ~ 4.76%.
    # De-vigged should be ~0.5/0.5.
    fair = _devig([1.909, 1.909])
    assert fair is not None
    assert abs(fair[0] - 0.5) < 0.01
    assert abs(fair[1] - 0.5) < 0.01
    assert abs(sum(fair) - 1.0) < 1e-9


def test_devig_three_way_market():
    # A soccer 3-way: 2.10 home / 3.40 draw / 3.80 away.
    fair = _devig([2.10, 3.40, 3.80])
    assert fair is not None
    assert abs(sum(fair) - 1.0) < 1e-9
    assert fair[0] > fair[1] and fair[0] > fair[2]


def test_outcome_key_includes_point_for_spreads_and_totals():
    assert _outcome_key("h2h", "Lakers", None) == "Lakers"
    assert _outcome_key("totals", "Over", 220.5) == "Over:220.5"
    assert _outcome_key("spreads", "Lakers", -3.5) == "Lakers:-3.5"


def _book(name: str, market: str, outcomes: list[dict]) -> dict:
    return {"key": name, "title": name, "markets": [{"key": market, "outcomes": outcomes}]}


def test_derive_finds_book_offering_better_than_consensus():
    """Three books quote a fair NBA h2h around 50/50.  A fourth book
    misprices the away side at 2.20 (45.45% implied) when consensus
    fair is ~50% — that's a +9% edge, which must surface as a
    positive-EV row tagged to that book."""
    games = [{
        "id": "game-A",
        "sport_key": "basketball_nba",
        "home_team": "Lakers",
        "away_team": "Celtics",
        "bookmakers": [
            _book("draftkings", "h2h", [
                {"name": "Lakers", "price": 1.91},
                {"name": "Celtics", "price": 1.91},
            ]),
            _book("fanduel", "h2h", [
                {"name": "Lakers", "price": 1.93},
                {"name": "Celtics", "price": 1.89},
            ]),
            _book("betmgm", "h2h", [
                {"name": "Lakers", "price": 1.92},
                {"name": "Celtics", "price": 1.90},
            ]),
            # Soft book — under-prices Celtics significantly.
            _book("mybookieag", "h2h", [
                {"name": "Lakers", "price": 1.65},
                {"name": "Celtics", "price": 2.20},
            ]),
        ],
    }]

    rows = derive_positive_ev(games, threshold_pct=1.0, min_consensus_books=3)
    # Expect at least one row for mybookieag/Celtics at strong positive edge.
    matches = [r for r in rows if r["sportsbook"] == "mybookieag" and r["side"] == "Celtics"]
    assert matches, f"expected mybookieag/Celtics edge row, got: {rows}"
    edge = matches[0]["edge_pct"]
    assert edge > 5.0, f"expected edge > 5%, got {edge}"
    # Sanity-check the metadata payload.
    payload = matches[0]["payload"]
    assert payload["n_books_in_consensus"] == 4
    assert payload["decimal_odds"] == 2.20
    # external_id must be unique enough to dedupe within a scan.
    assert "mybookieag" in matches[0]["external_id"]
    assert "Celtics" in matches[0]["external_id"]


def test_derive_skips_when_too_few_books_for_consensus():
    """With only 2 books, the median is unreliable and we MUST refuse
    to emit positive-EV signals — the floor is `min_consensus_books`."""
    games = [{
        "id": "game-B",
        "sport_key": "basketball_nba",
        "home_team": "Lakers",
        "away_team": "Celtics",
        "bookmakers": [
            _book("draftkings", "h2h", [
                {"name": "Lakers", "price": 1.91},
                {"name": "Celtics", "price": 1.91},
            ]),
            _book("mybookieag", "h2h", [
                {"name": "Lakers", "price": 1.65},
                {"name": "Celtics", "price": 2.20},
            ]),
        ],
    }]
    rows = derive_positive_ev(games, threshold_pct=1.0, min_consensus_books=3)
    assert rows == []


def test_derive_segments_totals_by_point():
    """Books offering different totals lines (220.5 vs 221.5) must NOT
    have their fair probabilities pooled together — they're different
    markets even though they share market_type='totals'."""
    games = [{
        "id": "game-C",
        "sport_key": "basketball_nba",
        "home_team": "Suns",
        "away_team": "Nuggets",
        "bookmakers": [
            _book("draftkings", "totals", [
                {"name": "Over", "price": 1.91, "point": 220.5},
                {"name": "Under", "price": 1.91, "point": 220.5},
            ]),
            _book("fanduel", "totals", [
                {"name": "Over", "price": 1.92, "point": 220.5},
                {"name": "Under", "price": 1.90, "point": 220.5},
            ]),
            _book("betmgm", "totals", [
                {"name": "Over", "price": 1.90, "point": 220.5},
                {"name": "Under", "price": 1.92, "point": 220.5},
            ]),
            # Different point — must not pollute the 220.5 consensus.
            _book("mybookieag", "totals", [
                {"name": "Over", "price": 1.65, "point": 221.5},
                {"name": "Under", "price": 2.20, "point": 221.5},
            ]),
        ],
    }]
    rows = derive_positive_ev(games, threshold_pct=1.0, min_consensus_books=3)
    # mybookieag at 221.5 is the only book at that point, so it must be
    # excluded by min_consensus_books — no rows should emit.
    soft_rows = [r for r in rows if r["sportsbook"] == "mybookieag"]
    assert soft_rows == [], f"mybookieag at 221.5 alone — must not emit: {soft_rows}"


def test_derive_skips_malformed_price_without_aborting_cycle():
    """If a single book pushes a non-numeric value through the ``price``
    field (a real production hazard observed when promo strings leak
    into the odds payload), we MUST skip that single market group and
    keep deriving everything else.  Pre-fix this raised ValueError out
    of the entire poll cycle.
    """
    games = [{
        "id": "game-bad-price",
        "sport_key": "basketball_nba",
        "home_team": "Lakers",
        "away_team": "Celtics",
        "bookmakers": [
            _book("draftkings", "h2h", [
                {"name": "Lakers", "price": 1.91},
                {"name": "Celtics", "price": 1.91},
            ]),
            _book("fanduel", "h2h", [
                {"name": "Lakers", "price": 1.93},
                {"name": "Celtics", "price": 1.89},
            ]),
            _book("betmgm", "h2h", [
                {"name": "Lakers", "price": 1.92},
                {"name": "Celtics", "price": 1.90},
            ]),
            # A real malformed payload — promo string in the price field.
            _book("badbook", "h2h", [
                {"name": "Lakers", "price": "BOOST!"},
                {"name": "Celtics", "price": 1.50},
            ]),
            # Soft book with a real edge — must still be detected.
            _book("mybookieag", "h2h", [
                {"name": "Lakers", "price": 1.65},
                {"name": "Celtics", "price": 2.20},
            ]),
        ],
    }]
    rows = derive_positive_ev(games, threshold_pct=1.0, min_consensus_books=3)
    # Bad book must be excluded from the consensus and from emitted rows.
    bad_rows = [r for r in rows if r["sportsbook"] == "badbook"]
    assert bad_rows == [], f"badbook must not emit signals: {bad_rows}"
    # Real soft-book edge must STILL surface despite the bad sibling.
    good_rows = [
        r for r in rows
        if r["sportsbook"] == "mybookieag" and r["side"] == "Celtics"
    ]
    assert good_rows, f"mybookieag/Celtics edge lost due to bad sibling: {rows}"


def test_poll_positive_ev_skips_when_cache_is_empty(monkeypatch):
    """Cold-start condition: ``the_odds_api`` hasn't populated the cache
    yet.  ``_poll_positive_ev`` must complete with 0 writes and not
    raise — otherwise the scheduler will mark the feed unhealthy on
    every cold start.
    """
    import asyncio

    from app.feeds import positive_ev as pev
    from app.feeds import odds_api

    # Force the cache to be empty for this test.
    monkeypatch.setattr(odds_api, "_LAST_GAMES_BY_SPORT", {}, raising=True)
    written = asyncio.run(pev._poll_positive_ev())
    assert written == 0


def test_poll_positive_ev_consumes_cache_without_http(monkeypatch):
    """When the cache is fresh, ``_poll_positive_ev`` must derive from
    it WITHOUT issuing any HTTP calls.  This is the architect-mandated
    contract that prevents quota contention with the_odds_api.
    """
    import asyncio
    import time as _time

    from app.feeds import positive_ev as pev
    from app.feeds import odds_api

    # One sport with a well-known +EV setup, plus one stale sport that
    # MUST be skipped silently (proving we honor max_age_s).
    games = [{
        "id": "cache-game",
        "sport_key": "basketball_nba",
        "home_team": "Lakers",
        "away_team": "Celtics",
        "bookmakers": [
            _book("draftkings", "h2h", [
                {"name": "Lakers", "price": 1.91},
                {"name": "Celtics", "price": 1.91},
            ]),
            _book("fanduel", "h2h", [
                {"name": "Lakers", "price": 1.92},
                {"name": "Celtics", "price": 1.90},
            ]),
            _book("betmgm", "h2h", [
                {"name": "Lakers", "price": 1.90},
                {"name": "Celtics", "price": 1.92},
            ]),
            _book("mybookieag", "h2h", [
                {"name": "Lakers", "price": 1.65},
                {"name": "Celtics", "price": 2.20},
            ]),
        ],
    }]
    fake_cache = {
        "basketball_nba": (_time.time(), games),
        "basketball_ncaab": (_time.time() - 10_000, games),  # stale
    }
    monkeypatch.setattr(odds_api, "_LAST_GAMES_BY_SPORT", fake_cache, raising=True)

    # Stub persist to count rows without touching DB.
    persisted: list[list[dict]] = []
    async def fake_persist(rows):
        persisted.append(rows)
        return len(rows)
    monkeypatch.setattr(pev, "_persist_signals", fake_persist, raising=True)

    written = asyncio.run(pev._poll_positive_ev())
    assert written > 0, "expected mybookieag/Celtics edge to surface"
    # All persisted rows came from the FRESH sport only — stale must be skipped.
    flat = [r for batch in persisted for r in batch]
    assert all(r["sport"] == "basketball_nba" for r in flat), \
        f"stale-cache leak: {[r['sport'] for r in flat]}"


def test_derive_emits_external_id_unique_per_book_and_outcome():
    """Different books on the same outcome must produce different
    external_ids so the ON CONFLICT (provider, external_id, scanned_at)
    DO NOTHING dedupe in `_persist_signals` doesn't collapse legitimate
    distinct signals into a single row."""
    games = [{
        "id": "game-D",
        "sport_key": "basketball_nba",
        "home_team": "Lakers",
        "away_team": "Celtics",
        "bookmakers": [
            _book("draftkings", "h2h", [
                {"name": "Lakers", "price": 1.91},
                {"name": "Celtics", "price": 1.91},
            ]),
            _book("fanduel", "h2h", [
                {"name": "Lakers", "price": 1.91},
                {"name": "Celtics", "price": 1.91},
            ]),
            _book("betmgm", "h2h", [
                {"name": "Lakers", "price": 2.50},  # huge mispricing
                {"name": "Celtics", "price": 1.55},
            ]),
            _book("mybookieag", "h2h", [
                {"name": "Lakers", "price": 2.40},  # huge mispricing too
                {"name": "Celtics", "price": 1.60},
            ]),
        ],
    }]
    rows = derive_positive_ev(games, threshold_pct=1.0, min_consensus_books=3)
    laker_rows = [r for r in rows if r["side"] == "Lakers"]
    ext_ids = {r["external_id"] for r in laker_rows}
    # Each emitted row must have its own external_id.
    assert len(ext_ids) == len(laker_rows)
