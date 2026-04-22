"""Static commodity-pair correlation matrix used by the per-vertical risk gate.

We hard-code the 11 highest-magnitude historical correlations across the
10 supported commodities (rolling 5-year daily-return correlations from
the EIA / USDA / LME composite series).  When a candidate trade is in
the commodities vertical the per_vertical_cap gate uses
`correlated_exposure()` to project the bot's effective exposure across
correlated symbols and reject when the projection exceeds the operator
cap (default 25% of bankroll).

Keeping the matrix sparse + static (vs. learning it online) keeps the
gate deterministic and explainable in postmortems — operators can read
exactly which pairs contributed to a rejection.
"""
from __future__ import annotations

from typing import Iterable, Mapping


# 11 ordered (sym_a, sym_b, rho) tuples.  Sym order is canonical
# (alphabetic by default code) so callers can index either way via
# `correlation_for()` without caring about pair direction.
CORRELATED_PAIRS: tuple[tuple[str, str, float], ...] = (
    # Energy complex
    ("WTI",    "NG",      0.30),
    ("WTI",    "COPPER",  0.40),
    ("WTI",    "GOLD",    0.20),
    # Precious metals (highly correlated)
    ("GOLD",   "SILVER",  0.78),
    ("GOLD",   "COPPER",  0.35),
    ("SILVER", "COPPER",  0.45),
    # Grains complex
    ("CORN",   "SOY",     0.65),
    ("CORN",   "WHEAT",   0.55),
    ("SOY",    "WHEAT",   0.50),
    # Softs
    ("COFFEE", "SUGAR",   0.40),
    # Cross-cluster (weather + USD beta)
    ("CORN",   "SUGAR",   0.30),
)


# Pre-computed lookup table for fast `correlation_for()` queries.  Keys
# are frozensets so (a,b) and (b,a) resolve identically.
_LOOKUP: dict[frozenset[str], float] = {
    frozenset((a, b)): rho for (a, b, rho) in CORRELATED_PAIRS
}


def correlation_for(symbol_a: str, symbol_b: str) -> float:
    """Return |rho| for the (a, b) pair, or 0.0 when uncorrelated.

    Self-pair returns 1.0 so the gate folds the candidate's own existing
    exposure into the projection without special-casing the call site.
    """
    if not symbol_a or not symbol_b:
        return 0.0
    a = symbol_a.upper()
    b = symbol_b.upper()
    if a == b:
        return 1.0
    return float(_LOOKUP.get(frozenset((a, b)), 0.0))


def correlated_exposure(
    open_per_symbol: Mapping[str, float],
    intended_symbol: str,
    intended_notional: float,
    *,
    extra_pairs: Iterable[tuple[str, str, float]] | None = None,
) -> dict[str, float]:
    """Project the correlation-weighted exposure of a candidate trade.

    `open_per_symbol` maps symbol -> currently-open USD notional in the
    commodities vertical; `intended_*` describes the trade we want to
    place.  Returns a dict with:

        * `intended_symbol`  : echoed
        * `intended_notional`: echoed
        * `correlated_open`  : sum over open positions of |rho| * notional
        * `projected`        : correlated_open + intended_notional
        * `pair_contributions`: {symbol: |rho| * notional} for diagnostics
    """
    inten = float(intended_notional or 0.0)
    sym = (intended_symbol or "").upper()
    contributions: dict[str, float] = {}
    correlated_open = 0.0

    extras_lookup: dict[frozenset[str], float] = {}
    if extra_pairs:
        for a, b, rho in extra_pairs:
            extras_lookup[frozenset((a.upper(), b.upper()))] = float(rho)

    for other_sym, notional in (open_per_symbol or {}).items():
        if not other_sym:
            continue
        other = other_sym.upper()
        n = float(notional or 0.0)
        if n <= 0:
            continue
        # Self-pair = 1.0 (own existing exposure on the same symbol counts
        # in full).  Otherwise consult the static matrix; extra pairs
        # passed by the caller can extend the matrix per cycle.
        rho = (
            extras_lookup.get(frozenset((sym, other)))
            if frozenset((sym, other)) in extras_lookup
            else correlation_for(sym, other)
        )
        weighted = abs(float(rho)) * n
        if weighted > 0:
            contributions[other] = weighted
            correlated_open += weighted

    return {
        "intended_symbol": sym,
        "intended_notional": inten,
        "correlated_open": correlated_open,
        "projected": correlated_open + inten,
        "pair_contributions": contributions,
    }
