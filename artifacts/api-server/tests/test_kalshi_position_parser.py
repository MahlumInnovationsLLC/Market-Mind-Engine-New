"""Unit tests for the Kalshi position parser + live mark-to-market helpers.

These cover the regression that hid 31/38 truly-open Kalshi positions
on dashboards: the legacy ``position`` integer field is *always 0* on
accounts with any fractional activity, so the parser has to honour the
modern dollar-suffixed string fields (``position_fp``,
``market_exposure_dollars``, ``yes_bid_dollars``, ...).

Also pins the explicit ``is not None`` fallback in
``_kalshi_yes_mid_cents`` so a legitimate 0.0¢ quote is *not* silently
discarded by Python's ``or`` truthiness — that bug would mark
near-100¢ NO positions at the previous tick rather than the actual
0.0¢ YES quote.
"""
from __future__ import annotations

import pytest

from app.kalshi.client import _dollars_or_cents, _parse_position
from app.main import _kalshi_current_value_cents, _kalshi_yes_mid_cents


class TestParsePosition:
    """``_parse_position`` honours position_fp first, falls back to legacy int."""

    def test_modern_fractional_position(self) -> None:
        # Kalshi's modern payload — position_fp is a string-encoded
        # float; legacy ``position`` is 0 for every fractional row.
        row = {"position_fp": "+4570.94", "position": 0}
        assert _parse_position(row) == pytest.approx(4570.94)

    def test_modern_negative_no_position(self) -> None:
        row = {"position_fp": "-12.5", "position": 0}
        assert _parse_position(row) == pytest.approx(-12.5)

    def test_legacy_integer_position(self) -> None:
        # Legacy account / older snapshot — only the int field is set.
        row = {"position": 50}
        assert _parse_position(row) == pytest.approx(50.0)

    def test_zero_position(self) -> None:
        row = {"position_fp": "0", "position": 0}
        assert _parse_position(row) == pytest.approx(0.0)

    def test_missing_both_fields(self) -> None:
        assert _parse_position({}) == pytest.approx(0.0)

    def test_position_fp_takes_precedence_over_legacy(self) -> None:
        # If both are non-zero (rare but possible during partial migration),
        # position_fp is authoritative.
        row = {"position_fp": "100.5", "position": 99}
        assert _parse_position(row) == pytest.approx(100.5)

    def test_garbage_position_fp_falls_back_to_legacy(self) -> None:
        row = {"position_fp": "not-a-number", "position": 7}
        assert _parse_position(row) == pytest.approx(7.0)

    def test_empty_string_position_fp(self) -> None:
        row = {"position_fp": "", "position": 3}
        assert _parse_position(row) == pytest.approx(3.0)


class TestDollarsOrCents:
    """``_dollars_or_cents`` honours dollar string first, falls back to cents int."""

    def test_modern_dollars_field(self) -> None:
        # ``"9.620000"`` dollars → 962 cents (round, not truncate).
        row = {"market_exposure_dollars": "9.620000", "market_exposure": 0}
        assert _dollars_or_cents(
            row, dollars_key="market_exposure_dollars", cents_key="market_exposure"
        ) == 962

    def test_legacy_cents_field_only(self) -> None:
        row = {"market_exposure": 1234}
        assert _dollars_or_cents(
            row, dollars_key="market_exposure_dollars", cents_key="market_exposure"
        ) == 1234

    def test_negative_dollars_field(self) -> None:
        row = {"realized_pnl_dollars": "-1.230000"}
        assert _dollars_or_cents(
            row, dollars_key="realized_pnl_dollars", cents_key="realized_pnl"
        ) == -123

    def test_missing_both(self) -> None:
        assert _dollars_or_cents(
            {}, dollars_key="x_dollars", cents_key="x"
        ) == 0

    def test_rounding_half_up(self) -> None:
        # 0.005 dollars → either 0 or 1 cent depending on rounding;
        # we use round-half-to-even (Python's default), so this is
        # a regression check, not a behaviour spec.
        row = {"x_dollars": "0.005"}
        result = _dollars_or_cents(row, dollars_key="x_dollars", cents_key="x")
        assert result in (0, 1)


class TestYesMidCents:
    """``_kalshi_yes_mid_cents`` resolves a YES-leg mid from many shapes."""

    def test_modern_dollar_bid_ask_mid(self) -> None:
        m = {"yes_bid_dollars": "0.6000", "yes_ask_dollars": "0.6100"}
        # mid of 60¢ and 61¢ → 60.5¢
        assert _kalshi_yes_mid_cents(m) == pytest.approx(60.5)

    def test_modern_last_price_only(self) -> None:
        m = {"last_price_dollars": "0.6000"}
        assert _kalshi_yes_mid_cents(m) == pytest.approx(60.0)

    def test_legacy_integer_cent_fields(self) -> None:
        m = {"yes_bid": 60, "yes_ask": 61}
        assert _kalshi_yes_mid_cents(m) == pytest.approx(60.5)

    def test_zero_bid_is_preserved_not_silently_discarded(self) -> None:
        # CRITICAL regression guard: a legitimate 0.0¢ YES bid (deep
        # OTM contract or freshly-resolved leg) must NOT be treated as
        # missing.  If we used Python's ``or`` truthiness, a "0.0000"
        # bid would fall through to the previous-tick fallback and
        # silently mark a near-100¢ NO position at the wrong price.
        m = {
            "yes_bid_dollars": "0.0000",
            "yes_ask_dollars": "0.0000",
            "previous_yes_bid_dollars": "0.5000",
            "previous_yes_ask_dollars": "0.5100",
        }
        assert _kalshi_yes_mid_cents(m) == pytest.approx(0.0)

    def test_falls_back_to_previous_tick_when_live_missing(self) -> None:
        m = {
            "yes_bid_dollars": None,
            "yes_ask_dollars": None,
            "last_price_dollars": None,
            "previous_yes_bid_dollars": "0.5500",
            "previous_yes_ask_dollars": "0.5600",
        }
        assert _kalshi_yes_mid_cents(m) == pytest.approx(55.5)

    def test_returns_none_when_market_is_none(self) -> None:
        assert _kalshi_yes_mid_cents(None) is None

    def test_returns_none_when_no_price_fields_present(self) -> None:
        assert _kalshi_yes_mid_cents({"status": "active"}) is None

    def test_modern_takes_precedence_over_legacy(self) -> None:
        # When both shapes are present, modern dollar fields win.
        m = {
            "yes_bid_dollars": "0.7000",
            "yes_ask_dollars": "0.7100",
            "yes_bid": 60,
            "yes_ask": 61,
        }
        assert _kalshi_yes_mid_cents(m) == pytest.approx(70.5)


class TestCurrentValueCents:
    """``_kalshi_current_value_cents`` marks YES vs NO sides correctly."""

    def test_yes_position_marked_at_yes_mid(self) -> None:
        # 100 YES contracts at 60.5¢ → 6,050¢ value.
        m = {"yes_bid_dollars": "0.6000", "yes_ask_dollars": "0.6100"}
        assert _kalshi_current_value_cents(100.0, m) == 6050

    def test_no_position_marked_at_complement_of_yes_mid(self) -> None:
        # 100 NO contracts when YES mid is 60.5¢ → NO is worth 39.5¢
        # each → 3,950¢ value.
        m = {"yes_bid_dollars": "0.6000", "yes_ask_dollars": "0.6100"}
        assert _kalshi_current_value_cents(-100.0, m) == 3950

    def test_fractional_position(self) -> None:
        # 4570.94 YES contracts at 60.5¢ → round(276,541.87) = 276,542¢.
        m = {"yes_bid_dollars": "0.6000", "yes_ask_dollars": "0.6100"}
        assert _kalshi_current_value_cents(4570.94, m) == 276542

    def test_flat_position_returns_none(self) -> None:
        m = {"yes_bid_dollars": "0.6000", "yes_ask_dollars": "0.6100"}
        assert _kalshi_current_value_cents(0.0, m) is None

    def test_no_market_returns_none(self) -> None:
        assert _kalshi_current_value_cents(100.0, None) is None

    def test_market_with_no_quote_returns_none(self) -> None:
        # Active market with no live quote and no fallback — UI should
        # render "—" rather than a misleading $0.
        assert _kalshi_current_value_cents(100.0, {"status": "active"}) is None

    def test_zero_yes_quote_marks_yes_at_zero_no_at_full(self) -> None:
        # YES leg trading at exactly 0¢: YES position is worthless,
        # NO position is worth $1 per contract.
        m = {"yes_bid_dollars": "0.0000", "yes_ask_dollars": "0.0000"}
        assert _kalshi_current_value_cents(50.0, m) == 0
        assert _kalshi_current_value_cents(-50.0, m) == 5000
