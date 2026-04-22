"""Parse Kalshi crypto-binary tickers into (symbol, timeframe, threshold).

Kalshi crypto markets follow a few related schemes; we accept the most
common shapes and a permissive fallback so a future series rename
doesn't quietly break the trading pipeline.

Recognised inputs (case-insensitive):

    KXBTC-26APR15H17-T68000      -> BTC, 1h,  68000  (closes above K)
    KXBTC-26APR15H17-B68000      -> BTC, 1h,  68000  (closes below K)
    KXBTCD-26APR15-T68000        -> BTC, 1h,  68000  (daily close, treated as 1h)
    KXBTC5M-26APR1517-T68000     -> BTC, 5m,  68000
    KXBTC15M-26APR15H17-T68000   -> BTC, 15m, 68000
    KXETH-26APR15H17-T3500       -> ETH, 1h,  3500

The threshold-prefix letter encodes settlement direction:
    T (the standard "target") -> "closes above K" YES contract
    B                          -> "closes below K" YES contract
Anything else is treated as the standard "above" YES contract — Kalshi
has only used T/B for crypto binaries to date, but the parser stays
permissive so a series rename doesn't silently break the trading loop.

We deliberately default to 1h when the ticker doesn't carry an explicit
granularity tag — the bulk of Kalshi crypto markets settle hourly.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final


SUPPORTED_SYMBOLS: Final[tuple[str, ...]] = ("BTC", "ETH")
SUPPORTED_TIMEFRAMES: Final[tuple[str, ...]] = ("5m", "15m", "1h")


class InvalidCryptoTicker(ValueError):
    """Raised when a ticker can't be parsed into a usable (symbol, tf, K)."""


@dataclass(frozen=True)
class CryptoTicker:
    """A parsed Kalshi crypto-binary ticker.

    `direction` is "above" for the standard "T<price>" suffix (Kalshi's
    "closes above K" YES contract) and "below" for the "B<price>" suffix
    (the inverse YES contract that pays when BTC closes BELOW K).  Any
    other prefix letter is treated as "above" — see module docstring.
    """
    raw: str
    symbol: str          # "BTC" | "ETH"
    timeframe: str       # "5m" | "15m" | "1h"
    threshold: float     # the K in "BTC > K"
    direction: str = "above"


# Strip the leading "KX" Kalshi exchange prefix so the body matches our
# regex either way (some integrations strip it before storage).
_LEADING_KX = re.compile(r"^kx", re.IGNORECASE)

# Body parser:
#   group(1) symbol with optional granularity suffix (BTC, BTC5M, BTC15M, BTCD, ETH...)
#   group(2) date/hour token (we ignore it for prediction purposes)
#   group(3) threshold prefix letter (T or B today; permissive for future)
#   group(4) numeric threshold
_BODY_RE = re.compile(
    r"""
    ^
    (?P<sym>[A-Z]+(?:\d+M|D|H)?)
    [-_]
    (?P<when>[A-Z0-9]+)
    [-_]
    (?P<pfx>[A-Z])
    (?P<k>\d+(?:\.\d+)?)
    $
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Map a parsed symbol-with-suffix back to (base_symbol, timeframe).  We
# treat anything we don't recognise as the 1h horizon — see module
# docstring for the rationale.
_SYM_TF_MAP: dict[str, tuple[str, str]] = {
    "BTC":   ("BTC", "1h"),
    "BTCH":  ("BTC", "1h"),
    "BTCD":  ("BTC", "1h"),
    "BTC5M": ("BTC", "5m"),
    "BTC15M": ("BTC", "15m"),
    "ETH":   ("ETH", "1h"),
    "ETHH":  ("ETH", "1h"),
    "ETHD":  ("ETH", "1h"),
    "ETH5M": ("ETH", "5m"),
    "ETH15M": ("ETH", "15m"),
}


def parse_crypto_ticker(ticker: str) -> CryptoTicker:
    """Best-effort parser for a Kalshi crypto-binary ticker."""
    if not isinstance(ticker, str) or not ticker.strip():
        raise InvalidCryptoTicker(f"empty ticker: {ticker!r}")
    raw = ticker.strip()
    body = _LEADING_KX.sub("", raw)
    m = _BODY_RE.match(body)
    if not m:
        raise InvalidCryptoTicker(f"unrecognised ticker shape: {ticker!r}")

    sym_raw = m.group("sym").upper()
    base_tf = _SYM_TF_MAP.get(sym_raw)
    if base_tf is None:
        # Permissive: the symbol portion starts with one of our supported
        # bases?  Treat as the 1h horizon then.
        for sup in SUPPORTED_SYMBOLS:
            if sym_raw.startswith(sup):
                base_tf = (sup, "1h")
                break
    if base_tf is None:
        raise InvalidCryptoTicker(
            f"ticker {ticker!r} doesn't reference a supported symbol "
            f"({', '.join(SUPPORTED_SYMBOLS)})"
        )
    symbol, timeframe = base_tf

    try:
        threshold = float(m.group("k"))
    except ValueError as e:
        raise InvalidCryptoTicker(
            f"ticker {ticker!r} has malformed threshold: {m.group('k')!r}"
        ) from e
    if threshold <= 0:
        raise InvalidCryptoTicker(
            f"ticker {ticker!r}: non-positive threshold {threshold}"
        )

    # "T" (target) and "B" (below) are the two prefixes Kalshi uses for
    # crypto-binary contracts.  Anything else is treated as the standard
    # "closes above K" YES contract — see module docstring.
    direction = "below" if m.group("pfx").upper() == "B" else "above"
    return CryptoTicker(
        raw=raw,
        symbol=symbol,
        timeframe=timeframe,
        threshold=threshold,
        direction=direction,
    )
