"""Parse Kalshi precipitation tickers (KXRAIN).

Recognised inputs (case-insensitive):

    KXRAINNYC-26APR15-T0.5   -> NYC, daily-precip >= 0.5 in
    KXRAINLAX-26MAY-B1       -> LAX, daily-precip <  1.0 in
    KXRAINMIA-26JUL04-T2.5   -> MIA, daily-precip >= 2.5 in

Threshold prefix mirrors the temperature parser:
    T -> "above K" YES contract
    B -> "below K" YES contract

Kept as a sibling module to ``tickers.py`` so the temperature parser
stays focused on a single contract family — KXRAIN's threshold is in
inches of precipitation rather than degrees Fahrenheit, and the cycle
dispatcher routes it to a different EMOS link function (gamma /
logistic instead of normal CDF).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from .tickers import SUPPORTED_CITIES, _CITY_ALIASES


class InvalidPrecipitationTicker(ValueError):
    """Raised when a ticker can't be parsed into (city, K_inches)."""


@dataclass(frozen=True)
class PrecipitationTicker:
    raw: str
    city: str          # canonical 3-letter code
    threshold_in: float  # daily accumulation threshold, inches
    direction: str = "above"


_LEADING_KX = re.compile(r"^kx", re.IGNORECASE)
_BODY_RE = re.compile(
    r"""
    ^
    RAIN
    (?P<city>[A-Z]{2,3})
    [-_]
    (?P<when>[A-Z0-9]+)
    [-_]
    (?P<pfx>[TB])
    (?P<k>-?\d+(?:\.\d+)?)
    $
    """,
    re.VERBOSE | re.IGNORECASE,
)


SUPPORTED_PRECIP_CITIES: Final[tuple[str, ...]] = SUPPORTED_CITIES


def parse_precipitation_ticker(ticker: str) -> PrecipitationTicker:
    """Best-effort parser for a Kalshi KXRAIN precipitation ticker."""
    if not isinstance(ticker, str) or not ticker.strip():
        raise InvalidPrecipitationTicker(f"empty ticker: {ticker!r}")
    raw = ticker.strip()
    body = _LEADING_KX.sub("", raw)
    m = _BODY_RE.match(body)
    if not m:
        raise InvalidPrecipitationTicker(
            f"unrecognised KXRAIN ticker shape: {ticker!r}"
        )

    city_raw = m.group("city").upper()
    city = _CITY_ALIASES.get(city_raw)
    if city is None or city not in SUPPORTED_PRECIP_CITIES:
        raise InvalidPrecipitationTicker(
            f"ticker {ticker!r}: unsupported city {city_raw!r} "
            f"(supported: {', '.join(SUPPORTED_PRECIP_CITIES)})"
        )

    try:
        threshold = float(m.group("k"))
    except ValueError as e:
        raise InvalidPrecipitationTicker(
            f"ticker {ticker!r}: malformed threshold {m.group('k')!r}"
        ) from e
    if threshold < 0:
        raise InvalidPrecipitationTicker(
            f"ticker {ticker!r}: precipitation threshold must be >= 0"
        )

    direction = "below" if m.group("pfx").upper() == "B" else "above"
    return PrecipitationTicker(
        raw=raw,
        city=city,
        threshold_in=threshold,
        direction=direction,
    )


__all__ = [
    "InvalidPrecipitationTicker",
    "PrecipitationTicker",
    "SUPPORTED_PRECIP_CITIES",
    "parse_precipitation_ticker",
]
