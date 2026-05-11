"""Tests for the entertainment trading vertical (task #203)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app import verticals as vertical_registry
from app.entertainment import promotion as promotion_mod
from app.entertainment.signals import SignalRow
from app.entertainment.model import (
    _parse_threshold_token,
    hybrid_probability,
)
from app.entertainment.tickers import (
    InvalidEntertainmentTicker,
    SUPPORTED_SIGNAL_TYPES,
    parse_entertainment_ticker,
)
from app.risk.gates import RiskContext, gate_kelly_fraction, gate_market_cooldown
from app.swarm import profiles as swarm_profiles


# ---------------------------------------------------------------------------
# Ticker parser
# ---------------------------------------------------------------------------


def test_parse_box_office_ticker():
    t = parse_entertainment_ticker("KXBOX-25MAR-AVATAR3-ABOVE150")
    assert t.signal_type == "BOX"
    assert t.when == "25MAR"
    assert t.title == "AVATAR3"
    assert t.threshold == "ABOVE150"
    assert t.category == "box_office"


def test_parse_oscar_ticker_three_segments():
    t = parse_entertainment_ticker("KXOSCAR-25-OPPENHEIMER")
    assert t.signal_type == "OSCAR"
    assert t.when == "25"
    assert t.title == "OPPENHEIMER"
    assert t.threshold is None
    assert t.candidate is None
    assert t.race_category is None
    assert t.category == "awards"


def test_parse_oscar_race_style_ticker_four_segments():
    t = parse_entertainment_ticker("KXOSCAR-25-BESTPIC-OPPENHEIMER")
    assert t.signal_type == "OSCAR"
    assert t.when == "25"
    # title carries the race category for race-style tickers; candidate
    # carries the nominee name.
    assert t.title == "BESTPIC"
    assert t.candidate == "OPPENHEIMER"
    assert t.race_category == "BESTPIC"
    assert t.threshold is None


def test_parse_emmy_grammy_tickers():
    e = parse_entertainment_ticker("KXEMMY-25-DRAMA-SUCCESSION")
    g = parse_entertainment_ticker("KXGRAMMY-25-RECORD-FLOWERS")
    assert e.signal_type == "EMMY" and e.category == "awards"
    assert e.candidate == "SUCCESSION" and e.race_category == "DRAMA"
    assert g.signal_type == "GRAMMY" and g.category == "awards"
    assert g.candidate == "FLOWERS" and g.race_category == "RECORD"


def test_parse_spotify_chart_top10():
    t = parse_entertainment_ticker("KXSPOTIFY-25W08-TAYLOR-TOP10")
    assert t.signal_type == "SPOTIFY"
    assert t.when == "25W08"
    assert t.threshold == "TOP10"
    assert t.category == "charts"


def test_parse_rt_above_threshold():
    t = parse_entertainment_ticker("KXRT-25MAR-AVATAR3-ABOVE85")
    assert t.signal_type == "RT"
    assert t.threshold == "ABOVE85"
    assert t.category == "reviews"


def test_parse_stream_ticker_top10():
    t = parse_entertainment_ticker("KXSTREAM-25MAR-WEDNESDAY-TOP10")
    assert t.signal_type == "STREAM"
    assert t.category == "charts"


def test_parse_case_insensitive():
    t = parse_entertainment_ticker("kxbox-25mar-avatar3-above150")
    assert t.signal_type == "BOX"
    assert t.title == "AVATAR3"


def test_supported_signal_types_present():
    for st in ("BOX", "OSCAR", "EMMY", "GRAMMY", "SPOTIFY", "STREAM", "RT"):
        assert st in SUPPORTED_SIGNAL_TYPES


def test_parse_rejects_blank():
    with pytest.raises(InvalidEntertainmentTicker):
        parse_entertainment_ticker("")


def test_parse_rejects_unrecognised_shape():
    with pytest.raises(InvalidEntertainmentTicker):
        parse_entertainment_ticker("NOT_A_TICKER")


# ---------------------------------------------------------------------------
# Vertical registration
# ---------------------------------------------------------------------------


def test_vertical_registered_with_kelly_005():
    spec = vertical_registry.get_spec("entertainment")
    assert spec is not None
    assert spec.predictor_hook == "entertainment_hybrid"
    assert spec.kelly_multiplier == pytest.approx(0.05)
    assert "ENTERTAINMENT" in spec.leagues
    for prefix in ("KXBOX", "KXOSCAR", "KXEMMY", "KXGRAMMY",
                   "KXSPOTIFY", "KXSTREAM", "KXRT"):
        assert prefix in spec.ticker_prefixes


def test_calibrators_includes_entertainment():
    from app.calibrators import SUPPORTED_LEAGUES
    assert "ENTERTAINMENT" in SUPPORTED_LEAGUES


def test_league_config_present():
    from app.sports.leagues import get_league_config
    cfg = get_league_config("ENTERTAINMENT")
    assert cfg is not None
    assert cfg.live_action_blackout is False


# ---------------------------------------------------------------------------
# Persona profile (4 baseline + 4 layered = 8 personas)
# ---------------------------------------------------------------------------


def test_entertainment_profile_has_eight_personas_including_layered():
    profile = swarm_profiles.get_profile("entertainment")
    assert profile is not None
    ids = {p.id for p in profile.personas}
    for layered in ("box_office_historian", "awards_pundit", "social_buzz_aggregator"):
        assert layered in ids, f"missing layered persona {layered}"
    assert len(profile.personas) == 8


# ---------------------------------------------------------------------------
# Hybrid model heads
# ---------------------------------------------------------------------------


def _row(*, signal_type="BOX", title="AVATAR3", value=None,
         settled=True, outcome=None, days_ago=30.0,
         source="box_office_mojo_rss") -> SignalRow:
    now = datetime.now(timezone.utc)
    return SignalRow(
        source=source,
        signal_id=f"{source}/{signal_type}/{title}/{days_ago}",
        signal_type=signal_type,
        title=title,
        event_at=now - timedelta(days=days_ago),
        value=value,
        settled=settled,
        outcome=outcome,
        payload={},
    )


def test_box_office_above_low_threshold_high_prob():
    t = parse_entertainment_ticker("KXBOX-25MAR-AVATAR3-ABOVE50")
    history = [_row(value=100.0 + i * 5.0, days_ago=30 + i) for i in range(20)]
    res = hybrid_probability(t, history)
    assert res.head == "box_office"
    assert res.probability > 0.7


def test_box_office_above_high_threshold_low_prob():
    t = parse_entertainment_ticker("KXBOX-25MAR-AVATAR3-ABOVE500")
    history = [_row(value=100.0 + i * 5.0, days_ago=30 + i) for i in range(20)]
    res = hybrid_probability(t, history)
    assert res.head == "box_office"
    assert res.probability < 0.3


def test_box_office_clamps_into_band():
    t = parse_entertainment_ticker("KXBOX-25MAR-AVATAR3-ABOVE100000")
    history = [_row(value=10.0, days_ago=30 + i) for i in range(5)]
    res = hybrid_probability(t, history)
    assert 0.05 <= res.probability <= 0.95


def test_rt_above_threshold():
    t = parse_entertainment_ticker("KXRT-25MAR-AVATAR3-ABOVE60")
    history = [_row(signal_type="RT", value=70.0 + i, days_ago=30 + i) for i in range(15)]
    res = hybrid_probability(t, history)
    assert res.head == "reviews"
    assert res.probability > 0.6


def test_awards_uses_precursor_prior():
    t = parse_entertainment_ticker("KXOSCAR-25-OPPENHEIMER")
    upcoming = _row(signal_type="OSCAR", title="OPPENHEIMER",
                    value=0.85, settled=False, days_ago=-30.0)
    settled_winners = [
        _row(signal_type="OSCAR", title=f"WINNER{i}", value=0.7,
             settled=True, outcome="win", days_ago=365 * (i + 1))
        for i in range(5)
    ]
    res = hybrid_probability(t, [upcoming, *settled_winners])
    assert res.head == "awards"
    # Posterior must be pulled toward the high prior.
    assert res.probability > 0.65


def test_awards_race_style_ticker_finds_candidate():
    t = parse_entertainment_ticker("KXOSCAR-25-BESTPIC-OPPENHEIMER")
    upcoming = SignalRow(
        source="variety_awards_rss",
        signal_id="variety/oscar/oppenheimer",
        signal_type="OSCAR",
        title="OPPENHEIMER",
        event_at=datetime.now(timezone.utc) + timedelta(days=30),
        value=0.82,
        settled=False,
        outcome=None,
        payload={"race_category": "BESTPIC"},
    )
    res = hybrid_probability(t, [upcoming])
    assert res.head == "awards"
    assert res.probability > 0.6


def test_awards_race_normalises_across_candidates():
    # Three-candidate BESTPIC field with priors {0.5, 0.3, 0.2}.  The
    # ticker for OPPENHEIMER (prior 0.5) must be priced at exactly its
    # normalized share (0.5 / 1.0 = 0.5), NOT inflated by a winners
    # base rate.
    t = parse_entertainment_ticker("KXOSCAR-25-BESTPIC-OPPENHEIMER")
    now = datetime.now(timezone.utc)
    field = [
        SignalRow(source="variety_awards_rss", signal_id=f"v/{c}",
                  signal_type="OSCAR", title=c,
                  event_at=now + timedelta(days=30), value=v,
                  settled=False, outcome=None,
                  payload={"race_category": "BESTPIC"})
        for c, v in [("OPPENHEIMER", 0.5), ("BARBIE", 0.3), ("KILLERS", 0.2)]
    ]
    res = hybrid_probability(t, field)
    assert res.head == "awards"
    assert res.probability == pytest.approx(0.5, abs=0.05)
    # Sum across all three candidates must be ~1 (within clamp band).
    sums = [
        hybrid_probability(
            parse_entertainment_ticker(f"KXOSCAR-25-BESTPIC-{c}"), field
        ).probability
        for c in ("OPPENHEIMER", "BARBIE", "KILLERS")
    ]
    assert sum(sums) == pytest.approx(1.0, abs=0.05)


def test_awards_no_candidate_match_returns_floor_not_unrelated_row():
    # A ticker for a candidate with NO row in the race field must NOT
    # bind to an unrelated candidate's signal — it must return the
    # floor probability instead.
    t = parse_entertainment_ticker("KXOSCAR-25-BESTPIC-DUNE2")
    now = datetime.now(timezone.utc)
    field = [
        SignalRow(source="variety_awards_rss", signal_id="v/oppenheimer",
                  signal_type="OSCAR", title="OPPENHEIMER",
                  event_at=now + timedelta(days=30), value=0.9,
                  settled=False, outcome=None,
                  payload={"race_category": "BESTPIC"}),
    ]
    res = hybrid_probability(t, field)
    assert res.head == "awards"
    assert res.probability == pytest.approx(0.05, abs=0.001)
    assert res.feature_summary["matched_candidate"] == 0.0


def test_awards_race_excludes_rows_missing_race_category():
    # A race-style ticker (carries race_category=BESTPIC) must NOT be
    # influenced by unsettled rows lacking a payload race_category.
    # Adding such an unscoped placeholder row must leave the
    # posterior unchanged.
    t = parse_entertainment_ticker("KXOSCAR-25-BESTPIC-OPPENHEIMER")
    now = datetime.now(timezone.utc)
    scoped_field = [
        SignalRow(source="v", signal_id="v/oppenheimer",
                  signal_type="OSCAR", title="OPPENHEIMER",
                  event_at=now + timedelta(days=30), value=0.5,
                  settled=False, outcome=None,
                  payload={"race_category": "BESTPIC"}),
        SignalRow(source="v", signal_id="v/barbie",
                  signal_type="OSCAR", title="BARBIE",
                  event_at=now + timedelta(days=30), value=0.5,
                  settled=False, outcome=None,
                  payload={"race_category": "BESTPIC"}),
    ]
    unscoped_noise = SignalRow(
        source="hollywood_reporter_awards_rss",
        signal_id="hr/headline", signal_type="OSCAR",
        title="OPPENHEIMER", event_at=now + timedelta(days=30),
        value=0.9, settled=False, outcome=None,
        payload={},  # no race_category
    )
    p_clean = hybrid_probability(t, scoped_field).probability
    p_with_noise = hybrid_probability(t, scoped_field + [unscoped_noise]).probability
    assert p_clean == pytest.approx(p_with_noise, abs=0.001)


def test_awards_feed_emits_structured_race_rows_for_race_tickers():
    # Integration: a Variety/HR-style headline mentioning a race +
    # candidate must produce a feed row with payload.race_category +
    # payload.candidate so a race-style ticker scores above the floor.
    from app.feeds.entertainment_signals import _parse_awards_rss_body
    body = """
    <rss><channel>
      <item>
        <title>Oppenheimer leads Oscar Best Picture race</title>
        <description>The Christopher Nolan epic is the favorite for the Academy Award Best Picture.</description>
        <guid>variety/oppenheimer-bestpic</guid>
        <pubDate>Wed, 15 Jan 2025 12:00:00 +0000</pubDate>
      </item>
      <item>
        <title>Barbie tipped for Best Picture nomination</title>
        <description>Greta Gerwig's blockbuster eyes Oscar Best Picture spot.</description>
        <guid>variety/barbie-bestpic</guid>
        <pubDate>Wed, 15 Jan 2025 12:00:00 +0000</pubDate>
      </item>
    </channel></rss>
    """
    now = datetime.now(timezone.utc)
    rows = _parse_awards_rss_body(body, "variety_awards_rss", now)
    assert len(rows) == 2
    for r in rows:
        assert r["payload"].get("race_category") == "BESTPIC"
        assert r["payload"].get("candidate") in {"OPPENHEIMER", "BARBIE"}
    feed_signal_rows = [
        SignalRow(
            source=r["source"], signal_id=r["signal_id"],
            signal_type=r["signal_type"], title=r["title"],
            event_at=r["event_at"], value=r["value"],
            settled=r["settled"], outcome=r["outcome"],
            payload=r["payload"],
        ) for r in rows
    ]
    t = parse_entertainment_ticker("KXOSCAR-25-BESTPIC-OPPENHEIMER")
    res = hybrid_probability(t, feed_signal_rows)
    assert res.head == "awards"
    # Two equal-prior candidates → posterior must be ~0.5, not the floor.
    assert res.probability == pytest.approx(0.5, abs=0.05)


def test_awards_winners_in_history_do_not_inflate_posterior():
    # Settled winners with value=1.0 (as written by the historical
    # poller) must NOT inflate the posterior — the model is purely
    # race-normalized, so adding winners to history must leave the
    # ticker's probability unchanged.
    t = parse_entertainment_ticker("KXOSCAR-25-BESTPIC-OPPENHEIMER")
    now = datetime.now(timezone.utc)
    field = [
        SignalRow(source="v", signal_id="v/oppenheimer",
                  signal_type="OSCAR", title="OPPENHEIMER",
                  event_at=now + timedelta(days=30), value=0.5,
                  settled=False, outcome=None,
                  payload={"race_category": "BESTPIC"}),
        SignalRow(source="v", signal_id="v/barbie",
                  signal_type="OSCAR", title="BARBIE",
                  event_at=now + timedelta(days=30), value=0.5,
                  settled=False, outcome=None,
                  payload={"race_category": "BESTPIC"}),
    ]
    winners = [
        SignalRow(source="awards_historical_outcomes",
                  signal_id=f"OSCAR/{2020 - i}/BESTPIC/PASTWINNER{i}",
                  signal_type="OSCAR", title=f"PASTWINNER{i}",
                  event_at=now - timedelta(days=365 * (i + 1)),
                  value=1.0, settled=True, outcome="win",
                  payload={"race_category": "BESTPIC", "year": 2020 - i})
        for i in range(5)
    ]
    p_no_winners = hybrid_probability(t, field).probability
    p_with_winners = hybrid_probability(t, field + winners).probability
    assert p_with_winners == pytest.approx(p_no_winners, abs=0.001)
    assert p_with_winners == pytest.approx(0.5, abs=0.05)


def test_charts_top10_share():
    t = parse_entertainment_ticker("KXSPOTIFY-25W08-TAYLOR-TOP10")
    history = [
        _row(signal_type="SPOTIFY", title="TAYLOR",
             value=float(rank), days_ago=7 + i)
        for i, rank in enumerate([3, 5, 8, 12, 15, 4, 6, 9, 11, 7])
    ]
    res = hybrid_probability(t, history)
    assert res.head == "charts"
    # 7 of 10 historical entries were rank <= 10.
    assert res.probability == pytest.approx(0.7, abs=0.05)


def test_unknown_signal_type_returns_05_prior():
    # Force an unknown signal_type via direct parse of an exotic shape.
    from app.entertainment.tickers import EntertainmentTicker
    t = EntertainmentTicker(
        raw="KXUNKNOWN-25-X", signal_type="UNKNOWN",
        when="25", title="X", threshold=None,
    )
    res = hybrid_probability(t, [])
    assert res.probability == pytest.approx(0.5)
    assert res.head == "unknown"


# ---------------------------------------------------------------------------
# Threshold token parsing
# ---------------------------------------------------------------------------


def test_above_token_parses():
    val, direction = _parse_threshold_token("ABOVE150")
    assert val == 150.0
    assert direction == "above"


def test_below_token_parses():
    val, direction = _parse_threshold_token("BELOW50")
    assert val == 50.0
    assert direction == "below"


def test_top_token_parses():
    val, direction = _parse_threshold_token("TOP10")
    assert val == 10.0
    assert direction == "top"


def test_decimal_token_with_underscore():
    val, _direction = _parse_threshold_token("ABOVE0_3")
    assert val == pytest.approx(0.3)


# ---------------------------------------------------------------------------
# Per-vertical Kelly multiplier override (gate)
# ---------------------------------------------------------------------------


def _ctx(**overrides) -> RiskContext:
    base = dict(
        market_id="KXBOX-25MAR-AVATAR3-ABOVE150",
        side="YES",
        league="ENTERTAINMENT",
        vertical="entertainment",
        market_type=None,
        matchup=None,
        current_price=0.50,
        model_probability=0.65,
        edge=0.15,
        confidence_band_low=None,
        confidence_band_high=None,
        agreement_rate=None,
        swarm_approved=True,
        swarm_recommended_action="BUY_YES",
        debate_id=None,
        intended_quantity=20,
        intended_notional_usd=20.0,
        bankroll_usd=10_000.0,
        open_positions_count=0,
        open_positions_notional_usd=0.0,
        open_positions_per_vertical_usd={},
        operator_overrides={},
        market_cooldown_until=None,
    )
    base.update(overrides)
    return RiskContext(**base)


def test_kelly_per_vertical_override_beats_spec_default():
    res = asyncio.run(gate_kelly_fraction(_ctx(
        intended_notional_usd=200.0,
        operator_overrides={"kelly_multiplier_entertainment": 0.10},
    )))
    assert res.details.get("kelly_multiplier") == pytest.approx(0.10)


# ---------------------------------------------------------------------------
# Release-day blackout (gate_market_cooldown sub-check)
# ---------------------------------------------------------------------------


def _fake_resolved_row(event_at):
    return SignalRow(
        source="box_office_mojo_rss",
        signal_id="fake/BOX/AVATAR3",
        signal_type="BOX",
        title="AVATAR3",
        event_at=event_at,
        value=None,
        settled=False,
        outcome=None,
        payload={},
    )


def test_release_day_blackout_rejects_within_window():
    """Within the 60m before this BOX ticker's release the gate rejects."""
    next_release = datetime.now(timezone.utc) + timedelta(minutes=20)

    async def _fake_resolved(_ticker):
        return _fake_resolved_row(next_release)

    with patch(
        "app.entertainment.signals.resolved_signal_for_ticker",
        side_effect=_fake_resolved,
    ):
        res = asyncio.run(gate_market_cooldown(_ctx()))
    assert res.passed is False
    assert res.details.get("reason") == "release_day_blackout"
    assert res.details.get("signal_type") == "BOX"


def test_release_day_blackout_passes_outside_window():
    next_release = datetime.now(timezone.utc) + timedelta(hours=12)

    async def _fake_resolved(_ticker):
        return _fake_resolved_row(next_release)

    with patch(
        "app.entertainment.signals.resolved_signal_for_ticker",
        side_effect=_fake_resolved,
    ):
        res = asyncio.run(gate_market_cooldown(_ctx()))
    if not res.passed:
        assert res.details.get("reason") != "release_day_blackout"


def test_release_day_blackout_skipped_for_awards_tickers():
    """Awards tickers (OSCAR/EMMY/GRAMMY) are not subject to release-day blackout."""
    next_release = datetime.now(timezone.utc) + timedelta(minutes=20)

    async def _fake_resolved(_ticker):
        return _fake_resolved_row(next_release)

    ctx = _ctx(market_id="KXOSCAR-25-OPPENHEIMER")
    with patch(
        "app.entertainment.signals.resolved_signal_for_ticker",
        side_effect=_fake_resolved,
    ):
        res = asyncio.run(gate_market_cooldown(ctx))
    if not res.passed:
        assert res.details.get("reason") != "release_day_blackout"


# ---------------------------------------------------------------------------
# Window parsing
# ---------------------------------------------------------------------------


def test_parse_when_to_window_month():
    from app.entertainment.signals import parse_when_to_window
    win = parse_when_to_window("25MAR")
    assert win is not None
    start, end = win
    assert start.year == 2025 and start.month == 3 and start.day == 1
    assert end.year == 2025 and end.month == 3


def test_parse_when_to_window_quarter():
    from app.entertainment.signals import parse_when_to_window
    win = parse_when_to_window("25Q2")
    assert win is not None
    start, end = win
    assert start.month == 4 and end.month == 6


def test_parse_when_to_window_week():
    from app.entertainment.signals import parse_when_to_window
    win = parse_when_to_window("25W08")
    assert win is not None


def test_parse_when_to_window_year():
    from app.entertainment.signals import parse_when_to_window
    win = parse_when_to_window("25")
    assert win is not None
    start, end = win
    assert start.year == 2025 and end.year == 2025


def test_parse_when_to_window_unparseable():
    from app.entertainment.signals import parse_when_to_window
    assert parse_when_to_window("garbage") is None
    assert parse_when_to_window("") is None
    assert parse_when_to_window(None) is None


# ---------------------------------------------------------------------------
# Promotion thresholds
# ---------------------------------------------------------------------------


def test_promotion_constants():
    assert promotion_mod.SETTLED_THRESHOLD == 30
    assert promotion_mod.BRIER_THRESHOLD == pytest.approx(0.22)
    assert promotion_mod.DEFAULT_MULTIPLIER == pytest.approx(0.05)
    assert promotion_mod.PROMOTED_MULTIPLIER == pytest.approx(0.08)
    assert promotion_mod.SETTING_KEY == "risk.kelly_multiplier_entertainment"
    assert promotion_mod.VERTICAL == "entertainment"


def test_promotion_ineligible_when_under_settled_threshold():
    async def _fake_read_mult():
        return promotion_mod.DEFAULT_MULTIPLIER

    async def _fake_fetchrow(_q, _vertical):
        return {"n": 5, "brier": 0.1}

    with patch.object(promotion_mod, "_read_current_multiplier",
                      side_effect=_fake_read_mult), \
         patch.object(promotion_mod.db, "fetchrow",
                      side_effect=_fake_fetchrow):
        status = asyncio.run(promotion_mod.evaluate_promotion())
    assert status.eligible is False
    assert "30 settled trades" in status.reason


def test_promotion_ineligible_when_brier_too_high():
    async def _fake_read_mult():
        return promotion_mod.DEFAULT_MULTIPLIER

    async def _fake_fetchrow(_q, _vertical):
        return {"n": 50, "brier": 0.30}

    with patch.object(promotion_mod, "_read_current_multiplier",
                      side_effect=_fake_read_mult), \
         patch.object(promotion_mod.db, "fetchrow",
                      side_effect=_fake_fetchrow):
        status = asyncio.run(promotion_mod.evaluate_promotion())
    assert status.eligible is False
    assert "Brier" in status.reason


def test_promotion_eligible_when_thresholds_met():
    async def _fake_read_mult():
        return promotion_mod.DEFAULT_MULTIPLIER

    async def _fake_fetchrow(_q, _vertical):
        return {"n": 50, "brier": 0.18}

    with patch.object(promotion_mod, "_read_current_multiplier",
                      side_effect=_fake_read_mult), \
         patch.object(promotion_mod.db, "fetchrow",
                      side_effect=_fake_fetchrow):
        status = asyncio.run(promotion_mod.evaluate_promotion())
    assert status.eligible is True
    assert status.reason == "eligible"


# ---------------------------------------------------------------------------
# Kalshi mid blend (predictor)
# ---------------------------------------------------------------------------


def test_kalshi_mid_blend_pulls_probability_toward_market():
    from app.entertainment import predictor as ent_pred
    from app.entertainment.signals import SignalRow as _SR

    # Build history with *explicit* event_at dates anchored in July 2024
    # so the "25MAR" release-window filter (which looks for %b == "MAR")
    # never fires and selects a date-dependent subset whose members might
    # all land above or below the threshold depending on the current date.
    # Values straddle the $150M ABOVE150 threshold (geometric mean ≈ $148M)
    # producing a date-stable baseline of ~0.45–0.55 regardless of when
    # the test runs, which is the range where both Kalshi-blend directions
    # can be distinguished.
    _BASE = datetime(2024, 7, 15, tzinfo=timezone.utc)
    _VALS = [80, 95, 110, 125, 135, 145, 150, 155, 165, 175, 190, 210, 230, 260, 300]
    upcoming = _SR(
        source="box_office_mojo_rss",
        signal_id="test/upcoming",
        signal_type="BOX",
        title="AVATAR3",
        event_at=datetime.now(timezone.utc) + timedelta(days=2),
        value=None,
        settled=False,
        outcome=None,
        payload={},
    )
    history = [
        _SR(
            source="box_office_mojo_rss",
            signal_id=f"test/hist/{i}",
            signal_type="BOX",
            title="AVATAR3",
            event_at=_BASE - timedelta(days=i * 30),
            value=float(v),
            settled=True,
            outcome=None,
            payload={},
        )
        for i, v in enumerate(_VALS)
    ]

    async def _fake_resolve(ticker, *, lookback_days=1825):
        return [upcoming, *history]

    with patch.object(ent_pred, "resolve_signals", side_effect=_fake_resolve):
        baseline = asyncio.run(
            ent_pred.predict_entertainment_binary("KXBOX-25MAR-AVATAR3-ABOVE150")
        )
        with_market_high = asyncio.run(
            ent_pred.predict_entertainment_binary(
                "KXBOX-25MAR-AVATAR3-ABOVE150", kalshi_mid=0.80,
            )
        )
        with_market_low = asyncio.run(
            ent_pred.predict_entertainment_binary(
                "KXBOX-25MAR-AVATAR3-ABOVE150", kalshi_mid=0.20,
            )
        )

    assert with_market_high.probability > baseline.probability
    assert with_market_low.probability < baseline.probability
    assert with_market_high.raw["kalshi_blend"]["applied"] is True
    assert baseline.raw["kalshi_blend"]["applied"] is False


# ---------------------------------------------------------------------------
# Cycle dispatcher: Kalshi mid is sourced from cand even when not on
# top-level current_price (regression — earlier wiring only inspected
# `current_price`, leaving the predictor's log-odds blend dormant).
# ---------------------------------------------------------------------------


def test_cycle_predictor_sources_kalshi_mid_from_kalshi_price_field():
    from app.orchestrator import cycle as cycle_mod

    captured: dict[str, object] = {}

    class _FakePred:
        def as_dict(self):
            return {
                "probability": 0.5,
                "raw": {"features": {}, "feature_count": 0},
            }

    async def _fake_predict(ticker, *, league=None, kalshi_mid=None):
        captured["kalshi_mid"] = kalshi_mid
        return _FakePred()

    with patch.object(
        cycle_mod, "_predict_for_entertainment_binary",
        wraps=cycle_mod._predict_for_entertainment_binary,
    ), patch(
        "app.entertainment.predictor.predict_entertainment_binary",
        side_effect=_fake_predict,
    ):
        cand = {
            "kalshi_market_id": "KXBOX-25MAR-AVATAR3-ABOVE150",
            "kalshi_price": 0.42,
            "league": "ENTERTAINMENT",
        }
        asyncio.run(cycle_mod._predict_for_entertainment_binary(cand))
    assert captured.get("kalshi_mid") == pytest.approx(0.42)


def test_cycle_predictor_sources_kalshi_mid_from_game_context():
    from app.orchestrator import cycle as cycle_mod

    captured: dict[str, object] = {}

    class _FakePred:
        def as_dict(self):
            return {
                "probability": 0.5,
                "raw": {"features": {}, "feature_count": 0},
            }

    async def _fake_predict(ticker, *, league=None, kalshi_mid=None):
        captured["kalshi_mid"] = kalshi_mid
        return _FakePred()

    with patch(
        "app.entertainment.predictor.predict_entertainment_binary",
        side_effect=_fake_predict,
    ):
        cand = {
            "kalshi_market_id": "KXBOX-25MAR-AVATAR3-ABOVE150",
            "league": "ENTERTAINMENT",
            "game_context": {"kalshi_price": 0.61},
        }
        asyncio.run(cycle_mod._predict_for_entertainment_binary(cand))
    assert captured.get("kalshi_mid") == pytest.approx(0.61)


# ---------------------------------------------------------------------------
# /api/entertainment/calendar route smoke-test
# ---------------------------------------------------------------------------


def test_calendar_route_populates_model_prob():
    from fastapi.testclient import TestClient
    from app.main import app
    from app.entertainment import signals as ent_signals

    upcoming = SignalRow(
        source="box_office_mojo_rss",
        signal_id="bom/BOX/AVATAR3",
        signal_type="BOX",
        title="AVATAR3",
        event_at=datetime.now(timezone.utc) + timedelta(days=2),
        value=None, settled=False, outcome=None, payload={},
    )
    history = [
        SignalRow(
            source="bom", signal_id=f"bom/BOX/{i}",
            signal_type="BOX", title=f"PRIOR{i}",
            event_at=datetime.now(timezone.utc) - timedelta(days=30 * (i + 1)),
            value=80.0 + 5.0 * i, settled=True, outcome="released", payload={},
        ) for i in range(10)
    ]

    async def _fake_upcoming(within_hours=168.0, limit=50, signal_type=None):
        return [upcoming]

    async def _fake_settled(signal_type, **_kw):
        return history

    with patch.object(ent_signals, "upcoming_signals",
                      side_effect=_fake_upcoming), \
         patch.object(ent_signals, "settled_history_for_type",
                      side_effect=_fake_settled):
        client = TestClient(app)
        resp = client.get("/api/entertainment/calendar")
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body and len(body["items"]) == 1
    item = body["items"][0]
    assert item["model_prob"] is not None
    assert 0.05 <= item["model_prob"] <= 0.95
    assert item["signal_type"] == "BOX"
    assert item["title"] == "AVATAR3"
