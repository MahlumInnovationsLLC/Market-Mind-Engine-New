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
    """Scraper-supplied tickers are trusted (DOM truth)."""
    from app.edge_terminal import market_resolver

    async def _ticker_known(_q, *args, **kwargs):
        return 1

    async def _explode(*a, **k):
        raise AssertionError("db.fetch should not be called on direct path")

    monkeypatch.setattr(market_resolver.db, "fetchval", _ticker_known)
    monkeypatch.setattr(market_resolver.db, "fetch", _explode)
    out = _run(market_resolver.resolve({"kalshi_ticker": "KX-NBA-LAL-1"}))
    assert out == "KX-NBA-LAL-1"


def test_resolver_uses_series_prefix_by_vertical_pick(monkeypatch):
    """vertical=mlb + pick_type=moneyline → KXMLBGAME prefix only."""
    from app.edge_terminal import market_resolver

    captured: list[tuple[str, ...]] = []

    async def _fake_fetch(_q, *args, **kwargs):
        captured.append(args)
        return [{"ticker": "KXMLBGAME-26APR261415SEAOAK-OAK", "title": "Seattle vs Oakland Winner?"}]

    async def _fetchval(*a, **k):
        return None

    monkeypatch.setattr(market_resolver.db, "fetch", _fake_fetch)
    monkeypatch.setattr(market_resolver.db, "fetchval", _fetchval)
    out = _run(market_resolver.resolve({
        "vertical": "mlb", "pick_type": "moneyline",
        "matchup": "Seattle Mariners @ Oakland Athletics",
        "selection": "Athletics",
    }))
    assert out == "KXMLBGAME-26APR261415SEAOAK-OAK"
    assert captured and captured[0][0] == "KXMLBGAME-%"


def test_resolver_picks_team_match_in_same_series(monkeypatch):
    """Of two MLB games, pick the one whose title mentions both teams."""
    from app.edge_terminal import market_resolver

    rows = [
        {"ticker": "KXMLBGAME-XX-NYY", "title": "Yankees vs Royals Winner?"},
        {"ticker": "KXMLBGAME-26APR261415SEAOAK-OAK", "title": "Seattle vs Oakland Winner?"},
    ]

    async def _fetch(*a, **k):
        return rows

    async def _fetchval(*a, **k):
        return None

    monkeypatch.setattr(market_resolver.db, "fetch", _fetch)
    monkeypatch.setattr(market_resolver.db, "fetchval", _fetchval)
    out = _run(market_resolver.resolve({
        "vertical": "mlb", "pick_type": "moneyline",
        "matchup": "Seattle Mariners @ Oakland Athletics",
        "selection": "Athletics",
    }))
    assert out == "KXMLBGAME-26APR261415SEAOAK-OAK"


def test_resolver_total_uses_line_number_bonus(monkeypatch):
    """For totals, the numeric line should boost the right ticker."""
    from app.edge_terminal import market_resolver

    rows = [
        {"ticker": "KXMLBTOTAL-26APR261415SEAOAK-8", "title": "Seattle vs Oakland: Total Runs"},
        {"ticker": "KXMLBTOTAL-26APR261415SEAOAK-10", "title": "Seattle vs Oakland: Total Runs"},
    ]

    async def _fetch(*a, **k):
        return rows

    async def _fetchval(*a, **k):
        return None

    monkeypatch.setattr(market_resolver.db, "fetch", _fetch)
    monkeypatch.setattr(market_resolver.db, "fetchval", _fetchval)
    out = _run(market_resolver.resolve({
        "vertical": "mlb", "pick_type": "total",
        "matchup": "Seattle Mariners @ Oakland Athletics",
        "selection": "UNDER 10",
    }))
    assert out == "KXMLBTOTAL-26APR261415SEAOAK-10"


def test_resolver_returns_none_when_no_team_overlap(monkeypatch):
    """If no candidate mentions the picked team, return None."""
    from app.edge_terminal import market_resolver

    async def _fetch(*a, **k):
        return [{"ticker": "KXMLBGAME-XX-NYY", "title": "Yankees vs Royals Winner?"}]

    async def _fetchval(*a, **k):
        return None

    monkeypatch.setattr(market_resolver.db, "fetch", _fetch)
    monkeypatch.setattr(market_resolver.db, "fetchval", _fetchval)
    # Disable live fallback by stubbing the Kalshi client path.
    monkeypatch.setattr(market_resolver, "_live_candidates", lambda *a, **k: _noop_list())
    out = _run(market_resolver.resolve({
        "vertical": "mlb", "pick_type": "moneyline",
        "matchup": "Warriors @ Suns", "selection": "Warriors",
    }))
    assert out is None


async def _noop_list():
    return []


def test_resolver_unmapped_vertical_returns_none(monkeypatch):
    from app.edge_terminal import market_resolver

    async def _fetchval(*a, **k):
        return None

    async def _fetch(*a, **k):
        raise AssertionError("should not query cache when no prefix mapped")

    monkeypatch.setattr(market_resolver.db, "fetchval", _fetchval)
    monkeypatch.setattr(market_resolver.db, "fetch", _fetch)
    out = _run(market_resolver.resolve({
        "vertical": "obscure_sport", "pick_type": "moneyline",
        "matchup": "A vs B", "selection": "A",
    }))
    assert out is None


def test_resolver_db_error_returns_none(monkeypatch):
    """Cache-read DB errors must not crash — fall back to live, and if
    that also yields nothing, return None gracefully."""
    from app.edge_terminal import market_resolver

    async def _boom(*a, **k):
        raise RuntimeError("db down")

    async def _fetchval(*a, **k):
        return None

    monkeypatch.setattr(market_resolver.db, "fetch", _boom)
    monkeypatch.setattr(market_resolver.db, "fetchval", _fetchval)
    monkeypatch.setattr(market_resolver, "_live_candidates", lambda *a, **k: _noop_list())
    out = _run(market_resolver.resolve({
        "vertical": "mlb", "pick_type": "moneyline",
        "matchup": "Lakers @ Celtics", "selection": "Lakers",
    }))
    assert out is None


def test_resolve_match_returns_structured_result(monkeypatch):
    """resolve_match() is the new public surface used by the dashboard widget."""
    from app.edge_terminal import market_resolver

    rows = [
        {"ticker": "KXMLBGAME-26APR261415SEAOAK-OAK", "title": "Seattle vs Oakland Winner?"},
    ]

    async def _fetch(*a, **k):
        return rows

    async def _fetchval(*a, **k):
        return None

    monkeypatch.setattr(market_resolver.db, "fetch", _fetch)
    monkeypatch.setattr(market_resolver.db, "fetchval", _fetchval)
    out = _run(market_resolver.resolve_match({
        "vertical": "mlb", "pick_type": "moneyline",
        "matchup": "Seattle Mariners @ Oakland Athletics",
        "selection": "Athletics",
    }))
    assert out.ticker == "KXMLBGAME-26APR261415SEAOAK-OAK"
    assert "matched via cache" in out.reason
    assert out.candidates and out.candidates[0]["ticker"] == out.ticker


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


def test_fire_one_min_stake_fallback_when_edge_metadata_missing(monkeypatch):
    """Operator intent: every fresh Edge Terminal pick should produce a
    real Kalshi order, even when the scraper didn't capture
    model_prob/edge_pct/ev_pct (the source DOM frequently omits them).
    When Kelly collapses to zero purely because the edge math is
    uncomputable, the executor falls back to a single-contract stake so
    the signal lands as a placed trade instead of silently skipping."""
    from app.edge_terminal import executor

    placed: list[dict[str, Any]] = []

    async def _resolve(sig):
        return "KX-MLB-OU-1"

    async def _bankroll(trading_mode="paper"):
        return 1000.0

    async def _place_order(ctx, **kwargs):
        placed.append({"ctx": ctx, "kwargs": kwargs})
        return {"trades_id": 99, "status": "placed"}

    monkeypatch.setattr(executor.market_resolver, "resolve", _resolve)
    monkeypatch.setattr(executor.cycle_module, "_latest_bankroll", _bankroll)
    monkeypatch.setattr(executor.paper_module, "place_order", _place_order)
    monkeypatch.setattr(executor, "_already_placed", lambda *a, **k: _async_false())

    # No model_prob, edge_pct, or ev_pct — pure signal with a market price.
    sig = {
        "id": 1,
        "vertical": "sports",
        "matchup": "CIN @ NYM",
        "selection": "Over 8.5",
        "market_price": 0.55,
    }
    out = _run(executor._fire_one(sig))
    assert out is not None, "min-stake fallback must produce a placed trade"
    ctx = placed[0]["ctx"]
    assert ctx.intended_quantity == 1, "fallback stakes exactly one contract"
    assert placed[0]["kwargs"].get("placed_by") == "edge_terminal"


async def _async_false(*_a, **_k):
    return False


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


def test_signal_enrichment_fields_roundtrip_db(monkeypatch):
    """Task #845: ``pick_type``, ``avg_odds``, ``ev_pct``,
    ``source_screenshot_url`` and ``kalshi_market_id`` must survive
    the INSERT → SELECT round trip through ``edge_terminal_signals``
    so the operator dashboard can show pick type and screenshot
    evidence."""
    from app.edge_terminal import poller

    table: dict[int, dict[str, Any]] = {}
    next_id = {"n": 1}

    async def _fetchval(q: str, *args):
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
                "pick_type": args[11],
                "avg_odds": args[12],
                "ev_pct": args[13],
                "source_screenshot_url": args[14],
                "kalshi_market_id": args[15],
                "status": "pending",
            }
            table[next_id["n"]] = row
            next_id["n"] += 1
            return row["id"]
        return None

    monkeypatch.setattr(poller.db, "fetchval", _fetchval)

    _run(poller._insert_signal({
        "vertical": "sports",
        "matchup": "LAL @ BOS",
        "selection": "Lakers ML",
        "signal_date": "2026-05-22",
        "side": "YES",
        "edge_pct": 8.5,
        "model_prob": 0.58,
        "market_price": 0.50,
        "kalshi_ticker": "KX-NBA-LAL-BOS",
        "market_title": "Lakers vs Celtics",
        "pick_type": "moneyline",
        "avg_odds": -110.0,
        "ev_pct": 7.2,
        "source_screenshot_url": "https://example.com/tile.png",
        "kalshi_market_id": "00000000-0000-0000-0000-000000000abc",
        "raw": {},
    }))

    row = table[1]
    assert row["pick_type"] == "moneyline"
    assert row["avg_odds"] == -110.0
    assert row["ev_pct"] == 7.2
    assert row["source_screenshot_url"] == "https://example.com/tile.png"
    assert row["kalshi_market_id"] == "00000000-0000-0000-0000-000000000abc"


def test_signal_enrichment_fields_default_to_null_when_missing(monkeypatch):
    """The scraper leaves the new task #845 fields out when the source
    tile doesn't expose them; the poller must still INSERT cleanly."""
    from app.edge_terminal import poller

    captured: list[tuple[Any, ...]] = []

    async def _fetchval(q: str, *args):
        if "INSERT INTO edge_terminal_signals" in q:
            captured.append(args)
            return 1
        return None

    monkeypatch.setattr(poller.db, "fetchval", _fetchval)

    _run(poller._insert_signal({
        "vertical": "sports",
        "matchup": "LAL @ BOS",
        "selection": "Lakers",
        "signal_date": "2026-05-22",
        "raw": {},
    }))

    assert captured, "poller must have called the INSERT"
    args = captured[0]
    # Indices 11..15 correspond to the new pick_type / avg_odds /
    # ev_pct / source_screenshot_url / kalshi_market_id columns.
    assert args[11] is None
    assert args[12] is None
    assert args[13] is None
    assert args[14] is None
    assert args[15] is None


def test_next_interval_is_within_jitter_window():
    """Sleep interval must be uniformly random in [MIN, MAX]."""
    from app.edge_terminal import poller
    samples = [poller._next_interval_seconds() for _ in range(200)]
    assert all(poller.POLL_INTERVAL_MIN_SECONDS <= s <= poller.POLL_INTERVAL_MAX_SECONDS
               for s in samples)
    # And not constant — vanishingly small chance of all 200 equal.
    assert len(set(round(s, 3) for s in samples)) > 5


def test_active_window_includes_mountain_4am_to_10pm(monkeypatch):
    """4:00 MT inclusive → active; 22:00 MT exclusive → inactive."""
    from datetime import datetime, timezone
    from app.edge_terminal import poller

    # Build naive local-MT datetimes and convert to UTC for the check.
    def mt(hour: int, minute: int = 0) -> datetime:
        # America/Denver: -07:00 in summer (MDT), -06:00 in winter (MST).
        # Use a fixed DST-stable summer date so the offset is predictable;
        # _in_active_window does its own zone conversion anyway.
        local = datetime(2026, 7, 15, hour, minute, tzinfo=poller.ACTIVE_TZ)
        return local.astimezone(timezone.utc)

    assert poller._in_active_window(mt(4, 0)) is True       # boundary open
    assert poller._in_active_window(mt(12, 0)) is True
    assert poller._in_active_window(mt(21, 59)) is True
    assert poller._in_active_window(mt(22, 0)) is False     # boundary close
    assert poller._in_active_window(mt(3, 59)) is False
    assert poller._in_active_window(mt(0, 0)) is False
    assert poller._in_active_window(mt(23, 30)) is False


def test_fire_one_no_ticker_returns_none(monkeypatch):
    from app.edge_terminal import executor

    async def _resolve(sig):
        return None

    monkeypatch.setattr(executor.market_resolver, "resolve", _resolve)
    out = _run(executor._fire_one({"id": 1}))
    assert out is None
