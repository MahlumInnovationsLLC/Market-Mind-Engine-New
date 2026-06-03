"""Task #616: derive the n-way (per-competitor) leg for tennis / F1 /
PGA outright trades from the candidate metadata, the candidate's
``entrant`` field, or the Kalshi market title — so the settlement
pipeline can apply the per-leg outcome from the actual race /
tournament winner instead of mis-scoring losing legs as model misses
against a binary moneyline frame.

Covers ``app.orchestrator.n_way.derive_n_way_competitor`` plus the
``app.orchestrator.paper.place_order`` integration that stamps the
result into ``trades.metadata`` jsonb at insert-time and gates on the
league's ``n_way_market`` flag, the
``app.settlement.pipeline.resolve_n_way_competitor`` settlement
read-back, the per-trade outcome derivation in ``settle_market``, and
the backfill script that walks pre-existing untagged trades.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.orchestrator import paper as _paper
from app.orchestrator.n_way import derive_n_way_competitor
from app.risk.gates import RiskContext


# ---------------------------------------------------------------------------
# Helper-level: derive_n_way_competitor
# ---------------------------------------------------------------------------


def test_metadata_pre_stamp_short_circuits_title_parsing():
    comp = derive_n_way_competitor(
        market_title=None,
        candidate={"metadata": {"competitor_id": "sr:competitor:14"}},
    )
    assert comp == {"competitor_id": "sr:competitor:14"}


def test_metadata_pre_stamp_carries_id_and_name():
    comp = derive_n_way_competitor(
        market_title=None,
        candidate={"metadata": {
            "competitor_id": "sr:competitor:42",
            "competitor_name": "Carlos Alcaraz",
        }},
    )
    assert comp == {
        "competitor_id": "sr:competitor:42",
        "competitor_name": "Carlos Alcaraz",
    }


def test_metadata_entrant_alias_is_accepted():
    """Some feed integrations stamp ``entrant`` instead of
    ``competitor_id`` — the helper must accept either spelling."""
    comp = derive_n_way_competitor(
        market_title=None,
        candidate={"metadata": {"entrant_id": "VER", "entrant_name": "Verstappen"}},
    )
    assert comp == {"competitor_id": "VER", "competitor_name": "Verstappen"}


def test_metadata_str_jsonb_is_decoded():
    """asyncpg occasionally hands jsonb back as a str."""
    comp = derive_n_way_competitor(
        market_title=None,
        candidate={"metadata": json.dumps({"competitor_id": "sr:competitor:1"})},
    )
    assert comp == {"competitor_id": "sr:competitor:1"}


def test_candidate_entrant_falls_back_to_field_lookup():
    """When the candidate names an entrant, look up the canonical id
    from the n_way_field so the stamp carries the durable identifier."""
    comp = derive_n_way_competitor(
        market_title=None,
        candidate={
            "entrant": "Carlos Alcaraz",
            "n_way_field": [
                {"id": "sr:competitor:1", "name": "Carlos Alcaraz"},
                {"id": "sr:competitor:2", "name": "Jannik Sinner"},
            ],
        },
    )
    assert comp == {
        "competitor_id": "sr:competitor:1",
        "competitor_name": "Carlos Alcaraz",
    }


def test_candidate_home_name_is_treated_as_entrant_for_outright_markets():
    """The n-way feed normalizer ships outright candidates with
    ``home.name = <entrant>`` and ``away.name = "FIELD"``."""
    comp = derive_n_way_competitor(
        market_title=None,
        candidate={
            "home": {"name": "Max Verstappen"},
            "away": {"name": "FIELD"},
            "n_way_field": [
                {"id": "VER", "name": "Max Verstappen"},
                {"id": "NOR", "name": "Lando Norris"},
            ],
        },
    )
    assert comp == {
        "competitor_id": "VER",
        "competitor_name": "Max Verstappen",
    }


def test_field_placeholder_home_name_returns_none():
    """A trade row whose only signal is ``home.name = "FIELD"`` is the
    placeholder pseudo-leg — must not be mistaken for a real entrant."""
    comp = derive_n_way_competitor(
        market_title=None,
        candidate={"home": {"name": "FIELD"}},
    )
    assert comp is None


def test_yes_side_alone_does_not_resolve_a_competitor():
    """Production trades ship with ``side`` = "yes"/"no"; the helper
    must not pretend that's a competitor identity (would mis-tag every
    n-way trade with the literal string "yes")."""
    comp = derive_n_way_competitor(
        market_title=None, candidate={"side": "yes"},
    )
    assert comp is None


def test_title_match_against_field_picks_longest_competitor_name():
    """The Plackett-Luce field can include both "Alcaraz" (last name)
    and "Carlos Alcaraz" (full name).  The longest match wins so a
    title that mentions the full name doesn't get tagged with the
    last-name alias."""
    comp = derive_n_way_competitor(
        market_title="Will Carlos Alcaraz win the 2026 Australian Open?",
        candidate={
            "n_way_field": [
                {"id": "sr:competitor:1", "name": "Carlos Alcaraz"},
                {"id": "sr:competitor:9", "name": "Alcaraz"},
                {"id": "sr:competitor:2", "name": "Jannik Sinner"},
            ],
        },
    )
    assert comp == {
        "competitor_id": "sr:competitor:1",
        "competitor_name": "Carlos Alcaraz",
    }


def test_title_match_falls_back_to_competitor_id_when_no_name_match():
    """Some feeds surface ticker-style ids in the title (F1: "VER",
    "HAM").  The helper accepts an id-in-title match as a last resort."""
    comp = derive_n_way_competitor(
        market_title="Will VER win the Monaco GP?",
        candidate={
            "n_way_field": [
                {"id": "VER", "name": "Max Verstappen"},
                {"id": "NOR", "name": "Lando Norris"},
            ],
        },
    )
    assert comp == {"competitor_id": "VER", "competitor_name": "Max Verstappen"}


def test_unrelated_title_returns_none():
    comp = derive_n_way_competitor(
        market_title="Will Bitcoin close above $50,000?",
        candidate={
            "n_way_field": [
                {"id": "VER", "name": "Max Verstappen"},
            ],
        },
    )
    assert comp is None


def test_no_signals_returns_none():
    assert derive_n_way_competitor(market_title=None, candidate=None) is None
    assert derive_n_way_competitor(market_title="", candidate={}) is None


def test_n_way_field_dict_shape_is_supported():
    """The process-local ratings cache stores ``{name: rating}`` —
    the helper must accept that shape too so a candidate that pulled
    its field from the cache still matches the title."""
    comp = derive_n_way_competitor(
        market_title="Will Max Verstappen win the Monaco GP?",
        candidate={
            "n_way_field": {"Max Verstappen": 2200, "Lando Norris": 2050},
        },
    )
    assert comp == {"competitor_name": "Max Verstappen"}


# ---------------------------------------------------------------------------
# Integration: paper.place_order stamps competitor metadata into trades insert
# ---------------------------------------------------------------------------


def _ctx(*, league: str | None, market_id: str = "KXATP-AO26-ALCARAZ") -> RiskContext:
    return RiskContext(
        market_id=market_id,
        side="YES",
        vertical="sports",
        league=league,
        market_type=None,
        matchup="ATP Outright",
        current_price=0.18,
        model_probability=0.22,
        edge=0.04,
        confidence_band_low=None,
        confidence_band_high=None,
        agreement_rate=None,
        swarm_approved=True,
        swarm_recommended_action="BUY_YES",
        debate_id=None,
        intended_quantity=10,
        intended_notional_usd=1.8,
        bankroll_usd=1000.0,
        open_positions_count=0,
        open_positions_notional_usd=0.0,
        open_positions_per_vertical_usd={},
        operator_overrides={},
        market_cooldown_until=None,
    )


class _InsertCapture:
    """Stand-in for ``app.db`` that captures the trades INSERT args."""

    def __init__(self) -> None:
        self.captured: dict[str, Any] = {}
        self.fetchval_calls = 0

    async def fetchval(self, query: str, *args: Any) -> Any:
        self.fetchval_calls += 1
        if "FROM app_users" in query or "is_admin" in query:
            return "user_op_test"
        if "INSERT INTO trades" in query:
            self.captured["query"] = query
            self.captured["args"] = args
            return 99001
        return None

    async def fetch(self, *_a: Any, **_kw: Any) -> list[Any]:
        return []

    async def execute(self, *_a: Any, **_kw: Any) -> str:
        return "OK"


def _patch_paper_for_place_order(
    monkeypatch: pytest.MonkeyPatch,
    *,
    capture: _InsertCapture,
    market_title: str | None,
) -> None:
    monkeypatch.setattr(_paper.db, "fetchval", capture.fetchval)
    monkeypatch.setattr(_paper.db, "fetch", capture.fetch)
    monkeypatch.setattr(_paper.db, "execute", capture.execute)
    monkeypatch.setattr(
        _paper, "get_settings",
        lambda: SimpleNamespace(
            operator_user_id="user_op_test", trading_mode="paper",
            marketable_fok_enabled=False, marketable_buffer_cents=3,
        ),
    )

    async def _titles(_ids):
        return {ctx_id: market_title for ctx_id in _ids}

    monkeypatch.setattr(_paper, "resolve_market_titles", _titles)

    async def _fake_submit(req, *, force_paper=None):
        return SimpleNamespace(
            exchange_order_id="exo-comp",
            state="filled",
            filled_count=req.count,
            avg_fill_price_cents=18,
            order_id=req.client_order_id,
        )

    from app.kalshi import lifecycle as kalshi_lc
    monkeypatch.setattr(kalshi_lc, "submit_order", _fake_submit)


def _metadata_arg_from_capture(capture: _InsertCapture) -> dict[str, Any]:
    # Migration 0039 added the ``placed_by`` column and
    # `app/orchestrator/paper.py` now binds it as the FINAL parameter
    # after ``metadata``, so the metadata jsonb is at index ``-2``
    # (was ``-1`` before the trade-provenance work).  See replit.md →
    # "Trade provenance tagging" for the full insert column ordering.
    raw = capture.captured["args"][-2]
    if raw is None:
        return {}
    return json.loads(raw) if isinstance(raw, str) else dict(raw)


def test_place_order_stamps_competitor_for_atp_outright(monkeypatch):
    _paper._reset_operator_user_id_cache()
    capture = _InsertCapture()
    _patch_paper_for_place_order(
        monkeypatch, capture=capture,
        market_title="Will Carlos Alcaraz win the 2026 Australian Open?",
    )

    cand = {
        "home": {"name": "Carlos Alcaraz"},
        "away": {"name": "FIELD"},
        "n_way_field": [
            {"id": "sr:competitor:1", "name": "Carlos Alcaraz"},
            {"id": "sr:competitor:2", "name": "Jannik Sinner"},
        ],
        "market_id": "KXATP-AO26-ALCARAZ",
    }
    result = asyncio.run(_paper.place_order(
        ctx=_ctx(league="ATP"),
        cand=cand,
        consensus=0.22,
        edge=0.04,
        kelly=0.02,
        debate_id=None,
        exchange="kalshi",
    ))

    assert capture.captured.get("query"), "trades INSERT was not executed"
    md = _metadata_arg_from_capture(capture)
    assert md == {
        "competitor_id": "sr:competitor:1",
        "competitor_name": "Carlos Alcaraz",
    }
    assert result["metadata"] == {
        "competitor_id": "sr:competitor:1",
        "competitor_name": "Carlos Alcaraz",
    }


def test_place_order_stamps_competitor_for_f1_outright_with_field_dict(monkeypatch):
    _paper._reset_operator_user_id_cache()
    capture = _InsertCapture()
    _patch_paper_for_place_order(
        monkeypatch, capture=capture,
        market_title="Will Max Verstappen win the Monaco GP?",
    )

    cand = {
        "home": {"name": "Max Verstappen"},
        "away": {"name": "FIELD"},
        "n_way_field": {"Max Verstappen": 2200, "Lando Norris": 2050},
    }
    asyncio.run(_paper.place_order(
        ctx=_ctx(league="F1", market_id="KXF1-MON26-VER"),
        cand=cand,
        consensus=0.45, edge=0.06, kelly=0.03,
        debate_id=None, exchange="kalshi",
    ))
    md = _metadata_arg_from_capture(capture)
    assert md.get("competitor_name") == "Max Verstappen"


def test_place_order_skips_competitor_metadata_for_non_n_way_league(monkeypatch):
    """NBA / NFL / soccer leagues must NOT get a competitor tag even
    when the title happens to mention an entrant-shaped name."""
    _paper._reset_operator_user_id_cache()
    capture = _InsertCapture()
    _patch_paper_for_place_order(
        monkeypatch, capture=capture,
        market_title="Will the Lakers beat the Celtics?",
    )

    asyncio.run(_paper.place_order(
        ctx=_ctx(league="NBA", market_id="KXNBA-LAKCEL"),
        cand={"home": {"name": "Lakers"}, "away": {"name": "Celtics"}},
        consensus=0.5, edge=0.08, kelly=0.05,
        debate_id=None, exchange="kalshi",
    ))
    md = _metadata_arg_from_capture(capture)
    assert md == {}


def test_place_order_omits_metadata_when_competitor_undeducible(monkeypatch):
    """N-way league but no title and no field info — the insert binds
    an empty jsonb so the COALESCE falls back to '{}'."""
    _paper._reset_operator_user_id_cache()
    capture = _InsertCapture()
    _patch_paper_for_place_order(
        monkeypatch, capture=capture, market_title=None,
    )

    asyncio.run(_paper.place_order(
        ctx=_ctx(league="ATP"),
        cand={"side": "yes"},
        consensus=0.5, edge=0.08, kelly=0.05,
        debate_id=None, exchange="kalshi",
    ))
    md = _metadata_arg_from_capture(capture)
    assert md == {}


def test_place_order_stamps_both_three_way_and_n_way_independently(monkeypatch):
    """A league flagged ``three_way_market`` and ``n_way_market`` would
    get both stamps — defensively asserting they don't collide on the
    metadata jsonb (different keys, distinct namespaces)."""
    # Currently no league ships with both flags True, but the
    # orchestrator iterates the two derivations independently so we
    # assert they coexist if the config ever changes.
    from app.sports.leagues import LEAGUE_CONFIGS, LeagueConfig
    base = LEAGUE_CONFIGS["ATP"]
    monkeypatch.setitem(
        LEAGUE_CONFIGS, "ATP",
        LeagueConfig(
            league=base.league, priority=base.priority,
            allocation_pct=base.allocation_pct,
            three_way_market=True,  # synthetic — won't normally happen
            n_way_market=True,
            persona_context=base.persona_context,
        ),
    )

    _paper._reset_operator_user_id_cache()
    capture = _InsertCapture()
    _patch_paper_for_place_order(
        monkeypatch, capture=capture,
        market_title="Will Carlos Alcaraz win?",
    )
    asyncio.run(_paper.place_order(
        ctx=_ctx(league="ATP"),
        cand={
            "metadata": {"three_way_leg": "home"},
            "home": {"name": "Carlos Alcaraz"},
            "n_way_field": [{"id": "sr:competitor:1", "name": "Carlos Alcaraz"}],
        },
        consensus=0.5, edge=0.08, kelly=0.05,
        debate_id=None, exchange="kalshi",
    ))
    md = _metadata_arg_from_capture(capture)
    assert md.get("three_way_leg") == "home"
    assert md.get("competitor_id") == "sr:competitor:1"
    assert md.get("competitor_name") == "Carlos Alcaraz"


# ---------------------------------------------------------------------------
# Integration: settlement.resolve_n_way_competitor + per-trade outcome
# ---------------------------------------------------------------------------


def test_settlement_resolves_competitor_from_orchestrator_metadata():
    from app.settlement.pipeline import resolve_n_way_competitor

    row = {
        "side": "yes",
        "metadata": {
            "competitor_id": "sr:competitor:1",
            "competitor_name": "Carlos Alcaraz",
        },
    }
    assert resolve_n_way_competitor(row) == {
        "competitor_id": "sr:competitor:1",
        "competitor_name": "Carlos Alcaraz",
    }

    # asyncpg-style metadata-as-string still works.
    row = {"side": "yes", "metadata": json.dumps({"competitor_id": "VER"})}
    assert resolve_n_way_competitor(row) == {"competitor_id": "VER"}

    # Non-n-way trades return None.
    assert resolve_n_way_competitor({"side": "yes", "metadata": {}}) is None
    assert resolve_n_way_competitor({"side": "yes", "metadata": None}) is None


def test_outcome_yes_for_n_way_competitor_winner_and_loser():
    from app.settlement.pipeline import outcome_yes_for_n_way_competitor

    leg_winner = {"competitor_id": "sr:competitor:1", "competitor_name": "A"}
    leg_loser = {"competitor_id": "sr:competitor:2", "competitor_name": "B"}
    assert outcome_yes_for_n_way_competitor(
        winning_competitor_id="sr:competitor:1",
        winning_competitor_name="A",
        leg=leg_winner,
    ) == 1
    assert outcome_yes_for_n_way_competitor(
        winning_competitor_id="sr:competitor:1",
        winning_competitor_name="A",
        leg=leg_loser,
    ) == 0


def test_outcome_yes_falls_back_to_name_when_id_missing():
    from app.settlement.pipeline import outcome_yes_for_n_way_competitor

    assert outcome_yes_for_n_way_competitor(
        winning_competitor_id=None,
        winning_competitor_name="Carlos Alcaraz",
        leg={"competitor_name": "Carlos Alcaraz"},
    ) == 1
    assert outcome_yes_for_n_way_competitor(
        winning_competitor_id=None,
        winning_competitor_name="Carlos Alcaraz",
        leg={"competitor_name": "Jannik Sinner"},
    ) == 0


def test_outcome_yes_raises_when_identity_missing():
    from app.settlement.pipeline import outcome_yes_for_n_way_competitor

    with pytest.raises(ValueError):
        outcome_yes_for_n_way_competitor(
            winning_competitor_id=None, winning_competitor_name=None,
            leg={"competitor_id": "VER"},
        )
    with pytest.raises(ValueError):
        outcome_yes_for_n_way_competitor(
            winning_competitor_id="VER", winning_competitor_name=None,
            leg={},
        )


# ---------------------------------------------------------------------------
# Backfill script: walks un-settled n-way trades and stamps the competitor.
# ---------------------------------------------------------------------------


def test_backfill_script_updates_untagged_n_way_trades(monkeypatch):
    from scripts import backfill_n_way_competitors as script

    field = [
        {"id": "sr:competitor:1", "name": "Carlos Alcaraz"},
        {"id": "sr:competitor:2", "name": "Jannik Sinner"},
        {"id": "sr:competitor:3", "name": "Novak Djokovic"},
    ]

    batch_one = [
        {  # full-name match in title
            "id": 1, "market_id": "KXATP-AO26-ALCARAZ",
            "market_title": "Will Carlos Alcaraz win the 2026 Australian Open?",
            "side": "yes",
            "prediction_context": {"n_way_field": field},
        },
        {  # different competitor
            "id": 2, "market_id": "KXATP-AO26-SINNER",
            "market_title": "Will Jannik Sinner win the 2026 Australian Open?",
            "side": "yes",
            "prediction_context": {"n_way_field": field},
        },
        {  # un-deducible: no title, no field signal
            "id": 3, "market_id": "KXATP-99-?",
            "market_title": None, "side": "yes",
            "prediction_context": None,
        },
    ]
    fetched_batches: list[list[dict]] = [batch_one, []]
    updates: list[tuple[Any, ...]] = []

    async def _fake_fetch(_query: str, *args: Any) -> list[dict]:
        return fetched_batches.pop(0) if fetched_batches else []

    async def _fake_execute(_query: str, *args: Any) -> str:
        updates.append(args)
        return "UPDATE 1"

    monkeypatch.setattr(script.db, "fetch", _fake_fetch)
    monkeypatch.setattr(script.db, "execute", _fake_execute)

    stats = asyncio.run(script.run(batch_size=500, dry_run=False))

    assert stats == {"scanned": 3, "updated": 2, "skipped": 1}
    # Updates carry (competitor_id, competitor_name, trade_id).
    assert ("sr:competitor:1", "Carlos Alcaraz", 1) in updates
    assert ("sr:competitor:2", "Jannik Sinner", 2) in updates
    # Trade 3 (un-deducible) must not have been touched.
    assert all(args[2] != 3 for args in updates)


def test_backfill_dry_run_does_not_execute_updates(monkeypatch):
    from scripts import backfill_n_way_competitors as script

    batch = [
        {
            "id": 11, "market_id": "KXATP-DR-ALCARAZ",
            "market_title": "Will Carlos Alcaraz win?",
            "side": "yes",
            "prediction_context": {
                "n_way_field": [
                    {"id": "sr:competitor:1", "name": "Carlos Alcaraz"},
                ],
            },
        },
    ]
    fetched_batches: list[list[dict]] = [batch, []]

    async def _fake_fetch(_q: str, *_a: Any) -> list[dict]:
        return fetched_batches.pop(0) if fetched_batches else []

    async def _boom(*_a: Any, **_kw: Any) -> str:
        raise AssertionError("dry_run must not call db.execute")

    monkeypatch.setattr(script.db, "fetch", _fake_fetch)
    monkeypatch.setattr(script.db, "execute", _boom)

    stats = asyncio.run(script.run(batch_size=500, dry_run=True))
    assert stats == {"scanned": 1, "updated": 1, "skipped": 0}


def test_backfill_dry_run_full_batch_does_not_loop_forever(monkeypatch):
    """Cursor pagination keeps dry-run from re-fetching the same page."""
    from scripts import backfill_n_way_competitors as script

    field = [{"id": "sr:competitor:1", "name": "Carlos Alcaraz"}]
    page1 = [
        {
            "id": i, "market_id": f"KXATP-{i}-ALCARAZ",
            "market_title": "Will Carlos Alcaraz win?",
            "side": "yes",
            "prediction_context": {"n_way_field": field},
        }
        for i in range(1, 4)
    ]
    page2 = [
        {
            "id": i, "market_id": f"KXATP-{i}-ALCARAZ",
            "market_title": "Will Carlos Alcaraz win?",
            "side": "yes",
            "prediction_context": {"n_way_field": field},
        }
        for i in range(4, 6)
    ]
    last_id_seen: list[int] = []
    page_idx = {"i": 0}

    async def _fake_fetch(_q: str, *args: Any) -> list[dict]:
        last_id_seen.append(int(args[1]))
        i = page_idx["i"]
        page_idx["i"] += 1
        if i == 0:
            return page1
        if i == 1:
            return page2
        raise AssertionError(
            f"unexpected third fetch — cursor history: {last_id_seen}"
        )

    async def _boom(*_a: Any, **_kw: Any) -> str:
        raise AssertionError("dry_run must not call db.execute")

    monkeypatch.setattr(script.db, "fetch", _fake_fetch)
    monkeypatch.setattr(script.db, "execute", _boom)

    stats = asyncio.run(script.run(batch_size=3, dry_run=True))
    assert stats == {"scanned": 5, "updated": 5, "skipped": 0}
    assert last_id_seen == [0, 3], last_id_seen


def test_backfill_handles_string_jsonb_prediction_context(monkeypatch):
    """asyncpg can hand jsonb back as a str — the backfill must decode."""
    from scripts import backfill_n_way_competitors as script

    field = [{"id": "VER", "name": "Max Verstappen"}]
    batch = [
        {
            "id": 21, "market_id": "KXF1-MON26-VER",
            "market_title": "Will Verstappen win the Monaco GP?",
            "side": "yes",
            "prediction_context": json.dumps({"n_way_field": field}),
        },
    ]
    fetched_batches: list[list[dict]] = [batch, []]
    updates: list[tuple[Any, ...]] = []

    async def _fake_fetch(_q: str, *_a: Any) -> list[dict]:
        return fetched_batches.pop(0) if fetched_batches else []

    async def _fake_execute(_q: str, *args: Any) -> str:
        updates.append(args)
        return "UPDATE 1"

    monkeypatch.setattr(script.db, "fetch", _fake_fetch)
    monkeypatch.setattr(script.db, "execute", _fake_execute)

    stats = asyncio.run(script.run(batch_size=500, dry_run=False))
    assert stats == {"scanned": 1, "updated": 1, "skipped": 0}
    assert updates[0] == ("VER", "Max Verstappen", 21)
