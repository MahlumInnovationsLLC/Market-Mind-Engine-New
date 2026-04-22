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
    candidate: str | None = None  # awards candidate (race-style tickers)

    @property
    def category(self) -> str:
        return SIGNAL_TYPE_CATEGORY.get(self.signal_type, "other")

    @property
    def race_category(self) -> str | None:
        """For awards tickers, the race category (e.g. BESTPIC, DRAMA, RECORD).

        For non-awards tickers, returns None.  For awards tickers in the
        4-segment race form (``KXOSCAR-25-BESTPIC-OPPENHEIMER``) this is
        the third token; the candidate name is in ``candidate``.
        """
        if self.candidate is None:
            return None
        return self.title


_LEADING_KX = re.compile(r"^kx", re.IGNORECASE)
# Awards tickers are race-style: TYPE-WHEN-CATEGORY-CANDIDATE
# (e.g. KXOSCAR-25-BESTPIC-OPPENHEIMER).  All other types follow the
# generic TYPE-WHEN-TITLE[-THRESHOLD] shape.
_AWARDS_TYPES: Final[frozenset[str]] = frozenset({"OSCAR", "EMMY", "GRAMMY"})

# Threshold tokens always begin with one of these prefixes.
_THRESHOLD_PREFIXES: Final[tuple[str, ...]] = ("ABOVE", "BELOW", "TOP", "UNDER")

_GENERIC_RE = re.compile(
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

_AWARDS_RE = re.compile(
    r"""
    ^
    (?P<type>[A-Z]+)
    [-_]
    (?P<when>[A-Z0-9]+)
    (?:
        [-_](?P<category>[A-Z][A-Z0-9_]*)
        [-_](?P<candidate>[A-Z][A-Z0-9_]*)
    )?
    (?:
        [-_](?P<title_only>[A-Z][A-Z0-9_]*)
    )?
    $
    """,
    re.VERBOSE | re.IGNORECASE,
)


def _is_threshold_token(tok: str) -> bool:
    up = tok.upper()
    return any(up.startswith(p) for p in _THRESHOLD_PREFIXES)


def parse_entertainment_ticker(ticker: str) -> EntertainmentTicker:
    if not isinstance(ticker, str) or not ticker.strip():
        raise InvalidEntertainmentTicker(f"empty ticker: {ticker!r}")
    raw = ticker.strip()
    body = _LEADING_KX.sub("", raw)

    # Split for type-aware dispatch.
    parts = re.split(r"[-_]", body)
    if len(parts) < 3 or not parts[0]:
        raise InvalidEntertainmentTicker(f"unrecognised ticker shape: {ticker!r}")
    signal_type = parts[0].upper()
    if signal_type not in SUPPORTED_SIGNAL_TYPES:
        raise InvalidEntertainmentTicker(
            f"unsupported signal type {signal_type!r} in ticker {ticker!r}"
        )
    when = parts[1].upper()

    # Awards tickers — race-style.  4 segments => CATEGORY-CANDIDATE,
    # 3 segments => CANDIDATE only (whole-cycle wager, no race split).
    if signal_type in _AWARDS_TYPES:
        if len(parts) == 3:
            return EntertainmentTicker(
                raw=raw, signal_type=signal_type, when=when,
                title=parts[2].upper(), threshold=None, candidate=None,
            )
        if len(parts) == 4:
            return EntertainmentTicker(
                raw=raw, signal_type=signal_type, when=when,
                title=parts[2].upper(),  # race category (BESTPIC, DRAMA, ...)
                threshold=None,
                candidate=parts[3].upper(),  # candidate (OPPENHEIMER, ...)
            )
        raise InvalidEntertainmentTicker(
            f"unrecognised awards ticker shape: {ticker!r}"
        )

    # Non-awards: generic TYPE-WHEN-TITLE[-THRESHOLD].
    if len(parts) == 3:
        return EntertainmentTicker(
            raw=raw, signal_type=signal_type, when=when,
            title=parts[2].upper(), threshold=None,
        )
    if len(parts) == 4:
        last = parts[3]
        if _is_threshold_token(last):
            return EntertainmentTicker(
                raw=raw, signal_type=signal_type, when=when,
                title=parts[2].upper(), threshold=last.upper(),
            )
        # No threshold prefix — treat as a 2-token title (e.g. franchise+number).
        return EntertainmentTicker(
            raw=raw, signal_type=signal_type, when=when,
            title=f"{parts[2].upper()}_{last.upper()}", threshold=None,
        )
    raise InvalidEntertainmentTicker(f"unrecognised ticker shape: {ticker!r}")
