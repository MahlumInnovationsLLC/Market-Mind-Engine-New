"""Built-in vertical specs migrated from the old hardcoded
`VERTICAL_LEAGUES` dict in `orchestrator.cycle`.

Registration order MUST stay sports → weather → crypto → crypto-binary
→ politics so the dashboard strip keeps its existing column order
(test_vertical_performance pins this).
"""
from __future__ import annotations

from .registry import REGISTRY, VerticalSpec


REGISTRY.register(VerticalSpec(
    name="sports",
    leagues=(),  # spans every supported league
    ticker_prefixes=("KXNFL", "KXNBA", "KXMLB", "KXNHL", "KXMLS", "KXEPL", "KXNCAAF", "KXNCAAB"),
    persona_profile="sports",
    include_in_dashboard_strip=True,
    description="Live sports moneyline / spread / total markets across every supported league.",
))

REGISTRY.register(VerticalSpec(
    name="weather",
    leagues=("WEATHER",),
    ticker_prefixes=("KXHIGH", "KXTEMP", "KXLOW", "KXRAIN"),
    persona_profile="weather",
    # Temperature contracts (KXHIGH/KXTEMP/KXLOW) route through the
    # GRU + EMOS pipeline (task #246).  KXRAIN parses cleanly fail and
    # falls through to the generic meta-model so precip markets keep
    # trading while a precip-specific stack is wired in.
    predictor_hook="weather_temp_gru_emos",
    include_in_dashboard_strip=True,
    description="NOAA-driven binary weather markets (temperature thresholds, precipitation, etc).",
))

REGISTRY.register(VerticalSpec(
    name="crypto",
    leagues=("CRYPTO",),
    ticker_prefixes=("KXBTC", "KXETH"),
    persona_profile="crypto",
    duplicate_trade_exempt=True,
    include_in_dashboard_strip=True,
    description="Spot-crypto direction markets routed through the generic meta-model.",
))

REGISTRY.register(VerticalSpec(
    name="crypto-binary",
    leagues=("CRYPTO",),
    ticker_prefixes=("KXBTC", "KXETH"),
    persona_profile="crypto",
    predictor_hook="crypto_binary",
    description=(
        "BTC/ETH 'closes above K' binaries served by the OHLCV ensemble "
        "(LGB+XGB 5m/15m/1h + GRU directional blend)."
    ),
))

REGISTRY.register(VerticalSpec(
    name="politics",
    leagues=("POLITICS",),
    persona_profile="politics",
    # Hard-disabled in task #767: the legacy politics vertical has no
    # live data feed and no fitted calibrator, but was still wired
    # into orchestrator.scheduler so a stray
    # `AXIOM_ORCHESTRATOR_VERTICALS=politics` could ship uncalibrated
    # raw probabilities into Kelly sizing.  We keep the spec
    # registered so the dashboard's frozen-order snapshot
    # (sports/weather/crypto/crypto-binary/politics) and the registry
    # introspection endpoints stay backwards-compatible, but
    # `enabled=False` causes `_CycleScheduler.start` to skip spawning
    # a loop task — there is no env-only escape hatch.  Use the
    # `elections` vertical for live political markets.
    enabled=False,
    description=(
        "Legacy politics vertical — hard-disabled in task #767; the "
        "live elections vertical replaces it.  Kept registered for "
        "dashboard frozen-order back-compat only."
    ),
))

# Commodities (task #198).  Per-contract LGB+XGB binary-close stack
# with a tight Kelly multiplier (0.15) — the swarm's geopolitics +
# weather + curve personas already concentrate risk on macro events,
# so the size cap stays well below the team-sport default.
# Registered AFTER `politics` so the dashboard frozen-order snapshot
# (sports/weather/crypto/crypto-binary/politics) is preserved.
REGISTRY.register(VerticalSpec(
    name="commodities",
    leagues=("COMMODITIES",),
    ticker_prefixes=(
        "KXOIL", "KXGOLD", "KXNATGAS", "KXCORN", "KXSOY",
        "KXWHEAT", "KXCOPPER", "KXSILVER", "KXCOFFEE", "KXSUGAR",
    ),
    persona_profile="commodities",
    predictor_hook="commodities_binary",
    kelly_multiplier=0.15,
    default_allocation_pct=0.10,
    duplicate_trade_exempt=True,
    # Promoted to the operator dashboard strip in task #218 alongside
    # `soccer` (#196) and `combat-individual` (#215).  Commodities is now
    # a first-class vertical that shows live trade count + P&L on the
    # main strip; the frozen-order test in tests/test_verticals_registry.py
    # asserts it appears after the legacy three + soccer + combat-individual.
    include_in_dashboard_strip=True,
    description=(
        "Commodities binary-close markets across WTI, gold, natgas, "
        "corn, soy, wheat, copper, silver, coffee and sugar.  Routed "
        "through the per-contract LGB+XGB stack with an 11-pair "
        "correlation sub-check on the per_vertical_cap risk gate."
    ),
))

# Tech / AI (task #199).  Bayesian evidence-driven binary stack with a
# conservative paper-grade Kelly multiplier (0.06).  Auto-promotes to
# 0.10 only after 30 settled trades show Brier<0.20 and an admin
# confirms via the `/api/admin/tech-ai/promote-kelly` endpoint, which
# writes `risk.kelly_multiplier_tech_ai=0.10` into `system_settings`.
# Registered AFTER `commodities` so the dashboard frozen-order snapshot
# is preserved.
REGISTRY.register(VerticalSpec(
    name="tech-ai",
    leagues=("TECH_AI",),
    ticker_prefixes=(
        "KXLLM", "KXGPT", "KXCLAUDE", "KXLLAMA", "KXAI",
        "KXNVDA", "KXTSMC", "KXMLPERF", "KXGPU", "KXIPO",
    ),
    persona_profile="tech-ai",
    predictor_hook="tech_ai_evidence",
    kelly_multiplier=0.06,
    default_allocation_pct=0.10,
    duplicate_trade_exempt=True,
    description=(
        "Tech / AI binary markets across model-release dates, GPU "
        "shipment milestones, IPO timing, AI benchmark thresholds and "
        "antitrust outcomes.  Routed through a Bayesian evidence "
        "model that combines GitHub releases, Hugging Face trending, "
        "ArXiv submissions and MLPerf signals."
    ),
))

# Soccer (task #196).  Distinct from `sports` so the orchestrator can
# route candidates through the soccer-specific persona stack (xG /
# rotation / market microstructure) and hit per-league caps tuned for
# 3-way moneyline shape.  Operators select between `sports` and
# `soccer` via `AXIOM_ORCHESTRATOR_VERTICALS`; running both is
# supported but soccer leagues will be debated under the soccer
# profile only when this spec is enabled.
# Combat & individual sports (task #197).  Distinct from the generic
# `sports` vertical because outcome shape (per-fighter / per-driver /
# per-player N-way fields) breaks the home/away assumption baked into
# the team-sport feature builder + persona prompts.  Operators select
# via `AXIOM_ORCHESTRATOR_VERTICALS`.  Promoted onto the operator
# dashboard strip in task #215 once the strip layout was widened to a
# responsive grid (5+ columns at xl, 2-up on mobile).
REGISTRY.register(VerticalSpec(
    name="combat-individual",
    leagues=("UFC", "ATP", "WTA", "F1", "PGA"),
    ticker_prefixes=("KXUFC", "KXATP", "KXWTA", "KXF1", "KXPGA"),
    persona_profile="combat-individual",
    # Tighter Kelly here than team sports — N-way fields carry more
    # variance per leg and the bot already concentrates exposure via
    # `LeagueConfig.per_event_cap_pct`.
    kelly_multiplier=0.5,
    default_allocation_pct=0.10,
    include_in_dashboard_strip=True,
    description=(
        "Combat & individual sports: UFC fight cards, ATP/WTA tennis "
        "tournaments (h2h + outright winner), F1 race + qualifying, "
        "PGA tournament leaderboards.  N-way winner markets priced "
        "via Bradley-Terry / Plackett-Luce on rolling rating priors."
    ),
))


# Elections (task #201).  Poll-of-polls + state-bias-correction +
# Polymarket cross-reference + arb detection.  Conservative
# paper-grade Kelly multiplier (0.05 — lower than tech-AI's 0.06
# because election markets historically resolve in correlated
# clusters that punish over-sizing).  Auto-promotes to 0.08 (also
# lower than tech-AI's 0.10) after 30 settled trades show
# Brier<0.20 and an admin confirms via the
# `/api/admin/elections/promote-kelly` endpoint, which writes
# `risk.kelly_multiplier_elections=0.08` into `system_settings`.
REGISTRY.register(VerticalSpec(
    name="elections",
    leagues=("ELECTIONS",),
    ticker_prefixes=(
        "KXPRES", "KXGOV", "KXSEN", "KXHOUSE",
        "KXFRPRES", "KXUKPM", "KXDEPM", "KXCAPM", "KXAUPM", "KXELE",
    ),
    persona_profile="elections",
    predictor_hook="elections_poll_of_polls",
    kelly_multiplier=0.05,
    default_allocation_pct=0.10,
    duplicate_trade_exempt=True,
    description=(
        "US + foreign election binary markets across presidential, "
        "gubernatorial, senate, house and foreign-leadership races.  "
        "Routed through a poll-of-polls model with per-state "
        "historical bias correction and a Polymarket cross-reference "
        "that powers a Kalshi-vs-Polymarket arb panel."
    ),
))


# Events / macro economics (task #202).  Consensus-prior model with
# rolling forecast-error variance.  Default Kelly multiplier 0.15 —
# the highest non-sports vertical per spec, justified by macro prints
# being information-rich and resolving cleanly on a known timestamp.
# Auto-promotes to 0.20 after 30 settled trades show Brier<0.20 and an
# admin confirms via `/api/admin/events-econ/promote-kelly`, which
# writes `risk.kelly_multiplier_events_econ=0.20` into system_settings.
REGISTRY.register(VerticalSpec(
    name="events-econ",
    leagues=("EVENTS_ECON",),
    ticker_prefixes=(
        "KXCPI", "KXPPI", "KXGDP", "KXFOMC", "KXNFP",
        "KXJOBLESS", "KXRETAIL", "KXPCE", "KXISM",
        "KXCONFIDENCE", "KXHOUSING",
    ),
    persona_profile="events-econ",
    predictor_hook="events_econ_consensus_prior",
    kelly_multiplier=0.15,
    default_allocation_pct=0.10,
    duplicate_trade_exempt=True,
    description=(
        "Macro economic-release binary markets across CPI, PPI, GDP, "
        "FOMC, NFP, jobless claims, retail sales and related prints.  "
        "Routed through a consensus-prior model with rolling "
        "forecast-error variance and a 5-minute pre-release blackout."
    ),
))


# Entertainment (task #203).  Hybrid log-normal (box-office) +
# Bayesian-precursor (awards) + empirical chart-rank (Spotify/streams)
# binary stack with a paper-grade Kelly multiplier (0.05).  Auto-promotes
# to 0.08 after 30 settled trades show Brier<0.22 and an admin confirms
# via `/api/admin/entertainment/promote-kelly`, which writes
# `risk.kelly_multiplier_entertainment=0.08` into `system_settings`.
REGISTRY.register(VerticalSpec(
    name="entertainment",
    leagues=("ENTERTAINMENT",),
    ticker_prefixes=(
        "KXBOX", "KXOSCAR", "KXEMMY", "KXGRAMMY",
        "KXSPOTIFY", "KXSTREAM", "KXRT",
    ),
    persona_profile="entertainment",
    predictor_hook="entertainment_hybrid",
    kelly_multiplier=0.05,
    default_allocation_pct=0.10,
    duplicate_trade_exempt=True,
    description=(
        "Entertainment binary markets across box-office opening "
        "weekends, awards-show winners (Oscars, Emmys, Grammys), "
        "Spotify / streaming chart positions and Rotten Tomatoes "
        "critic-score thresholds.  Routed through a hybrid log-normal "
        "/ Bayesian / empirical model with a release-day blackout."
    ),
))


REGISTRY.register(VerticalSpec(
    name="soccer",
    leagues=("EPL", "LALIGA", "SERIEA", "BUNDES", "LIGUE1", "MLS", "UCL"),
    ticker_prefixes=(
        "KXEPL", "KXLALIGA", "KXSERIEA", "KXBUNDES",
        "KXLIGUE1", "KXMLS", "KXUCL",
    ),
    persona_profile="soccer",
    # Soccer joins the operator strip alongside sports/weather/crypto so
    # operators can see per-vertical P&L, open positions and today's
    # cycle activity at a glance.  The legacy-order regression test
    # was updated to append "soccer" after the original three.
    include_in_dashboard_strip=True,
    description=(
        "International soccer 3-way moneyline (home/draw/away) markets "
        "across EPL, La Liga, Serie A, Bundesliga, Ligue 1, MLS and "
        "UEFA Champions League."
    ),
))
