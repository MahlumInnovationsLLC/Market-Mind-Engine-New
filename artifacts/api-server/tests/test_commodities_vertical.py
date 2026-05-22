"""Tests for the commodities trading vertical (task #198)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import verticals as vertical_registry
from app.commodities import (
    InvalidCommodityTicker,
    SUPPORTED_SYMBOLS,
    correlated_exposure,
    correlation_for,
    parse_commodity_ticker,
)
from app.commodities.correlations import CORRELATED_PAIRS
from app.commodities.features import (
    FEATURE_COUNT,
    FEATURE_NAMES,
    build_features_from_bars,
)
from app.commodities.models import BaselineLGBXGBStack
from app.risk.gates import GATES, RiskContext, gate_per_vertical_cap


# ---------------------------------------------------------------------------
# Ticker parser
# ---------------------------------------------------------------------------


def test_parse_simple_oil_ticker():
    t = parse_commodity_ticker("KXOIL-26APR15-T75")
    assert t.symbol == "WTI"
    assert t.timeframe == "1d"
    assert t.threshold == 75.0
    assert t.direction == "above"


def test_parse_below_direction():
    t = parse_commodity_ticker("KXGOLD-26MAY15-B2400")
    assert t.symbol == "GOLD"
    assert t.threshold == 2400.0
    assert t.direction == "below"


def test_parse_intraday_natgas():
    t = parse_commodity_ticker("KXNATGAS1H-26APR1517-T2.50")
    assert t.symbol == "NG"
    assert t.timeframe == "1h"
    assert t.threshold == pytest.approx(2.50)


def test_parse_grains_and_softs():
    assert parse_commodity_ticker("KXCORN-26MAY-T450").symbol == "CORN"
    assert parse_commodity_ticker("KXSOY-26MAY-T1100").symbol == "SOY"
    assert parse_commodity_ticker("KXWHEAT-26JUN-T550").symbol == "WHEAT"
    assert parse_commodity_ticker("KXCOFFEE-26MAR-T200").symbol == "COFFEE"
    assert parse_commodity_ticker("KXSUGAR-26MAR-T22").symbol == "SUGAR"


def test_parse_rejects_unknown_symbol():
    with pytest.raises(InvalidCommodityTicker):
        parse_commodity_ticker("KXFOO-26APR-T100")


def test_parse_rejects_blank():
    with pytest.raises(InvalidCommodityTicker):
        parse_commodity_ticker("")


def test_parse_rejects_malformed_shape():
    with pytest.raises(InvalidCommodityTicker):
        parse_commodity_ticker("KXOIL_26APR")


def test_supported_symbols_cover_ten():
    assert len(SUPPORTED_SYMBOLS) == 10
    assert "WTI" in SUPPORTED_SYMBOLS and "SUGAR" in SUPPORTED_SYMBOLS


# ---------------------------------------------------------------------------
# Vertical registry
# ---------------------------------------------------------------------------


def test_commodities_vertical_registered():
    spec = vertical_registry.get_spec("commodities")
    assert spec is not None
    assert spec.predictor_hook == "commodities_binary"
    assert spec.persona_profile == "commodities"
    assert spec.kelly_multiplier == pytest.approx(0.15)
    assert spec.default_allocation_pct == pytest.approx(0.10)
    assert "KXOIL" in spec.ticker_prefixes
    assert "KXSUGAR" in spec.ticker_prefixes
    assert spec.duplicate_trade_exempt is True


def test_commodities_listed_in_calibrators_supported():
    from app.calibrators import SUPPORTED_LEAGUES

    assert "COMMODITIES" in SUPPORTED_LEAGUES


# ---------------------------------------------------------------------------
# Correlation matrix
# ---------------------------------------------------------------------------


def test_correlation_matrix_has_eleven_pairs():
    assert len(CORRELATED_PAIRS) == 11


def test_correlation_lookup_symmetric():
    assert correlation_for("GOLD", "SILVER") == pytest.approx(0.78)
    assert correlation_for("SILVER", "GOLD") == pytest.approx(0.78)
    assert correlation_for("GOLD", "GOLD") == 1.0
    assert correlation_for("GOLD", "COFFEE") == 0.0


def test_correlated_exposure_aggregates():
    # 100 USD open on SILVER → contributes 0.78 * 100 to a GOLD candidate.
    # 50 USD open on COFFEE → uncorrelated to GOLD (rho=0), excluded.
    report = correlated_exposure(
        {"SILVER": 100.0, "COFFEE": 50.0},
        intended_symbol="GOLD",
        intended_notional=20.0,
    )
    assert report["correlated_open"] == pytest.approx(78.0)
    assert report["projected"] == pytest.approx(98.0)
    assert "SILVER" in report["pair_contributions"]
    assert "COFFEE" not in report["pair_contributions"]


def test_correlated_exposure_self_pair_full_weight():
    # Re-buying GOLD when 200 USD is already open should count fully.
    report = correlated_exposure(
        {"GOLD": 200.0},
        intended_symbol="GOLD",
        intended_notional=10.0,
    )
    assert report["correlated_open"] == pytest.approx(200.0)
    assert report["projected"] == pytest.approx(210.0)


# ---------------------------------------------------------------------------
# Feature builder + baseline predictor
# ---------------------------------------------------------------------------


def _synthetic_bars(n: int = 60, *, base: float = 75.0,
                    trend: float = 0.001) -> list[dict]:
    """Slightly trending bars for deterministic feature output."""
    now = datetime.now(timezone.utc)
    bars = []
    price = base
    for i in range(n):
        price *= 1.0 + trend
        bars.append({
            "ts": now - timedelta(hours=(n - i)),
            "o": price * 0.999, "h": price * 1.002,
            "l": price * 0.997, "c": price,
            "v": 1000.0 + i,
        })
    return bars


def test_feature_builder_produces_expected_width():
    t = parse_commodity_ticker("KXOIL-26APR-T80")
    fv = build_features_from_bars(_synthetic_bars(), t)
    assert len(fv.values) == FEATURE_COUNT
    assert set(fv.as_dict().keys()) == set(FEATURE_NAMES)
    assert fv.last_price > 0


def test_baseline_stack_returns_well_formed_proba():
    t = parse_commodity_ticker("KXOIL-26APR-T80")
    fv = build_features_from_bars(_synthetic_bars(trend=0.002), t)
    stack = BaselineLGBXGBStack(timeframe="1d")
    import numpy as np

    proba = stack.predict_proba(np.asarray([fv.values]))
    assert proba.shape == (1, 2)
    p_yes = float(proba[0, 1])
    assert 0.0 <= p_yes <= 1.0
    # Each row must sum to 1.0.
    assert float(proba[0].sum()) == pytest.approx(1.0)


def test_baseline_stack_directionality_above_vs_below():
    """A market that's already above the strike, with bullish momentum,
    should have HIGHER YES probability for an `above` direction than for
    the same setup re-cast as a `below` direction."""
    bars = _synthetic_bars(trend=0.003)
    above = parse_commodity_ticker("KXOIL-26APR-T75")  # bars trend through 75
    below = parse_commodity_ticker("KXOIL-26APR-B75")
    stack = BaselineLGBXGBStack(timeframe="1d")
    import numpy as np

    fv_above = build_features_from_bars(bars, above)
    fv_below = build_features_from_bars(bars, below)
    p_above = stack.predict_proba(np.asarray([fv_above.values]))[0, 1]
    p_below = stack.predict_proba(np.asarray([fv_below.values]))[0, 1]
    assert p_above > p_below


# ---------------------------------------------------------------------------
# 12-gate invariant + commodities correlation sub-check
# ---------------------------------------------------------------------------


def test_twelve_gate_invariant_unchanged():
    # Bumped 14 → 17 in task #730 (sharpe_ratio + ruin_monte_carlo +
    # segment_throttler audit gates).  The point of this assertion is
    # still that *commodities* sub-checks fold into existing gates
    # rather than adding new entries — only cross-vertical risk plumbing
    # is allowed to grow this number.
    # 17 → 24 in task #830 (stale_live_state + live_kelly_multiplier).
    assert len(GATES) == 24


def _commodities_ctx(**overrides) -> RiskContext:
    base = dict(
        market_id="KXGOLD-26APR15-T2400",
        side="YES",
        vertical="commodities",
        league="COMMODITIES",
        market_type="binary",
        matchup="GOLD",
        current_price=0.50,
        model_probability=0.60,
        edge=0.10,
        confidence_band_low=0.55,
        confidence_band_high=0.65,
        agreement_rate=0.80,
        swarm_approved=True,
        swarm_recommended_action="BUY_YES",
        debate_id=999,
        intended_quantity=10,
        intended_notional_usd=50.0,
        bankroll_usd=10_000.0,
        open_positions_count=0,
        open_positions_notional_usd=0.0,
        open_positions_per_vertical_usd={},
        operator_overrides={},
        market_cooldown_until=None,
    )
    base.update(overrides)
    return RiskContext(**base)


def test_per_vertical_cap_passes_under_correlation_cap():
    ctx = _commodities_ctx(
        intended_notional_usd=50.0,
        open_positions_per_matchup_usd={"COMMODITIES:SILVER": 100.0},
    )
    res = asyncio.run(gate_per_vertical_cap(ctx))
    assert res.passed, res.reason


def test_per_vertical_cap_rejects_correlated_overflow():
    """Tight correlation cap + heavy SILVER exposure should reject a
    new GOLD bet via the commodities_correlation sub-check."""
    ctx = _commodities_ctx(
        intended_notional_usd=200.0,
        # 0.78 * 3000 + 200 = 2540 → cap of 0.20 * 10_000 = 2000 fires.
        operator_overrides={"commodities_correlation_cap_pct": 0.20},
        open_positions_per_matchup_usd={"COMMODITIES:SILVER": 3000.0},
    )
    res = asyncio.run(gate_per_vertical_cap(ctx))
    assert not res.passed
    assert res.details["sub_check"] == "commodities_correlation"
    assert res.details["intended_symbol"] == "GOLD"
    assert "SILVER" in res.details["pair_contributions"]


def test_end_to_end_predictor_from_bars(monkeypatch):
    """End-to-end cold path: synthetic bars -> commodities_ohlcv stub
    -> predict_commodity_binary -> Prediction.

    Validates the full pipeline including model loader fallback to
    baseline, calibrator application (no-op in test env), and the
    Prediction dataclass shape orchestrator consumes.
    """
    from app.commodities import predictor as cp

    bars = _synthetic_bars(n=80, base=75.0, trend=0.0015)
    db_rows = [
        {"ts": b["ts"], "o": b["o"], "h": b["h"],
         "l": b["l"], "c": b["c"], "v": b["v"]}
        for b in reversed(bars)  # build_commodity_features fetches DESC then re-reverses
    ]

    async def fake_fetch(query, *args, **kwargs):
        return db_rows

    monkeypatch.setattr(cp, "build_commodity_features",
                        cp.build_commodity_features)  # keep real builder
    from app import db as app_db
    monkeypatch.setattr(app_db, "fetch", fake_fetch)

    pred = asyncio.run(cp.predict_commodity_binary("KXOIL-26APR-T80"))
    assert 0.0 <= pred.probability <= 1.0
    assert pred.model_name.startswith("commodities:1d:")
    assert pred.elapsed_ms is not None and pred.elapsed_ms >= 0
    raw = pred.raw or {}
    assert raw["ticker"]["symbol"] == "WTI"
    assert raw["ticker"]["timeframe"] == "1d"
    assert raw["ticker"]["threshold"] == 80.0
    assert raw["feature_count"] == FEATURE_COUNT
    assert raw["feature_present"] == len(db_rows)
    assert "calibration" in raw


def test_end_to_end_predictor_handles_missing_bars(monkeypatch):
    """Empty OHLCV cache must surface as CommodityPredictionError so the
    cycle's `_predict_for_commodities_binary` branch can degrade to
    swarm-only signals (rather than crash the orchestrator)."""
    from app.commodities import predictor as cp
    from app import db as app_db

    async def empty_fetch(query, *args, **kwargs):
        return []

    monkeypatch.setattr(app_db, "fetch", empty_fetch)
    with pytest.raises(cp.CommodityPredictionError):
        asyncio.run(cp.predict_commodity_binary("KXOIL-26APR-T80"))


def test_upsert_bars_writes_rows(monkeypatch):
    """Feed-side smoke test: upsert_bars hands each bar to db.execute
    and reports the count of rows touched.  Guards against silent
    breakage of the (symbol, granularity, ts) UPSERT path."""
    from app.feeds import commodities as feed
    from app import db as app_db

    calls: list[tuple] = []

    async def fake_execute(query, *args, **kwargs):
        calls.append(args)

    monkeypatch.setattr(app_db, "execute", fake_execute)
    bars = [
        {"ts": datetime.now(timezone.utc), "o": 75.0, "h": 75.5,
         "l": 74.8, "c": 75.2, "v": 1000.0},
        {"ts": datetime.now(timezone.utc), "o": 75.2, "h": 75.6,
         "l": 75.0, "c": 75.4, "v": 1100.0},
    ]
    n = asyncio.run(feed.upsert_bars("WTI", "1h", bars, source="test"))
    assert n == 2
    assert len(calls) == 2
    # Validates the binding order matches the SQL: symbol, granularity, ts, o, h, l, c, v, source
    args0 = calls[0]
    assert args0[0] == "WTI"
    assert args0[1] == "1h"
    assert args0[8] == "test"


def test_eia_fetch_bars_parses_daily_series(monkeypatch):
    """Live-shape EIA payload → oldest-first OHLCV bars with synthesised
    O==H==L==C and zero volume.  Skips intraday gracefully."""
    from app.feeds import commodities as feed
    import httpx

    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={
            "response": {
                "data": [
                    {"period": "2026-04-22", "value": 81.34},
                    {"period": "2026-04-21", "value": 80.91},
                    {"period": "2026-04-20", "value": 80.55},
                ]
            }
        })

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setenv("EIA_API_KEY", "test-eia")
    monkeypatch.setattr(feed.httpx, "AsyncClient", fake_client)

    bars = asyncio.run(feed._eia_fetch_bars("WTI", "1d"))
    assert len(bars) == 3
    # Oldest-first
    assert bars[0]["ts"] < bars[-1]["ts"]
    assert bars[-1]["c"] == pytest.approx(81.34)
    # OHLC collapsed onto the single daily settlement value.
    assert bars[0]["o"] == bars[0]["h"] == bars[0]["l"] == bars[0]["c"]
    assert bars[0]["v"] == 0.0
    # Confirms we hit the right series id.
    assert "PET.RWTC.D" in captured["url"]
    # Intraday is intentionally not served by EIA.
    assert asyncio.run(feed._eia_fetch_bars("WTI", "1h")) == []


def test_alphavantage_fetch_bars_parses_daily_payload(monkeypatch):
    """AlphaVantage TIME_SERIES_DAILY → ETF OHLCV bars, oldest-first."""
    from app.feeds import commodities as feed
    import httpx

    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={
            "Meta Data": {"2. Symbol": "GLD"},
            "Time Series (Daily)": {
                "2026-04-22": {"1. open": "215.10", "2. high": "216.40",
                               "3. low": "214.55", "4. close": "215.92",
                               "5. volume": "5421000"},
                "2026-04-21": {"1. open": "213.80", "2. high": "215.20",
                               "3. low": "213.50", "4. close": "214.95",
                               "5. volume": "4980000"},
            },
        })

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", "test-av")
    monkeypatch.setattr(feed.httpx, "AsyncClient", fake_client)

    bars = asyncio.run(feed._alphavantage_fetch_bars("GOLD", "1d"))
    assert len(bars) == 2
    assert bars[0]["ts"] < bars[-1]["ts"]
    assert bars[-1]["c"] == pytest.approx(215.92)
    assert bars[-1]["v"] == pytest.approx(5421000.0)
    # Confirm we routed via the GLD proxy ticker.
    assert "symbol=GLD" in captured["url"]
    assert "function=TIME_SERIES_DAILY" in captured["url"]


def test_alphavantage_soft_error_returns_empty(monkeypatch):
    """AlphaVantage rate-limit notes must yield zero bars (so the
    fallback can take over) instead of crashing."""
    from app.feeds import commodities as feed
    import httpx

    transport = httpx.MockTransport(lambda r: httpx.Response(
        200, json={"Note": "rate limit, slow down"}
    ))
    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", "test-av")
    monkeypatch.setattr(feed.httpx, "AsyncClient", fake_client)
    assert asyncio.run(feed._alphavantage_fetch_bars("GOLD", "1d")) == []


def test_alphavantage_4h_aggregates_intraday_into_buckets(monkeypatch):
    """4h granularity is synthesised by bucketing the 60min intraday
    series — open=first, high=max, low=min, close=last, vol=sum."""
    from app.feeds import commodities as feed
    import httpx

    # Eight consecutive hourly bars starting 2026-04-22 12:00Z.
    series: dict[str, dict[str, str]] = {}
    base_ts = datetime(2026, 4, 22, 12, 0, 0)
    for i in range(8):
        ts = base_ts + timedelta(hours=i)
        series[ts.strftime("%Y-%m-%d %H:%M:%S")] = {
            "1. open": f"{100 + i}.00",
            "2. high": f"{100 + i + 1}.00",
            "3. low":  f"{100 + i - 1}.00",
            "4. close": f"{100 + i}.50",
            "5. volume": f"{1000 + i}",
        }
    transport = httpx.MockTransport(lambda r: httpx.Response(
        200, json={"Time Series (60min)": series}
    ))
    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", "test-av")
    monkeypatch.setattr(feed.httpx, "AsyncClient", fake_client)

    bars = asyncio.run(feed._alphavantage_fetch_bars("COPPER", "4h"))
    # 8 hourly bars → 2 four-hour buckets.
    assert len(bars) == 2
    assert all(b["ts"].minute == 0 and b["ts"].hour % 4 == 0 for b in bars)
    # First bucket spans hours 0..3 of the synthesised series; volume sums.
    assert bars[0]["v"] == pytest.approx(sum(1000 + i for i in range(4)))


def test_eia_skips_grains_by_design(monkeypatch):
    """EIA's series-id endpoint has no grain spot prices, so the EIA
    fetcher must return [] for CORN/SOY/WHEAT and let AlphaVantage take
    over.  Routing reflects this: grains have alphavantage as primary."""
    from app.feeds import commodities as feed
    monkeypatch.setenv("EIA_API_KEY", "test-eia")
    for sym in ("CORN", "SOY", "WHEAT"):
        assert asyncio.run(feed._eia_fetch_bars(sym, "1d")) == []
        assert feed._PROVIDER_FOR_SYMBOL[sym] == "alphavantage"


def test_grain_ingest_via_alphavantage_writes_rows(monkeypatch):
    """End-to-end ingest cycle for CORN: AlphaVantage TIME_SERIES_DAILY
    on the CORN ETF proxy should produce real OHLCV bars and the
    `_make_poller` cycle should write them with source='alphavantage'."""
    from app.feeds import commodities as feed
    from app import db as app_db
    import httpx

    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={
            "Meta Data": {"2. Symbol": "CORN"},
            "Time Series (Daily)": {
                "2026-04-22": {"1. open": "21.10", "2. high": "21.40",
                               "3. low": "20.90", "4. close": "21.25",
                               "5. volume": "120000"},
                "2026-04-21": {"1. open": "20.95", "2. high": "21.15",
                               "3. low": "20.80", "4. close": "21.05",
                               "5. volume": "118000"},
            },
        })

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", "test-av")
    # Ensure EIA is configured too — proves grains intentionally skip
    # EIA via _eia_fetch_bars even when the key is present.
    monkeypatch.setenv("EIA_API_KEY", "test-eia")
    monkeypatch.setattr(feed.httpx, "AsyncClient", fake_client)

    written: list[tuple] = []

    async def fake_execute(query, *args, **kwargs):
        written.append(args)

    monkeypatch.setattr(app_db, "execute", fake_execute)

    poll = feed._make_poller("CORN", "1d")
    result = asyncio.run(poll())
    assert result["ok"] is True
    assert result["source"] == "alphavantage"
    assert result["rows"] == 2
    assert "symbol=CORN" in captured["url"]
    # Each upsert call carries source='alphavantage' as the last arg.
    assert all(call[-1] == "alphavantage" for call in written)


def test_no_api_keys_returns_empty(monkeypatch):
    """With neither key configured, both providers must no-op cleanly."""
    from app.feeds import commodities as feed

    monkeypatch.delenv("EIA_API_KEY", raising=False)
    monkeypatch.delenv("ALPHAVANTAGE_API_KEY", raising=False)
    assert asyncio.run(feed._eia_fetch_bars("WTI", "1d")) == []
    assert asyncio.run(feed._alphavantage_fetch_bars("GOLD", "1d")) == []


def test_end_to_end_eia_ingest_then_predict(monkeypatch):
    """Full cold-path integration: EIA HTTP → upsert_bars → predictor.

    Validates that real-shape EIA payloads, ingested through the live
    `_eia_fetch_bars` path and persisted via `upsert_bars`, surface in
    the predictor as a non-default probability (i.e. the feature
    pipeline actually saw the bars).
    """
    from app.feeds import commodities as feed
    from app.commodities import predictor as cp
    from app import db as app_db
    import httpx

    # Build 60 daily WTI bars walking $70 → $82 so the trend-aware
    # baseline stack must produce something other than the cold-start
    # midpoint when asked about a $75 strike.
    eia_data = []
    base_day = datetime(2026, 2, 22)
    for i in range(60):
        eia_data.append({
            "period": (base_day + timedelta(days=i)).strftime("%Y-%m-%d"),
            "value": round(70.0 + (12.0 * i / 59.0), 2),
        })
    # EIA returns newest-first when we ask for desc sort.
    eia_data.reverse()

    transport = httpx.MockTransport(lambda r: httpx.Response(
        200, json={"response": {"data": eia_data}}
    ))
    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setenv("EIA_API_KEY", "test-eia")
    monkeypatch.setattr(feed.httpx, "AsyncClient", fake_client)

    # In-memory store mimicking commodities_ohlcv.
    store: list[dict] = []

    async def fake_execute(query, *args, **kwargs):
        # Args mirror the ordering in upsert_bars()'s INSERT.
        symbol, granularity, ts, o, h, l, c, v, source = args
        for row in store:
            if (row["symbol"] == symbol
                    and row["granularity"] == granularity
                    and row["ts"] == ts):
                row.update(o=o, h=h, l=l, c=c, v=v)
                return
        store.append(dict(symbol=symbol, granularity=granularity, ts=ts,
                          o=o, h=h, l=l, c=c, v=v))

    async def fake_fetch(query, *args, **kwargs):
        symbol, granularity, limit = args
        rows = [r for r in store
                if r["symbol"] == symbol and r["granularity"] == granularity]
        rows.sort(key=lambda r: r["ts"], reverse=True)
        return rows[:limit]

    monkeypatch.setattr(app_db, "execute", fake_execute)
    monkeypatch.setattr(app_db, "fetch", fake_fetch)

    # Drive one poll cycle through the live provider routing.
    poll = feed._make_poller("WTI", "1d")
    poll_result = asyncio.run(poll())
    assert poll_result["ok"] is True
    assert poll_result["source"] == "eia"
    assert poll_result["rows"] == 60
    assert len(store) == 60

    # Now run the cold-path predictor against the freshly-populated
    # store — should produce a real probability, not the 0.50 default.
    pred = asyncio.run(cp.predict_commodity_binary("KXOIL-26MAY-T75"))
    assert pred.model_name.startswith("commodities:1d:")
    assert 0.0 < pred.probability < 1.0
    assert pred.probability != pytest.approx(0.5, abs=1e-6)
    raw = pred.raw or {}
    assert raw["feature_present"] == 60


def test_per_vertical_cap_correlation_ignores_uncorrelated_sym():
    """Heavy COFFEE exposure must NOT trip a GOLD candidate (rho=0)."""
    ctx = _commodities_ctx(
        intended_notional_usd=10.0,
        operator_overrides={"commodities_correlation_cap_pct": 0.05},
        open_positions_per_matchup_usd={"COMMODITIES:COFFEE": 9000.0},
    )
    res = asyncio.run(gate_per_vertical_cap(ctx))
    assert res.passed, res.reason
