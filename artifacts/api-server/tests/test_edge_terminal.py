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


def test_control_ensure_seeded_preserves_existing(monkeypatch):
    from app.edge_terminal import control
    fake = _FakeDB({"app.input_mode": "pipeline"})
    _patch_db(monkeypatch, fake)
    assert _run(control.ensure_seeded()) == "pipeline"
    # No INSERT should have happened — value was already set.
    assert not any("INSERT" in q for q, _ in fake.exec_calls)


def test_control_ensure_seeded_writes_default_when_missing(monkeypatch):
    from app.edge_terminal import control
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

    monkeypatch.setattr(market_resolver.db, "fetch", _explode)
    out = _run(market_resolver.resolve({"kalshi_ticker": "KX-NBA-LAL-1"}))
    assert out == "KX-NBA-LAL-1"


def test_resolver_token_overlap_picks_best(monkeypatch):
    from app.edge_terminal import market_resolver

    rows = [
        {"ticker": "KX-NBA-LAL-BOS", "title": "Lakers vs Celtics — Lakers win"},
        {"ticker": "KX-NBA-MIA-DEN", "title": "Heat vs Nuggets"},
    ]

    async def _fake_fetch(_q, *args, **kwargs):
        return rows

    monkeypatch.setattr(market_resolver.db, "fetch", _fake_fetch)
    out = _run(market_resolver.resolve({
        "matchup": "Lakers @ Celtics", "selection": "Lakers",
    }))
    assert out == "KX-NBA-LAL-BOS"


def test_resolver_returns_none_below_overlap_threshold(monkeypatch):
    from app.edge_terminal import market_resolver

    rows = [{"ticker": "KX-FOO", "title": "Lakers parade attendance"}]

    async def _fake_fetch(_q, *args, **kwargs):
        return rows

    monkeypatch.setattr(market_resolver.db, "fetch", _fake_fetch)
    # Only "lakers" overlaps the title; "warriors" and "win" do not.
    out = _run(market_resolver.resolve({
        "matchup": "Warriors vs Suns", "selection": "Warriors win",
    }))
    assert out is None


def test_resolver_db_error_returns_none(monkeypatch):
    from app.edge_terminal import market_resolver

    async def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(market_resolver.db, "fetch", _boom)
    out = _run(market_resolver.resolve({
        "matchup": "Lakers @ Celtics", "selection": "Lakers",
    }))
    assert out is None


# ---------------------------------------------------------------------
# executor._fire_one — full flow with paper.place_order stubbed.
# ---------------------------------------------------------------------

def test_fire_one_resolves_ticker_sizes_and_retags(monkeypatch):
    from app.edge_terminal import executor

    placed: list[dict[str, Any]] = []
    updates: list[tuple[str, tuple[Any, ...]]] = []

    async def _resolve(sig):
        return "KX-NBA-LAL-1"

    async def _bankroll(trading_mode="paper"):
        return 1000.0

    async def _place_order(ctx, **kwargs):
        placed.append({"ctx": ctx, "kwargs": kwargs})
        return {"trades_id": 4242, "status": "placed"}

    async def _execute(q, *args):
        updates.append((q, args))

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
    # ctx is RiskContext-shaped
    ctx = placed[0]["ctx"]
    assert ctx.market_id == "KX-NBA-LAL-1"
    assert ctx.side == "YES"
    assert ctx.intended_quantity in (99, 100)  # quarter-Kelly @ 0.50 (FP rounding)
    # Re-tag UPDATE fired against trades table.
    assert any(
        "UPDATE trades" in q and "edge_terminal" in q and args == (4242,)
        for q, args in updates
    )


def test_fire_one_no_ticker_returns_none(monkeypatch):
    from app.edge_terminal import executor

    async def _resolve(sig):
        return None

    monkeypatch.setattr(executor.market_resolver, "resolve", _resolve)
    out = _run(executor._fire_one({"id": 1}))
    assert out is None
