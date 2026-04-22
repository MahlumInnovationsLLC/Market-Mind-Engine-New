"""Feature builder for the crypto-binary predictor stack.

Mirrors the spec ("returns, vol, RSI, OBV") plus the contextual
features the model needs to translate a market threshold K into a
probability of "BTC closes above K at horizon H":

    last_price                  spot at the moment of evaluation
    return_1, return_5, return_10   percentage change over the last
                                N bars at the candidate timeframe
    vol_5, vol_20               rolling stdev of log-returns
    rsi_14                      classic Wilder RSI (0..100)
    obv_z                       OBV z-score over a 50-bar window
    hour_norm                   UTC hour / 24 (handles weekend effect)
    log_distance_to_strike      log(K / last_price) — sign carries
                                the direction of the move required
    direction_sign              +1 for "above", -1 for "below"
    timeframe_idx_norm          (5m=0, 15m=1, 1h=2) / 2

12 features in total.  The ensemble baseline (`BaselineLGBXGBStack`)
expects exactly this width; future trained models that swap in via the
canonical `models/saved/crypto/...` path will keep the same layout so a
hot-reload doesn't break feature alignment.

The builder reads from the `crypto_ohlcv` table populated by
`app.feeds.crypto`.  When the table is empty (cold install before the
backfill has run) `build_crypto_features` raises so the predictor can
return a clean 503-style error rather than an undefined probability.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from .. import db
from .tickers import CryptoTicker


# How many historical bars we pull to satisfy every rolling-window
# feature in one go.  rsi_14 + 50-bar OBV window + 20-bar vol all fit
# comfortably under 80; we round to 100 for headroom.
_LOOKBACK_BARS: int = 100

FEATURE_NAMES: tuple[str, ...] = (
    "last_price",
    "return_1",
    "return_5",
    "return_10",
    "vol_5",
    "vol_20",
    "rsi_14",
    "obv_z",
    "hour_norm",
    "log_distance_to_strike",
    "direction_sign",
    "timeframe_idx_norm",
)
FEATURE_COUNT: int = len(FEATURE_NAMES)

_TF_INDEX: dict[str, int] = {"5m": 0, "15m": 1, "1h": 2}


class CryptoFeatureError(RuntimeError):
    """Raised when we don't have enough OHLCV history to build features."""


@dataclass(frozen=True)
class CryptoFeatureVector:
    """Aligned (names, values) plus the raw bars list for diagnostics."""
    names: tuple[str, ...]
    values: list[float]
    last_price: float
    bar_count: int
    last_ts: datetime | None

    def as_dict(self) -> dict[str, float]:
        return dict(zip(self.names, self.values))


# ---------------------------------------------------------------------------
# Pure-numeric helpers (kept dependency-free so unit tests don't need a DB)
# ---------------------------------------------------------------------------


def _rolling_log_returns(closes: Sequence[float]) -> list[float]:
    out: list[float] = []
    prev = closes[0] if closes else 0.0
    for c in closes[1:]:
        if prev <= 0 or c <= 0:
            out.append(0.0)
        else:
            out.append(math.log(c / prev))
        prev = c
    return out


def _stdev(xs: Sequence[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mean = sum(xs) / n
    var = sum((x - mean) ** 2 for x in xs) / (n - 1)
    return math.sqrt(var)


def _rsi(closes: Sequence[float], period: int = 14) -> float:
    """Wilder RSI; falls back to 50 when there aren't enough bars."""
    if len(closes) <= period:
        return 50.0
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            avg_gain = (avg_gain * (period - 1) + diff) / period
            avg_loss = (avg_loss * (period - 1)) / period
        else:
            avg_gain = (avg_gain * (period - 1)) / period
            avg_loss = (avg_loss * (period - 1) + (-diff)) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _obv(closes: Sequence[float], volumes: Sequence[float]) -> list[float]:
    """Classic OBV: +volume on up bars, -volume on down bars."""
    out: list[float] = [0.0]
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            out.append(out[-1] + volumes[i])
        elif closes[i] < closes[i - 1]:
            out.append(out[-1] - volumes[i])
        else:
            out.append(out[-1])
    return out


def _zscore_last(series: Sequence[float], window: int) -> float:
    """Z-score of the last value vs the previous `window` values."""
    if len(series) < 2:
        return 0.0
    tail = list(series)[-window:]
    if len(tail) < 2:
        return 0.0
    last = tail[-1]
    body = tail[:-1] or tail
    mean = sum(body) / len(body)
    sd = _stdev(body)
    if sd <= 0:
        return 0.0
    return (last - mean) / sd


def _safe_pct_change(series: Sequence[float], lag: int) -> float:
    if len(series) <= lag:
        return 0.0
    base = series[-lag - 1]
    if base <= 0:
        return 0.0
    return (series[-1] - base) / base


def build_features_from_bars(
    bars: Sequence[dict[str, Any]],
    ticker: CryptoTicker,
    *,
    now: datetime | None = None,
) -> CryptoFeatureVector:
    """Pure-function variant used by both the live builder and the backtest.

    `bars` is an oldest-first list of dicts with at least `ts`, `c`, `v`.
    """
    if not bars:
        raise CryptoFeatureError(
            f"no OHLCV bars available for {ticker.symbol}/{ticker.timeframe}"
        )
    closes = [float(b["c"]) for b in bars]
    volumes = [float(b.get("v") or 0.0) for b in bars]
    last_price = closes[-1]
    if last_price <= 0:
        raise CryptoFeatureError(
            f"non-positive last close ({last_price}) for {ticker.symbol}"
        )

    return_1 = _safe_pct_change(closes, 1)
    return_5 = _safe_pct_change(closes, 5)
    return_10 = _safe_pct_change(closes, 10)

    log_rets = _rolling_log_returns(closes)
    vol_5 = _stdev(log_rets[-5:]) if len(log_rets) >= 2 else 0.0
    vol_20 = _stdev(log_rets[-20:]) if len(log_rets) >= 2 else 0.0
    rsi_14 = _rsi(closes, period=14)
    obv = _obv(closes, volumes)
    obv_z = _zscore_last(obv, window=50)

    last_ts = bars[-1].get("ts")
    if isinstance(last_ts, datetime):
        ref = last_ts.astimezone(timezone.utc)
    else:
        ref = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    hour_norm = ref.hour / 24.0

    log_dist = math.log(ticker.threshold / last_price) if ticker.threshold > 0 else 0.0
    direction_sign = 1.0 if ticker.direction == "above" else -1.0
    tf_idx = _TF_INDEX.get(ticker.timeframe, 2)
    tf_norm = tf_idx / max(1, len(_TF_INDEX) - 1)

    values = [
        last_price,
        return_1, return_5, return_10,
        vol_5, vol_20,
        rsi_14,
        obv_z,
        hour_norm,
        log_dist, direction_sign, tf_norm,
    ]
    assert len(values) == FEATURE_COUNT, (
        f"crypto feature builder produced {len(values)} values, expected {FEATURE_COUNT}"
    )
    return CryptoFeatureVector(
        names=FEATURE_NAMES,
        values=values,
        last_price=last_price,
        bar_count=len(bars),
        last_ts=last_ts if isinstance(last_ts, datetime) else None,
    )


async def build_crypto_features(
    ticker: CryptoTicker,
    *,
    now: datetime | None = None,
    lookback: int = _LOOKBACK_BARS,
) -> CryptoFeatureVector:
    """Async live variant: pulls the most recent `lookback` bars from db."""
    rows = await db.fetch(
        """
        SELECT ts, o, h, l, c, v
          FROM crypto_ohlcv
         WHERE symbol = $1 AND granularity = $2
         ORDER BY ts DESC
         LIMIT $3
        """,
        ticker.symbol,
        ticker.timeframe,
        lookback,
    )
    bars = [dict(r) for r in reversed(rows)]
    return build_features_from_bars(bars, ticker, now=now)
