"""Tests for the round-2 audit gates (task #730):

* ``gate_sharpe_ratio``    — trailing realised-Sharpe floor.
* ``gate_ruin_monte_carlo``— bootstrap MC ruin-probability cap.
* ``gate_segment_throttler`` — per-(league, market_type) loss throttle.

Each gate is exercised through three contract paths:

1. **Cold start / missing data** — gate returns PASS so a fresh book
   isn't blocked by its own newness.
2. **Healthy book** — gate returns PASS with the diagnostic details
   wired through to the operator dashboard.
3. **Veto path** — gate returns FAIL with a structured reason that the
   pipeline event stream can render.

DB IO is stubbed by monkey-patching ``app.risk.gates.db`` so no Supabase
pool is required.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.risk import gates as _gates


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _isolate_event_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield
    loop.close()


def _make_ctx(
    *,
    vertical: str = "sports",
    league: str | None = "NFL",
    market_type: str | None = "moneyline",
    bankroll_usd: float | None = 1_000.0,
    intended_notional_usd: float = 25.0,
    overrides: dict | None = None,
) -> _gates.RiskContext:
    return _gates.RiskContext(
        market_id="TEST-T1",
        side="YES",
        vertical=vertical,
        league=league,
        market_type=market_type,
        matchup="A vs B",
        current_price=0.50,
        model_probability=0.60,
        edge=0.10,
        confidence_band_low=0.55,
        confidence_band_high=0.65,
        agreement_rate=0.7,
        swarm_approved=True,
        swarm_recommended_action="BUY_YES",
        debate_id=42,
        intended_quantity=50,
        intended_notional_usd=intended_notional_usd,
        bankroll_usd=bankroll_usd,
        open_positions_count=0,
        open_positions_notional_usd=0.0,
        open_positions_per_vertical_usd={},
        operator_overrides=overrides or {},
        market_cooldown_until=None,
    )


class _FakeDb:
    """Just enough of :mod:`app.db` to feed the round-2 gates.

    ``rows`` is the list returned by ``fetch``; ``count`` is what
    ``fetchval`` returns; either side raises an exception when set,
    letting tests exercise the "query degraded" PASS branch.
    """

    def __init__(
        self,
        *,
        rows: list[dict[str, Any]] | None = None,
        count: int | None = None,
        raise_on_fetch: Exception | None = None,
        raise_on_fetchval: Exception | None = None,
    ) -> None:
        self.rows = rows or []
        self.count = count
        self.raise_on_fetch = raise_on_fetch
        self.raise_on_fetchval = raise_on_fetchval
        self.fetch_calls: list[tuple] = []
        self.fetchval_calls: list[tuple] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.fetch_calls.append((sql, args))
        if self.raise_on_fetch is not None:
            raise self.raise_on_fetch
        return list(self.rows)

    async def fetchval(self, sql: str, *args: Any) -> int | None:
        self.fetchval_calls.append((sql, args))
        if self.raise_on_fetchval is not None:
            raise self.raise_on_fetchval
        return self.count


def _patch_db(monkeypatch, fake: _FakeDb) -> None:
    monkeypatch.setattr(_gates, "db", fake)


def _trade_rows(*returns_and_notionals: tuple[float, float]) -> list[dict[str, Any]]:
    """Build the (pnl, notional) row shape the gates' SELECT returns."""
    return [
        {"pnl": ret * notional, "notional": notional}
        for ret, notional in returns_and_notionals
    ]


# ---------------------------------------------------------------------------
# gate_sharpe_ratio
# ---------------------------------------------------------------------------


def test_sharpe_passes_when_history_below_min_samples(monkeypatch):
    """Cold start: 3 trades < default min_samples=10 → PASS."""
    fake = _FakeDb(rows=_trade_rows((0.1, 50), (-0.2, 50), (0.05, 50)))
    _patch_db(monkeypatch, fake)
    ctx = _make_ctx(overrides={"sharpe_min_ratio": 0.5})

    res = _run(_gates.gate_sharpe_ratio(ctx))

    assert res.name == "sharpe_ratio"
    assert res.passed is True
    assert "insufficient" in res.reason
    assert res.details["samples"] == 3
    assert res.details["threshold"] == 0.5


def test_sharpe_passes_with_healthy_returns(monkeypatch):
    """Healthy book: 12 mostly-winning trades → mean/stdev > 0 → PASS."""
    rows = _trade_rows(*([(0.10, 100)] * 8 + [(-0.05, 100)] * 4))
    fake = _FakeDb(rows=rows)
    _patch_db(monkeypatch, fake)
    ctx = _make_ctx(overrides={"sharpe_min_ratio": 0.0, "sharpe_min_samples": 5})

    res = _run(_gates.gate_sharpe_ratio(ctx))

    assert res.passed is True
    assert res.details["samples"] == 12
    assert res.details["sharpe"] > 0


def test_sharpe_vetoes_when_trailing_sharpe_decays(monkeypatch):
    """Decayed book: 12 mostly-losing trades drives Sharpe negative → VETO."""
    rows = _trade_rows(*([(-0.10, 100)] * 8 + [(0.05, 100)] * 4))
    fake = _FakeDb(rows=rows)
    _patch_db(monkeypatch, fake)
    ctx = _make_ctx(overrides={"sharpe_min_ratio": 0.0, "sharpe_min_samples": 5})

    res = _run(_gates.gate_sharpe_ratio(ctx))

    assert res.passed is False
    assert "Sharpe" in res.reason
    assert res.details["sharpe"] < 0
    assert res.details["threshold"] == 0.0


def test_sharpe_passes_on_zero_variance(monkeypatch):
    """All identical returns → stdev=0 → PASS (ratio is undefined)."""
    rows = _trade_rows(*([(0.05, 100)] * 12))
    fake = _FakeDb(rows=rows)
    _patch_db(monkeypatch, fake)
    ctx = _make_ctx(overrides={"sharpe_min_ratio": 5.0, "sharpe_min_samples": 5})

    res = _run(_gates.gate_sharpe_ratio(ctx))

    assert res.passed is True
    assert res.reason == "zero variance"
    assert res.details["stdev_return"] == 0


def test_sharpe_passes_on_query_failure(monkeypatch):
    """DB error must not kill the cycle — degrade to PASS."""
    fake = _FakeDb(raise_on_fetch=RuntimeError("supabase down"))
    _patch_db(monkeypatch, fake)

    res = _run(_gates.gate_sharpe_ratio(_make_ctx()))

    assert res.passed is True
    assert "degraded" in res.reason


# ---------------------------------------------------------------------------
# gate_ruin_monte_carlo
# ---------------------------------------------------------------------------


def test_ruin_passes_when_bankroll_missing(monkeypatch):
    fake = _FakeDb(rows=[])
    _patch_db(monkeypatch, fake)
    ctx = _make_ctx(bankroll_usd=None)

    res = _run(_gates.gate_ruin_monte_carlo(ctx))

    assert res.passed is True
    assert res.reason == "no bankroll snapshot"
    # Did NOT issue the trades query when bankroll is missing.
    assert fake.fetch_calls == []


def test_ruin_passes_when_history_below_min_samples(monkeypatch):
    """Cold start: 5 returns < default min_samples=20 → PASS."""
    rows = _trade_rows(*([(-0.50, 100)] * 5))
    fake = _FakeDb(rows=rows)
    _patch_db(monkeypatch, fake)

    res = _run(_gates.gate_ruin_monte_carlo(_make_ctx()))

    assert res.passed is True
    assert res.reason == "insufficient history"


def test_ruin_passes_for_safe_distribution(monkeypatch):
    """Mostly-winning history + large bankroll → P(ruin) < default 0.05."""
    rows = _trade_rows(*([(0.05, 100)] * 50 + [(-0.04, 100)] * 10))
    fake = _FakeDb(rows=rows)
    _patch_db(monkeypatch, fake)
    ctx = _make_ctx(
        bankroll_usd=10_000.0,
        intended_notional_usd=50.0,
        overrides={
            "ruin_max_probability": 0.05,
            "ruin_min_samples": 20,
            "ruin_paths": 500,        # tighter than default — still meaningful
            "ruin_horizon_trades": 50,
            "bankroll_floor_usd": 100.0,
        },
    )

    res = _run(_gates.gate_ruin_monte_carlo(ctx))

    assert res.passed is True
    assert res.details["p_ruin"] <= 0.05
    assert res.details["paths"] == 500
    assert res.details["horizon"] == 50


def test_ruin_vetoes_for_runaway_loss_distribution(monkeypatch):
    """Mostly-losing history + intended notional ~ bankroll → ruin
    inevitable inside horizon → VETO."""
    rows = _trade_rows(*([(-0.50, 100)] * 40 + [(0.10, 100)] * 5))
    fake = _FakeDb(rows=rows)
    _patch_db(monkeypatch, fake)
    ctx = _make_ctx(
        bankroll_usd=200.0,
        intended_notional_usd=200.0,
        overrides={
            "ruin_max_probability": 0.10,
            "ruin_min_samples": 20,
            "ruin_paths": 500,
            "ruin_horizon_trades": 50,
            "bankroll_floor_usd": 50.0,
        },
    )

    res = _run(_gates.gate_ruin_monte_carlo(ctx))

    assert res.passed is False
    assert "P(ruin)" in res.reason
    assert res.details["p_ruin"] > 0.10
    assert res.details["ruined_paths"] > 0


def test_ruin_passes_on_query_failure(monkeypatch):
    fake = _FakeDb(raise_on_fetch=RuntimeError("boom"))
    _patch_db(monkeypatch, fake)

    res = _run(_gates.gate_ruin_monte_carlo(_make_ctx()))

    assert res.passed is True
    assert "degraded" in res.reason


def test_ruin_clamps_runaway_path_count(monkeypatch):
    """Operator typo (paths=10_000_000) gets clamped to the 50k ceiling
    so the gate can't wedge a cycle."""
    rows = _trade_rows(*([(0.0, 100)] * 25))
    fake = _FakeDb(rows=rows)
    _patch_db(monkeypatch, fake)
    ctx = _make_ctx(
        overrides={"ruin_paths": 10_000_000, "ruin_min_samples": 5}
    )

    res = _run(_gates.gate_ruin_monte_carlo(ctx))

    assert res.details["paths"] == 50_000


# ---------------------------------------------------------------------------
# gate_segment_throttler
# ---------------------------------------------------------------------------


def test_segment_passes_without_league_or_market_type(monkeypatch):
    """No segment defined → PASS without DB IO."""
    fake = _FakeDb(count=999)
    _patch_db(monkeypatch, fake)
    ctx = _make_ctx(league=None, market_type=None)

    res = _run(_gates.gate_segment_throttler(ctx))

    assert res.passed is True
    assert res.reason == "no segment on candidate"
    assert fake.fetchval_calls == []


def test_segment_passes_when_losses_below_cap(monkeypatch):
    fake = _FakeDb(count=2)
    _patch_db(monkeypatch, fake)
    ctx = _make_ctx(overrides={"segment_max_losses": 3})

    res = _run(_gates.gate_segment_throttler(ctx))

    assert res.passed is True
    assert res.details["losses_in_window"] == 2
    assert res.details["max_losses"] == 3
    # Query parameters are correctly normalised (NFL upper / moneyline lower).
    assert fake.fetchval_calls
    args = fake.fetchval_calls[0][1]
    assert args[0] == "NFL"
    assert args[1] == "moneyline"


def test_segment_vetoes_when_losses_reach_cap(monkeypatch):
    fake = _FakeDb(count=3)
    _patch_db(monkeypatch, fake)
    ctx = _make_ctx(overrides={"segment_max_losses": 3, "segment_window_hours": 12.0})

    res = _run(_gates.gate_segment_throttler(ctx))

    assert res.passed is False
    assert "NFL/moneyline" in res.reason
    assert "3 losses" in res.reason
    assert res.details["losses_in_window"] == 3
    assert res.details["window_hours"] == 12.0


def test_segment_disabled_when_max_losses_zero(monkeypatch):
    fake = _FakeDb(count=999)
    _patch_db(monkeypatch, fake)
    ctx = _make_ctx(overrides={"segment_max_losses": 0})

    res = _run(_gates.gate_segment_throttler(ctx))

    assert res.passed is True
    assert res.reason == "disabled"
    # Disabled → skip the DB round-trip entirely.
    assert fake.fetchval_calls == []


def test_segment_passes_on_query_failure(monkeypatch):
    fake = _FakeDb(raise_on_fetchval=RuntimeError("supabase down"))
    _patch_db(monkeypatch, fake)

    res = _run(_gates.gate_segment_throttler(_make_ctx()))

    assert res.passed is True
    assert "degraded" in res.reason


# ---------------------------------------------------------------------------
# Registration invariant — the GATES tuple must keep these gates ordered
# between the model-side and sizing gates.  An accidental re-shuffle
# would change cycle-time DB cost dramatically.
# ---------------------------------------------------------------------------


def test_round2_gates_registered_between_agreement_and_kelly():
    names = [n for n, _ in _gates.GATES]
    assert "agreement" in names
    assert "kelly_fraction" in names
    i_agreement = names.index("agreement")
    i_kelly = names.index("kelly_fraction")
    for new in ("sharpe_ratio", "ruin_monte_carlo", "segment_throttler"):
        assert new in names, f"{new} missing from GATES"
        idx = names.index(new)
        assert i_agreement < idx < i_kelly, (
            f"{new} must sit between agreement ({i_agreement}) and "
            f"kelly_fraction ({i_kelly}); got {idx}"
        )
    # 17 from the round-2 audit + 2 added by task #743 (tail-risk policy
    # gates `tail_price_size_cap` + `jumbo_quantity_cap`).
    assert len(_gates.GATES) == 20
