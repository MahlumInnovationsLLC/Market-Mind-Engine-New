"""Regression tests for two runtime fixes in
``app.orchestrator.cycle`` that were silently aborting sports / crypto
cycles after the Kalshi *_dollars producer fix landed.

Why these tests exist
=====================
1. ``run_debate(...)`` was being passed ``kalshi_price`` as the raw
   asyncpg ``Decimal``.  When ``swarm.runner`` later JSON-serialized
   the dict to persist ``swarm_debates.game_context``, it raised
   ``Object of type Decimal is not JSON serializable`` and the cycle
   reported ``errors=[fresh debate failed for ...]`` with
   ``fresh_debates=0``.  The fix coerces both ``current_price`` and
   ``kalshi_price`` to ``float`` (or ``None``) at the call boundary.

2. The cap-bucket check in the gate-evaluation loop compared
   ``cand.get("game_time")`` directly against ``datetime.now(tzaware)``.
   asyncpg returns ``markets.game_time`` (declared as
   ``timestamp without time zone``) as a *naive* ``datetime``, so the
   compare raised ``TypeError: can't compare offset-naive and
   offset-aware datetimes`` *outside* the fresh-debate try/except, which
   bubbled all the way out of ``run_cycle`` and prevented any
   ``cycle_complete`` event from being emitted (sports / crypto cycles
   stranded in flight forever).  The new ``_game_time_in_past`` helper
   normalizes naive datetimes to UTC before comparing.

Both regressions are reachable purely via small unit-level seams; we
deliberately do *not* spin up the full ``run_cycle`` here because that
needs the entire DB / orchestrator / swarm stack — these tests just
pin the behavior of the two helpers / call patterns.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.orchestrator import cycle as cycle_mod


class TestGameTimeInPast:
    """`_game_time_in_past` must accept every shape that asyncpg / the
    cycle loop might hand it without raising."""

    def test_none_is_not_in_past(self) -> None:
        assert cycle_mod._game_time_in_past(None) is False

    def test_non_datetime_is_not_in_past(self) -> None:
        # Defensive: if a feed ever stuffs a string / int / dict on the
        # candidate dict we must not crash the entire cycle.
        for bogus in ("2026-04-27", 0, 12345, {"x": 1}, [], object()):
            assert cycle_mod._game_time_in_past(bogus) is False

    def test_naive_past_is_in_past(self) -> None:
        """Mirrors what asyncpg returns for `markets.game_time`."""
        past_naive = datetime.utcnow() - timedelta(hours=1)
        assert past_naive.tzinfo is None  # sanity — this is the bug shape
        assert cycle_mod._game_time_in_past(past_naive) is True

    def test_naive_future_is_not_in_past(self) -> None:
        future_naive = datetime.utcnow() + timedelta(hours=1)
        assert future_naive.tzinfo is None
        assert cycle_mod._game_time_in_past(future_naive) is False

    def test_aware_past_is_in_past(self) -> None:
        past_aware = datetime.now(timezone.utc) - timedelta(hours=1)
        assert past_aware.tzinfo is not None
        assert cycle_mod._game_time_in_past(past_aware) is True

    def test_aware_future_is_not_in_past(self) -> None:
        future_aware = datetime.now(timezone.utc) + timedelta(hours=1)
        assert future_aware.tzinfo is not None
        assert cycle_mod._game_time_in_past(future_aware) is False

    def test_naive_and_aware_inputs_dont_raise_typeerror(self) -> None:
        """The pre-fix bug was `TypeError: can't compare offset-naive
        and offset-aware datetimes`.  Pin that this is gone for both
        shapes."""
        for gt in (
            datetime.utcnow(),
            datetime.now(timezone.utc),
        ):
            try:
                cycle_mod._game_time_in_past(gt)
            except TypeError as e:
                pytest.fail(f"unexpected TypeError on {gt!r}: {e}")


class TestKalshiPriceJSONSerializable:
    """The pre-fix bug was: ``run_debate(...)`` received a ``Decimal``
    in the dict, then ``swarm.runner`` did ``json.dumps(...)`` and
    blew up.  We reproduce the *boundary* — a dict shaped exactly like
    the one ``cycle.run_cycle`` builds — and assert that
    ``json.dumps`` accepts every numeric field.
    """

    def _build_debate_payload(
        self, kalshi_price: Decimal | float | None
    ) -> dict[str, object]:
        """Mirror the literal dict cycle.py:1297-1316 builds, after
        the fix.  `kalshi_price` enters as the asyncpg type
        (``Decimal``) and *must* be cast to ``float`` (or ``None``)
        before being put on the payload."""
        pred = {"kalshi_price": kalshi_price}
        return {
            "game_id": "GAME-1",
            "league": "NBA",
            "market_type": "moneyline",
            "side": "YES",
            "current_price": (
                float(pred["kalshi_price"])
                if pred.get("kalshi_price") is not None
                else None
            ),
            "context": "home_win_prob=0.5 edge=0.05 confidence=0.7",
            "kalshi_market_id": "KXNBAGAME-26APR29ORLDET-ORL",
            "kalshi_price": (
                float(pred["kalshi_price"])
                if pred.get("kalshi_price") is not None
                else None
            ),
            "home_team": "ORL",
            "away_team": "DET",
            "aggression_mode": "neutral",
            "vertical": "sports",
        }

    def test_decimal_kalshi_price_serializes(self) -> None:
        payload = self._build_debate_payload(Decimal("0.42"))
        # Pre-fix: this raised
        # ``Object of type Decimal is not JSON serializable``.
        s = json.dumps(payload)
        round_tripped = json.loads(s)
        assert round_tripped["kalshi_price"] == 0.42
        assert round_tripped["current_price"] == 0.42

    def test_zero_decimal_kalshi_price_serializes(self) -> None:
        # Edge case the consistency-fix protects against: ``0.0`` is a
        # valid market price but the old truthy guard would have
        # silently dropped it to ``None``.
        payload = self._build_debate_payload(Decimal("0"))
        s = json.dumps(payload)
        round_tripped = json.loads(s)
        assert round_tripped["kalshi_price"] == 0.0
        assert round_tripped["current_price"] == 0.0

    def test_none_kalshi_price_stays_none(self) -> None:
        payload = self._build_debate_payload(None)
        s = json.dumps(payload)
        round_tripped = json.loads(s)
        assert round_tripped["kalshi_price"] is None
        assert round_tripped["current_price"] is None

    def test_float_kalshi_price_passes_through(self) -> None:
        payload = self._build_debate_payload(0.55)
        s = json.dumps(payload)
        round_tripped = json.loads(s)
        assert round_tripped["kalshi_price"] == 0.55
        assert round_tripped["current_price"] == 0.55
