"""Tests for the live-mode variant of ``gate_best_line`` (task #835).

Coverage:
  * When ``ctx.is_live`` is True the gate passes a 30s freshness
    budget into ``lookup_best_line`` (vs the 300s default for
    pre-game candidates).
  * The dedupe key emitted by ``_emit_best_line_alert`` is keyed on
    ``(kind, market_id)`` at a 5-minute TTL — repeat firings on the
    same market collapse to one alert.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from app.risk import best_line_lookup as _bll
from app.risk import gates as _gates


@pytest.fixture(autouse=True)
def _isolate_event_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield
    loop.close()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@dataclass
class _FakeQuote:
    book: str
    implied: float


@dataclass
class _FakeResult:
    quotes: list
    best_book: str | None
    best_implied: float | None
    best_traditional_book: str | None
    best_traditional_implied: float | None
    polymarket_implied: float | None
    error: str | None = None


def _make_ctx(*, is_live: bool, current_price: float = 0.55) -> _gates.RiskContext:
    return _gates.RiskContext(
        market_id="KX-NBA-LAL-BOS-ML",
        side="YES",
        vertical="sports",
        league="NBA",
        market_type="moneyline",
        matchup="LAL vs BOS",
        current_price=current_price,
        model_probability=0.60,
        edge=0.05,
        confidence_band_low=None,
        confidence_band_high=None,
        agreement_rate=None,
        swarm_approved=True,
        swarm_recommended_action="BUY_YES",
        debate_id=1,
        intended_quantity=100,
        intended_notional_usd=55.0,
        bankroll_usd=1_000.0,
        open_positions_count=0,
        open_positions_notional_usd=0.0,
        open_positions_per_vertical_usd={},
        operator_overrides={},
        market_cooldown_until=None,
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        is_live=is_live,
    )


def test_live_mode_uses_30s_freshness_window(monkeypatch):
    """Live candidates must shrink the lookup window to 30s.

    The 5-minute default would let a stale traditional-book quote
    fire a block on a live trade where every book has already
    re-priced past it; the live-loop variant exists to close that
    gap.
    """
    captured: dict[str, object] = {}

    async def _fake_lookup(**kwargs):
        captured.update(kwargs)
        return _FakeResult([], None, None, None, None, None)

    monkeypatch.setattr(_bll, "lookup_best_line", _fake_lookup)

    ctx = _make_ctx(is_live=True)
    _run(_gates.gate_best_line(ctx))
    assert captured.get("max_age_seconds") == 30

    captured.clear()
    ctx = _make_ctx(is_live=False)
    _run(_gates.gate_best_line(ctx))
    assert captured.get("max_age_seconds") == 300, (
        "pre-game candidates must keep the 5-minute window — only "
        "live trades get the tighter budget"
    )


def test_emit_alert_dedupes_by_market_id_and_kind(monkeypatch):
    """Task #835 dedupe contract: (kind, market_id) at 5 min TTL.

    A second firing for the same market within the TTL is a no-op
    and the alert helper is never invoked.
    """
    seen_keys: list[tuple[str, str, int]] = []
    claimed_keys: set[str] = set()

    async def _fake_dedupe(channel: str, key: str, *, ttl_seconds: int) -> bool:
        seen_keys.append((channel, key, ttl_seconds))
        if key in claimed_keys:
            return False
        claimed_keys.add(key)
        return True

    sent: list[dict] = []

    def _fake_alert(**kwargs):
        sent.append(kwargs)

    from app.notifications import alerts as _alerts

    monkeypatch.setattr(_alerts, "dedupe_or_skip", _fake_dedupe)
    monkeypatch.setattr(_alerts, "best_line_alert", _fake_alert)

    ctx = _make_ctx(is_live=True)
    _run(_gates._emit_best_line_alert(
        kind="block_traditional",
        ctx=ctx,
        details={"decision": "block_traditional_better"},
    ))
    _run(_gates._emit_best_line_alert(
        kind="block_traditional",
        ctx=ctx,
        details={"decision": "block_traditional_better"},
    ))

    # Both attempts hit the dedupe layer with the same key shape.
    assert len(seen_keys) == 2
    channel1, key1, ttl1 = seen_keys[0]
    channel2, key2, ttl2 = seen_keys[1]
    assert channel1 == "best_line_alert" == channel2
    assert ttl1 == 300 == ttl2, "dedupe TTL must be 5 minutes per task #835"
    assert key1 == key2, "same (kind, market_id) must produce a stable key"
    assert "block_traditional" in key1
    assert ctx.market_id in key1

    # Only the first claim wins — the helper fans out exactly once.
    assert len(sent) == 1

    # A different kind for the same market must produce a different key.
    _run(_gates._emit_best_line_alert(
        kind="reroute_polymarket",
        ctx=ctx,
        details={"decision": "reroute_polymarket"},
    ))
    assert seen_keys[-1][1] != key1
    assert len(sent) == 2
