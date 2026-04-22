"""Feature builder for the commodities-binary predictor stack.

Mirrors the crypto feature spec ("returns, vol, RSI, OBV") with
commodity-specific extras for the binary-close model:

    last_price                  spot at evaluation time
    return_1, return_5, return_10   pct change over the last N bars
    vol_5, vol_20               rolling stdev of log-returns
    rsi_14                      Wilder RSI
    obv_z                       OBV z-score over a 50-bar window
    seasonality_norm            (UTC day-of-year - 183) / 183
    log_distance_to_strike      log(K / last_price)
    direction_sign              +1 above / -1 below
    timeframe_idx_norm          (1h=0, 4h=1, 1d=2) / 2

12 features in total; matches `BaselineLGBXGBStack.n_features_in_`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from .. import db
from ..crypto.features import (
    _obv,
    _rolling_log_returns,
    _rsi,
    _safe_pct_change,
    _stdev,
    _zscore_last,
)
from .tickers import CommodityTicker


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
    "seasonality_norm",
    "log_distance_to_strike",
    "direction_sign",
    "timeframe_idx_norm",
)
FEATURE_COUNT: int = len(FEATURE_NAMES)

_TF_INDEX: dict[str, int] = {"1h": 0, "4h": 1, "1d": 2}


class CommodityFeatureError(RuntimeError):
    """Raised when we don't have enough OHLCV history to build features."""


@dataclass(frozen=True)
class CommodityFeatureVector:
    names: tuple[str, ...]
    values: list[float]
    last_price: float
    bar_count: int
    last_ts: datetime | None

    def as_dict(self) -> dict[str, float]:
        return dict(zip(self.names, self.values))


def build_features_from_bars(
    bars: Sequence[dict[str, Any]],
    ticker: CommodityTicker,
    *,
    now: datetime | None = None,
) -> CommodityFeatureVector:
    """Pure-function variant used by both the live builder and tests.

    `bars` is an oldest-first list of dicts with at least `ts`, `c`, `v`.
    """
    if not bars:
        raise CommodityFeatureError(
            f"no OHLCV bars available for {ticker.symbol}/{ticker.timeframe}"
        )
    closes = [float(b["c"]) for b in bars]
    volumes = [float(b.get("v") or 0.0) for b in bars]
    last_price = closes[-1]
    if last_price <= 0:
        raise CommodityFeatureError(
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
    # Centred day-of-year in [-1, 1] approximating cosine seasonality —
    # commodity demand has very strong calendar effects (heating-degree
    # days for NG, harvest cycles for grains, etc).
    doy = ref.timetuple().tm_yday
    seasonality_norm = (doy - 183.0) / 183.0

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
        seasonality_norm,
        log_dist, direction_sign, tf_norm,
    ]
    assert len(values) == FEATURE_COUNT, (
        f"commodity feature builder produced {len(values)} values, "
        f"expected {FEATURE_COUNT}"
    )
    return CommodityFeatureVector(
        names=FEATURE_NAMES,
        values=values,
        last_price=last_price,
        bar_count=len(bars),
        last_ts=last_ts if isinstance(last_ts, datetime) else None,
    )


async def build_commodity_features(
    ticker: CommodityTicker,
    *,
    now: datetime | None = None,
    lookback: int = _LOOKBACK_BARS,
) -> CommodityFeatureVector:
    """Async live variant: pulls recent bars from `commodities_ohlcv`."""
    rows = await db.fetch(
        """
        SELECT ts, o, h, l, c, v
          FROM commodities_ohlcv
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
