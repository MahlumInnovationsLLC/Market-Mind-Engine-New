"""Parse Kalshi temperature tickers (KXHIGH/KXTEMP/KXLOW).

Recognised inputs (case-insensitive):

    KXHIGHNYC-26APR15-T75   -> NYC, daily-high, threshold=75, above
    KXHIGHNY-26APR15-T75    -> NYC alias accepted
    KXTEMPLAX-26MAY-B60     -> LAX, current-temp, threshold=60, below
    KXLOWCHI-26JAN20-T20    -> CHI, daily-low, threshold=20, above

Threshold prefix:
    T -> "above K" YES contract
    B -> "below K" YES contract
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final


# City codes match the climatology / payload modules.
SUPPORTED_CITIES: Final[tuple[str, ...]] = (
    "NYC", "AUS", "CHI", "DEN", "MIA", "LAX", "PHL",
)
# Common Kalshi 2-letter / alt aliases mapped to our 3-letter canonical codes.
_CITY_ALIASES: dict[str, str] = {
    "NY": "NYC", "NYC": "NYC", "JFK": "NYC",
    "AUS": "AUS", "AU": "AUS",
    "CHI": "CHI", "ORD": "CHI", "CH": "CHI",
    "DEN": "DEN", "DE": "DEN",
    "MIA": "MIA", "MI": "MIA",
    "LAX": "LAX", "LA": "LAX",
    "PHL": "PHL", "PH": "PHL",
}

SUPPORTED_METRICS: Final[tuple[str, ...]] = ("HIGH", "LOW", "TEMP")


class InvalidTemperatureTicker(ValueError):
    """Raised when a ticker can't be parsed into (metric, city, K)."""


@dataclass(frozen=True)
class TemperatureTicker:
    raw: str
    metric: str        # "HIGH" | "LOW" | "TEMP"
    city: str          # canonical 3-letter city code (NYC/AUS/...)
    threshold: float   # K in "TMAX > K"
    direction: str = "above"


_LEADING_KX = re.compile(r"^kx", re.IGNORECASE)
_BODY_RE = re.compile(
    r"""
    ^
    (?P<metric>HIGH|LOW|TEMP)
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


def parse_temperature_ticker(ticker: str) -> TemperatureTicker:
    """Best-effort parser for a Kalshi temperature ticker."""
    if not isinstance(ticker, str) or not ticker.strip():
        raise InvalidTemperatureTicker(f"empty ticker: {ticker!r}")
    raw = ticker.strip()
    body = _LEADING_KX.sub("", raw)
    m = _BODY_RE.match(body)
    if not m:
        raise InvalidTemperatureTicker(f"unrecognised ticker shape: {ticker!r}")

    metric = m.group("metric").upper()
    if metric not in SUPPORTED_METRICS:
        raise InvalidTemperatureTicker(
            f"ticker {ticker!r}: unsupported metric {metric!r}"
        )

    city_raw = m.group("city").upper()
    city = _CITY_ALIASES.get(city_raw)
    if city is None or city not in SUPPORTED_CITIES:
        raise InvalidTemperatureTicker(
            f"ticker {ticker!r}: unsupported city {city_raw!r} "
            f"(supported: {', '.join(SUPPORTED_CITIES)})"
        )

    try:
        threshold = float(m.group("k"))
    except ValueError as e:
        raise InvalidTemperatureTicker(
            f"ticker {ticker!r}: malformed threshold {m.group('k')!r}"
        ) from e

    direction = "below" if m.group("pfx").upper() == "B" else "above"
    return TemperatureTicker(
        raw=raw,
        metric=metric,
        city=city,
        threshold=threshold,
        direction=direction,
    )
