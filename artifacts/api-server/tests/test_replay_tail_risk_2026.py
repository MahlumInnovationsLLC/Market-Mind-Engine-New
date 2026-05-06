"""Replay-harness tests for `scripts/replay_tail_risk_2026.py`
(task #745).

Synthetic 2026-shaped trade cohort exercises the full GATES tuple
plus the headline counterfactual-savings math.  The cohort is
intentionally small (12 rows) and self-contained so the test asserts
exact dollar numbers without touching a real DB.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from scripts import replay_tail_risk_2026 as replay
from app.risk import gates as _gates


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _isolate_event_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield
    loop.close()


# ---------------------------------------------------------------------------
# Cohort fixture — every row encodes one audit pattern.
# ---------------------------------------------------------------------------


def _cohort() -> list[replay.TradeRow]:
    return [
        # 1. The Feb-20 NBA trade — the single biggest blow-up in the
        #    audit. Tail gate must reject; jumbo gate must reject too.
        replay.TradeRow(
            trade_id="feb20-bos-gsw",
            market_id="KXNBA-FEB20-BOSGSW-BOS",
            side="NO", vertical="sports", league="NBA",
            quantity=4_394, price=0.04, pnl=-4_218.24,
            model_edge=0.06,
        ),
        # 2-4. Three more deep-OTM losers (sports). Each tripped the
        #      tail gate because qty * (1-price) > $400 max-loss cap.
        replay.TradeRow(
            trade_id="otm-loser-1", market_id="KXNFL-A",
            side="NO", vertical="sports", league="NFL",
            quantity=1_500, price=0.05, pnl=-1_425.0, model_edge=0.05,
        ),
        replay.TradeRow(
            trade_id="otm-loser-2", market_id="KXNBA-B",
            side="YES", vertical="sports", league="NBA",
            quantity=12_000, price=0.04, pnl=-480.0, model_edge=0.04,
        ),
        replay.TradeRow(
            trade_id="otm-loser-3", market_id="KXMLB-C",
            side="NO", vertical="sports", league="MLB",
            quantity=900, price=0.06, pnl=-846.0, model_edge=0.06,
        ),
        # 5. Deep-OTM crypto winner (the audit shows crypto deep-OTM is
        #    roughly flat, so this is the false-positive bucket the
        #    per-vertical override is supposed to address).
        replay.TradeRow(
            trade_id="otm-crypto-winner", market_id="KXBTCD-DAY",
            side="YES", vertical="crypto", league="BTC",
            quantity=12_000, price=0.04, pnl=+360.0, model_edge=0.05,
        ),
        # 6. Jumbo loser at a normal price — only the jumbo gate fires
        #    (price 0.50, max-loss = $1,000).  Tail gate sees min(0.50,
        #    0.50)=0.50 > 0.10 threshold → tail PASSES, jumbo VETOES.
        replay.TradeRow(
            trade_id="jumbo-loser", market_id="KXNFL-D",
            side="YES", vertical="sports", league="NFL",
            quantity=2_000, price=0.50, pnl=-700.0, model_edge=0.04,
        ),
        # 7. Jumbo "winner" at qty=900 — only the jumbo gate fires
        #    (price 0.55 → tail gate passes).
        replay.TradeRow(
            trade_id="jumbo-winner", market_id="KXNFL-E",
            side="YES", vertical="sports", league="NFL",
            quantity=900, price=0.55, pnl=+90.0, model_edge=0.04,
        ),
        # 8-10. Good favorites in the $0.60-$0.90 band — the bot's
        #       working sleeve. All small qty, must PASS both gates.
        replay.TradeRow(
            trade_id="fav-w-1", market_id="KXNFL-F",
            side="YES", vertical="sports", league="NFL",
            quantity=200, price=0.72, pnl=+25.0, model_edge=0.05,
        ),
        replay.TradeRow(
            trade_id="fav-w-2", market_id="KXNBA-G",
            side="YES", vertical="sports", league="NBA",
            quantity=150, price=0.80, pnl=+18.0, model_edge=0.04,
        ),
        replay.TradeRow(
            trade_id="fav-l-1", market_id="KXMLB-H",
            side="YES", vertical="sports", league="MLB",
            quantity=100, price=0.65, pnl=-65.0, model_edge=0.04,
        ),
        # 11. Open trade (pnl is None) — included to exercise the
        #     "settled-only" math in the summary.
        replay.TradeRow(
            trade_id="open-bet", market_id="KXNFL-I",
            side="YES", vertical="sports", league="NFL",
            quantity=50, price=0.55, pnl=None, model_edge=0.04,
        ),
        # 12. Tail-priced crypto NO at $0.97 entry — exercises NO-side
        #     max-loss math symmetrically with row 1.
        replay.TradeRow(
            trade_id="otm-crypto-no", market_id="KXBTC-WEEK",
            side="NO", vertical="crypto", league="BTC",
            quantity=600, price=0.03, pnl=-580.0, model_edge=0.05,
        ),
    ]


# ---------------------------------------------------------------------------
# Per-trade veto wiring (default mode = full GATES tuple)
# ---------------------------------------------------------------------------


def test_feb20_nba_row_attributed_to_tail_gate():
    rows, _ = _run(replay.replay(_cohort()))
    feb = next(r for r in rows if r["trade_id"] == "feb20-bos-gsw")
    assert feb["tail_risk_vetoed"] is True
    assert feb["tail_risk_attrib"] == "tail_price_size_cap"
    # In full-GATES mode the tail gate is the first veto in the
    # tuple too — kill_switch / daily_loss / etc. are all bypassed
    # by the replay overrides.
    assert feb["first_veto_gate"] == "tail_price_size_cap"


def test_good_favorites_pass_full_gate_stack():
    """Mid-priced, small-qty favorites must clear every gate even in
    full-stack mode — the replay overrides keep the
    state-dependent gates from spuriously vetoing them."""
    rows, _ = _run(replay.replay(_cohort()))
    for tid in ("fav-w-1", "fav-w-2", "fav-l-1", "open-bet"):
        r = next(r for r in rows if r["trade_id"] == tid)
        assert r["tail_risk_vetoed"] is False, f"{tid} tail-vetoed: {r}"
        assert r["first_veto_gate"] == "", f"{tid} also had: {r}"


def test_jumbo_winner_attributed_to_jumbo_gate():
    rows, summary = _run(replay.replay(_cohort()))
    jw = next(r for r in rows if r["trade_id"] == "jumbo-winner")
    assert jw["tail_risk_vetoed"] is True
    assert jw["tail_risk_attrib"] == "jumbo_quantity_cap"
    assert summary["by_gate"]["jumbo_quantity_cap"][
        "profit_forfeited_usd"
    ] == pytest.approx(90.0)


# ---------------------------------------------------------------------------
# Headline counterfactual numbers
# ---------------------------------------------------------------------------


def test_summary_quantifies_audit_savings():
    rows, summary = _run(replay.replay(_cohort()))
    assert summary["mode"] == "full_gates"
    assert summary["trades_total"] == 12
    assert summary["trades_settled"] == 11

    expected_audit_loss = (
        4_218.24 + 1_425.0 + 480.0 + 846.0 + 700.0 + 65.0 + 580.0
    )
    assert summary["audit_total_loss_usd"] == pytest.approx(
        expected_audit_loss, abs=0.01
    )
    expected_prevented = expected_audit_loss - 65.0
    assert summary["loss_prevented_usd"] == pytest.approx(
        expected_prevented, abs=0.01
    )
    expected_pct = 100.0 * expected_prevented / expected_audit_loss
    assert summary["pct_audit_losses_prevented"] == pytest.approx(
        expected_pct, abs=0.01
    )
    assert summary["pct_audit_losses_prevented"] > 95.0
    assert summary["profit_forfeited_usd"] == pytest.approx(450.0)
    assert summary["counterfactual_delta_usd"] == pytest.approx(
        expected_prevented - 450.0, abs=0.01
    )
    assert summary["by_gate"]["tail_price_size_cap"]["vetoed"] >= 4
    assert summary["by_gate"]["jumbo_quantity_cap"]["vetoed"] >= 1


def test_tail_only_mode_matches_full_mode_for_tail_attribution():
    """`--tail-only` skips the rest of the gate stack but the
    headline tail-risk attribution numbers must match full-gates
    mode (the full stack only adds *additional* veto reasons; it
    must not change which trades the tail/jumbo gates catch)."""
    rows_full, sum_full = _run(replay.replay(_cohort()))
    rows_tail, sum_tail = _run(replay.replay(_cohort(), tail_only=True))
    assert sum_tail["mode"] == "tail_only"
    assert sum_full["loss_prevented_usd"] == sum_tail["loss_prevented_usd"]
    assert sum_full["profit_forfeited_usd"] == sum_tail["profit_forfeited_usd"]
    assert sum_full["trades_vetoed"] == sum_tail["trades_vetoed"]
    full_attrib = {
        r["trade_id"]: r["tail_risk_attrib"] for r in rows_full
    }
    tail_attrib = {
        r["trade_id"]: r["tail_risk_attrib"] for r in rows_tail
    }
    assert full_attrib == tail_attrib


# ---------------------------------------------------------------------------
# Side-effect suppression — replay must not fan out alerts or write
# to risk_gate_trips.
# ---------------------------------------------------------------------------


def test_replay_suppresses_alerts_and_persistence(monkeypatch):
    alert_calls: list[tuple] = []
    persist_calls: list[tuple] = []

    async def _spy_alert(**kwargs):
        alert_calls.append(("alert", kwargs))

    async def _spy_persist(**kwargs):
        persist_calls.append(("persist", kwargs))

    monkeypatch.setattr(_gates, "_emit_tail_risk_trip_alert", _spy_alert)
    monkeypatch.setattr(_gates, "_persist_risk_gate_trip", _spy_persist)

    _run(replay.replay(_cohort()))

    assert alert_calls == [], (
        f"replay leaked tail-risk alerts: {alert_calls}"
    )
    assert persist_calls == [], (
        f"replay leaked risk_gate_trips writes: {persist_calls}"
    )

    # And the originals are restored after the context manager exits.
    assert _gates._emit_tail_risk_trip_alert is _spy_alert
    assert _gates._persist_risk_gate_trip is _spy_persist


# ---------------------------------------------------------------------------
# Per-vertical override threading
# ---------------------------------------------------------------------------


def test_crypto_per_vertical_override_loosens_tail_gate():
    """Tightening the crypto threshold to 0.01 lets the otm-crypto
    winner (price=0.04) through.  Confirms the script honours the
    same per-vertical override knobs as the gates themselves."""
    rows, _ = _run(replay.replay(
        _cohort(),
        overrides={
            "tail_price_threshold": 0.10,
            "tail_price_threshold.crypto": 0.01,
            "jumbo_quantity_cap.crypto": 50_000,
        },
    ))
    cw = next(r for r in rows if r["trade_id"] == "otm-crypto-winner")
    assert cw["tail_risk_vetoed"] is False, cw


# ---------------------------------------------------------------------------
# JSON ingest path used by --from-json
# ---------------------------------------------------------------------------


def test_load_from_json_round_trip(tmp_path: Path):
    payload = [
        {
            "trade_id": "x1", "market_id": "M1", "side": "no",
            "vertical": "sports", "league": "nba",
            "quantity": 4_394, "price": 0.04, "pnl": -4_218.0,
        },
        {
            "trade_id": "x2", "market_id": "M2", "side": "YES",
            "vertical": "sports", "league": "NFL",
            "quantity": 100, "price": 0.55, "pnl": None,
            "model_edge": 0.07,
        },
    ]
    p = tmp_path / "trades.json"
    p.write_text(json.dumps(payload))
    rows = replay._load_from_json(p)
    assert len(rows) == 2
    assert rows[0].side == "NO"
    assert rows[0].league == "NBA"
    assert rows[1].pnl is None
    assert rows[1].model_edge == 0.07


# ---------------------------------------------------------------------------
# CLI override parser
# ---------------------------------------------------------------------------


def test_parse_overrides_coerces_numerics():
    out = replay._parse_overrides([
        "tail_price_threshold=0.05",
        "jumbo_quantity_cap=1500",
        "max_tail_loss_usd.crypto=2000.0",
        "trading_mode=paper",
    ])
    assert out == {
        "tail_price_threshold": 0.05,
        "jumbo_quantity_cap": 1500,
        "max_tail_loss_usd.crypto": 2000.0,
        "trading_mode": "paper",
    }
