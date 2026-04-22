"""Parse Kalshi entertainment tickers into a structured `EntertainmentTicker`.

Recognised inputs (case-insensitive), all KX-prefixed:

    KXBOX-25MAR-AVATAR3-ABOVE150     -> signal_type=BOX, opening weekend > $150M
    KXOSCAR-25-BESTPIC-OPPENHEIMER   -> signal_type=OSCAR, BESTPIC, Oppenheimer wins
    KXEMMY-25-DRAMA-SUCCESSION       -> signal_type=EMMY, DRAMA, Succession wins
    KXGRAMMY-25-RECORD-FLOWERS       -> signal_type=GRAMMY, RECORD, Flowers wins
    KXSPOTIFY-25W08-TAYLOR-TOP10     -> signal_type=SPOTIFY, weekly chart entry
    KXRT-25MAR-AVATAR3-ABOVE85       -> signal_type=RT, RT score > 85%
    KXSTREAM-25MAR-WEDNESDAY-TOP10   -> signal_type=STREAM, Netflix top 10

Generic shape: ``KX{TYPE}-{WHEN}-{TITLE}[-{THRESHOLD}]``.

Two-segment shape (e.g. KXBOX-WEEKEND) is rejected — entertainment
tickers always carry at least when + title because the market always
resolves on a specific instance (movie, season, week).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final


SUPPORTED_SIGNAL_TYPES: Final[tuple[str, ...]] = (
    "BOX",        # box-office opening weekend / cumulative gross
    "OSCAR",      # Academy Awards
    "EMMY",       # Primetime Emmys
    "GRAMMY",     # Grammys
    "SPOTIFY",    # Spotify chart positions
    "STREAM",     # Netflix / Disney+ / etc top-10
    "RT",         # Rotten Tomatoes critic scores
)


# Map signal types to their market category for internal routing.
SIGNAL_TYPE_CATEGORY: Final[dict[str, str]] = {
    "BOX":     "box_office",
    "OSCAR":   "awards",
    "EMMY":    "awards",
    "GRAMMY":  "awards",
    "SPOTIFY": "charts",
    "STREAM":  "charts",
    "RT":      "reviews",
}


class InvalidEntertainmentTicker(ValueError):
    """Raised when a ticker can't be parsed."""


@dataclass(frozen=True)
class EntertainmentTicker:
    raw: str
    signal_type: str        # canonical signal class (BOX, OSCAR, ...)
    when: str               # release / cycle token (e.g. "25MAR", "25", "25W08")
    title: str              # subject token (movie / show / artist / category)
    threshold: str | None   # threshold token (e.g. "ABOVE150", "TOP10", or None)

    @property
    def category(self) -> str:
        return SIGNAL_TYPE_CATEGORY.get(self.signal_type, "other")


_LEADING_KX = re.compile(r"^kx", re.IGNORECASE)
# Allow 3-segment (TYPE-WHEN-TITLE) and 4-segment (TYPE-WHEN-TITLE-THRESHOLD).
_BODY_RE = re.compile(
    r"""
    ^
    (?P<type>[A-Z]+)
    [-_]
    (?P<when>[A-Z0-9]+)
    [-_]
    (?P<title>[A-Z0-9]+)
    (?:[-_](?P<threshold>[A-Z][A-Z0-9_]*))?
    $
    """,
    re.VERBOSE | re.IGNORECASE,
)


def parse_entertainment_ticker(ticker: str) -> EntertainmentTicker:
    if not isinstance(ticker, str) or not ticker.strip():
        raise InvalidEntertainmentTicker(f"empty ticker: {ticker!r}")
    raw = ticker.strip()
    body = _LEADING_KX.sub("", raw)
    m = _BODY_RE.match(body)
    if not m:
        raise InvalidEntertainmentTicker(f"unrecognised ticker shape: {ticker!r}")
    signal_type = m.group("type").upper()
    if signal_type not in SUPPORTED_SIGNAL_TYPES:
        raise InvalidEntertainmentTicker(
            f"unsupported signal type {signal_type!r} in ticker {ticker!r}"
        )
    when = m.group("when").upper()
    title = m.group("title").upper()
    threshold = m.group("threshold")
    threshold = threshold.upper() if threshold else None
    return EntertainmentTicker(
        raw=raw,
        signal_type=signal_type,
        when=when,
        title=title,
        threshold=threshold,
    )
