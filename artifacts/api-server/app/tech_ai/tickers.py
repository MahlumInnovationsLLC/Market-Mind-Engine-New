"""Parse Kalshi tech-AI binary tickers into a structured `TechAITicker`.

Recognised inputs (case-insensitive), all KX-prefixed:

    KXLLM-26MAY-Y                   -> generic LLM event,    direction=YES
    KXGPT5-26JUN30-Y                -> GPT-5 release-by date
    KXCLAUDE-26JUL15-Y              -> Claude release-by date
    KXLLAMA-26AUG-Y                 -> Llama release-by date
    KXAI-26DEC-T9000                -> AI sector threshold (e.g. MMLU>=K)
    KXNVDA-26Q3-T100                -> NVDA earnings beat threshold
    KXTSMC-26Q2-T2000               -> TSMC capex threshold
    KXMLPERF-26NOV-T0.85            -> MLPerf submission threshold
    KXGPU-26OCT-T1000000            -> GPU shipment milestone
    KXIPO-26SEP-Y                   -> IPO timing event

Symbol classes the resolver groups markets into:
    LLM, GPT, CLAUDE, LLAMA, AI, NVDA, TSMC, MLPERF, GPU, IPO
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final


SUPPORTED_SYMBOLS: Final[tuple[str, ...]] = (
    "LLM", "GPT", "CLAUDE", "LLAMA", "AI",
    "NVDA", "TSMC", "MLPERF", "GPU", "IPO",
)


# Kalshi ticker prefix -> canonical symbol class
_PREFIX_TO_SYMBOL: dict[str, str] = {
    "LLM":     "LLM",
    "GPT":     "GPT",
    "GPT5":    "GPT",
    "CLAUDE":  "CLAUDE",
    "LLAMA":   "LLAMA",
    "AI":      "AI",
    "NVDA":    "NVDA",
    "TSMC":    "TSMC",
    "MLPERF":  "MLPERF",
    "GPU":     "GPU",
    "IPO":     "IPO",
}


class InvalidTechAITicker(ValueError):
    """Raised when a ticker can't be parsed."""


@dataclass(frozen=True)
class TechAITicker:
    raw: str
    symbol: str           # canonical class
    when: str             # date / quarter token (informational)
    threshold: float | None  # numeric K (T-form) or None (Y/N event form)
    direction: str        # "above" | "below" | "yes"


_LEADING_KX = re.compile(r"^kx", re.IGNORECASE)
_BODY_RE = re.compile(
    r"""
    ^
    (?P<sym>[A-Z][A-Z0-9]*?)
    [-_]
    (?P<when>[A-Z0-9]+)
    [-_]
    (?P<pfx>[YNTB])
    (?P<k>\d+(?:\.\d+)?)?
    $
    """,
    re.VERBOSE | re.IGNORECASE,
)


def _resolve_symbol(sym_raw: str) -> str | None:
    s = sym_raw.upper()
    if s in _PREFIX_TO_SYMBOL:
        return _PREFIX_TO_SYMBOL[s]
    # Longest-prefix fallback so e.g. KXNVDAQ3 still maps to NVDA.
    for prefix in sorted(_PREFIX_TO_SYMBOL, key=len, reverse=True):
        if s.startswith(prefix):
            return _PREFIX_TO_SYMBOL[prefix]
    return None


def parse_tech_ai_ticker(ticker: str) -> TechAITicker:
    if not isinstance(ticker, str) or not ticker.strip():
        raise InvalidTechAITicker(f"empty ticker: {ticker!r}")
    raw = ticker.strip()
    body = _LEADING_KX.sub("", raw)
    m = _BODY_RE.match(body)
    if not m:
        raise InvalidTechAITicker(f"unrecognised ticker shape: {ticker!r}")
    symbol = _resolve_symbol(m.group("sym"))
    if symbol is None:
        raise InvalidTechAITicker(
            f"ticker {ticker!r} doesn't reference a supported tech-AI symbol "
            f"({', '.join(SUPPORTED_SYMBOLS)})"
        )
    pfx = m.group("pfx").upper()
    k_raw = m.group("k")
    threshold: float | None = None
    if k_raw is not None:
        try:
            threshold = float(k_raw)
        except ValueError as e:
            raise InvalidTechAITicker(
                f"ticker {ticker!r} has malformed threshold: {k_raw!r}"
            ) from e
        if threshold <= 0:
            raise InvalidTechAITicker(
                f"ticker {ticker!r}: non-positive threshold {threshold}"
            )
    if pfx == "Y":
        direction = "yes"
    elif pfx == "N":
        direction = "yes"  # NO-leg markets still resolve YES vs threshold semantics
    elif pfx == "B":
        direction = "below"
    else:
        direction = "above"
    return TechAITicker(
        raw=raw,
        symbol=symbol,
        when=m.group("when"),
        threshold=threshold,
        direction=direction,
    )
