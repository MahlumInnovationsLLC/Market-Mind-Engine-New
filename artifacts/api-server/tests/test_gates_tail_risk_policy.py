"""Tests for the tail-risk policy gates (task #743):

* ``gate_tail_price_size_cap`` — caps notional and max-loss exposure
  on markets where one side is trading deep in the tail
  (``min(price, 1−price) < tail_price_threshold``).
* ``gate_jumbo_quantity_cap`` — hard ceiling on contract count.

These gates are no-DB / pure functions of ``RiskContext``; the
forensic-audit replay test reconstructs the Feb-20 NBA Boston/GSW NO
trade (qty=4,394, YES_price=$0.04, side=NO) and asserts
``gate_tail_price_size_cap`` would have rejected it before placement.
"""
from __future__ import annotations

import asyncio

import pytest

from app.risk import gates as _gates


# ---------------------------------------------------------------------------
# Helpers (kept independent from test_gates_round2_audit.py so a refactor
# of either file doesn't cascade into the other).
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
    side: str = "YES",
    vertical: str = "sports",
    current_price: float | None = 0.50,
    intended_quantity: int = 50,
    intended_notional_usd: float | None = None,
    overrides: dict | None = None,
) -> _gates.RiskContext:
    if intended_notional_usd is None:
        intended_notional_usd = intended_quantity * float(current_price or 0.0)
    return _gates.RiskContext(
        market_id="TEST-T1",
        side=side,
        vertical=vertical,
        league="NFL",
        market_type="moneyline",
        matchup="A vs B",
        current_price=current_price,
        model_probability=0.60,
        edge=0.10,
        confidence_band_low=0.55,
        confidence_band_high=0.65,
        agreement_rate=0.7,
        swarm_approved=True,
        swarm_recommended_action="BUY_YES" if side == "YES" else "BUY_NO",
        debate_id=42,
        intended_quantity=intended_quantity,
        intended_notional_usd=intended_notional_usd,
        bankroll_usd=1_000.0,
        open_positions_count=0,
        open_positions_notional_usd=0.0,
        open_positions_per_vertical_usd={},
        operator_overrides=overrides or {},
        market_cooldown_until=None,
    )


# ---------------------------------------------------------------------------
# gate_tail_price_size_cap
# ---------------------------------------------------------------------------


def test_tail_price_passes_when_price_in_normal_range():
    """Mid-range price (0.50) — neither leg is in the tail → PASS."""
    ctx = _make_ctx(current_price=0.50, intended_quantity=10_000)
    res = _run(_gates.gate_tail_price_size_cap(ctx))
    assert res.name == "tail_price_size_cap"
    assert res.passed is True
    assert res.details["tail_skew"] == 0.50
    assert res.details["threshold"] == 0.10


def test_tail_price_passes_when_threshold_zero_disables():
    """``tail_price_threshold=0`` → operator opt-out → PASS even on a
    deep-OTM YES trade."""
    ctx = _make_ctx(
        current_price=0.02,
        intended_quantity=10_000,
        overrides={"tail_price_threshold": 0},
    )
    res = _run(_gates.gate_tail_price_size_cap(ctx))
    assert res.passed is True
    assert res.reason == "disabled"


def test_tail_price_passes_when_current_price_missing():
    """Missing tick can't kill a cycle — PASS with explicit reason."""
    ctx = _make_ctx(current_price=None, intended_quantity=10_000)
    res = _run(_gates.gate_tail_price_size_cap(ctx))
    assert res.passed is True
    assert res.reason == "no current_price"


def test_tail_price_passes_when_within_caps():
    """Deep-OTM but tiny size — both caps respected → PASS."""
    ctx = _make_ctx(
        side="YES",
        current_price=0.05,
        intended_quantity=100,
        # YES at $0.05 × 100 = $5 notional, $5 max-loss → far under
        # the default $400 caps.
    )
    res = _run(_gates.gate_tail_price_size_cap(ctx))
    assert res.passed is True
    assert res.details["max_loss_usd"] == pytest.approx(5.0)
    assert res.details["entry_price"] == pytest.approx(0.05)


def test_tail_price_vetoes_deep_otm_yes_when_max_loss_over_cap():
    """YES at $0.05 × 10,000 = $500 max-loss; default cap is $400."""
    ctx = _make_ctx(
        side="YES",
        current_price=0.05,
        intended_quantity=10_000,
    )
    res = _run(_gates.gate_tail_price_size_cap(ctx))
    assert res.passed is False
    assert "max-loss" in res.reason
    assert "YES entry $0.050" in res.reason
    assert res.details["entry_price"] == pytest.approx(0.05)
    assert res.details["max_loss_usd"] == pytest.approx(500.0)
    assert res.details["max_loss_cap_usd"] == pytest.approx(400.0)


def test_tail_price_vetoes_no_side_with_side_aware_max_loss():
    """NO trade at YES_price=$0.04 → entry_price=$0.96; with qty=500
    the max-loss is $480 > default $400 cap, even though notional
    (qty × YES_price) is only $20 — confirms the side-aware math."""
    ctx = _make_ctx(
        side="NO",
        current_price=0.04,
        intended_quantity=500,
        intended_notional_usd=500 * 0.04,
    )
    res = _run(_gates.gate_tail_price_size_cap(ctx))
    assert res.passed is False
    assert "NO entry $0.960" in res.reason
    assert res.details["entry_price"] == pytest.approx(0.96)
    assert res.details["max_loss_usd"] == pytest.approx(480.0)
    # Notional alone wouldn't have tripped the gate (only $20).
    assert res.details["intended_notional_usd"] == pytest.approx(20.0)


def test_tail_price_vetoes_when_notional_exceeds_cap_even_if_max_loss_ok():
    """A deep-OTM YES with large notional still gets caught — exercises
    the second cap path independently from max-loss."""
    ctx = _make_ctx(
        side="YES",
        current_price=0.05,
        intended_quantity=2_000,           # $100 notional, $100 max-loss
        overrides={
            # Loosen max-loss cap so it doesn't fire; tighten notional
            # cap so THAT cap fires alone.
            "max_tail_loss_usd": 1_000.0,
            "max_tail_notional_usd": 50.0,
        },
    )
    res = _run(_gates.gate_tail_price_size_cap(ctx))
    assert res.passed is False
    assert "notional" in res.reason
    assert res.details["intended_notional_usd"] == pytest.approx(100.0)


def test_tail_price_max_loss_cap_zero_disables_max_loss_subcheck_only():
    """Setting `max_tail_loss_usd=0` skips the max-loss path but the
    notional sub-check still fires.  Lets an operator opt out of the
    side-aware exposure cap without disabling the gate entirely."""
    ctx = _make_ctx(
        side="YES",
        current_price=0.05,
        intended_quantity=20_000,            # $1,000 notional, $1,000 max-loss
        overrides={
            "max_tail_loss_usd": 0,           # disable max-loss sub-check
            "max_tail_notional_usd": 100.0,   # keep notional sub-check tight
        },
    )
    res = _run(_gates.gate_tail_price_size_cap(ctx))
    assert res.passed is False
    assert "notional" in res.reason
    assert res.details["max_loss_cap_usd"] == 0


def test_tail_price_notional_cap_zero_disables_notional_subcheck_only():
    """Setting `max_tail_notional_usd=0` skips the notional path but
    the max-loss path still fires."""
    ctx = _make_ctx(
        side="NO",
        current_price=0.04,
        intended_quantity=500,
        intended_notional_usd=500 * 0.04,
        overrides={
            "max_tail_loss_usd": 100.0,       # 480 > 100 → fires
            "max_tail_notional_usd": 0,       # disable notional sub-check
        },
    )
    res = _run(_gates.gate_tail_price_size_cap(ctx))
    assert res.passed is False
    assert "max-loss" in res.reason
    assert res.details["max_notional_cap_usd"] == 0


def test_tail_price_both_subcheck_caps_zero_disables_gate_entirely():
    """Both caps zero → gate is effectively a no-op once tripped."""
    ctx = _make_ctx(
        side="YES",
        current_price=0.05,
        intended_quantity=999_999,
        overrides={
            "max_tail_loss_usd": 0,
            "max_tail_notional_usd": 0,
        },
    )
    res = _run(_gates.gate_tail_price_size_cap(ctx))
    assert res.passed is True


def test_tail_price_per_vertical_loss_cap_overrides_global():
    """Per-vertical max_tail_loss_usd override (sports stricter than
    the global default) is honored."""
    ctx = _make_ctx(
        vertical="sports",
        side="YES",
        current_price=0.05,
        intended_quantity=2_000,             # $100 max-loss
        overrides={
            "max_tail_loss_usd": 1_000.0,        # global loose
            "max_tail_loss_usd.sports": 50.0,    # sports tight
        },
    )
    res = _run(_gates.gate_tail_price_size_cap(ctx))
    assert res.passed is False
    assert res.details["max_loss_cap_usd"] == 50.0


def test_tail_price_per_vertical_override_loosens_crypto():
    """Crypto-deep-OTM was flat in the audit — operator can loosen the
    threshold per vertical."""
    ctx = _make_ctx(
        vertical="crypto",
        side="YES",
        current_price=0.04,
        intended_quantity=10_000,
        overrides={
            "tail_price_threshold": 0.10,            # global default
            "tail_price_threshold.crypto": 0.01,     # crypto: only the
                                                     # *very* deep tail
        },
    )
    res = _run(_gates.gate_tail_price_size_cap(ctx))
    assert res.passed is True
    assert res.details["threshold"] == 0.01


def test_tail_price_passes_for_high_yes_price_when_buying_yes():
    """YES@$0.96 is the favorite SIDE — buying YES at the favorite
    leaves the underdog (NO@$0.04) in the tail.  Buying YES means
    entry_price=$0.96, max-loss is huge per contract — qty=10,000
    gives $9,600 max-loss, well over the $400 cap."""
    ctx = _make_ctx(
        side="YES",
        current_price=0.96,
        intended_quantity=10_000,
    )
    res = _run(_gates.gate_tail_price_size_cap(ctx))
    assert res.passed is False
    assert res.details["tail_skew"] == pytest.approx(0.04)
    assert res.details["entry_price"] == pytest.approx(0.96)


# ---------------------------------------------------------------------------
# gate_jumbo_quantity_cap
# ---------------------------------------------------------------------------


def test_jumbo_passes_under_default_cap():
    ctx = _make_ctx(intended_quantity=500)
    res = _run(_gates.gate_jumbo_quantity_cap(ctx))
    assert res.name == "jumbo_quantity_cap"
    assert res.passed is True
    assert res.details["cap"] == 750
    assert res.details["intended_quantity"] == 500


def test_jumbo_passes_at_exactly_default_cap():
    """Cap is inclusive (==750) per spec — only `>` rejects."""
    ctx = _make_ctx(intended_quantity=750)
    res = _run(_gates.gate_jumbo_quantity_cap(ctx))
    assert res.passed is True


def test_jumbo_vetoes_above_default_cap():
    ctx = _make_ctx(intended_quantity=4_394)  # the audit's worst trade
    res = _run(_gates.gate_jumbo_quantity_cap(ctx))
    assert res.passed is False
    assert "4394" in res.reason
    assert "750" in res.reason


def test_jumbo_per_vertical_override_loosens_crypto():
    ctx = _make_ctx(
        vertical="crypto",
        intended_quantity=2_000,
        overrides={
            "jumbo_quantity_cap": 750,            # global
            "jumbo_quantity_cap.crypto": 5_000,   # crypto-only loosen
        },
    )
    res = _run(_gates.gate_jumbo_quantity_cap(ctx))
    assert res.passed is True
    assert res.details["cap"] == 5_000


def test_jumbo_disabled_when_cap_zero():
    ctx = _make_ctx(
        intended_quantity=10_000,
        overrides={"jumbo_quantity_cap": 0},
    )
    res = _run(_gates.gate_jumbo_quantity_cap(ctx))
    assert res.passed is True
    assert res.reason == "disabled"


# ---------------------------------------------------------------------------
# Registration invariants
# ---------------------------------------------------------------------------


def test_tail_risk_gates_registered_before_round2_audit_gates():
    """Cheap no-DB caps must run BEFORE the DB-touching round-2 gates
    so we don't pay query cost on candidates a deep-OTM/jumbo cap
    would reject.  Keep the round-2 gates' position invariant
    (between agreement and kelly_fraction) intact."""
    names = [n for n, _ in _gates.GATES]
    assert "tail_price_size_cap" in names
    assert "jumbo_quantity_cap" in names
    i_agreement = names.index("agreement")
    i_tail = names.index("tail_price_size_cap")
    i_jumbo = names.index("jumbo_quantity_cap")
    i_sharpe = names.index("sharpe_ratio")
    i_kelly = names.index("kelly_fraction")
    assert i_agreement < i_tail < i_sharpe < i_kelly
    assert i_agreement < i_jumbo < i_sharpe < i_kelly


# ---------------------------------------------------------------------------
# Feb-20 NBA Boston/GSW replay — the single trade that motivated this
# entire policy.  Reconstructs the RiskContext exactly as the orchestrator
# would have built it that night and asserts the new gates would have
# rejected the candidate before placement.
#
# Forensic numbers (from the YTD 2026 audit query against the `trades`
# table, placed_by IN orchestrator/autotrader/swarm/operator_recovery):
#
#   trade_id     ~ Feb-20 NBA Boston/GSW
#   side         = NO
#   YES_price    = $0.04   (so NO entry price = $0.96)
#   quantity     = 4,394
#   pnl          = −$4,218
#
# Side-aware max-loss = qty × (1 − YES_price) = 4,394 × $0.96 ≈ $4,218,
# which matches the realised loss to the cent.  Either gate (tail or
# jumbo) would have caught it; both are exercised below.
# ---------------------------------------------------------------------------


def _feb_20_nba_ctx() -> _gates.RiskContext:
    return _gates.RiskContext(
        market_id="KXNBAGAME-26FEB20BOSGSW-BOS",
        side="NO",
        vertical="sports",
        league="NBA",
        market_type="moneyline",
        matchup="BOS@GSW",
        current_price=0.04,
        model_probability=0.10,
        edge=0.06,
        confidence_band_low=0.06,
        confidence_band_high=0.16,
        agreement_rate=0.6,
        swarm_approved=True,
        swarm_recommended_action="BUY_NO",
        debate_id=99_999,
        intended_quantity=4_394,
        intended_notional_usd=4_394 * 0.04,
        bankroll_usd=10_000.0,
        open_positions_count=0,
        open_positions_notional_usd=0.0,
        open_positions_per_vertical_usd={},
        operator_overrides={},
        market_cooldown_until=None,
    )


def test_feb20_nba_rejected_by_tail_price_size_cap():
    """The single trade that motivated task #743 — must reject."""
    ctx = _feb_20_nba_ctx()
    res = _run(_gates.gate_tail_price_size_cap(ctx))
    assert res.passed is False
    # Side-aware max-loss math matches the realised loss to the cent.
    assert res.details["entry_price"] == pytest.approx(0.96)
    assert res.details["max_loss_usd"] == pytest.approx(4_394 * 0.96)
    assert res.details["max_loss_usd"] > 4_000  # well over default $400 cap


def test_feb20_nba_rejected_by_jumbo_quantity_cap():
    """Belt-and-suspenders: even with the tail gate disabled, the
    jumbo-qty cap (default 750) still catches the 4,394-contract size."""
    ctx = _feb_20_nba_ctx()
    res = _run(_gates.gate_jumbo_quantity_cap(ctx))
    assert res.passed is False
    assert res.details["intended_quantity"] == 4_394
    assert res.details["cap"] == 750


def test_feb20_nba_rejected_by_run_all_gates_pipeline():
    """End-to-end through ``run_all_gates``: the cycle short-circuits at
    the tail gate (the first new gate after `agreement`) and never
    reaches the DB-touching round-2 / sizing gates."""
    ctx = _feb_20_nba_ctx()
    events: list[dict] = []

    async def _capture(ev: dict) -> None:
        events.append(ev)

    passed, results = _run(_gates.run_all_gates(ctx, on_event=_capture))
    assert passed is False
    assert results[-1].name == "tail_price_size_cap"
    assert results[-1].passed is False
    # Must have short-circuited BEFORE the round-2 / sizing gates.
    seen = {r.name for r in results}
    for must_not_run in (
        "sharpe_ratio",
        "ruin_monte_carlo",
        "segment_throttler",
        "kelly_fraction",
        "bankroll_floor",
        "global_exposure_cap",
    ):
        assert must_not_run not in seen, (
            f"{must_not_run} ran despite tail gate veto — "
            "ordering invariant broken"
        )
    # Pipeline event stream got every result up to (and including) the
    # vetoing gate.
    assert events[-1]["gate"] == "tail_price_size_cap"
    assert events[-1]["passed"] is False
