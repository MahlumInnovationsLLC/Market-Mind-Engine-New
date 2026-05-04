"""Re-derive a real ``league`` value for V1-archive ``trade_analyses`` rows
that V1 wrote as literal ``'unknown'`` / ``'UNKNOWN'`` / NULL.

Task #740.  V1's writer fell back to the literal string ``'unknown'``
(or NULL) when its own per-trade league heuristic failed, so 598,218 of
603,128 imported rows landed in the synthetic ``UNKNOWN`` bucket.  The
backtest harness was filtering on ``league IS NOT NULL`` AND treating
the literal ``'UNKNOWN'`` as a real league key, which meant the per-
league calibrator lookup never matched and ~99% of usable archive rows
contributed nothing to the calibrated-Brier metric.

This module implements a deterministic, data-driven inference cascade
that runs ENTIRELY off the imported row's existing columns — no
network calls, no extra DB lookups beyond the row itself.  Order of
precedence matches the Task #740 contract; the first rule to fire wins:

1. **``analysis_json.kalshi_ticker`` (or ``kalshi_market_ticker`` /
   ``market_ticker`` / ``ticker`` / ``market_id`` / ``marketId``)
   prefix lookup** against the canonical 22-league Kalshi series
   prefix table.  V1's writer often recorded the Kalshi ticker
   inside the per-trade analysis blob even when the top-level
   ``league`` column was ``'unknown'``.  Highest confidence because
   the series segment is canonical and maps 1:1 to a league.
2. **``vertical`` 1:1 fallback** — if the row's ``vertical`` is one of
   the single-league verticals (``commodities`` -> ``COMMODITIES``,
   ``tech_ai`` -> ``TECH_AI``, ``elections`` -> ``ELECTIONS``,
   ``events_econ`` -> ``EVENTS_ECON``, ``entertainment`` ->
   ``ENTERTAINMENT``).  ``sports`` and ``soccer`` are intentionally
   NOT included here because they fan out to many leagues — leaving
   them at ``UNRESOLVED`` is more honest than mis-bucketing them.
3. **``analysis_json.sport`` / ``analysis_json.league``** — V1
   occasionally stashed the league directly in the per-trade analysis
   blob even when the column was blank.  Trusted only when the value
   normalises to one of the supported leagues (so a ``sport`` value of
   the literal string ``"basketball"`` will NOT mis-fire — it has no
   canonical SUPPORTED_LEAGUE that name maps to).
4. **``market_id`` regex/prefix fallback** — last-resort Kalshi-prefix
   match against the top-level ``market_id`` column for rows that have
   no analysis_json blob at all.

When NO rule fires the function returns ``None`` and the caller leaves
the row's ``league`` untouched.  The backtest harness must then treat
literal ``'UNKNOWN'`` / NULL as ``rows_unresolved`` (NOT a per-league
bucket) so the calibrator metrics aren't silently polluted.
"""
from __future__ import annotations

import re
from typing import Any, Mapping


# ---------------------------------------------------------------------------
# Supported leagues — mirrors ``app.calibrators.SUPPORTED_LEAGUES``.
# Kept inline (not imported) so this module has zero deps on the live
# calibrator state and can be unit-tested without DB / Supabase
# settings present.
# ---------------------------------------------------------------------------
SUPPORTED_LEAGUES: frozenset[str] = frozenset({
    "NBA", "NHL", "MLB", "NCAAB", "NFL",
    "EPL", "LALIGA", "SERIEA", "BUNDES", "LIGUE1", "MLS", "UCL",
    "UFC", "ATP", "WTA", "F1", "PGA",
    "COMMODITIES", "TECH_AI", "ELECTIONS", "EVENTS_ECON", "ENTERTAINMENT",
    # Task #754: ``CRYPTO`` joined the calibrator registry.  The
    # prefix rules below still don't map KXBTC/KXETH on purpose —
    # the V1 archive backfill identifies crypto rows via
    # ``vertical='crypto'`` (handled directly by
    # ``scripts/backtest_v1_archive.py`` and the v1_settled CTE),
    # which is more robust than a Kalshi-prefix heuristic.  Adding
    # the literal here just keeps this module's mirror in sync with
    # ``app.calibrators.SUPPORTED_LEAGUES`` so the drift-guard test
    # in ``test_v1_archive_league_inference.py`` passes.
    "CRYPTO",
    # Task #767: ``WEATHER`` joined the calibrator registry so the
    # GRU+EMOS temperature stack and the precipitation predictor stop
    # silently bypassing the calibrator.  Mirrored here per the same
    # drift-guard contract as CRYPTO above; the prefix-rule table
    # below intentionally leaves WEATHER unmapped because the V1
    # archive backfill identifies weather rows via
    # ``vertical='weather'`` (the precip + temperature predictors
    # always set the column directly), which is more robust than a
    # Kalshi-prefix heuristic across the WX-* / KXHIGH* / KXLOW*
    # series.
    "WEATHER",
})

# Sentinel league values V1 wrote when its own per-trade heuristic
# failed.  Treated identically to NULL by the inference cascade.
_UNRESOLVED_LITERALS: frozenset[str] = frozenset({"", "UNKNOWN", "NONE", "NULL"})


# ---------------------------------------------------------------------------
# Kalshi series prefix -> league.  Source of truth: the live producer
# in ``app/feeds/kalshi_sports_markets.py`` (sports series), the crypto
# producer ``app/feeds/kalshi_crypto_markets.py`` (BTC/ETH), and the
# per-vertical Kalshi series lists in ``app/{commodities,elections,
# events_econ,entertainment,tech_ai}/feeds.py`` etc.  Order matters
# only for ties: longer prefixes are checked first so ``KXATPMATCH``
# wins over a hypothetical ``KXATP`` shorthand.
#
# Conservative philosophy: when in doubt, prefer to NOT map.  A row
# left at UNRESOLVED contributes nothing to the calibrator, but a
# mis-mapped row pollutes a real per-league Brier score with off-
# distribution data.
# ---------------------------------------------------------------------------
_KALSHI_PREFIX_RULES: tuple[tuple[str, str], ...] = (
    # --- Sports: GAME-style series (one game per event) ----------------
    ("KXNFLGAME",       "NFL"),
    ("KXNBAGAME",       "NBA"),
    ("KXMLBGAME",       "MLB"),
    ("KXNHLGAME",       "NHL"),
    ("KXMLSGAME",       "MLS"),
    ("KXEPLGAME",       "EPL"),
    ("KXLALIGAGAME",    "LALIGA"),
    ("KXSERIEAGAME",    "SERIEA"),
    ("KXBUNDESGAME",    "BUNDES"),
    ("KXLIGUE1GAME",    "LIGUE1"),
    ("KXUCLGAME",       "UCL"),
    ("KXNCAABGAME",     "NCAAB"),
    # NCAAF maps to no per-league calibrator today (NCAAF is not in
    # SUPPORTED_LEAGUES); leave it unmapped so the row stays at
    # UNRESOLVED rather than landing in a synthetic NCAAF bucket the
    # calibrator stack can never serve.
    #
    # --- Combat / individual sports ------------------------------------
    ("KXUFCFIGHT",      "UFC"),
    ("KXUFC",           "UFC"),
    ("KXATPMATCH",      "ATP"),
    ("KXATP",           "ATP"),
    ("KXWTAMATCH",      "WTA"),
    ("KXWTA",           "WTA"),
    ("KXF1RACE",        "F1"),
    ("KXF1",            "F1"),
    ("KXPGATOURNEY",    "PGA"),
    ("KXPGA",           "PGA"),
    # --- Commodities ---------------------------------------------------
    ("KXOIL",           "COMMODITIES"),
    ("KXWTI",           "COMMODITIES"),
    ("KXBRENT",         "COMMODITIES"),
    ("KXGOLD",          "COMMODITIES"),
    ("KXSILVER",        "COMMODITIES"),
    ("KXNATGAS",        "COMMODITIES"),
    ("KXCORN",          "COMMODITIES"),
    ("KXWHEAT",         "COMMODITIES"),
    ("KXSOY",           "COMMODITIES"),
    ("KXCOPPER",        "COMMODITIES"),
    # --- Tech / AI -----------------------------------------------------
    ("KXAI",            "TECH_AI"),
    ("KXOPENAI",        "TECH_AI"),
    ("KXANTHROPIC",     "TECH_AI"),
    ("KXMETA",          "TECH_AI"),
    ("KXGOOGLE",        "TECH_AI"),
    ("KXAPPLE",         "TECH_AI"),
    ("KXMSFT",          "TECH_AI"),
    ("KXNVDA",          "TECH_AI"),
    ("KXTSLA",          "TECH_AI"),
    # --- Elections -----------------------------------------------------
    ("KXPRES",          "ELECTIONS"),
    ("KXSENATE",        "ELECTIONS"),
    ("KXHOUSE",         "ELECTIONS"),
    ("KXGOV",           "ELECTIONS"),
    ("KXELECTION",      "ELECTIONS"),
    ("KXPRIMARY",       "ELECTIONS"),
    ("KXCAUCUS",        "ELECTIONS"),
    # --- Events / macro econ -------------------------------------------
    ("KXFED",           "EVENTS_ECON"),
    ("KXFOMC",          "EVENTS_ECON"),
    ("KXCPI",           "EVENTS_ECON"),
    ("KXJOBS",          "EVENTS_ECON"),
    ("KXNFP",           "EVENTS_ECON"),
    ("KXGDP",           "EVENTS_ECON"),
    ("KXUNRATE",        "EVENTS_ECON"),
    ("KXRATE",          "EVENTS_ECON"),
    ("KXPCE",           "EVENTS_ECON"),
    # --- Entertainment -------------------------------------------------
    ("KXOSCAR",         "ENTERTAINMENT"),
    ("KXEMMY",          "ENTERTAINMENT"),
    ("KXGRAMMY",        "ENTERTAINMENT"),
    ("KXBOXOFFICE",     "ENTERTAINMENT"),
    ("KXMOVIE",         "ENTERTAINMENT"),
    ("KXTV",            "ENTERTAINMENT"),
    ("KXBILLBOARD",     "ENTERTAINMENT"),
    # --- Crypto (NOT in SUPPORTED_LEAGUES — leave unmapped) ------------
    # Intentionally absent: KXBTC / KXETH route through the crypto
    # vertical's own pricing stack, not the per-league calibrator.
    # Mapping them here would create a synthetic ``CRYPTO`` bucket the
    # calibrator can never serve.
)

# Sort longest-prefix-first so e.g. ``KXATPMATCH-...`` matches the ATP
# rule before falling through to a hypothetical shorter ``KXATP`` rule.
_KALSHI_PREFIX_RULES_SORTED: tuple[tuple[str, str], ...] = tuple(
    sorted(_KALSHI_PREFIX_RULES, key=lambda kv: -len(kv[0]))
)


# ---------------------------------------------------------------------------
# Vertical fallback — only single-league verticals.  Multi-league
# verticals (``sports``, ``soccer``) are intentionally left unmapped:
# we'd rather a row stay UNRESOLVED than be silently mis-bucketed into
# (e.g.) NBA when it was actually NHL.
# ---------------------------------------------------------------------------
_VERTICAL_RULES: dict[str, str] = {
    "commodities":   "COMMODITIES",
    "tech_ai":       "TECH_AI",
    "techai":        "TECH_AI",
    "elections":     "ELECTIONS",
    "events_econ":   "EVENTS_ECON",
    "eventsecon":    "EVENTS_ECON",
    "events":        "EVENTS_ECON",
    "macro":         "EVENTS_ECON",
    "entertainment": "ENTERTAINMENT",
}

# Common analysis_json key aliases for an explicit league or sport
# string field.  ``sport`` is included per the Task #740 contract;
# ``_normalize_league_value`` is conservative — it only fires for
# values that map to a SUPPORTED_LEAGUE, so a sport value like
# ``"basketball"`` will NOT mis-bucket as NBA.
_ANALYSIS_LEAGUE_KEYS: tuple[str, ...] = (
    "league", "League", "LEAGUE", "league_name", "leagueName",
    "sport", "Sport", "SPORT",
)
# Common analysis_json key aliases for a Kalshi market ticker.
_ANALYSIS_TICKER_KEYS: tuple[str, ...] = (
    "kalshi_ticker", "kalshi_market_ticker", "market_ticker",
    "ticker", "market_id", "marketId",
)


# Strip leading non-letter characters so a leading ``"``/``'`` from a
# malformed JSON-string-of-a-string can't block the prefix match.
_LEADING_NONALPHA = re.compile(r"^[^A-Za-z]+")


# ---------------------------------------------------------------------------
# V1-writer ``SPORT-<LEAGUE>-<SUB>-<HASH>`` market-id schema.
#
# Discovered while running the live backfill probe (2026-04-30) against
# Supabase: V1 wrote ~40,088 unresolved sports rows whose top-level
# ``market_id`` is the synthetic placeholder ``SPORT-<LEAGUE>-<SUB>-
# <md5hash>`` (e.g. ``SPORT-NCAAB-NCAAB-873982815bce991e0e334990ca56e228``)
# rather than a real Kalshi ticker.  The second hyphen-segment is the
# canonical league key.  This rule is V1-schema-specific, so it carries
# its own audit key (``market_id:v1_sport_schema``) and is separate from
# the Kalshi-prefix rule (rule 4).
# ---------------------------------------------------------------------------
_V1_SPORT_SCHEMA_RE = re.compile(r"^SPORT-([A-Z0-9_]+)-")


def _league_from_v1_sport_schema(market_id: Any) -> tuple[str, str] | None:
    """Parse ``SPORT-<LEAGUE>-...`` V1 placeholder market ids."""
    if not isinstance(market_id, str):
        return None
    m = _V1_SPORT_SCHEMA_RE.match(market_id.strip().upper())
    if m is None:
        return None
    candidate = _normalize_league_value(m.group(1))
    if candidate is None:
        return None
    return candidate, "market_id:v1_sport_schema"


def _normalize_league_value(raw: Any) -> str | None:
    """Return ``raw`` upper-cased if it maps to a SUPPORTED league, else ``None``.

    Also rewrites a few common league-name variants V1 stored loosely
    (``serie_a`` -> ``SERIEA``, ``bundesliga`` -> ``BUNDES`` etc.) onto
    the canonical SUPPORTED_LEAGUES key.
    """
    if not isinstance(raw, str):
        return None
    val = raw.strip().upper()
    if not val or val in _UNRESOLVED_LITERALS:
        return None
    # Canonical aliases — V1 was loose with these.
    aliases: dict[str, str] = {
        "SERIE_A":       "SERIEA",
        "SERIE A":       "SERIEA",
        "BUNDESLIGA":    "BUNDES",
        "LIGUE_1":       "LIGUE1",
        "LIGUE 1":       "LIGUE1",
        "LA_LIGA":       "LALIGA",
        "LA LIGA":       "LALIGA",
        "CHAMPIONS_LEAGUE": "UCL",
        "CHAMPIONS LEAGUE": "UCL",
        "TECH-AI":       "TECH_AI",
        "TECHAI":        "TECH_AI",
        "EVENTS-ECON":   "EVENTS_ECON",
        "EVENTSECON":    "EVENTS_ECON",
    }
    val = aliases.get(val, val)
    if val in SUPPORTED_LEAGUES:
        return val
    return None


def _league_from_kalshi_ticker(ticker: Any) -> str | None:
    """Map a Kalshi ticker to a league via series-prefix lookup."""
    if not isinstance(ticker, str):
        return None
    t = _LEADING_NONALPHA.sub("", ticker).strip().upper()
    if not t:
        return None
    for prefix, league in _KALSHI_PREFIX_RULES_SORTED:
        if t.startswith(prefix):
            return league
    return None


def _league_from_analysis_ticker(blob: Any) -> tuple[str, str] | None:
    """Run the Kalshi-prefix lookup against any ticker-shaped key in the blob.

    Used by cascade rule (1) — highest precedence.
    """
    if not isinstance(blob, Mapping):
        return None
    for key in _ANALYSIS_TICKER_KEYS:
        if key in blob:
            mapped = _league_from_kalshi_ticker(blob.get(key))
            if mapped is not None:
                return mapped, f"analysis_json.{key}:kalshi_prefix"
    return None


def _league_from_analysis_field(blob: Any) -> tuple[str, str] | None:
    """Inspect explicit ``league`` / ``sport`` fields on the analysis blob.

    Used by cascade rule (3).  Conservative: only fires for values
    that ``_normalize_league_value`` recognises as a SUPPORTED_LEAGUE,
    so a generic sport name like ``"basketball"`` will NOT mis-fire.
    """
    if not isinstance(blob, Mapping):
        return None
    for key in _ANALYSIS_LEAGUE_KEYS:
        if key in blob:
            mapped = _normalize_league_value(blob.get(key))
            if mapped is not None:
                return mapped, f"analysis_json.{key}"
    return None


def _league_from_vertical(vertical: Any) -> tuple[str, str] | None:
    if not isinstance(vertical, str):
        return None
    v = vertical.strip().lower()
    if not v:
        return None
    league = _VERTICAL_RULES.get(v)
    if league is None:
        return None
    return league, f"vertical:{v}"


def is_unresolved(raw_league: Any) -> bool:
    """True when ``raw_league`` is the V1 sentinel ('unknown'/NULL/'')."""
    if raw_league is None:
        return True
    if not isinstance(raw_league, str):
        return False
    return raw_league.strip().upper() in _UNRESOLVED_LITERALS


def infer_league(
    *,
    market_id: Any,
    vertical: Any,
    analysis_json: Any,
    raw_league: Any = None,
) -> tuple[str, str] | None:
    """Re-derive a real league for a V1 row whose ``league`` is unresolved.

    Returns ``(league, rule_key)`` on success, ``None`` if no rule
    fires.  ``rule_key`` is the audit string written to
    ``v1_archive.trade_analyses.league_inferred_from`` so a future
    audit can see WHICH rule fired without re-running the cascade.

    The caller is responsible for ALSO appending the original league
    value (``orig=<v1_value>``) to the audit string before persisting,
    so a rollback is possible without re-querying the dump.

    Cascade order (matches Task #740 contract):
      (1) ``analysis_json.kalshi_ticker`` (and aliases) Kalshi prefix
      (2) ``vertical`` 1:1 fallback for single-league verticals
      (3) ``analysis_json.sport`` / ``analysis_json.league`` direct field
      (4) ``market_id`` top-level Kalshi prefix fallback
    """
    # Rule 1 — analysis_json ticker prefix (highest precedence: explicit
    # ticker reference is the strongest signal).
    aj_ticker = _league_from_analysis_ticker(analysis_json)
    if aj_ticker is not None:
        return aj_ticker
    # Rule 2 — vertical 1:1 mapping (only single-league verticals; runs
    # before the analysis_json field check because a single-league
    # vertical is deterministic — even if analysis_json carries a
    # garbled league string, the vertical is the source of truth).
    vf = _league_from_vertical(vertical)
    if vf is not None:
        return vf
    # Rule 3 — analysis_json explicit league/sport field.
    aj_field = _league_from_analysis_field(analysis_json)
    if aj_field is not None:
        return aj_field
    # Rule 4 — top-level market_id Kalshi prefix fallback (last resort
    # for rows whose analysis_json is missing entirely).
    mapped = _league_from_kalshi_ticker(market_id)
    if mapped is not None:
        return mapped, "market_id:kalshi_prefix"
    # Rule 5 — V1-writer's ``SPORT-<LEAGUE>-<SUB>-<HASH>`` synthetic
    # placeholder schema.  Discovered live (2026-04-30); recovers
    # ~40k sports rows whose original Kalshi ticker was dropped by V1.
    v1_sport = _league_from_v1_sport_schema(market_id)
    if v1_sport is not None:
        return v1_sport
    return None


__all__ = [
    "SUPPORTED_LEAGUES",
    "infer_league",
    "is_unresolved",
]
