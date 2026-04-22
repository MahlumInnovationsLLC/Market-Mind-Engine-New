"""Parse Kalshi election binary tickers into a structured `ElectionTicker`.

Recognised inputs (case-insensitive), all KX-prefixed:

    KXPRES-28-DEM            -> US Presidential, party / candidate=DEM
    KXPRES-28NOV-TRUMP       -> US Presidential, candidate=TRUMP
    KXGOV-26CA-DEM           -> Governor, state=CA
    KXSEN-26OH-VANCE         -> US Senate, state=OH, candidate=VANCE
    KXHOUSE-26NY03-DEM       -> US House, district=NY-03
    KXFRPRES-27APR-LEPEN     -> Foreign election: France presidential
    KXUKPM-29MAY-LAB         -> Foreign election: UK PM, party=LAB
    KXELE-26DE-AFD           -> Generic foreign election prefix

Office classes the resolver groups markets into:
    PRES, GOV, SEN, HOUSE, FRPRES, UKPM, ELE
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final


SUPPORTED_OFFICES: Final[tuple[str, ...]] = (
    "PRES", "GOV", "SEN", "HOUSE",
    "FRPRES", "UKPM", "DEPM", "CAPM", "AUPM",
    "ELE",
)


# Kalshi ticker prefix -> canonical office class
_PREFIX_TO_OFFICE: dict[str, str] = {
    "PRES":   "PRES",
    "GOV":    "GOV",
    "SEN":    "SEN",
    "HOUSE":  "HOUSE",
    "FRPRES": "FRPRES",
    "UKPM":   "UKPM",
    "DEPM":   "DEPM",
    "CAPM":   "CAPM",
    "AUPM":   "AUPM",
    "ELE":    "ELE",
}


class InvalidElectionTicker(ValueError):
    """Raised when a ticker can't be parsed."""


@dataclass(frozen=True)
class ElectionTicker:
    raw: str
    office: str            # canonical office class
    when: str              # cycle / date token (e.g. "28", "26CA", "27APR")
    candidate: str         # candidate / party label (uppercased)
    region: str | None     # parsed state / country / district code if present
    scope: str             # resolver key (e.g. "US_PRES_2028", "GOV_CA_2026")


_LEADING_KX = re.compile(r"^kx", re.IGNORECASE)
_BODY_RE = re.compile(
    r"""
    ^
    (?P<office>[A-Z]+?)
    [-_]
    (?P<when>[A-Z0-9]+)
    [-_]
    (?P<candidate>[A-Z][A-Z0-9_]*)
    $
    """,
    re.VERBOSE | re.IGNORECASE,
)


# Cycle token (e.g. "28", "26", "26NOV", "27APR") -> 4-digit year.
# Two-digit years are interpreted as 20xx (we are nowhere near 2099).
_CYCLE_RE = re.compile(r"(?P<yr>\d{2,4})(?P<rest>[A-Z0-9]*)", re.IGNORECASE)
# Region embedded in `when` for state / district markets.
_STATE_RE = re.compile(r"(?P<yr>\d{2,4})(?P<state>[A-Z]{2})(?P<dist>\d{0,3})$", re.IGNORECASE)


def _resolve_office(office_raw: str) -> str | None:
    s = office_raw.upper()
    if s in _PREFIX_TO_OFFICE:
        return _PREFIX_TO_OFFICE[s]
    for prefix in sorted(_PREFIX_TO_OFFICE, key=len, reverse=True):
        if s.startswith(prefix):
            return _PREFIX_TO_OFFICE[prefix]
    return None


def _normalise_year(yr_token: str) -> str:
    """Return a 4-digit year from a 2- or 4-digit cycle token."""
    if len(yr_token) == 2:
        return f"20{yr_token}"
    return yr_token


def _parse_when(when: str) -> tuple[str | None, str | None]:
    """Return (year_4digit, region_or_district) parsed from the `when` token."""
    upper = when.upper()
    m_state = _STATE_RE.match(upper)
    if m_state:
        year = _normalise_year(m_state.group("yr"))
        state = m_state.group("state")
        dist = m_state.group("dist")
        region = f"{state}_{dist.zfill(2)}" if dist else state
        return year, region
    m_cycle = _CYCLE_RE.match(upper)
    if m_cycle:
        return _normalise_year(m_cycle.group("yr")), None
    return None, None


def _build_scope(office: str, year: str | None, region: str | None) -> str:
    """Canonical resolver key — keep stable; pollers write rows keyed on it."""
    parts: list[str] = []
    if office in {"PRES"}:
        parts.append("US_PRES")
    elif office in {"FRPRES", "UKPM", "DEPM", "CAPM", "AUPM"}:
        parts.append(office)
    elif office == "ELE":
        parts.append("FOREIGN")
    else:
        parts.append(f"US_{office}")
    if region:
        parts.append(region)
    if year:
        parts.append(year)
    return "_".join(parts)


def parse_election_ticker(ticker: str) -> ElectionTicker:
    if not isinstance(ticker, str) or not ticker.strip():
        raise InvalidElectionTicker(f"empty ticker: {ticker!r}")
    raw = ticker.strip()
    body = _LEADING_KX.sub("", raw)
    m = _BODY_RE.match(body)
    if not m:
        raise InvalidElectionTicker(f"unrecognised ticker shape: {ticker!r}")
    office = _resolve_office(m.group("office"))
    if office is None:
        raise InvalidElectionTicker(
            f"ticker {ticker!r} doesn't reference a supported office "
            f"({', '.join(SUPPORTED_OFFICES)})"
        )
    when = m.group("when").upper()
    candidate = m.group("candidate").upper()
    year, region = _parse_when(when)
    scope = _build_scope(office, year, region)
    return ElectionTicker(
        raw=raw,
        office=office,
        when=when,
        candidate=candidate,
        region=region,
        scope=scope,
    )
