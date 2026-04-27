"""Unit tests for the per-entrant outright market resolver.

Covers ``_kalshi_public_market_url``, ``_normalize_entrant_token``,
``resolve_entrant_outright_markets``, and
``get_entrant_outright_markets_for_debate`` in ``app.dashboard``.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app import dashboard


class _FakeDB:
    """Stub ``app.dashboard.db`` with deterministic title/tick rows.

    Routes by the table referenced in the SQL so a single fake covers
    both the title-cache lookup in ``resolve_entrant_outright_markets``
    and the per-debate ``swarm_debates`` row read in
    ``get_entrant_outright_markets_for_debate``.
    """

    def __init__(
        self,
        title_rows: list[dict[str, Any]] | None = None,
        tick_rows: list[dict[str, Any]] | None = None,
        debate_row: dict[str, Any] | None = None,
        raise_on_titles: bool = False,
        raise_on_ticks: bool = False,
    ) -> None:
        self.title_rows = title_rows or []
        self.tick_rows = tick_rows or []
        self.debate_row = debate_row
        self.raise_on_titles = raise_on_titles
        self.raise_on_ticks = raise_on_ticks
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((query, args))
        if "FROM market_title_cache" in query:
            if self.raise_on_titles:
                raise RuntimeError("boom-titles")
            return self.title_rows
        if "FROM market_price_ticks" in query:
            if self.raise_on_ticks:
                raise RuntimeError("boom-ticks")
            return self.tick_rows
        return []

    async def fetchrow(self, query: str, *args: Any) -> Any:
        self.calls.append((query, args))
        if "FROM swarm_debates" in query:
            return self.debate_row
        return None

    async def fetchval(self, query: str, *args: Any) -> Any:  # pragma: no cover
        return None


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# _kalshi_public_market_url
# ---------------------------------------------------------------------------

def test_kalshi_public_market_url_two_segments() -> None:
    assert (
        dashboard._kalshi_public_market_url("KXATPRG-25SEP-ALCARAZ")
        == "https://kalshi.com/markets/kxatprg/25sep"
    )


def test_kalshi_public_market_url_single_segment_uses_series_only() -> None:
    assert (
        dashboard._kalshi_public_market_url("KXATPRG")
        == "https://kalshi.com/markets/kxatprg"
    )


def test_kalshi_public_market_url_handles_empty_inputs() -> None:
    assert dashboard._kalshi_public_market_url(None) is None
    assert dashboard._kalshi_public_market_url("") is None
    # Leading dash → empty series → unusable URL.
    assert dashboard._kalshi_public_market_url("-FOO") is None


# ---------------------------------------------------------------------------
# _normalize_entrant_token
# ---------------------------------------------------------------------------

def test_normalize_entrant_token_strips_punctuation_and_case() -> None:
    assert dashboard._normalize_entrant_token("Carlos Alcaraz") == "carlosalcaraz"
    assert dashboard._normalize_entrant_token("Jannik-Sinner!") == "janniksinner"
    assert dashboard._normalize_entrant_token("") == ""
    assert dashboard._normalize_entrant_token(None) == ""


# ---------------------------------------------------------------------------
# resolve_entrant_outright_markets
# ---------------------------------------------------------------------------

def test_resolve_short_circuits_without_traded_market_or_entrants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeDB()
    monkeypatch.setattr(dashboard, "db", fake)
    assert _run(dashboard.resolve_entrant_outright_markets(None, ["a", "b"])) == {}
    assert _run(dashboard.resolve_entrant_outright_markets("KX-FOO", [])) == {}
    # Neither short-circuit should hit the database.
    assert fake.calls == []


def test_resolve_skips_when_traded_market_lacks_event_segment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeDB()
    monkeypatch.setattr(dashboard, "db", fake)
    # Single-segment ticker: there's no event prefix to derive siblings from.
    out = _run(dashboard.resolve_entrant_outright_markets("KXFOO", ["alice"]))
    assert out == {}
    assert fake.calls == []


def test_resolve_matches_by_title_substring_and_attaches_mid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeDB(
        title_rows=[
            {
                "ticker": "KXATPRG-25SEP-ALCARAZ",
                "title": "Will Carlos Alcaraz win Roland-Garros 2025?",
            },
            {
                "ticker": "KXATPRG-25SEP-SINNER",
                "title": "Will Jannik Sinner win Roland-Garros 2025?",
            },
            {
                "ticker": "KXATPRG-25SEP-DJOKOVIC",
                "title": "Will Novak Djokovic win Roland-Garros 2025?",
            },
        ],
        tick_rows=[
            # Alcaraz: mean_cents present → preferred over close_cents.
            {
                "market_ticker": "KXATPRG-25SEP-ALCARAZ",
                "mean_cents": 47,
                "close_cents": 45,
            },
            # Sinner: mean_cents NULL → falls back to close_cents.
            {
                "market_ticker": "KXATPRG-25SEP-SINNER",
                "mean_cents": None,
                "close_cents": 33,
            },
            # Djokovic: no tick at all (omitted from rows) → mid_cents=None.
        ],
    )
    monkeypatch.setattr(dashboard, "db", fake)

    out = _run(
        dashboard.resolve_entrant_outright_markets(
            "KXATPRG-25SEP-ALCARAZ",
            ["Carlos Alcaraz", "Jannik Sinner", "Novak Djokovic"],
            traded_entrant="Carlos Alcaraz",
        )
    )

    assert set(out.keys()) == {"Carlos Alcaraz", "Jannik Sinner", "Novak Djokovic"}
    assert out["Carlos Alcaraz"]["kalshi_market_id"] == "KXATPRG-25SEP-ALCARAZ"
    assert out["Carlos Alcaraz"]["mid_cents"] == 47
    assert (
        out["Carlos Alcaraz"]["kalshi_url"]
        == "https://kalshi.com/markets/kxatprg/25sep"
    )
    assert out["Jannik Sinner"]["kalshi_market_id"] == "KXATPRG-25SEP-SINNER"
    assert out["Jannik Sinner"]["mid_cents"] == 33  # fell back to close_cents
    assert out["Novak Djokovic"]["kalshi_market_id"] == "KXATPRG-25SEP-DJOKOVIC"
    assert out["Novak Djokovic"]["mid_cents"] is None  # no tick row → null

    # The title query is scoped by the event prefix derived from the
    # traded ticker (everything before the final ``-`` segment).
    title_call = next(c for c in fake.calls if "FROM market_title_cache" in c[0])
    assert title_call[1] == ("KXATPRG-25SEP-%",)


def test_resolve_falls_back_to_ticker_suffix_when_titles_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Title cache only has tickers — no human-readable titles yet.
    fake = _FakeDB(
        title_rows=[
            {"ticker": "KXATPRG-25SEP-ALCARAZ", "title": ""},
            {"ticker": "KXATPRG-25SEP-SINNER", "title": ""},
        ],
        tick_rows=[],
    )
    monkeypatch.setattr(dashboard, "db", fake)
    out = _run(
        dashboard.resolve_entrant_outright_markets(
            "KXATPRG-25SEP-ALCARAZ",
            ["Alcaraz", "Sinner"],
        )
    )
    assert out["Alcaraz"]["kalshi_market_id"] == "KXATPRG-25SEP-ALCARAZ"
    assert out["Sinner"]["kalshi_market_id"] == "KXATPRG-25SEP-SINNER"


def test_resolve_force_resolves_traded_entrant_even_without_cache_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Title cache is cold for the traded ticker → the only way the
    # traded entrant gets surfaced is the explicit force-map.
    fake = _FakeDB(title_rows=[], tick_rows=[])
    monkeypatch.setattr(dashboard, "db", fake)
    out = _run(
        dashboard.resolve_entrant_outright_markets(
            "KXATPRG-25SEP-ALCARAZ",
            ["Carlos Alcaraz", "Jannik Sinner"],
            traded_entrant="Carlos Alcaraz",
        )
    )
    assert "Carlos Alcaraz" in out
    assert out["Carlos Alcaraz"]["kalshi_market_id"] == "KXATPRG-25SEP-ALCARAZ"
    # Sinner was never matchable with no cache → omitted.
    assert "Jannik Sinner" not in out


def test_resolve_omits_entrants_without_a_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeDB(
        title_rows=[
            {
                "ticker": "KXATPRG-25SEP-ALCARAZ",
                "title": "Will Carlos Alcaraz win Roland-Garros 2025?",
            },
        ],
        tick_rows=[],
    )
    monkeypatch.setattr(dashboard, "db", fake)
    out = _run(
        dashboard.resolve_entrant_outright_markets(
            "KXATPRG-25SEP-ALCARAZ",
            ["Carlos Alcaraz", "Some Unknown Player"],
        )
    )
    # Unknown entrant has no title or ticker-suffix match → omitted
    # so the frontend can render a non-clickable row.
    assert set(out.keys()) == {"Carlos Alcaraz"}


def test_resolve_swallows_title_lookup_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Title cache errors must not blow up the endpoint — siblings end
    # up empty, and only the force-resolved traded entrant survives.
    fake = _FakeDB(raise_on_titles=True)
    monkeypatch.setattr(dashboard, "db", fake)
    out = _run(
        dashboard.resolve_entrant_outright_markets(
            "KXATPRG-25SEP-ALCARAZ",
            ["Carlos Alcaraz", "Jannik Sinner"],
            traded_entrant="Carlos Alcaraz",
        )
    )
    assert set(out.keys()) == {"Carlos Alcaraz"}
    assert out["Carlos Alcaraz"]["kalshi_market_id"] == "KXATPRG-25SEP-ALCARAZ"


def test_resolve_swallows_tick_lookup_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeDB(
        title_rows=[
            {
                "ticker": "KXATPRG-25SEP-ALCARAZ",
                "title": "Will Carlos Alcaraz win Roland-Garros 2025?",
            },
        ],
        raise_on_ticks=True,
    )
    monkeypatch.setattr(dashboard, "db", fake)
    out = _run(
        dashboard.resolve_entrant_outright_markets(
            "KXATPRG-25SEP-ALCARAZ",
            ["Carlos Alcaraz"],
        )
    )
    # Match still resolves; mid_cents is just None because the tick
    # query failed.
    assert out["Carlos Alcaraz"]["kalshi_market_id"] == "KXATPRG-25SEP-ALCARAZ"
    assert out["Carlos Alcaraz"]["mid_cents"] is None


# ---------------------------------------------------------------------------
# get_entrant_outright_markets_for_debate
# ---------------------------------------------------------------------------

def test_get_for_debate_returns_none_for_missing_debate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeDB(debate_row=None)
    monkeypatch.setattr(dashboard, "db", fake)
    assert _run(dashboard.get_entrant_outright_markets_for_debate(999)) is None


def test_get_for_debate_returns_empty_entrants_when_no_sketch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Binary moneyline-style debate: game_context exists but has no
    # plackett_luce_sketch → endpoint returns an empty entrants map
    # rather than 404 so the frontend can show "no field".
    fake = _FakeDB(
        debate_row={
            "id": 42,
            "game_context": {"kalshi_market_id": "KXNBA-001-LAL"},
        },
    )
    monkeypatch.setattr(dashboard, "db", fake)
    out = _run(dashboard.get_entrant_outright_markets_for_debate(42))
    assert out == {
        "debate_id": 42,
        "traded_market_id": "KXNBA-001-LAL",
        "traded_entrant": None,
        "entrants": {},
    }


def test_get_for_debate_resolves_full_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sketch = {
        "model": "plackett_luce",
        "entrant": "Carlos Alcaraz",
        "field_size": 2,
        "ratings": {"Carlos Alcaraz": 2200, "Jannik Sinner": 2150},
        "probabilities": {"Carlos Alcaraz": 0.6, "Jannik Sinner": 0.4},
    }
    fake = _FakeDB(
        debate_row={
            "id": 7,
            # game_context is sometimes JSON-serialized in the row, so
            # the resolver must accept either shape.
            "game_context": json.dumps(
                {
                    "kalshi_market_id": "KXATPRG-25SEP-ALCARAZ",
                    "plackett_luce_sketch": sketch,
                }
            ),
        },
        title_rows=[
            {
                "ticker": "KXATPRG-25SEP-ALCARAZ",
                "title": "Will Carlos Alcaraz win Roland-Garros 2025?",
            },
            {
                "ticker": "KXATPRG-25SEP-SINNER",
                "title": "Will Jannik Sinner win Roland-Garros 2025?",
            },
        ],
        tick_rows=[
            {
                "market_ticker": "KXATPRG-25SEP-ALCARAZ",
                "mean_cents": 60,
                "close_cents": None,
            },
            {
                "market_ticker": "KXATPRG-25SEP-SINNER",
                "mean_cents": 40,
                "close_cents": None,
            },
        ],
    )
    monkeypatch.setattr(dashboard, "db", fake)

    out = _run(dashboard.get_entrant_outright_markets_for_debate(7))
    assert out is not None
    assert out["debate_id"] == 7
    assert out["traded_market_id"] == "KXATPRG-25SEP-ALCARAZ"
    assert out["traded_entrant"] == "Carlos Alcaraz"
    assert out["entrants"]["Carlos Alcaraz"]["mid_cents"] == 60
    assert out["entrants"]["Jannik Sinner"]["mid_cents"] == 40
    assert (
        out["entrants"]["Carlos Alcaraz"]["kalshi_url"]
        == "https://kalshi.com/markets/kxatprg/25sep"
    )


def test_get_for_debate_swallows_db_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _RaisingDB(_FakeDB):
        async def fetchrow(self, query: str, *args: Any) -> Any:
            raise RuntimeError("boom-debate")

    fake = _RaisingDB()
    monkeypatch.setattr(dashboard, "db", fake)
    assert _run(dashboard.get_entrant_outright_markets_for_debate(1)) is None
