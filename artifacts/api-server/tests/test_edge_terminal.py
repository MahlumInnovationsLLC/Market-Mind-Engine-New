"""Unit tests for the Edge Terminal scraper pipeline (task #844).

Covers the three pure-Python helpers that have no Playwright /
Postgres dependencies:

  * ``control`` — input_mode get / set / ensure_seeded with the
    asyncpg layer monkey-patched.
  * ``executor._quarter_kelly_quantity`` — Kelly math + the 5%
    notional cap.
  * ``executor._fire_one`` integration: ticker resolution + the
    paper.place_order follow-up UPDATE that re-tags the trade row
    as ``placed_by='edge_terminal'``.
  * ``market_resolver.resolve`` — direct-ticker passthrough,
    token-overlap match (≥2 tokens required), no-match → None.
"""
from __future__ import annotations

import asyncio
from typing import Any


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


# ---------------------------------------------------------------------
# control
# ---------------------------------------------------------------------

class _FakeDB:
    """Mock asyncpg helpers backed by a plain dict for system_settings."""

    def __init__(self, initial: dict[str, str] | None = None):
        self.kv: dict[str, str] = dict(initial or {})
        self.exec_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchval(self, q: str, *args):
        if "system_settings" in q and "WHERE key" in q:
            return self.kv.get(args[0])
        return None

    async def execute(self, q: str, *args):
        self.exec_calls.append((q, args))
        if "INSERT INTO system_settings" in q:
            self.kv[args[0]] = args[1]


def _patch_db(monkeypatch, fake: _FakeDB):
    from app.edge_terminal import control
    monkeypatch.setattr(control.db, "fetchval", fake.fetchval)
    monkeypatch.setattr(control.db, "execute", fake.execute)


def test_control_get_defaults_when_missing(monkeypatch):
    from app.edge_terminal import control
    _patch_db(monkeypatch, _FakeDB())
    assert _run(control.get_input_mode()) == "edge_terminal"


def test_control_set_then_get_roundtrips(monkeypatch):
    from app.edge_terminal import control
    fake = _FakeDB()
    _patch_db(monkeypatch, fake)
    out = _run(control.set_input_mode("pipeline"))
    assert out == "pipeline"
    assert _run(control.get_input_mode()) == "pipeline"


def test_control_set_invalid_raises(monkeypatch):
    from app.edge_terminal import control
    _patch_db(monkeypatch, _FakeDB())
    try:
        _run(control.set_input_mode("garbage"))
    except ValueError as e:
        assert "garbage" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_control_ensure_seeded_forces_edge_terminal_on_boot(monkeypatch):
    """Task #844 default: every boot resets to edge_terminal regardless
    of any prior operator-flipped value."""
    from app.edge_terminal import control
    monkeypatch.delenv("AXIOM_FORCE_EDGE_TERMINAL_ON_BOOT", raising=False)
    fake = _FakeDB({"app.input_mode": "pipeline"})
    _patch_db(monkeypatch, fake)
    assert _run(control.ensure_seeded()) == "edge_terminal"
    assert fake.kv["app.input_mode"] == "edge_terminal"


def test_control_ensure_seeded_writes_default_when_missing(monkeypatch):
    from app.edge_terminal import control
    monkeypatch.delenv("AXIOM_FORCE_EDGE_TERMINAL_ON_BOOT", raising=False)
    fake = _FakeDB()
    _patch_db(monkeypatch, fake)
    assert _run(control.ensure_seeded()) == "edge_terminal"
    assert fake.kv["app.input_mode"] == "edge_terminal"


def test_control_ensure_seeded_preserves_existing_when_force_off(monkeypatch):
    """Audit fix: AXIOM_FORCE_EDGE_TERMINAL_ON_BOOT=0 preserves the
    operator-flipped value across restarts."""
    from app.edge_terminal import control
    monkeypatch.setenv("AXIOM_FORCE_EDGE_TERMINAL_ON_BOOT", "0")
    fake = _FakeDB({"app.input_mode": "pipeline"})
    _patch_db(monkeypatch, fake)
    assert _run(control.ensure_seeded()) == "pipeline"
    assert fake.kv["app.input_mode"] == "pipeline"


def test_control_ensure_seeded_force_off_still_seeds_when_missing(monkeypatch):
    from app.edge_terminal import control
    monkeypatch.setenv("AXIOM_FORCE_EDGE_TERMINAL_ON_BOOT", "0")
    fake = _FakeDB()
    _patch_db(monkeypatch, fake)
    assert _run(control.ensure_seeded()) == "edge_terminal"
    assert fake.kv["app.input_mode"] == "edge_terminal"


# ---------------------------------------------------------------------
# executor: quarter-Kelly math
# ---------------------------------------------------------------------

def test_kelly_positive_edge_sizes_quarter_kelly():
    from app.edge_terminal.executor import _quarter_kelly_quantity

    # p=0.60 at price=0.50 → b=1, kelly = (1*0.6 - 0.4)/1 = 0.20
    # quarter-Kelly = 0.05 of bankroll = $50 → 100 contracts @ 0.50.
    qty, fk = _quarter_kelly_quantity(
        bankroll=1000.0, model_prob=0.60, price=0.50,
    )
    # int($50/$0.50) = 100 mathematically, but FP rounding gives 99.
    assert qty in (99, 100)
    assert 0.04 < fk < 0.06  # 0.25 * 0.20 = 0.05


def test_kelly_capped_by_5pct_of_bankroll():
    from app.edge_terminal.executor import _quarter_kelly_quantity

    # p=0.99 at price=0.10 → wildly favourable; cap clamps to 5%.
    qty, _fk = _quarter_kelly_quantity(
        bankroll=10_000.0, model_prob=0.99, price=0.10,
    )
    # 5% cap = $500 / $0.10 = 5000 contracts.
    assert qty == 5000


def test_kelly_negative_returns_zero():
    from app.edge_terminal.executor import _quarter_kelly_quantity
    qty, fk = _quarter_kelly_quantity(
        bankroll=1000.0, model_prob=0.40, price=0.50,
    )
    assert qty == 0
    assert fk == 0.0


def test_kelly_zero_bankroll_returns_zero():
    from app.edge_terminal.executor import _quarter_kelly_quantity
    qty, _ = _quarter_kelly_quantity(bankroll=0, model_prob=0.7, price=0.5)
    assert qty == 0


def test_kelly_missing_prob_returns_zero():
    from app.edge_terminal.executor import _quarter_kelly_quantity
    qty, _ = _quarter_kelly_quantity(bankroll=1000, model_prob=None, price=0.5)
    assert qty == 0


# ---------------------------------------------------------------------
# market_resolver
# ---------------------------------------------------------------------

def test_resolver_direct_ticker_passthrough(monkeypatch):
    from app.edge_terminal import market_resolver

    async def _explode(*a, **k):  # would only be called on fallback
        raise AssertionError("db.fetch should not be called")

    async def _ticker_known(_q, *args, **kwargs):
        return 1  # ticker exists in markets cache

    monkeypatch.setattr(market_resolver.db, "fetch", _explode)
    monkeypatch.setattr(market_resolver.db, "fetchval", _ticker_known)
    out = _run(market_resolver.resolve({"kalshi_ticker": "KX-NBA-LAL-1"}))
    assert out == "KX-NBA-LAL-1"


def test_resolver_unknown_direct_ticker_falls_through_to_title(monkeypatch):
    """Audit fix: scraped tickers must be validated against the markets
    cache; an unknown ticker falls through to the title-match path."""
    from app.edge_terminal import market_resolver

    async def _ticker_missing(_q, *args, **kwargs):
        return None

    async def _fake_fetch(_q, *args, **kwargs):
        return [{"ticker": "KX-NBA-LAL-BOS", "title": "Lakers vs Celtics"}]

    monkeypatch.setattr(market_resolver.db, "fetchval", _ticker_missing)
    monkeypatch.setattr(market_resolver.db, "fetch", _fake_fetch)
    out = _run(market_resolver.resolve({
        "kalshi_ticker": "KX-STALE",
        "matchup": "Lakers @ Celtics",
        "selection": "Lakers",
    }))
    assert out == "KX-NBA-LAL-BOS"


def test_resolver_anchors_on_longest_token(monkeypatch):
    """Audit fix: ILIKE anchor must be the longest unique token, not
    ``tokens[0]`` — otherwise short stopwords (vs/at/on) dump
    unrelated markets into the scorer."""
    from app.edge_terminal import market_resolver

    captured: list[tuple[str, ...]] = []

    async def _fake_fetch(_q, *args, **kwargs):
        captured.append(args)
        return []

    async def _ticker_known(*a, **k):
        return None

    monkeypatch.setattr(market_resolver.db, "fetch", _fake_fetch)
    monkeypatch.setattr(market_resolver.db, "fetchval", _ticker_known)
    _run(market_resolver.resolve({
        "matchup": "vs at on",  # 3 short stopwords
        "selection": "Manchester",
    }))
    assert captured, "fetch should have been called"
    assert "%manchester%" in captured[0][0].lower()


def test_resolver_token_overlap_picks_best(monkeypatch):
    from app.edge_terminal import market_resolver

    rows = [
        {"ticker": "KX-NBA-LAL-BOS", "title": "Lakers vs Celtics — Lakers win"},
        {"ticker": "KX-NBA-MIA-DEN", "title": "Heat vs Nuggets"},
    ]

    async def _fake_fetch(_q, *args, **kwargs):
        return rows

    async def _ticker_known(*a, **k):
        return None

    monkeypatch.setattr(market_resolver.db, "fetch", _fake_fetch)
    monkeypatch.setattr(market_resolver.db, "fetchval", _ticker_known)
    out = _run(market_resolver.resolve({
        "matchup": "Lakers @ Celtics", "selection": "Lakers",
    }))
    assert out == "KX-NBA-LAL-BOS"


def test_resolver_returns_none_below_overlap_threshold(monkeypatch):
    from app.edge_terminal import market_resolver

    rows = [{"ticker": "KX-FOO", "title": "Lakers parade attendance"}]

    async def _fake_fetch(_q, *args, **kwargs):
        return rows

    async def _ticker_known(*a, **k):
        return None

    monkeypatch.setattr(market_resolver.db, "fetch", _fake_fetch)
    monkeypatch.setattr(market_resolver.db, "fetchval", _ticker_known)
    # Only "lakers" overlaps the title; "warriors" and "win" do not.
    out = _run(market_resolver.resolve({
        "matchup": "Warriors vs Suns", "selection": "Warriors win",
    }))
    assert out is None


def test_resolver_single_token_signal_allows_one_overlap(monkeypatch):
    """Audit fix: crypto/forex picks naturally produce <2 unique tokens
    (e.g. "BTC"); requiring 2 overlaps would always skip them."""
    from app.edge_terminal import market_resolver

    rows = [{"ticker": "KX-BTC-100K", "title": "BTC closes above 100k"}]

    async def _fake_fetch(_q, *args, **kwargs):
        return rows

    async def _ticker_known(*a, **k):
        return None

    monkeypatch.setattr(market_resolver.db, "fetch", _fake_fetch)
    monkeypatch.setattr(market_resolver.db, "fetchval", _ticker_known)
    out = _run(market_resolver.resolve({
        "matchup": "BTC", "selection": "",
    }))
    assert out == "KX-BTC-100K"


def test_resolver_db_error_returns_none(monkeypatch):
    from app.edge_terminal import market_resolver

    async def _boom(*a, **k):
        raise RuntimeError("db down")

    async def _ticker_known(*a, **k):
        return None

    monkeypatch.setattr(market_resolver.db, "fetch", _boom)
    monkeypatch.setattr(market_resolver.db, "fetchval", _ticker_known)
    out = _run(market_resolver.resolve({
        "matchup": "Lakers @ Celtics", "selection": "Lakers",
    }))
    assert out is None


# ---------------------------------------------------------------------
# executor._fire_one — full flow with paper.place_order stubbed.
# ---------------------------------------------------------------------

def test_fire_one_resolves_ticker_sizes_and_tags_at_insert(monkeypatch):
    """Audit fix: ``placed_by='edge_terminal'`` is threaded through
    paper.place_order at insert time rather than via a racy follow-up
    UPDATE."""
    from app.edge_terminal import executor

    placed: list[dict[str, Any]] = []

    async def _resolve(sig):
        return "KX-NBA-LAL-1"

    async def _bankroll(trading_mode="paper"):
        return 1000.0

    async def _place_order(ctx, **kwargs):
        placed.append({"ctx": ctx, "kwargs": kwargs})
        return {"trades_id": 4242, "status": "placed"}

    async def _execute(q, *args):
        raise AssertionError(
            "executor must NOT issue a follow-up UPDATE — placed_by is "
            "now threaded through paper.place_order at insert time"
        )

    monkeypatch.setattr(executor.market_resolver, "resolve", _resolve)
    monkeypatch.setattr(executor.cycle_module, "_latest_bankroll", _bankroll)
    monkeypatch.setattr(executor.paper_module, "place_order", _place_order)
    monkeypatch.setattr(executor.db, "execute", _execute)

    sig = {
        "id": 1,
        "vertical": "sports",
        "matchup": "LAL @ BOS",
        "selection": "Lakers",
        "edge_pct": 10.0,
        "model_prob": 0.60,
        "market_price": 0.50,
    }
    out = _run(executor._fire_one(sig))
    assert out is not None
    assert out["trades_id"] == 4242
    ctx = placed[0]["ctx"]
    assert ctx.market_id == "KX-NBA-LAL-1"
    assert ctx.side == "YES"
    assert ctx.intended_quantity in (99, 100)  # quarter-Kelly @ 0.50 (FP rounding)
    # placed_by was tagged at insert time.
    assert placed[0]["kwargs"].get("placed_by") == "edge_terminal"


def test_fire_one_honors_no_side(monkeypatch):
    """Audit fix: a scraper-provided ``side='NO'`` must reach the
    RiskContext rather than being silently flipped to YES."""
    from app.edge_terminal import executor

    placed: list[dict[str, Any]] = []

    async def _resolve(sig):
        return "KX-NBA-OU-1"

    async def _bankroll(trading_mode="paper"):
        return 1000.0

    async def _place_order(ctx, **kwargs):
        placed.append({"ctx": ctx, "kwargs": kwargs})
        return {"trades_id": 7, "status": "placed"}

    monkeypatch.setattr(executor.market_resolver, "resolve", _resolve)
    monkeypatch.setattr(executor.cycle_module, "_latest_bankroll", _bankroll)
    monkeypatch.setattr(executor.paper_module, "place_order", _place_order)

    sig = {
        "id": 1,
        "vertical": "sports",
        "matchup": "LAL @ BOS",
        "selection": "Under 220.5",
        "side": "NO",
        # For NO side: kelly favourable when (1-p) > price, e.g.
        # p=0.40, price=0.50 → NO has edge.
        "model_prob": 0.40,
        "market_price": 0.50,
    }
    out = _run(executor._fire_one(sig))
    assert out is not None
    ctx = placed[0]["ctx"]
    assert ctx.side == "NO"
    # Edge signed from the NO-side perspective: price - model_prob.
    assert ctx.edge is not None and ctx.edge > 0


def test_signal_side_roundtrips_db_to_executor(monkeypatch):
    """Audit fix: side='NO' from the scraper must survive the round
    trip through the edge_terminal_signals table and reach RiskContext.
    Before the 0056 migration the column did not exist, so the poller
    INSERT dropped ``side`` and ``drain_pending`` reloaded the row with
    no side hint — every fired pick defaulted to YES, which is a
    wrong-side trade for under/no selections."""
    import json
    from app.edge_terminal import executor, poller

    # In-memory "row" that the poller will INSERT and the executor
    # will SELECT.  Keyed by what column list each query asks for.
    table: dict[int, dict[str, Any]] = {}
    next_id = {"n": 1}

    async def _fetchval(q: str, *args):
        # poller INSERT
        if "INSERT INTO edge_terminal_signals" in q:
            row = {
                "id": next_id["n"],
                "vertical": args[0],
                "matchup": args[1],
                "selection": args[2],
                "signal_date": args[3],
                "edge_pct": args[4],
                "model_prob": args[5],
                "market_price": args[6],
                "kalshi_ticker": args[7],
                "market_title": args[8],
                "raw": args[9],
                "side": args[10],
                "status": "pending",
            }
            table[next_id["n"]] = row
            next_id["n"] += 1
            return row["id"]
        return None

    async def _fetch(q: str, *args):
        # executor.drain_pending SELECT
        if "FROM edge_terminal_signals" in q and "status = 'pending'" in q:
            return [
                {
                    "id": r["id"],
                    "vertical": r["vertical"],
                    "matchup": r["matchup"],
                    "selection": r["selection"],
                    "signal_date": r["signal_date"],
                    "edge_pct": r["edge_pct"],
                    "model_prob": r["model_prob"],
                    "market_price": r["market_price"],
                    "kalshi_ticker": r["kalshi_ticker"],
                    "market_title": r["market_title"],
                    "side": r["side"],
                }
                for r in table.values()
                if r["status"] == "pending"
            ]
        return []

    async def _execute(q: str, *args):
        # _mark UPDATE — flip row status so we don't loop.
        if "UPDATE edge_terminal_signals" in q:
            sid = args[-1] if isinstance(args[-1], int) else None
            if sid in table:
                table[sid]["status"] = "placed"

    monkeypatch.setattr(poller.db, "fetchval", _fetchval)
    monkeypatch.setattr(executor.db, "fetch", _fetch)
    monkeypatch.setattr(executor.db, "execute", _execute)

    placed: list[dict[str, Any]] = []

    async def _resolve(sig):
        return "KX-NBA-OU-1"

    async def _bankroll(trading_mode="paper"):
        return 1000.0

    async def _place_order(ctx, **kwargs):
        placed.append({"ctx": ctx, "kwargs": kwargs})
        return {"trades_id": 999, "status": "placed"}

    monkeypatch.setattr(executor.market_resolver, "resolve", _resolve)
    monkeypatch.setattr(executor.cycle_module, "_latest_bankroll", _bankroll)
    monkeypatch.setattr(executor.paper_module, "place_order", _place_order)

    _run(poller._insert_signal({
        "vertical": "sports",
        "matchup": "LAL @ BOS",
        "selection": "Under 220.5",
        "signal_date": "2026-05-22",
        "side": "no",  # lower-case from scraper → normalized to "NO"
        "edge_pct": 10.0,
        "model_prob": 0.40,
        "market_price": 0.50,
        "kalshi_ticker": None,
        "market_title": None,
        "raw": {},
    }))
    assert table[1]["side"] == "NO", "poller must persist normalized side"

    _run(executor.drain_pending())
    assert len(placed) == 1, "executor must have fired the pending signal"
    assert placed[0]["ctx"].side == "NO", (
        "side='NO' was lost between DB INSERT and RiskContext — this is "
        "the audit's wrong-side-trade bug"
    )


def test_fire_one_no_ticker_returns_none(monkeypatch):
    from app.edge_terminal import executor

    async def _resolve(sig):
        return None

    monkeypatch.setattr(executor.market_resolver, "resolve", _resolve)
    out = _run(executor._fire_one({"id": 1}))
    assert out is None
