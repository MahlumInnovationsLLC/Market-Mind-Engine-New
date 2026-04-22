"""Regression tests for the per-league persona-weight resolver (Task #100).

The resolver must:
* Prefer rows whose `last_updated` falls in the last 90 days.
* Fall back to all-time rows when no recent metrics exist for a key.
* Honor lookup precedence: (analyst, league, market_type) >
  (analyst, league, None) > (analyst, None, None) > neutral 0.50.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.swarm import weights as w


class _Row(dict):
    pass


def _row(
    analyst: str,
    league: str | None,
    mt: str | None,
    brier: float,
    recent: bool,
    vertical: str | None = None,
) -> _Row:
    return _Row(
        analyst_id=analyst,
        vertical=vertical,
        league=league,
        market_type=mt,
        brier_score=brier,
        total_predictions=10,
        recent=recent,
    )


def _patch_rows(monkeypatch, rows: list[_Row]) -> None:
    async def fake_fetch(_sql: str, *_params: Any) -> list[_Row]:
        return rows

    monkeypatch.setattr(w.db, "fetch", fake_fetch)
    w._weights = {}
    w._loaded = False


def test_recent_row_wins_over_alltime(monkeypatch):
    # Same exact key — recent (low brier => high weight) should beat
    # the older row (high brier => low weight).
    _patch_rows(
        monkeypatch,
        [
            _row("quant", "NFL", "moneyline", brier=0.10, recent=True),
            _row("quant", "NFL", "moneyline", brier=0.40, recent=False),
        ],
    )

    weight = asyncio.run(w.get_persona_weight("quant", "NFL", "moneyline"))
    # 1 - 0.10 = 0.90.  If alltime had won we'd see 0.60.
    assert abs(weight - 0.90) < 1e-6


def test_alltime_used_when_no_recent_for_key(monkeypatch):
    _patch_rows(
        monkeypatch,
        [
            _row("quant", "NFL", "moneyline", brier=0.40, recent=False),
        ],
    )

    weight = asyncio.run(w.get_persona_weight("quant", "NFL", "moneyline"))
    assert abs(weight - 0.60) < 1e-6


def test_falls_back_to_league_then_neutral(monkeypatch):
    # Only league-level (no market_type) row.
    _patch_rows(
        monkeypatch,
        [
            _row("quant", "NFL", None, brier=0.20, recent=True),
        ],
    )

    # exact key misses -> falls back to (analyst, league, None) with weight 0.80
    league_weight = asyncio.run(w.get_persona_weight("quant", "NFL", "spread"))
    assert abs(league_weight - 0.80) < 1e-6

    # totally unknown analyst -> neutral
    neutral = asyncio.run(w.get_persona_weight("ghost", "NFL", "moneyline"))
    assert abs(neutral - w._NEUTRAL) < 1e-6


def test_snapshot_only_returns_specific_tuples(monkeypatch):
    _patch_rows(
        monkeypatch,
        [
            _row("quant", "NFL", "moneyline", brier=0.20, recent=True, vertical="sports"),
            _row("quant", None, None, brier=0.50, recent=True, vertical="sports"),
        ],
    )

    snap = asyncio.run(w.snapshot_persona_weights())
    keys = {(r["persona"], r["league"], r["market_type"]) for r in snap}
    assert ("quant", "NFL", "moneyline") in keys
    # The fallback aggregates (None, None) must not surface to the dashboard.
    assert all(r["league"] and r["market_type"] for r in snap)


# ---------------------------------------------------------------------------
# Per-vertical scoping (Task #194)
# ---------------------------------------------------------------------------


def test_vertical_scoped_lookup_isolates_brier(monkeypatch):
    # Same persona, two verticals — sports has a great Brier (=> high
    # weight) and crypto has a terrible one.  Each lookup must return
    # the weight for its own vertical without the other leaking in.
    _patch_rows(
        monkeypatch,
        [
            _row("quant", "NFL", "moneyline", brier=0.10, recent=True, vertical="sports"),
            _row("quant", "BTC", "binary",    brier=0.45, recent=True, vertical="crypto"),
        ],
    )
    sports_w = asyncio.run(
        w.get_persona_weight("quant", "NFL", "moneyline", vertical="sports")
    )
    crypto_w = asyncio.run(
        w.get_persona_weight("quant", "BTC", "binary", vertical="crypto")
    )
    assert abs(sports_w - 0.90) < 1e-6
    assert abs(crypto_w - 0.55) < 1e-6


def test_vertical_lookup_falls_back_to_unscoped_legacy_rows(monkeypatch):
    # Legacy rows pre-0005-backfill have vertical=None.  A brand-new
    # vertical (geopolitics) with no rows of its own must inherit the
    # persona's all-domain history rather than collapsing to neutral.
    _patch_rows(
        monkeypatch,
        [
            _row("quant", "NFL", "moneyline", brier=0.20, recent=True, vertical=None),
        ],
    )
    geo_w = asyncio.run(
        w.get_persona_weight("quant", "NFL", "moneyline", vertical="geopolitics")
    )
    # Falls back through the (analyst, None, league, market_type) tier.
    assert abs(geo_w - 0.80) < 1e-6


def test_vertical_lookup_falls_back_to_neutral_when_unknown(monkeypatch):
    _patch_rows(monkeypatch, [])
    val = asyncio.run(
        w.get_persona_weight("ghost", "NFL", "moneyline", vertical="sports")
    )
    assert abs(val - w._NEUTRAL) < 1e-6


def test_legacy_call_signature_without_vertical_still_works(monkeypatch):
    # Backwards compatibility: callers that haven't been updated to
    # pass `vertical=` keep getting the unscoped resolver behaviour.
    _patch_rows(
        monkeypatch,
        [
            _row("quant", "NFL", "moneyline", brier=0.10, recent=True, vertical="sports"),
        ],
    )
    val = asyncio.run(w.get_persona_weight("quant", "NFL", "moneyline"))
    # Hits the (analyst, None, league, market_type) legacy tier.
    assert abs(val - 0.90) < 1e-6
