"""Parse Kalshi macro / events binary tickers into a structured `EventsEconTicker`.

Recognised inputs (case-insensitive), all KX-prefixed:

    KXCPI-24DEC-ABOVE3        -> release_type=CPI, when=24DEC, threshold=ABOVE3
    KXPPI-25JAN-ABOVE0_3      -> release_type=PPI
    KXGDP-25Q1-ABOVE2_5       -> release_type=GDP
    KXFOMC-25MAR-HIKE25       -> release_type=FOMC
    KXNFP-25FEB-ABOVE200      -> release_type=NFP
    KXJOBLESS-25W08-ABOVE220  -> release_type=JOBLESS
    KXRETAIL-24DEC-ABOVE0_4   -> release_type=RETAIL

Unknown KX-prefixed economics tickers (e.g. KXISM, KXPCE) are accepted
with `release_type` set to the prefix sans 'KX' so the swarm-only path
can still operate while a model is added.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final


SUPPORTED_RELEASES: Final[tuple[str, ...]] = (
    "CPI", "PPI", "GDP", "FOMC", "NFP", "JOBLESS", "RETAIL",
    "PCE", "ISM", "CONFIDENCE", "HOUSING",
)


class InvalidEventsEconTicker(ValueError):
    """Raised when a ticker can't be parsed."""


@dataclass(frozen=True)
class EventsEconTicker:
    raw: str
    release_type: str       # canonical release class (CPI, NFP, ...)
    when: str               # release / cycle token (e.g. "24DEC", "25Q1", "25W08")
    threshold: str          # threshold token (e.g. "ABOVE3", "HIKE25", "ABOVE0_3")


_LEADING_KX = re.compile(r"^kx", re.IGNORECASE)
_BODY_RE = re.compile(
    r"""
    ^
    (?P<release>[A-Z]+)
    [-_]
    (?P<when>[A-Z0-9]+)
    [-_]
    (?P<threshold>[A-Z][A-Z0-9_]*)
    $
    """,
    re.VERBOSE | re.IGNORECASE,
)


def parse_events_econ_ticker(ticker: str) -> EventsEconTicker:
    if not isinstance(ticker, str) or not ticker.strip():
        raise InvalidEventsEconTicker(f"empty ticker: {ticker!r}")
    raw = ticker.strip()
    body = _LEADING_KX.sub("", raw)
    m = _BODY_RE.match(body)
    if not m:
        raise InvalidEventsEconTicker(f"unrecognised ticker shape: {ticker!r}")
    release = m.group("release").upper()
    when = m.group("when").upper()
    threshold = m.group("threshold").upper()
    return EventsEconTicker(
        raw=raw,
        release_type=release,
        when=when,
        threshold=threshold,
    )
