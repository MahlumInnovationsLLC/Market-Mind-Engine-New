"""Tests for the events / macro-economics trading vertical (task #202)."""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app import verticals as vertical_registry
from app.events_econ import promotion as promotion_mod
from app.events_econ.calendar import ReleaseRow
from app.events_econ.model import consensus_prior_probability
from app.events_econ.tickers import (
    InvalidEventsEconTicker,
    SUPPORTED_RELEASES,
    parse_events_econ_ticker,
)
from app.risk.gates import RiskContext, gate_kelly_fraction, gate_market_cooldown
from app.swarm import profiles as swarm_profiles


# ---------------------------------------------------------------------------
# Ticker parser
# ---------------------------------------------------------------------------


def test_parse_cpi_ticker():
    t = parse_events_econ_ticker("KXCPI-24DEC-ABOVE3")
    assert t.release_type == "CPI"
    assert t.when == "24DEC"
    assert t.threshold == "ABOVE3"


def test_parse_decimal_threshold_with_underscore():
    t = parse_events_econ_ticker("KXPPI-25JAN-ABOVE0_3")
    assert t.release_type == "PPI"
    assert t.threshold == "ABOVE0_3"


def test_parse_fomc_ticker():
    t = parse_events_econ_ticker("KXFOMC-25MAR-HIKE25")
    assert t.release_type == "FOMC"
    assert t.threshold == "HIKE25"


def test_parse_jobless_weekly_when():
    t = parse_events_econ_ticker("KXJOBLESS-25W08-ABOVE220")
    assert t.release_type == "JOBLESS"
    assert t.when == "25W08"


def test_parse_case_insensitive():
    t = parse_events_econ_ticker("kxnfp-25feb-above200")
    assert t.release_type == "NFP"
    assert t.when == "25FEB"


def test_supported_releases_present():
    for rel in ("CPI", "PPI", "GDP", "FOMC", "NFP", "JOBLESS", "RETAIL"):
        assert rel in SUPPORTED_RELEASES


def test_parse_rejects_blank():
    with pytest.raises(InvalidEventsEconTicker):
        parse_events_econ_ticker("")


def test_parse_rejects_unrecognised_shape():
    with pytest.raises(InvalidEventsEconTicker):
        parse_events_econ_ticker("NOT-A-MACRO-TICKER-AT-ALL!!!")


# ---------------------------------------------------------------------------
# Vertical registration
# ---------------------------------------------------------------------------


def test_vertical_registered_with_kelly_015():
    spec = vertical_registry.get_spec("events-econ")
    assert spec is not None
    assert spec.predictor_hook == "events_econ_consensus_prior"
    assert spec.kelly_multiplier == pytest.approx(0.15)
    assert "EVENTS_ECON" in spec.leagues
    assert "KXCPI" in spec.ticker_prefixes
    assert "KXFOMC" in spec.ticker_prefixes
    assert "KXNFP" in spec.ticker_prefixes


def test_calibrators_includes_events_econ():
    from app.calibrators import SUPPORTED_LEAGUES
    assert "EVENTS_ECON" in SUPPORTED_LEAGUES


def test_league_config_present():
    from app.sports.leagues import get_league_config
    cfg = get_league_config("EVENTS_ECON")
    assert cfg is not None
    assert cfg.live_action_blackout is False


# ---------------------------------------------------------------------------
# Persona profile (5 baseline + 3 layered = 8)
# ---------------------------------------------------------------------------


def test_events_econ_profile_has_eight_personas_including_layered():
    profile = swarm_profiles.get_profile("events-econ")
    assert profile is not None
    ids = {p.id for p in profile.personas}
    for layered in ("macro_economist", "fed_watcher", "print_revisions"):
        assert layered in ids, f"missing layered persona {layered}"
    assert len(profile.personas) == 8


# ---------------------------------------------------------------------------
# Consensus-prior model
# ---------------------------------------------------------------------------


def _row(
    *,
    release_type: str = "CPI",
    consensus: float | None = None,
    actual: float | None = None,
    previous: float | None = None,
    days_ago: float = 30.0,
    source: str = "trading_economics_rss",
) -> ReleaseRow:
    now = datetime.now(timezone.utc)
    return ReleaseRow(
        source=source,
        signal_id=f"{source}/{release_type}/{days_ago}",
        release_type=release_type,
        release_at=now - timedelta(days=days_ago),
        consensus=consensus,
        previous=previous,
        actual=actual,
        revised_previous=None,
        payload={},
    )


def test_no_data_returns_prior_05_when_no_threshold():
    # Ticker with non-numeric threshold token + no history => prior 0.5.
    t = parse_events_econ_ticker("KXCPI-24DEC-NEUTRAL")
    res = consensus_prior_probability(t, [])
    assert res.probability == pytest.approx(0.5)


def test_above_threshold_lower_than_consensus_yields_high_prob():
    # Consensus 3.0, threshold 2.0 -> P(actual >= 2.0) should be HIGH.
    t = parse_events_econ_ticker("KXCPI-24DEC-ABOVE2")
    upcoming = _row(release_type="CPI", consensus=3.0, days_ago=-1.0)
    history = [_row(release_type="CPI", consensus=2.5 + i*0.1, actual=2.5 + i*0.1)
               for i in range(10)]
    res = consensus_prior_probability(t, [upcoming, *history])
    assert res.probability > 0.7


def test_above_threshold_higher_than_consensus_yields_low_prob():
    # Consensus 2.0, threshold 4.0 -> P(actual >= 4.0) should be LOW.
    t = parse_events_econ_ticker("KXCPI-24DEC-ABOVE4")
    upcoming = _row(release_type="CPI", consensus=2.0, days_ago=-1.0)
    history = [_row(release_type="CPI", consensus=2.0, actual=2.0 + (i-5)*0.1)
               for i in range(10)]
    res = consensus_prior_probability(t, [upcoming, *history])
    assert res.probability < 0.3


def test_clamps_into_band():
    t = parse_events_econ_ticker("KXCPI-24DEC-ABOVE100")  # absurd threshold
    upcoming = _row(release_type="CPI", consensus=2.0, days_ago=-1.0)
    res = consensus_prior_probability(t, [upcoming])
    assert 0.05 <= res.probability <= 0.95


def test_more_supportive_consensus_pushes_probability_higher():
    t = parse_events_econ_ticker("KXCPI-24DEC-ABOVE3")
    low_consensus = _row(release_type="CPI", consensus=2.5, days_ago=-1.0)
    high_consensus = _row(release_type="CPI", consensus=3.5, days_ago=-1.0)
    low = consensus_prior_probability(t, [low_consensus])
    high = consensus_prior_probability(t, [high_consensus])
    assert high.probability > low.probability


# ---------------------------------------------------------------------------
# Per-vertical Kelly multiplier override (gate)
# ---------------------------------------------------------------------------


def _ctx(**overrides) -> RiskContext:
    base = dict(
        market_id="KXCPI-24DEC-ABOVE3",
        side="YES",
        league="EVENTS_ECON",
        vertical="events-econ",
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
        operator_overrides={"kelly_multiplier_events_econ": 0.20},
    )))
    assert res.details.get("kelly_multiplier") == pytest.approx(0.20)


# ---------------------------------------------------------------------------
# Pre-release blackout (gate_market_cooldown sub-check)
# ---------------------------------------------------------------------------


def _fake_resolved_row(release_at):
    return ReleaseRow(
        source="trading_economics_rss",
        signal_id="fake/CPI/2024-12-15",
        release_type="CPI",
        release_at=release_at,
        consensus=2.5, previous=2.4, actual=None,
        revised_previous=None, payload={},
    )


def test_pre_release_blackout_rejects_within_window():
    """5-min before THIS ticker's release prints, the gate rejects new entries."""
    next_release = datetime.now(timezone.utc) + timedelta(minutes=2)

    async def _fake_resolved(_ticker):
        return _fake_resolved_row(next_release)

    with patch(
        "app.events_econ.calendar.resolved_release_for_ticker",
        side_effect=_fake_resolved,
    ):
        res = asyncio.run(gate_market_cooldown(_ctx()))
    assert res.passed is False
    assert res.details.get("reason") == "pre_release_blackout"
    assert res.details.get("release_type") == "CPI"


def test_pre_release_blackout_passes_outside_window():
    """Outside the 5-minute window, the gate falls through."""
    next_release = datetime.now(timezone.utc) + timedelta(hours=4)

    async def _fake_resolved(_ticker):
        return _fake_resolved_row(next_release)

    with patch(
        "app.events_econ.calendar.resolved_release_for_ticker",
        side_effect=_fake_resolved,
    ):
        res = asyncio.run(gate_market_cooldown(_ctx()))
    if not res.passed:
        assert res.details.get("reason") != "pre_release_blackout"


def test_pre_release_blackout_only_scopes_this_ticker():
    """When THIS ticker's release isn't imminent, the gate must not block.

    Verifies the fix for the bug where a near-month CPI release would
    incorrectly blackout a far-month CPI contract.
    """
    # The ticker is for KXCPI-25MAR (March 2025); resolved release
    # for THIS ticker is months out, so no blackout.
    far_release = datetime.now(timezone.utc) + timedelta(days=60)

    async def _fake_resolved(_ticker):
        return _fake_resolved_row(far_release)

    ctx = _ctx(market_id="KXCPI-25MAR-ABOVE3")
    with patch(
        "app.events_econ.calendar.resolved_release_for_ticker",
        side_effect=_fake_resolved,
    ):
        res = asyncio.run(gate_market_cooldown(ctx))
    if not res.passed:
        assert res.details.get("reason") != "pre_release_blackout"


# ---------------------------------------------------------------------------
# Window parsing + ticker-scoped release resolution
# ---------------------------------------------------------------------------


def test_parse_when_to_window_month():
    from app.events_econ.calendar import parse_when_to_window
    win = parse_when_to_window("24DEC")
    assert win is not None
    start, end = win
    assert start.year == 2024 and start.month == 12 and start.day == 1
    assert end.year == 2024 and end.month == 12


def test_parse_when_to_window_quarter():
    from app.events_econ.calendar import parse_when_to_window
    win = parse_when_to_window("25Q1")
    assert win is not None
    start, end = win
    assert start.year == 2025 and start.month == 1
    assert end.month == 3


def test_parse_when_to_window_week():
    from app.events_econ.calendar import parse_when_to_window
    win = parse_when_to_window("25W08")
    assert win is not None


def test_parse_when_to_window_unparseable():
    from app.events_econ.calendar import parse_when_to_window
    assert parse_when_to_window("garbage") is None
    assert parse_when_to_window("") is None
    assert parse_when_to_window(None) is None


# ---------------------------------------------------------------------------
# FOMC threshold unit (basis points -> percentage points)
# ---------------------------------------------------------------------------


def test_fomc_hike25_token_parses_as_quarter_pp():
    """HIKE25 must be 25bp = 0.25pp, not 25.0."""
    from app.events_econ.model import _parse_threshold_token
    val, direction = _parse_threshold_token("HIKE25")
    assert val == pytest.approx(0.25)
    assert direction == "above"


def test_fomc_cut50_token_parses_as_half_pp_below():
    from app.events_econ.model import _parse_threshold_token
    val, direction = _parse_threshold_token("CUT50")
    assert val == pytest.approx(0.50)
    assert direction == "below"


def test_fomc_threshold_is_pp_not_raw_bp():
    """ConsensusPriorResult.threshold must reflect 0.25pp for HIKE25,
    not the raw 25.0 that the previous bug produced."""
    t = parse_events_econ_ticker("KXFOMC-25MAR-HIKE25")
    upcoming = _row(release_type="FOMC", consensus=0.0, days_ago=-1.0)
    history = [_row(release_type="FOMC", consensus=0.0, actual=0.25 * (i % 2))
               for i in range(20)]
    res = consensus_prior_probability(t, [upcoming, *history])
    assert res.threshold == pytest.approx(0.25)
    assert res.direction == "above"


# ---------------------------------------------------------------------------
# Promotion thresholds
# ---------------------------------------------------------------------------


def test_promotion_constants():
    assert promotion_mod.SETTLED_THRESHOLD == 30
    assert promotion_mod.BRIER_THRESHOLD == pytest.approx(0.20)
    assert promotion_mod.DEFAULT_MULTIPLIER == pytest.approx(0.15)
    assert promotion_mod.PROMOTED_MULTIPLIER == pytest.approx(0.20)
    assert promotion_mod.SETTING_KEY == "risk.kelly_multiplier_events_econ"
    assert promotion_mod.VERTICAL == "events-econ"


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
    assert "need 30 settled trades" in status.reason


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


# ---------------------------------------------------------------------------
# Variance estimator falls back to (previous, actual) when consensus is missing
# ---------------------------------------------------------------------------


def test_variance_uses_previous_when_consensus_missing():
    """The 2-yr FRED back-fill carries `previous` instead of `consensus`.

    The variance estimator must still learn a sigma from those rows so
    the model isn't permanently stuck on the per-release-type default.
    """
    from app.events_econ.model import _rolling_forecast_error_sigma
    # Errors vary so the variance estimator gets a non-zero sigma.
    history = [
        _row(release_type="CPI", previous=2.0, actual=2.0 + (i - 10) * 0.05,
             days_ago=30 + i)
        for i in range(20)
    ]
    sigma, n = _rolling_forecast_error_sigma(history)
    assert n == 20
    assert sigma > 0.0


# ---------------------------------------------------------------------------
# Kalshi mid blend (predictor)
# ---------------------------------------------------------------------------


def test_kalshi_mid_blend_pulls_probability_toward_market():
    """When kalshi_mid is provided, probability blends toward it in log-odds."""
    import asyncio
    from app.events_econ import predictor as ee_pred
    from unittest.mock import patch

    upcoming = _row(release_type="CPI", consensus=3.0, days_ago=-1.0)
    history = [_row(release_type="CPI", consensus=3.0, actual=3.0 + (i-5)*0.05)
               for i in range(15)]

    async def _fake_resolve(ticker, *, lookback_days=730):
        return [upcoming, *history]

    with patch.object(ee_pred, "resolve_releases", side_effect=_fake_resolve):
        # No kalshi_mid -> baseline probability.
        baseline = asyncio.run(
            ee_pred.predict_events_econ_binary("KXCPI-24DEC-ABOVE3")
        )
        # kalshi_mid = 0.80 -> probability shifts up toward market.
        with_market_high = asyncio.run(
            ee_pred.predict_events_econ_binary(
                "KXCPI-24DEC-ABOVE3", kalshi_mid=0.80,
            )
        )
        # kalshi_mid = 0.20 -> probability shifts down.
        with_market_low = asyncio.run(
            ee_pred.predict_events_econ_binary(
                "KXCPI-24DEC-ABOVE3", kalshi_mid=0.20,
            )
        )

    assert with_market_high.probability > baseline.probability
    assert with_market_low.probability < baseline.probability
    assert with_market_high.raw["kalshi_blend"]["applied"] is True
    assert with_market_high.raw["kalshi_blend"]["weight"] > 0.0
    assert baseline.raw["kalshi_blend"]["applied"] is False


# ---------------------------------------------------------------------------
# /api/events-econ/calendar route
# ---------------------------------------------------------------------------


def test_calendar_route_populates_model_prob():
    """Smoke-test the public route: model_prob must be a real float
    (not null) for normal upcoming-release rows.  Catches dataclass
    field/constant typos in at_consensus_probability."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.events_econ import calendar as ee_cal
    from unittest.mock import patch
    from datetime import datetime, timezone, timedelta

    upcoming = ReleaseRow(
        source="trading_economics_rss",
        signal_id="te/CPI/2025-03-12",
        release_type="CPI",
        release_at=datetime.now(timezone.utc) + timedelta(hours=2),
        consensus=2.7, previous=2.5, actual=None,
        revised_previous=None, payload={},
    )
    history = [
        ReleaseRow(
            source="te", signal_id=f"te/CPI/{i}", release_type="CPI",
            release_at=datetime.now(timezone.utc) - timedelta(days=30 * (i + 1)),
            consensus=2.5 + 0.05 * i,
            previous=2.4, actual=2.5 + 0.05 * i + (i - 5) * 0.02,
            revised_previous=None, payload={},
        ) for i in range(15)
    ]

    async def _fake_upcoming(within_hours=168.0, limit=50):
        return [upcoming]

    async def _fake_settled(release_type, **_kw):
        return history

    async def _fake_fetchval(_q, _arg):
        return None

    with patch.object(ee_cal, "upcoming_releases", side_effect=_fake_upcoming), \
         patch.object(ee_cal, "settled_history_for_type", side_effect=_fake_settled), \
         patch("app.main.db.fetchval", side_effect=_fake_fetchval):
        client = TestClient(app)
        resp = client.get("/api/events-econ/calendar")
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body and len(body["items"]) == 1
    item = body["items"][0]
    assert item["model_prob"] is not None
    assert 0.05 <= item["model_prob"] <= 0.95
    assert item["release_type"] == "CPI"
    assert item["kalshi_ticker_prefix"] is not None
    assert item["kalshi_ticker_prefix"].startswith("KXCPI-")


def test_release_history_route_returns_settled_with_surprise():
    """`/api/events-econ/release-history` returns settled prints (actual
    set) with the computed surprise (actual − consensus) and a colored
    direction tag the dashboard uses."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.events_econ import calendar as ee_cal

    settled = [
        ReleaseRow(
            source="te", signal_id="te/CPI/2025-03-12", release_type="CPI",
            release_at=datetime.now(timezone.utc) - timedelta(days=2),
            consensus=2.7, previous=2.5, actual=2.9,
            revised_previous=None, payload={},
        ),
        ReleaseRow(
            source="te", signal_id="te/NFP/2025-03-07", release_type="NFP",
            release_at=datetime.now(timezone.utc) - timedelta(days=7),
            consensus=180.0, previous=175.0, actual=150.0,
            revised_previous=None, payload={},
        ),
        ReleaseRow(
            source="te", signal_id="te/PPI/2025-03-05", release_type="PPI",
            release_at=datetime.now(timezone.utc) - timedelta(days=9),
            consensus=0.3, previous=0.2, actual=0.3,
            revised_previous=None, payload={},
        ),
    ]

    async def _fake_recent(*, lookback_days=90, limit=20):
        return settled

    with patch.object(ee_cal, "recent_releases", side_effect=_fake_recent):
        client = TestClient(app)
        resp = client.get("/api/events-econ/release-history?limit=20")

    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body and len(body["items"]) == 3
    by_type = {it["release_type"]: it for it in body["items"]}

    assert by_type["CPI"]["actual"] == 2.9
    assert by_type["CPI"]["consensus"] == 2.7
    assert abs(by_type["CPI"]["surprise"] - 0.2) < 1e-6
    assert by_type["CPI"]["direction"] == "above"

    assert by_type["NFP"]["surprise"] == -30.0
    assert by_type["NFP"]["direction"] == "below"

    assert by_type["PPI"]["surprise"] == 0.0
    assert by_type["PPI"]["direction"] == "inline"


def test_recent_releases_only_returns_settled_rows():
    """`recent_releases()` must filter on actual IS NOT NULL — verified
    by inspecting the SQL passed to db.fetch."""
    from app.events_econ import calendar as ee_cal

    captured: dict[str, object] = {}

    async def _fake_fetch(query, *args):
        captured["query"] = query
        captured["args"] = args
        return []

    with patch.object(ee_cal.db, "fetch", side_effect=_fake_fetch):
        rows = asyncio.run(ee_cal.recent_releases(lookback_days=30, limit=10))

    assert rows == []
    assert "actual IS NOT NULL" in captured["query"]
    assert "ORDER BY release_at DESC" in captured["query"]


def test_promotion_eligible_when_thresholds_met():
    async def _fake_read_mult():
        return promotion_mod.DEFAULT_MULTIPLIER

    async def _fake_fetchrow(_q, _vertical):
        return {"n": 50, "brier": 0.15}

    with patch.object(promotion_mod, "_read_current_multiplier",
                      side_effect=_fake_read_mult), \
         patch.object(promotion_mod.db, "fetchrow",
                      side_effect=_fake_fetchrow):
        status = asyncio.run(promotion_mod.evaluate_promotion())
    assert status.eligible is True
    assert status.reason == "eligible"


# ---------------------------------------------------------------------------
# Per-feed `records_written_total` cumulative counter (task #223)
# ---------------------------------------------------------------------------


def test_registry_tracks_records_written_total():
    """Cumulative `records_written_total` increments across successful polls,
    so the dashboard can spot a degraded source whose per-poll count drops
    to zero even while `success_count` keeps climbing."""
    from app.feeds.registry import Feed, FeedRegistry

    reg = FeedRegistry()

    async def _noop():
        return 0

    reg.register(Feed(name="econ_calendar.test", cadence_seconds=60.0, poll=_noop))

    async def _drive():
        for written in (3, 5, 0, 2):
            started = await reg.record_start("econ_calendar.test")
            await reg.record_success("econ_calendar.test", started, written)

    asyncio.run(_drive())
    snap = {s["name"]: s for s in reg.snapshot()}["econ_calendar.test"]
    assert snap["records_written_total"] == 10
    assert snap["last_records_written"] == 2
    assert snap["success_count"] == 4


def test_feeds_status_route_exposes_econ_counters():
    """`/api/feeds/status` must surface both the per-poll `last_records_written`
    AND the cumulative `records_written_total` for every econ feed so the
    operator dashboard can spot a degraded source even when the feed keeps
    polling successfully but stops writing rows."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.auth import require_auth
    from app.feeds.registry import registry as live_registry, Feed

    # Drop in three test econ feeds so we don't depend on lifespan having
    # actually wired up the real ones (some are env-gated).
    async def _noop():
        return 0

    test_names = (
        "econ_calendar.calendar.test",
        "econ_calendar.actuals.test",
        "econ_calendar.fred.test",
    )
    for n in test_names:
        try:
            live_registry.register(Feed(name=n, cadence_seconds=60.0, poll=_noop))
        except ValueError:
            pass

    async def _drive():
        for n in test_names:
            started = await live_registry.record_start(n)
            await live_registry.record_success(n, started, 4)

    asyncio.run(_drive())

    app.dependency_overrides[require_auth] = lambda: {"id": "test", "role": "admin"}
    try:
        client = TestClient(app)
        resp = client.get("/api/feeds/status")
    finally:
        app.dependency_overrides.pop(require_auth, None)

    assert resp.status_code == 200
    body = resp.json()
    by_name = {it["name"]: it for it in body["items"]}
    for n in test_names:
        assert n in by_name, f"missing {n} in /api/feeds/status"
        item = by_name[n]
        assert "last_records_written" in item
        assert "records_written_total" in item
        assert item["last_records_written"] == 4
        assert item["records_written_total"] == 4


# ---------------------------------------------------------------------------
# Live integration: hit the 3 real upstream econ feeds (network-gated).
#
# Skipped by default so CI without internet stays green.  Opt in with
# `AXIOM_RUN_LIVE_FEED_TESTS=1` to run a smoke check against the real
# Trading Economics RSS, BLS RSS, and FRED CSV endpoints.  Each upstream
# is asserted independently so a single source going down only fails one
# sub-assertion, not the entire test.
# ---------------------------------------------------------------------------


pytestmark_live = pytest.mark.skipif(
    os.environ.get("AXIOM_RUN_LIVE_FEED_TESTS", "").lower() not in {"1", "true", "yes", "on"},
    reason="live upstream feed test; set AXIOM_RUN_LIVE_FEED_TESTS=1 to run",
)


_REQUIRED_KEYS = {"source", "signal_id", "release_type", "release_at"}


def _assert_econ_release_shape(rows: list[dict]) -> None:
    assert len(rows) >= 1, "no rows parsed from upstream"
    sample = rows[0]
    missing = _REQUIRED_KEYS - sample.keys()
    assert not missing, f"row missing required keys: {missing}; row={sample}"
    assert isinstance(sample["release_at"], datetime)
    assert sample["release_at"].tzinfo is not None
    assert isinstance(sample["release_type"], str) and sample["release_type"]
    assert isinstance(sample["signal_id"], str) and sample["signal_id"]


@pytestmark_live
def test_live_trading_economics_rss_parses_at_least_one_row():
    from app.feeds import econ_calendar as ec

    rows = asyncio.run(ec._fetch_calendar_rss())
    _assert_econ_release_shape(rows)
    assert all(r["source"] == "trading_economics_rss" for r in rows)


@pytestmark_live
def test_live_bls_releases_rss_parses_at_least_one_row():
    """Drives `poll_actuals` end-to-end but stubs the DB upsert so the
    parser is exercised against the real BLS RSS without any side effect."""
    from app.feeds import econ_calendar as ec

    captured: list[dict] = []

    async def _capture(rows):
        for r in rows:
            captured.append(r)
        return len(captured)

    with patch.object(ec, "upsert_releases", side_effect=_capture):
        asyncio.run(ec.poll_actuals())

    _assert_econ_release_shape(captured)
    assert all(r["source"] == "bls_releases_rss" for r in captured)


@pytestmark_live
def test_live_fred_backfill_parses_at_least_one_row():
    """Drives `poll_fred_backfill` against the real FRED CSV endpoint with
    the DB upsert stubbed so we only verify the parser shape."""
    from app.feeds import econ_calendar as ec

    captured: list[dict] = []

    async def _capture(rows):
        for r in rows:
            captured.append(r)
        return len(captured)

    with patch.object(ec, "upsert_releases", side_effect=_capture):
        asyncio.run(ec.poll_fred_backfill())

    _assert_econ_release_shape(captured)
    assert all(r["source"] == "fred" for r in captured)
    # FRED back-fill carries actuals + naive-consensus payload tag.
    sample = captured[0]
    assert sample.get("actual") is not None
    assert sample.get("payload", {}).get("naive_consensus") is True
