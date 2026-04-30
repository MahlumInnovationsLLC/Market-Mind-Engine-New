"""Unit tests for the ``_tag_placed_by`` heuristic in
``scripts/backfill_kalshi_fills.py``.

Pins the operator's $400 bimodal rule (see migration 0039 docstring +
2026-04-30 directive in replit.md): notional > $400 -> ``manual_ui``,
otherwise -> ``orchestrator``.  Crucially, no input ever produces
``'unknown'`` — that is the whole point of the operator's "NO
unknowns" directive that prompted migration 0040 + this heuristic.

Edge cases covered:
- Boundary at exactly $400 (inclusive of orchestrator side, per
  ``> 400`` not ``>= 400``).
- Decimal vs float vs int inputs (all three appear in the actual
  call sites — crypto fills use floats, consolidated buckets use
  Decimals).
- Missing / non-numeric qty or price (defensive fallback to
  ``'orchestrator'``, never ``'unknown'``).
"""
from __future__ import annotations

from decimal import Decimal

import pytest

# Add scripts dir to path the way the existing scripts/ tests do.
import sys, pathlib
_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from backfill_kalshi_fills import _tag_placed_by  # noqa: E402


class TestTagPlacedByBimodalHeuristic:
    """Core $400 boundary behaviour."""

    @pytest.mark.parametrize("qty,price", [
        (1, 401),         # $401 — just over the threshold
        (10, 50),         # $500
        (100, 4.01),      # $401 with fractional price
        (Decimal("50"), Decimal("9.00")),  # $450 with Decimals
        (1, 1000),        # well over
    ])
    def test_over_400_is_manual_ui(self, qty, price):
        assert _tag_placed_by(qty, price) == "manual_ui"

    @pytest.mark.parametrize("qty,price", [
        (1, 400),         # exactly $400 — boundary is exclusive
        (10, 40),         # $400 exactly via integers
        (100, 4.00),      # $400 with fractional price
        (Decimal("50"), Decimal("8.00")),  # $400 with Decimals
        (1, 50),          # well under
        (1, 1),           # tiny
        (0, 1000),        # zero qty (notional 0)
    ])
    def test_at_or_under_400_is_orchestrator(self, qty, price):
        assert _tag_placed_by(qty, price) == "orchestrator"


class TestTagPlacedByNoUnknownsEver:
    """No input — including malformed ones — may produce 'unknown'.

    Operator's directive (2026-04-30): "make sure there are NO
    unknowns".  The defensive fallback for missing or non-numeric
    inputs is ``'orchestrator'`` (system-side), per the heuristic that
    anything not flagged as a $400+ manual bet is a system trade.
    """

    @pytest.mark.parametrize("qty,price", [
        (None, 100),
        (10, None),
        (None, None),
        ("not-a-number", 100),
        (10, "also-not-a-number"),
        (object(), object()),  # both non-numeric, non-None
    ])
    def test_missing_or_bad_inputs_fall_back_to_orchestrator(self, qty, price):
        result = _tag_placed_by(qty, price)
        assert result == "orchestrator"
        assert result != "unknown"

    def test_no_input_combination_returns_unknown(self):
        # Belt-and-suspenders: the function literally never returns
        # 'unknown' for any of the inputs it sees in production.
        for qty in [None, 0, 1, 10, 100, Decimal("0"), Decimal("50"), "bad"]:
            for price in [None, 0, 1, 50, 400, 401, 1000, Decimal("0"), Decimal("9"), "bad"]:
                assert _tag_placed_by(qty, price) != "unknown", (
                    f"unexpected 'unknown' for qty={qty!r}, price={price!r}"
                )
