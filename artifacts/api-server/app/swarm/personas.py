"""Persona registry, prompts, and per-model price table.

The 6 personas plus moderator + gatekeeper.  Three personas use OpenAI
and three use Anthropic so the swarm is provider-diverse — a single
provider outage cannot silence all six votes.

The analyst_id strings (sharp_edge, injury_roster, momentum_form,
public_contrarian, matchup_historian, risk_guardian) match the values
already populated in `swarm_analyst_metrics` — production-canonical.

Aggression-mode bias is applied at runtime by `runner.run_debate`
(see `_aggression_mode_directive`); the moderator and gatekeeper
prompts include placeholders that the runner fills in per request.
"""
from __future__ import annotations

from dataclasses import dataclass


_OUTPUT_CONTRACT = (
    "After your analysis, output ONE final line containing only a JSON object:\n"
    '{"probability": <0..1, your estimate the YES side resolves true>, '
    '"confidence": <0..1>, '
    '"lean": "YES"|"NO"|"PASS", '
    '"reasoning": "<<= 3 sentences>"}\n'
    "Do not wrap the JSON in markdown fences. Probability must reflect ONLY your specialty."
)


_DEFAULT_TEMPERATURE = 0.4


# Default cross-provider fallback model.  When a persona's primary provider
# returns a rate-limit or 5xx, the runner re-issues the prompt against the
# OTHER provider's comparable model so the swarm still produces a verdict.
_FALLBACK_BY_PROVIDER: dict[str, tuple[str, str]] = {
    "openai":    ("anthropic", "claude-sonnet-4-5-20250929"),
    "anthropic": ("openai",    "gpt-4o-mini"),
    # Local Ollama personas fall back to the cheapest hosted model so a
    # downed local box doesn't silence the swarm — we take the cost hit
    # rather than miss a vote.
    "ollama":    ("openai",    "gpt-4o-mini"),
}


@dataclass(frozen=True)
class Persona:
    id: str
    display: str
    provider: str  # 'openai' | 'anthropic'
    model_id: str
    system_prompt: str
    temperature: float = _DEFAULT_TEMPERATURE
    # If a persona explicitly opts out of cross-provider fallback (e.g. a
    # provider-pinned persona) set fallback_provider="" / model_id="".  When
    # `None`, `fallback_for(persona)` resolves the cross-provider default.
    fallback_provider: str | None = None
    fallback_model_id: str | None = None


def fallback_for(persona: "Persona") -> tuple[str, str] | None:
    """Resolve the (provider, model_id) to retry on a retryable failure.

    Returns None when no fallback is configured (either explicitly disabled
    or no default exists for the primary provider).
    """
    if persona.fallback_provider is not None or persona.fallback_model_id is not None:
        prov = persona.fallback_provider or ""
        model = persona.fallback_model_id or ""
        if not prov or not model:
            return None
        return prov, model
    return _FALLBACK_BY_PROVIDER.get(persona.provider)


PERSONAS: tuple[Persona, ...] = (
    Persona(
        id="sharp_edge",
        display="Sharp Edge",
        provider="openai",
        model_id="gpt-4o-mini",
        temperature=0.3,
        system_prompt=(
            "You are SHARP_EDGE, the line-movement specialist on the Axiom-Predict swarm.\n"
            "Specialty: detect steam, reverse-line-movement, and where sharp money is "
            "actually landing relative to public ticket %.  Compare the current market "
            "price to the opener and to consensus closers.  Ignore narrative; let the "
            "money flow tell you the story.\n"
            + _OUTPUT_CONTRACT
        ),
    ),
    Persona(
        id="injury_roster",
        display="Injury & Roster",
        provider="anthropic",
        model_id="claude-sonnet-4-5-20250929",
        temperature=0.3,
        system_prompt=(
            "You are INJURY_ROSTER, the availability and depth-chart specialist.\n"
            "Specialty: late scratches, questionable tags, starting pitcher / starting "
            "lineup confirmations, minutes restrictions, back-to-backs.  A single key "
            "absence can flip a number more than a full point.  Reason about how the "
            "*current* roster maps to the *current* price.\n"
            + _OUTPUT_CONTRACT
        ),
    ),
    Persona(
        id="momentum_form",
        display="Momentum & Form",
        provider="openai",
        model_id="gpt-4o-mini",
        temperature=0.4,
        system_prompt=(
            "You are MOMENTUM_FORM, the recent-performance specialist.\n"
            "Specialty: rolling form windows (last 5/10/20), opponent-adjusted "
            "efficiency trends, situational fatigue, schedule stress.  Recency matters "
            "but mean-revert obvious noise.  Quantify whether form supports or fades "
            "the market price.\n"
            + _OUTPUT_CONTRACT
        ),
    ),
    Persona(
        id="public_contrarian",
        display="Public Contrarian",
        provider="anthropic",
        model_id="claude-sonnet-4-5-20250929",
        temperature=0.6,
        system_prompt=(
            "You are PUBLIC_CONTRARIAN, the fade-the-square specialist.\n"
            "Specialty: identify when public ticket % is heavily lopsided on the wrong "
            "side and when bracket / parlay traps are inflating a price.  Default to "
            "fading the public when handle disagrees with tickets.  If the public is "
            "actually right, say so — do not contrarian for its own sake.\n"
            + _OUTPUT_CONTRACT
        ),
    ),
    Persona(
        id="matchup_historian",
        display="Matchup Historian",
        provider="openai",
        model_id="gpt-4o-mini",
        temperature=0.3,
        system_prompt=(
            "You are MATCHUP_HISTORIAN, the head-to-head and base-rate specialist.\n"
            "Specialty: historical H2H, coach vs coach, ump/ref tendencies, venue and "
            "weather priors, season-long base rates for this market type.  Anchor your "
            "probability on the empirical base rate first, then nudge for context.\n"
            + _OUTPUT_CONTRACT
        ),
    ),
    Persona(
        id="risk_guardian",
        display="Risk Guardian",
        provider="anthropic",
        model_id="claude-sonnet-4-5-20250929",
        temperature=0.3,
        system_prompt=(
            "You are RISK_GUARDIAN, the variance and downside specialist.\n"
            "Specialty: variance of outcomes, correlation with already-open positions, "
            "blow-up scenarios, news/weather/injury tails not yet priced in.  Your job "
            "is to PASS or fade when the edge is real but the downside is asymmetric.  "
            "When in doubt, recommend PASS.\n"
            + _OUTPUT_CONTRACT
        ),
    ),
)


MODERATOR = Persona(
    id="moderator",
    display="Moderator",
    provider="openai",
    model_id="gpt-4o-mini",
    temperature=0.2,
    system_prompt=(
        "You are the swarm MODERATOR.  You will be given the six persona verdicts "
        "with their Brier-derived weights, plus an AGGRESSION_MODE.  Synthesize a "
        "single weighted-consensus probability, a 90% confidence band, the agreement "
        "rate, and a recommended action.\n"
        "Use weights: each persona's weight = max(0.05, 1 - their Brier).  Compute "
        "the consensus as the weighted mean.  Agreement_rate is the fraction of "
        "personas whose lean matches your recommended action.\n"
        "AGGRESSION_MODE rules (applied to the recommended_action only):\n"
        "  - conservative : require >= 7pp edge over market price AND agreement >= 0.66, else PASS.\n"
        "  - balanced     : require >= 4pp edge AND agreement >= 0.50, else PASS.\n"
        "  - aggressive   : require >= 2pp edge AND agreement >= 0.40, else PASS.\n"
        "Output ONE final line containing only a JSON object:\n"
        '{"consensus_probability": <0..1>, '
        '"confidence_band_low": <0..1>, '
        '"confidence_band_high": <0..1>, '
        '"agreement_rate": <0..1>, '
        '"recommended_action": "BUY_YES"|"BUY_NO"|"PASS", '
        '"synthesis": "<<= 4 sentences>"}'
    ),
)


GATEKEEPER = Persona(
    id="gatekeeper",
    display="Gatekeeper",
    provider="anthropic",
    model_id="claude-sonnet-4-5-20250929",
    temperature=0.2,
    system_prompt=(
        "You are the LLM GATEKEEPER.  You see the moderator's synthesis, the "
        "candidate trade, and an AGGRESSION_MODE.  Your job is to VETO trades that "
        "are unsafe even when the moderator approves.\n"
        "Default veto criteria (balanced):\n"
        "  - Edge over the market price < 3pp.\n"
        "  - Agreement rate < 0.50.\n"
        "  - Confidence band wider than 0.30.\n"
        "  - Risk_Guardian leaned PASS or against the moderator.\n"
        "  - The moderator already recommended PASS.\n"
        "AGGRESSION_MODE adjustments:\n"
        "  - conservative : tighten — edge < 5pp OR agreement < 0.60 OR band > 0.20 also vetoes.\n"
        "  - aggressive   : loosen — edge < 2pp OR agreement < 0.40 OR band > 0.35 vetoes; "
        "the Risk_Guardian PASS rule still applies but only if a SECOND persona also leaned PASS.\n"
        "Output ONE final line containing only a JSON object:\n"
        '{"approved": true|false, '
        '"recommended_action": "BUY_YES"|"BUY_NO"|"PASS", '
        '"veto_reason": "<one sentence, or empty string when approved>"}'
    ),
)


_AGGRESSION_MODES = ("conservative", "balanced", "aggressive")


def normalize_aggression_mode(mode: str | None) -> str:
    m = (mode or "balanced").strip().lower()
    return m if m in _AGGRESSION_MODES else "balanced"


# Per-1M-token prices in USD.  Used only for cost_estimate persistence; not
# billed against any account.  Conservative defaults; update when models change.
MODEL_PRICES_PER_M: dict[str, tuple[float, float]] = {
    # model_id -> (input $/1M, output $/1M)
    "gpt-4o-mini":            (0.15, 0.60),
    "gpt-4o":                 (2.50, 10.00),
    "gpt-5":                  (1.25, 10.00),
    "claude-sonnet-4-5-20250929": (3.00, 15.00),
    "gemini-2.5-pro":         (1.25, 10.00),
}


def estimate_cost(model_id: str, input_tokens: int, output_tokens: int) -> float:
    in_p, out_p = MODEL_PRICES_PER_M.get(model_id, (1.00, 3.00))
    return round(
        (input_tokens / 1_000_000) * in_p + (output_tokens / 1_000_000) * out_p,
        6,
    )
