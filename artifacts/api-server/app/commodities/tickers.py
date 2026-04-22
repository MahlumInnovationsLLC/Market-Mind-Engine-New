"""Parse Kalshi commodity-binary tickers into (symbol, horizon, threshold).

Recognised inputs (case-insensitive):

    KXOIL-26APR15-T75            -> WTI,  1d, 75    (closes above K)
    KXOIL-26APR15-B75            -> WTI,  1d, 75    (closes below K)
    KXGOLD-26APR15-T2400         -> GOLD, 1d, 2400
    KXNATGAS1H-26APR1517-T2.50   -> NG,   1h, 2.50
    KXCORN-26MAY-T450            -> CORN, 1d, 450

The threshold-prefix letter encodes settlement direction:
    T (target) -> "closes above K" YES contract
    B          -> "closes below K" YES contract
Other letters fall back to "above" so a future series rename doesn't
silently break the trading pipeline.

Supported symbols cover the 10 commodities ticker prefixes registered
on the `commodities` VerticalSpec (oil/gold/natgas/corn/soy/wheat/
copper/silver/coffee/sugar).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final


# Canonical internal symbols.  Order matches the dashboard / correlation
# matrix layout so log/diff output stays stable.
SUPPORTED_SYMBOLS: Final[tuple[str, ...]] = (
    "WTI", "GOLD", "NG", "CORN", "SOY",
    "WHEAT", "COPPER", "SILVER", "COFFEE", "SUGAR",
)
SUPPORTED_TIMEFRAMES: Final[tuple[str, ...]] = ("1h", "4h", "1d")


# Map of ticker-prefix -> internal symbol.  Kalshi uses friendly names
# (KXOIL/KXNATGAS) which we normalise to commodity codes.
_PREFIX_TO_SYMBOL: dict[str, str] = {
    "OIL":    "WTI",
    "WTI":    "WTI",
    "GOLD":   "GOLD",
    "NATGAS": "NG",
    "NG":     "NG",
    "CORN":   "CORN",
    "SOY":    "SOY",
    "SOYB":   "SOY",
    "WHEAT":  "WHEAT",
    "COPPER": "COPPER",
    "SILVER": "SILVER",
    "COFFEE": "COFFEE",
    "SUGAR":  "SUGAR",
}


class InvalidCommodityTicker(ValueError):
    """Raised when a ticker can't be parsed into a usable (symbol, tf, K)."""


@dataclass(frozen=True)
class CommodityTicker:
    """Parsed Kalshi commodity-binary ticker."""
    raw: str
    symbol: str          # "WTI" | "GOLD" | ...
    timeframe: str       # "1h" | "4h" | "1d"
    threshold: float     # K in "WTI > K"
    direction: str = "above"


_LEADING_KX = re.compile(r"^kx", re.IGNORECASE)

# The body of a parsed ticker: prefix + optional granularity, hyphen,
# date/hour token (ignored), hyphen, threshold prefix letter, numeric K.
_BODY_RE = re.compile(
    r"""
    ^
    (?P<sym>[A-Z]+(?:1H|4H|1D|H|D)?)
    [-_]
    (?P<when>[A-Z0-9]+)
    [-_]
    (?P<pfx>[A-Z])
    (?P<k>\d+(?:\.\d+)?)
    $
    """,
    re.VERBOSE | re.IGNORECASE,
)


def _split_symbol_timeframe(sym_raw: str) -> tuple[str, str] | None:
    """Strip an optional `1H`/`4H`/`1D`/`H`/`D` suffix from the symbol.

    Returns `(symbol, timeframe)` or None if the symbol portion isn't
    one of our recognised commodity prefixes.
    """
    s = sym_raw.upper()
    for suffix, tf in (("1H", "1h"), ("4H", "4h"), ("1D", "1d"),
                       ("H", "1h"), ("D", "1d")):
        if s.endswith(suffix) and s != suffix:
            base = s[: -len(suffix)]
            if base in _PREFIX_TO_SYMBOL:
                return _PREFIX_TO_SYMBOL[base], tf
    if s in _PREFIX_TO_SYMBOL:
        return _PREFIX_TO_SYMBOL[s], "1d"
    # Permissive: longest-prefix match for future rename safety.
    for prefix in sorted(_PREFIX_TO_SYMBOL, key=len, reverse=True):
        if s.startswith(prefix):
            return _PREFIX_TO_SYMBOL[prefix], "1d"
    return None


def parse_commodity_ticker(ticker: str) -> CommodityTicker:
    """Best-effort parser for a Kalshi commodity-binary ticker."""
    if not isinstance(ticker, str) or not ticker.strip():
        raise InvalidCommodityTicker(f"empty ticker: {ticker!r}")
    raw = ticker.strip()
    body = _LEADING_KX.sub("", raw)
    m = _BODY_RE.match(body)
    if not m:
        raise InvalidCommodityTicker(f"unrecognised ticker shape: {ticker!r}")

    sym_raw = m.group("sym")
    parsed = _split_symbol_timeframe(sym_raw)
    if parsed is None:
        raise InvalidCommodityTicker(
            f"ticker {ticker!r} doesn't reference a supported commodity "
            f"({', '.join(SUPPORTED_SYMBOLS)})"
        )
    symbol, timeframe = parsed

    try:
        threshold = float(m.group("k"))
    except ValueError as e:
        raise InvalidCommodityTicker(
            f"ticker {ticker!r} has malformed threshold: {m.group('k')!r}"
        ) from e
    if threshold <= 0:
        raise InvalidCommodityTicker(
            f"ticker {ticker!r}: non-positive threshold {threshold}"
        )

    direction = "below" if m.group("pfx").upper() == "B" else "above"
    return CommodityTicker(
        raw=raw,
        symbol=symbol,
        timeframe=timeframe,
        threshold=threshold,
        direction=direction,
    )
