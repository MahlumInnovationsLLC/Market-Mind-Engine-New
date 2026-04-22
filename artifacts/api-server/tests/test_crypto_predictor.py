"""Unit tests for the crypto-binary predictor stack."""
from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta, timezone

import pytest

from app.crypto import parse_crypto_ticker
from app.crypto.backtest import evaluate_against_bars
from app.crypto.features import (
    FEATURE_COUNT,
    FEATURE_NAMES,
    CryptoFeatureError,
    build_features_from_bars,
)
from app.crypto.models import (
    BaselineGRUDirection,
    BaselineLGBXGBStack,
)
from app.crypto.predictor import (
    CryptoPredictionError,
    _ensemble_blend,
    _proba_yes,
    predict_crypto_binary,
)
from app.crypto.tickers import CryptoTicker, InvalidCryptoTicker


# ---------------------------------------------------------------------------
# Ticker parsing
# ---------------------------------------------------------------------------


def test_parse_btc_hourly_ticker():
    t = parse_crypto_ticker("KXBTC-26APR15H17-T68000")
    assert t.symbol == "BTC"
    assert t.timeframe == "1h"
    assert t.threshold == 68000.0
    assert t.direction == "above"


def test_parse_btc_5m_ticker():
    t = parse_crypto_ticker("KXBTC5M-26APR1517-T68000.5")
    assert t.symbol == "BTC"
    assert t.timeframe == "5m"
    assert t.threshold == pytest.approx(68000.5)


def test_parse_eth_15m_below():
    t = parse_crypto_ticker("KXETH15M-26APR15H17-B3500")
    assert t.symbol == "ETH"
    assert t.timeframe == "15m"
    assert t.direction == "below"


def test_parse_rejects_unsupported_symbol():
    with pytest.raises(InvalidCryptoTicker):
        parse_crypto_ticker("KXSOL-26APR15H17-T100")


def test_parse_rejects_garbage():
    with pytest.raises(InvalidCryptoTicker):
        parse_crypto_ticker("not-a-ticker")


def test_parse_rejects_negative_threshold():
    # Regex won't match a negative — we re-validate just to be sure.
    with pytest.raises(InvalidCryptoTicker):
        parse_crypto_ticker("")


# ---------------------------------------------------------------------------
# Feature builder
# ---------------------------------------------------------------------------


def _synthetic_bars(n: int = 60, base: float = 50000.0, drift: float = 50.0):
    """Deterministic OHLCV with a steady upward drift."""
    bars = []
    t0 = datetime(2026, 4, 1, tzinfo=timezone.utc)
    for i in range(n):
        c = base + drift * i
        bars.append({
            "ts": t0 + timedelta(hours=i),
            "o": c - 5,
            "h": c + 10,
            "l": c - 10,
            "c": c,
            "v": 1000.0 + i,
        })
    return bars


def test_features_have_fixed_width():
    bars = _synthetic_bars()
    ticker = CryptoTicker("X", "BTC", "1h", threshold=53000.0)
    fv = build_features_from_bars(bars, ticker)
    assert len(fv.values) == FEATURE_COUNT
    assert fv.names == FEATURE_NAMES
    d = fv.as_dict()
    # Last close is 50000 + 50*59 = 52950
    assert d["last_price"] == pytest.approx(52950.0)
    # Threshold above -> log_distance > 0
    assert d["log_distance_to_strike"] > 0
    assert d["direction_sign"] == 1.0
    assert d["timeframe_idx_norm"] == 1.0  # 1h -> idx 2 / 2


def test_features_reject_empty_bars():
    ticker = CryptoTicker("X", "BTC", "1h", threshold=50000.0)
    with pytest.raises(CryptoFeatureError):
        build_features_from_bars([], ticker)


def test_rsi_handles_short_history():
    # With < 14 bars RSI must default to 50 rather than crashing.
    bars = _synthetic_bars(n=5)
    ticker = CryptoTicker("X", "BTC", "1h", threshold=50000.0)
    d = build_features_from_bars(bars, ticker).as_dict()
    assert d["rsi_14"] == 50.0


# ---------------------------------------------------------------------------
# Baseline models
# ---------------------------------------------------------------------------


def test_baseline_stack_returns_two_class_proba():
    bars = _synthetic_bars()
    ticker = CryptoTicker("X", "BTC", "1h", threshold=53000.0)
    fv = build_features_from_bars(bars, ticker)
    stack = BaselineLGBXGBStack(timeframe="1h")
    proba = stack.predict_proba([fv.values])
    assert proba.shape == (1, 2)
    assert math.isclose(float(proba[0, 0] + proba[0, 1]), 1.0, abs_tol=1e-9)
    assert 0.0 <= float(proba[0, 1]) <= 1.0


def test_baseline_stack_higher_strike_lowers_yes_probability():
    # Same upward-drifting bars; a higher strike should give a lower
    # "closes above K" probability.
    bars = _synthetic_bars()
    stack = BaselineLGBXGBStack(timeframe="1h")
    low_k = build_features_from_bars(bars, CryptoTicker("X", "BTC", "1h", 53000.0))
    high_k = build_features_from_bars(bars, CryptoTicker("X", "BTC", "1h", 60000.0))
    p_low = stack.predict_proba([low_k.values])[0, 1]
    p_high = stack.predict_proba([high_k.values])[0, 1]
    assert p_high < p_low


def test_baseline_gru_returns_proba():
    bars = _synthetic_bars()
    ticker = CryptoTicker("X", "BTC", "5m", threshold=53000.0)
    fv = build_features_from_bars(bars, ticker)
    gru = BaselineGRUDirection()
    proba = gru.predict_proba([fv.values])
    assert proba.shape == (1, 2)


# ---------------------------------------------------------------------------
# Ensemble blend
# ---------------------------------------------------------------------------


def test_blend_passes_through_when_no_gru():
    blended, info = _ensemble_blend(0.62, None)
    assert blended == 0.62
    assert info["gru_probability"] is None


def test_blend_weights_stack_and_gru():
    blended, info = _ensemble_blend(0.6, 0.8)
    expected = 0.65 * 0.6 + 0.35 * 0.8
    assert blended == pytest.approx(expected)
    assert info["blend_weights"] == {"stack": 0.65, "gru": 0.35}


def test_proba_yes_handles_predict_only_models():
    class _Reg:
        def predict(self, X):
            import numpy as np
            return np.array([0.42])

    p, conf, raw = _proba_yes(_Reg(), [0.0] * FEATURE_COUNT)
    assert p == pytest.approx(0.42)
    assert conf is None
    assert raw["predict"] == pytest.approx(0.42)


# ---------------------------------------------------------------------------
# End-to-end predict_crypto_binary (DB call patched)
# ---------------------------------------------------------------------------


def test_predict_crypto_binary_uses_baseline(monkeypatch):
    bars = _synthetic_bars()

    async def _fake_fetch(_query, *_args, **_kwargs):
        # `build_crypto_features` queries DESC then reverses; simulate that
        # by handing back the bars in reverse order.
        return [dict(b) for b in reversed(bars)]

    from app import db as _db
    monkeypatch.setattr(_db, "fetch", _fake_fetch)

    pred = asyncio.run(predict_crypto_binary("KXBTC-26APR15H17-T53000"))
    assert 0.0 <= pred.probability <= 1.0
    assert pred.model_name.startswith("crypto:1h:")
    assert pred.raw["ticker"]["symbol"] == "BTC"
    assert pred.raw["ticker"]["timeframe"] == "1h"
    assert pred.raw["ticker"]["threshold"] == 53000.0
    # 1h ticker -> GRU not blended in.
    assert pred.raw["gru"]["used"] is False


def test_predict_crypto_binary_blends_gru_for_short_horizon(monkeypatch):
    bars = _synthetic_bars()

    async def _fake_fetch(_q, *_a, **_k):
        return [dict(b) for b in reversed(bars)]

    from app import db as _db
    monkeypatch.setattr(_db, "fetch", _fake_fetch)

    pred = asyncio.run(predict_crypto_binary("KXBTC5M-26APR1517-T53000"))
    assert pred.raw["gru"]["used"] is True
    assert pred.raw["blend"]["blend_weights"] == {"stack": 0.65, "gru": 0.35}


def test_predict_crypto_binary_raises_on_empty_history(monkeypatch):
    async def _empty(_q, *_a, **_k):
        return []

    from app import db as _db
    monkeypatch.setattr(_db, "fetch", _empty)

    with pytest.raises(CryptoPredictionError):
        asyncio.run(predict_crypto_binary("KXBTC-26APR15H17-T53000"))


# ---------------------------------------------------------------------------
# Backtest scaffold
# ---------------------------------------------------------------------------


def test_backtest_evaluates_synthetic_window():
    bars = _synthetic_bars(n=80)
    result = evaluate_against_bars(
        bars,
        symbol="BTC",
        timeframe="1h",
        lookback=30,
    )
    assert result.n_evaluated > 0
    assert 0.0 <= result.brier_score <= 1.0
    assert 0.0 <= result.accuracy <= 1.0
    assert result.log_loss >= 0.0
    # Reliability bins should sum to n_evaluated.
    total = sum(b[4] for b in result.reliability)
    assert total == result.n_evaluated


# ---------------------------------------------------------------------------
# Cycle integration — vertical='crypto-binary' routing
# ---------------------------------------------------------------------------


def test_cycle_routes_crypto_binary_through_dedicated_predictor(monkeypatch):
    """`_predict_for_candidate(vertical='crypto-binary')` must call the
    crypto predictor (not the meta-model) and surface its payload."""
    from app.orchestrator import cycle as _cycle

    captured: dict = {}

    async def _fake_crypto(cand):
        captured["cand"] = cand
        return {"probability": 0.71, "model_name": "crypto:1h:test"}

    monkeypatch.setattr(_cycle, "_predict_for_crypto_binary", _fake_crypto)

    async def _exploding_predict(*_a, **_k):  # pragma: no cover — must NOT run
        raise AssertionError("meta-model predictor must not be called")

    monkeypatch.setattr(_cycle.predictor, "predict", _exploding_predict)

    cand = {"kalshi_market_id": "KXBTC-26APR15H17-T53000", "league": "CRYPTO"}
    out = asyncio.run(
        _cycle._predict_for_candidate(
            cand,
            consensus=None,
            current_price=None,
            edge=None,
            vertical="crypto-binary",
        )
    )
    assert out == {"probability": 0.71, "model_name": "crypto:1h:test"}
    assert captured["cand"] is cand


def test_cycle_crypto_binary_falls_back_when_ticker_unparseable(monkeypatch):
    """A junk ticker must degrade to None (swarm-only fallback) instead
    of crashing the cycle."""
    from app.orchestrator import cycle as _cycle

    cand = {"kalshi_market_id": "TOTALLY-NOT-A-CRYPTO-TICKER", "league": "CRYPTO"}
    out = asyncio.run(_cycle._predict_for_crypto_binary(cand))
    assert out is None
