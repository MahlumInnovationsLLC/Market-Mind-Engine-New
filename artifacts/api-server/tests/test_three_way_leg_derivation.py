"""Task #444: derive the 3-way (h/d/a) leg for soccer trades from the
candidate metadata, the candidate ``side``, or the Kalshi market title
so the settlement pipeline can apply the per-leg outcome on a draw.

Covers ``app.orchestrator.three_way.derive_three_way_leg`` plus the
``app.orchestrator.paper.place_order`` integration that stamps the
result into ``trades.metadata`` jsonb at insert-time and gates on the
league's ``three_way_market`` flag.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from app import db as _db
from app.orchestrator import paper as _paper
from app.orchestrator.three_way import derive_three_way_leg
from app.risk.gates import RiskContext


# ---------------------------------------------------------------------------
# Helper-level: derive_three_way_leg
# ---------------------------------------------------------------------------


def test_metadata_pre_stamp_short_circuits_title_parsing():
    """A pre-stamped ``three_way_leg`` on the candidate metadata
    wins over every other signal — feeds that already know the leg
    can short-circuit even when no title is available."""
    leg = derive_three_way_leg(
        market_title=None,
        candidate={"metadata": {"three_way_leg": "draw"}},
    )
    assert leg == "draw"


def test_metadata_outcome_3way_alias_is_normalized():
    leg = derive_three_way_leg(
        market_title=None,
        candidate={"metadata": {"outcome_3way": "1"}},  # alias for "home"
    )
    assert leg == "home"


def test_metadata_str_jsonb_is_decoded():
    """asyncpg occasionally hands jsonb back as a str (depending on
    codec); the helper decodes it rather than silently ignoring."""
    leg = derive_three_way_leg(
        market_title=None,
        candidate={"metadata": json.dumps({"leg": "away_win"})},
    )
    assert leg == "away"


def test_side_field_falls_back_to_leg_name():
    leg = derive_three_way_leg(
        market_title=None, candidate={"side": "home_win"},
    )
    assert leg == "home"


def test_yes_side_does_not_resolve_a_leg():
    """Production trades ship with ``side`` = "yes"/"no"; the helper
    must not pretend that's a leg name (would mis-tag every trade)."""
    leg = derive_three_way_leg(
        market_title=None, candidate={"side": "yes"},
    )
    assert leg is None


def test_draw_keyword_in_title_wins_over_team_match():
    """Even when both team names appear in the title, a "draw" keyword
    has to win — Kalshi soccer draw markets are typically titled
    ``"Will the Liverpool vs Manchester City match end in a draw?"``."""
    leg = derive_three_way_leg(
        market_title="Will the Liverpool vs Manchester City match end in a draw?",
        candidate={"home_team": "Liverpool", "away_team": "Manchester City"},
    )
    assert leg == "draw"


def test_tie_keyword_also_resolves_draw():
    leg = derive_three_way_leg(
        market_title="Liverpool vs Manchester City — tie?",
        candidate={"home_team": "Liverpool", "away_team": "Manchester City"},
    )
    assert leg == "draw"


def test_home_team_only_match_resolves_home_leg():
    leg = derive_three_way_leg(
        market_title="Will Liverpool win?",
        candidate={"home_team": "Liverpool", "away_team": "Manchester City"},
    )
    assert leg == "home"


def test_away_team_only_match_resolves_away_leg():
    leg = derive_three_way_leg(
        market_title="Will Manchester City win?",
        candidate={"home_team": "Liverpool", "away_team": "Manchester City"},
    )
    assert leg == "away"


def test_both_teams_in_title_picks_first_mentioned():
    """``"Will Manchester City beat Liverpool?"`` — City is the YES
    subject even though Liverpool is the home team."""
    leg = derive_three_way_leg(
        market_title="Will Manchester City beat Liverpool?",
        candidate={"home_team": "Liverpool", "away_team": "Manchester City"},
    )
    assert leg == "away"

    leg = derive_three_way_leg(
        market_title="Will Liverpool beat Manchester City?",
        candidate={"home_team": "Liverpool", "away_team": "Manchester City"},
    )
    assert leg == "home"


def test_team_names_in_game_context_dict_are_honored():
    leg = derive_three_way_leg(
        market_title="Will Liverpool win?",
        candidate={
            "game_context": {
                "home_team": "Liverpool", "away_team": "Manchester City",
            },
        },
    )
    assert leg == "home"


def test_unrelated_title_returns_none():
    leg = derive_three_way_leg(
        market_title="Will Bitcoin close above $50,000?",
        candidate={"home_team": "Liverpool", "away_team": "Manchester City"},
    )
    assert leg is None


def test_no_signals_returns_none():
    assert derive_three_way_leg(market_title=None, candidate=None) is None
    assert derive_three_way_leg(market_title="", candidate={}) is None


# ---------------------------------------------------------------------------
# Integration: paper.place_order stamps metadata into trades insert
# ---------------------------------------------------------------------------


def _ctx(*, league: str | None, market_id: str = "KXEPL-26APR25LIVMCI-LIV") -> RiskContext:
    return RiskContext(
        market_id=market_id,
        side="YES",
        vertical="sports",
        league=league,
        market_type=None,
        matchup="LIV vs MCI",
        current_price=0.42,
        model_probability=0.5,
        edge=0.08,
        confidence_band_low=None,
        confidence_band_high=None,
        agreement_rate=None,
        swarm_approved=True,
        swarm_recommended_action="BUY_YES",
        debate_id=None,
        intended_quantity=10,
        intended_notional_usd=4.2,
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
        # First call inside the dispatch path resolves the operator
        # user_id; the second is the trades INSERT.
        self.fetchval_calls += 1
        if "FROM app_users" in query or "is_admin" in query:
            return "user_op_test"
        if "INSERT INTO trades" in query:
            self.captured["query"] = query
            self.captured["args"] = args
            return 12345
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
        ),
    )

    async def _titles(_ids):
        return {ctx_id: market_title for ctx_id in _ids}

    monkeypatch.setattr(_paper, "resolve_market_titles", _titles)

    async def _fake_submit(req, *, force_paper=None):
        return SimpleNamespace(
            exchange_order_id="exo-leg",
            state="filled",
            filled_count=req.count,
            avg_fill_price_cents=42,
            order_id=req.client_order_id,
        )

    from app.kalshi import lifecycle as kalshi_lc
    monkeypatch.setattr(kalshi_lc, "submit_order", _fake_submit)


def _metadata_arg_from_capture(capture: _InsertCapture) -> dict[str, Any]:
    """The orchestrator binds the metadata jsonb as the *last* arg.
    Decode it back into a dict for assertion."""
    raw = capture.captured["args"][-1]
    if raw is None:
        return {}
    return json.loads(raw) if isinstance(raw, str) else dict(raw)


def test_place_order_stamps_three_way_leg_for_soccer_home_win(monkeypatch):
    _paper._reset_operator_user_id_cache()
    capture = _InsertCapture()
    _patch_paper_for_place_order(
        monkeypatch, capture=capture,
        market_title="Will Liverpool beat Manchester City?",
    )

    cand = {
        "home_team": "Liverpool",
        "away_team": "Manchester City",
        "market_id": "KXEPL-26APR25LIVMCI-LIV",
    }
    result = asyncio.run(_paper.place_order(
        ctx=_ctx(league="EPL"),
        cand=cand,
        consensus=0.5,
        edge=0.08,
        kelly=0.05,
        debate_id=None,
        exchange="kalshi",
    ))

    assert capture.captured.get("query"), "trades INSERT was not executed"
    assert "metadata" in capture.captured["query"]
    md = _metadata_arg_from_capture(capture)
    assert md == {"three_way_leg": "home"}
    # The orchestrator also surfaces the leg on the return payload so
    # callers don't have to re-query the trades row to know what got
    # tagged.
    assert result["metadata"] == {"three_way_leg": "home"}


def test_place_order_stamps_draw_leg_when_title_says_draw(monkeypatch):
    _paper._reset_operator_user_id_cache()
    capture = _InsertCapture()
    _patch_paper_for_place_order(
        monkeypatch, capture=capture,
        market_title="Will the Liverpool vs Manchester City match end in a draw?",
    )

    asyncio.run(_paper.place_order(
        ctx=_ctx(league="EPL"),
        cand={"home_team": "Liverpool", "away_team": "Manchester City"},
        consensus=0.5, edge=0.08, kelly=0.05,
        debate_id=None, exchange="kalshi",
    ))
    md = _metadata_arg_from_capture(capture)
    assert md == {"three_way_leg": "draw"}


def test_place_order_skips_metadata_for_non_three_way_league(monkeypatch):
    """NBA / non-soccer leagues must NOT get a leg tag even when the
    title happens to mention "draw" or a team name (would corrupt
    settlement for binary markets)."""
    _paper._reset_operator_user_id_cache()
    capture = _InsertCapture()
    _patch_paper_for_place_order(
        monkeypatch, capture=capture,
        market_title="Will the Lakers beat the Celtics?",
    )

    asyncio.run(_paper.place_order(
        ctx=_ctx(league="NBA", market_id="KXNBA-LAKCEL"),
        cand={"home_team": "Lakers", "away_team": "Celtics"},
        consensus=0.5, edge=0.08, kelly=0.05,
        debate_id=None, exchange="kalshi",
    ))
    md = _metadata_arg_from_capture(capture)
    assert md == {}, f"NBA trade should carry no leg tag, got {md!r}"


def test_place_order_omits_metadata_when_leg_undeducible(monkeypatch):
    """Soccer league but no title and no team-name signals — the
    insert binds an empty jsonb so the COALESCE falls back to '{}'."""
    _paper._reset_operator_user_id_cache()
    capture = _InsertCapture()
    _patch_paper_for_place_order(
        monkeypatch, capture=capture, market_title=None,
    )

    asyncio.run(_paper.place_order(
        ctx=_ctx(league="EPL"),
        cand={"side": "yes"},  # production candidates ship with yes/no
        consensus=0.5, edge=0.08, kelly=0.05,
        debate_id=None, exchange="kalshi",
    ))
    md = _metadata_arg_from_capture(capture)
    assert md == {}


# ---------------------------------------------------------------------------
# Integration: settlement.resolve_three_way_leg returns non-None for a
# trade row carrying the orchestrator's metadata stamp.
# ---------------------------------------------------------------------------


def test_settlement_resolves_leg_from_orchestrator_metadata():
    """The whole point of stamping metadata: the settlement pipeline's
    ``resolve_three_way_leg`` reads it back without needing the legacy
    ``side`` column to encode home/draw/away."""
    from app.settlement.pipeline import resolve_three_way_leg

    # Trades built by the orchestrator carry side="yes"/"no" and the
    # leg lives only in metadata — settlement must still resolve.
    row = {"side": "yes", "metadata": {"three_way_leg": "draw"}}
    assert resolve_three_way_leg(row) == "draw"

    # asyncpg-style metadata-as-string still works.
    row = {"side": "yes", "metadata": json.dumps({"three_way_leg": "home"})}
    assert resolve_three_way_leg(row) == "home"


# ---------------------------------------------------------------------------
# Backfill script: walks un-settled soccer trades and stamps the leg.
# ---------------------------------------------------------------------------


def test_backfill_script_updates_untagged_soccer_trades(monkeypatch):
    """End-to-end shape test: feed the script a synthetic batch of
    trades and assert the UPDATE bind values land on the right rows
    with the right leg.  Idempotent re-runs (already-tagged rows
    excluded by the SELECT) are exercised by the empty-batch case."""
    from scripts import backfill_soccer_three_way_legs as script

    # Synthetic rows the SELECT would return on the first pass.
    batch_one = [
        {  # home win, full team-name match in title
            "id": 1, "market_id": "KXEPL-01-LIV",
            "market_title": "Will Liverpool beat Manchester City?",
            "side": "yes",
            "home_team": "Liverpool", "away_team": "Manchester City",
        },
        {  # draw via keyword
            "id": 2, "market_id": "KXEPL-01-DRAW",
            "market_title": "Will Liverpool vs Manchester City end in a draw?",
            "side": "yes",
            "home_team": "Liverpool", "away_team": "Manchester City",
        },
        {  # away win
            "id": 3, "market_id": "KXEPL-01-MCI",
            "market_title": "Will Manchester City beat Liverpool?",
            "side": "yes",
            "home_team": "Liverpool", "away_team": "Manchester City",
        },
        {  # un-deducible — no title, no leg side, no team names
            "id": 4, "market_id": "KXEPL-99-?",
            "market_title": None, "side": "yes",
            "home_team": None, "away_team": None,
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

    assert stats == {"scanned": 4, "updated": 3, "skipped": 1}
    # Updates carry the (leg, trade_id) bind tuple in order.
    assert (("home", 1) in updates), updates
    assert (("draw", 2) in updates), updates
    assert (("away", 3) in updates), updates
    # Trade 4 (un-deducible) must not have been touched.
    assert all(args[1] != 4 for args in updates)


def test_backfill_dry_run_does_not_execute_updates(monkeypatch):
    from scripts import backfill_soccer_three_way_legs as script

    batch = [
        {
            "id": 11, "market_id": "KXEPL-DR-LIV",
            "market_title": "Will Liverpool beat Manchester City?",
            "side": "yes",
            "home_team": "Liverpool", "away_team": "Manchester City",
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
    """Regression: dry-run mode never narrows the candidate set via
    UPDATE, so without cursor pagination the loop would re-fetch the
    same batch on every pass when the dataset has >= batch_size
    matching rows.  We assert the script terminates after a bounded
    number of fetches and uses the cursor (id > $2) to advance.
    """
    from scripts import backfill_soccer_three_way_legs as script

    # Two pages of rows that *all* derive a leg, returned in
    # ascending id order.  batch_size matches page size so the
    # ``len(rows) < batch_size`` early-exit doesn't fire on page 1.
    page1 = [
        {
            "id": i, "market_id": f"KXEPL-{i}-LIV",
            "market_title": "Will Liverpool beat Manchester City?",
            "side": "yes",
            "home_team": "Liverpool", "away_team": "Manchester City",
        }
        for i in range(1, 4)
    ]
    page2 = [
        {
            "id": i, "market_id": f"KXEPL-{i}-LIV",
            "market_title": "Will Liverpool beat Manchester City?",
            "side": "yes",
            "home_team": "Liverpool", "away_team": "Manchester City",
        }
        for i in range(4, 6)  # short page → early exit
    ]

    last_id_seen: list[int] = []
    page_idx = {"i": 0}

    async def _fake_fetch(_q: str, *args: Any) -> list[dict]:
        # The script must pass the cursor as $2 (last_id) so the
        # next pass returns strictly newer rows.
        last_id_seen.append(int(args[1]))
        i = page_idx["i"]
        page_idx["i"] += 1
        if i == 0:
            return page1
        if i == 1:
            return page2
        # If the script ever asks for a third page, the cursor must
        # have advanced past page2's max id; otherwise we'd be in an
        # infinite loop and the test should fail loudly.
        raise AssertionError(
            f"unexpected third fetch — cursor history: {last_id_seen}"
        )

    async def _boom(*_a: Any, **_kw: Any) -> str:
        raise AssertionError("dry_run must not call db.execute")

    monkeypatch.setattr(script.db, "fetch", _fake_fetch)
    monkeypatch.setattr(script.db, "execute", _boom)

    stats = asyncio.run(script.run(batch_size=3, dry_run=True))
    assert stats == {"scanned": 5, "updated": 5, "skipped": 0}
    # Cursor advanced after page 1 (max id = 3) before fetching page 2.
    assert last_id_seen == [0, 3], last_id_seen
