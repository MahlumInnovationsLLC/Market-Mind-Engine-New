"""In-game live prediction loop (task #830).

Schedule-driven re-evaluator for open sports markets while their
underlying game is in-progress.  Mirrors the shape of
:mod:`app.learning.shadow_predictor`:

  * A single :func:`run_in_game_cycle` entrypoint per sport that the
    scheduler ticks on a per-sport cadence.
  * Per-sport cadence floor read from ``runtime_settings`` under
    ``risk.live_loop_cadence_<sport>`` (seconds) so the operator can
    bump or throttle a single league without redeploy.
  * Per-sport enable flag at ``risk.live_loop_enabled_<sport>`` —
    **default False**.  This ships per-league disabled until the
    shadow Brier for the league is within 0.02 of the pre-game model
    (the validation rollout sequence is documented in the task
    brief; the per-league flip happens via the operator console once
    the criterion is met).
  * Idempotency keyed on ``(market_id, minute_bucket)`` so a
    same-minute re-run is a no-op.
  * In-memory ``_LAST_RUN_AT`` map enforces the cadence floor without
    needing a separate scheduler-side lock.

This module deliberately does NOT itself call the swarm / predictor
chain — that path is large enough that each sport's live feature
builder lands as its own follow-up task (#831/#832 + per-sport).
What this module DOES do:

  1. Enumerate open ``predictions`` rows whose league belongs to the
     requested sport and whose game is currently in ``sportradar_live_state``
     with a status in ``_LIVE_STATUSES``.
  2. Tag each row's underlying ``predictions.is_live`` to ``true``
     (monotonic — the column never flips back).
  3. Return a structured result the scheduler / dashboard can render
     so operators can see the live loop is actually doing work even
     before the per-sport prediction wiring lands.

Per-sport prediction wiring plugs in via
``_predict_live_for_market`` and the
:data:`_LIVE_FEATURE_BUILDERS` registry.  NHL ships first (task #856)
with its own live-feature builder
(:mod:`app.learning.nhl_live_features`); additional sports register
their builders here as they come online.  When a builder returns a
probability the loop writes it back to ``predictions.model_yes_prob``
so the orchestrator's next sweep routes the updated value through a
``RiskContext`` with ``is_live=True`` and the
:mod:`app.risk.gates` ``stale_live_state`` and
``live_kelly_multiplier`` gates take effect.

A sport without a registered builder still has its open markets
tagged ``is_live=true`` (driving the dashboard tile + the live risk
gates against the pre-game probability) — the per-sport rollout
sequence is strictly opt-in.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .. import db
from . import nba_live as _nba_live
from . import nba_live_features
from . import nhl_live_features as _nhl_features
from . import soccer_live as _soccer_live


log = logging.getLogger(__name__)


# Sports for which the NBA-style predictor + gate-routed path is
# wired up (task #855).  These go through
# ``_predict_live_for_market_nba`` + the live-only gate chain and
# stamp the calibrated YES probability via the
# ``model_yes_prob + is_live`` UPDATE.
_PREDICT_LIVE_SUPPORTED_SPORTS: frozenset[str] = frozenset({"nba"})


# Per-sport live-prediction wiring registry (task #856 + follow-ups).
# Mapping is keyed on the normalised sport name from
# :data:`SPORT_LEAGUES`.  Each entry is a callable
# ``(cand) -> (names, values)`` that builds the live feature vector
# for one candidate row.  Sports not in this map keep the historical
# stub behaviour so the per-sport rollout sequence in the parent
# task #836 brief stays strictly opt-in: a new league only goes live
# once both its builder is registered here AND the operator flips
# ``risk.live_loop_enabled_<sport>`` in runtime_settings.
_LIVE_FEATURE_BUILDERS = {
    "nhl": _nhl_features.build_nhl_live,
}


# Sportradar live-state statuses that count as "game in-progress" for
# the loop.  Matches the set the sportradar feed itself filters on
# (see ``app.feeds.sportradar._LIVE_STATUSES``) so the loop and the
# feed agree on what "live" means.
_LIVE_STATUSES = frozenset(
    {
        "inprogress",
        "in_progress",
        "live",
        "started",
        "halftime",
        "delayed",
        "1st_half",
        "2nd_half",
        "pause",
        "1st_quarter",
        "2nd_quarter",
        "3rd_quarter",
        "4th_quarter",
        "ot",
        "overtime",
    }
)


# Per-sport cadence defaults (seconds between re-evaluations).  These
# are the floor — the operator can override per sport via the
# runtime_setting key ``risk.live_loop_cadence_<sport>``.  Tighter
# leagues (NBA, NHL, soccer) re-evaluate every 30s; slower leagues
# (NFL between drives, MLB between innings) settle for 60-90s.
# Defaults sourced from the task #830 brief and the per-league feed
# tempo: NBA play-by-play ~10s so a 60s re-eval window is plenty;
# NHL/NCAAB tick a bit slower → 90s; MLB/NFL have long between-play
# gaps → 120s; soccer's tight feed re-evaluates at 60s.
DEFAULT_CADENCE_SECONDS: dict[str, float] = {
    "nba": 60.0,
    "nhl": 90.0,
    "mlb": 120.0,
    "nfl": 120.0,
    "ncaab": 90.0,
    "ncaaf": 120.0,
    "soccer": 60.0,
}
DEFAULT_CADENCE_FALLBACK_SECONDS = 90.0


# Per-sport batch cap so a runaway sportradar feed cannot make the
# loop scan more than this many markets in one pass.  Matches the
# shadow_predictor convention.
DEFAULT_BATCH_LIMIT = 1_000


# Sport name → ordered list of league strings stored on ``predictions.league``.
# Keeps the loop sport-scoped (operator throttles one sport without
# touching the others) while still letting a "sport" like "soccer"
# fan out across multiple leagues.  Source of truth for league names
# is the predictions table itself — the values below match what the
# orchestrator + sportradar feed write.
SPORT_LEAGUES: dict[str, tuple[str, ...]] = {
    "nba": ("NBA",),
    "nhl": ("NHL",),
    "mlb": ("MLB",),
    "nfl": ("NFL",),
    "ncaab": ("NCAAB",),
    "ncaaf": ("NCAAF",),
    # Canonical soccer league names (match ``predictions.league`` —
    # see ``app.sports.soccer_context.SOCCER_LEAGUES`` and
    # ``app.verticals.builtins`` for the source of truth).  Earlier
    # drafts of this list used the longer spellings (BUNDESLIGA /
    # SERIE_A / LIGUE_1) which never matched any row.
    "soccer": (
        "EPL", "LALIGA", "SERIEA", "BUNDES", "LIGUE1", "MLS", "UCL",
    ),
}


# In-process cadence enforcement.  A dict keyed on (sport,
# minute_bucket) is sufficient — the worker process is restart-safe
# because the next tick after a restart will simply re-run (idempotent
# tagging means no harm), and the cadence floor is "at most once per
# window" not "exactly once".
_LAST_RUN_AT: dict[str, float] = {}


# Idempotency cache: keyed on (market_id, minute_bucket).  We don't
# write a new "shadow_predictions"-style table for the live loop —
# the operator-visible effect of a run is the ``predictions.is_live``
# tag and the dashboard tile counts, both of which are idempotent
# under same-minute re-runs by construction.
_TAGGED_THIS_MINUTE: dict[tuple[str, str], float] = {}
_TAGGED_THIS_MINUTE_MAX_ENTRIES = 50_000


@dataclass
class InGameCycleResult:
    sport: str
    started_at: str
    completed_at: str | None = None
    duration_seconds: float = 0.0
    enabled: bool = False
    skipped_reason: str | None = None
    cadence_seconds: float = 0.0
    candidates_scanned: int = 0
    candidates_live: int = 0
    candidates_tagged: int = 0
    candidates_skipped_duplicate: int = 0
    # Task #835 — cross-book best-line re-checks fired against each
    # in-progress market on every loop tick.  ``best_line_checks`` is
    # the number of rows we ran the live-mode gate against;
    # ``best_line_blocks`` and ``best_line_reroutes`` count the
    # decisions the gate made so operators can see at a glance how
    # often the live freshness window is producing actionable cross-
    # book deltas.
    best_line_checks: int = 0
    best_line_blocks: int = 0
    best_line_reroutes: int = 0
    # Per-sport live prediction wiring counters (task #857 — soccer
    # via the soccer_live dispatcher path).
    live_predictions_computed: int = 0
    live_predictions_persisted: int = 0
    # Task #855 — NBA-specific predictor path: scores each candidate
    # via the meta-model with league="NBA", routes through the
    # live-only gates (gate_stale_live_state +
    # gate_live_kelly_multiplier), and stamps the calibrated YES
    # probability via the model_yes_prob+is_live UPDATE.
    predictions_made: int = 0
    gate_rejections: int = 0
    predictions_persisted: int = 0
    # Task #856 — registry-based predictor path (currently NHL).
    # Counts candidates that produced a calibrated YES probability and
    # those whose ``predictions.model_yes_prob`` UPDATE flipped a row.
    candidates_predicted: int = 0
    candidates_prob_updated: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "sport": self.sport,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_seconds": round(self.duration_seconds, 4),
            "enabled": self.enabled,
            "skipped_reason": self.skipped_reason,
            "cadence_seconds": self.cadence_seconds,
            "candidates_scanned": self.candidates_scanned,
            "candidates_live": self.candidates_live,
            "candidates_tagged": self.candidates_tagged,
            "candidates_skipped_duplicate": self.candidates_skipped_duplicate,
            "best_line_checks": self.best_line_checks,
            "best_line_blocks": self.best_line_blocks,
            "best_line_reroutes": self.best_line_reroutes,
            "live_predictions_computed": self.live_predictions_computed,
            "live_predictions_persisted": self.live_predictions_persisted,
            "predictions_made": self.predictions_made,
            "gate_rejections": self.gate_rejections,
            "predictions_persisted": self.predictions_persisted,
            "candidates_predicted": self.candidates_predicted,
            "candidates_prob_updated": self.candidates_prob_updated,
            "errors": list(self.errors),
        }


def _minute_bucket(now: datetime | None = None) -> str:
    n = now or datetime.now(timezone.utc)
    return n.replace(second=0, microsecond=0).isoformat()


def _normalize_sport(sport: str) -> str:
    return (sport or "").strip().lower()


async def _runtime_value(key: str) -> str | None:
    try:
        row = await db.fetchrow(
            "SELECT value FROM runtime_settings WHERE key = $1", key
        )
        return (row["value"] or "").strip() if row else None
    except Exception as e:  # pragma: no cover — defensive
        log.debug("runtime_settings lookup for %s failed: %s", key, e)
        return None


async def is_enabled(sport: str) -> bool:
    """Per-sport enable flag.  Default False (ships disabled)."""
    raw = await _runtime_value(f"risk.live_loop_enabled_{_normalize_sport(sport)}")
    if raw is None:
        return False
    return raw.strip().lower() in ("1", "true", "yes", "on", "y")


async def cadence_seconds(sport: str) -> float:
    """Per-sport cadence floor (seconds)."""
    sp = _normalize_sport(sport)
    raw = await _runtime_value(f"risk.live_loop_cadence_{sp}")
    default = DEFAULT_CADENCE_SECONDS.get(sp, DEFAULT_CADENCE_FALLBACK_SECONDS)
    if raw is None:
        return default
    try:
        v = float(raw)
        if v <= 0:
            return default
        return v
    except (TypeError, ValueError):
        return default


async def _fetch_live_eligible(
    sport: str, limit: int = DEFAULT_BATCH_LIMIT
) -> list[dict[str, Any]]:
    """Open ``predictions`` rows whose league belongs to ``sport`` and
    whose sportradar game is currently in-progress.  Returned dicts
    carry the fields the future swarm/predictor wiring will need
    (market_id, league, game_id, fetched_at) plus the loop's bookkeeping
    keys.
    """
    leagues = SPORT_LEAGUES.get(_normalize_sport(sport)) or ()
    if not leagues:
        return []
    try:
        rows = await db.fetch(
            """
            SELECT p.kalshi_market_id  AS market_id,
                   p.league             AS league,
                   p.game_id            AS game_id,
                   p.game_time          AS game_time,
                   p.is_live            AS is_live,
                   p.model_yes_prob     AS model_yes_prob,
                   p.kalshi_price       AS kalshi_price,
                   p.matchup            AS matchup,
                   p.home_team          AS home_team,
                   p.away_team          AS away_team,
                   -- ``metadata`` is needed by the per-sport live
                   -- predictors that want to read producer-side hints
                   -- (e.g. soccer's ``leg_orientation`` set by the
                   -- ``kalshi_sports_markets`` feed — task #880).
                   p.metadata           AS metadata,
                   s.status             AS status,
                   s.home_score         AS home_score,
                   s.away_score         AS away_score,
                   s.period             AS period,
                   s.clock              AS clock,
                   extract(epoch from (now() - s.fetched_at))::float
                                        AS state_age_seconds
              FROM predictions p
              JOIN sportradar_live_state s
                ON s.game_id = p.game_id
             WHERE upper(p.league) = ANY($1::text[])
               AND p.kalshi_market_id IS NOT NULL
               -- Settled rows must NEVER get re-tagged as live: they
               -- pollute the pre-vs-in-game Brier split in the daily
               -- honesty report and confuse the dashboard tile count.
               -- The is_live column is meant for OPEN markets whose
               -- game is currently in progress.
               AND p.settled_at IS NULL
               AND lower(s.status) = ANY($2::text[])
             ORDER BY s.fetched_at DESC
             LIMIT $3
            """,
            list(leagues),
            list(_LIVE_STATUSES),
            int(limit),
        )
        return [dict(r) for r in rows]
    except Exception as e:  # pragma: no cover — defensive
        log.warning("in_game_loop _fetch_live_eligible(%s) failed: %s", sport, e)
        return []


async def _tag_is_live(market_ids: list[str]) -> int:
    """Mark each ``predictions`` row as live.  Monotonic: a row that
    was already live stays live.  Returns the number of rows whose
    value actually flipped this call (the UPDATE's natural row-count
    so we don't double-count the dashboard tile)."""
    if not market_ids:
        return 0
    try:
        result = await db.execute(
            """
            UPDATE predictions
               SET is_live = true
             WHERE kalshi_market_id = ANY($1::text[])
               AND is_live = false
            """,
            market_ids,
        )
        # asyncpg returns "UPDATE <n>" as the status string.
        if isinstance(result, str):
            parts = result.split()
            if len(parts) == 2 and parts[0].upper() == "UPDATE":
                try:
                    return int(parts[1])
                except ValueError:
                    return 0
        return 0
    except Exception as e:  # pragma: no cover — defensive
        log.warning("in_game_loop tag is_live UPDATE failed: %s", e)
        return 0


def _gc_tagged_cache() -> None:
    """Evict the cache if it has grown past the cap.  Trivial LRU-ish
    sweep (drop oldest half) — the cache only matters for cadence-
    floor de-duplication, not correctness."""
    if len(_TAGGED_THIS_MINUTE) <= _TAGGED_THIS_MINUTE_MAX_ENTRIES:
        return
    items = sorted(_TAGGED_THIS_MINUTE.items(), key=lambda kv: kv[1])
    keep_from = len(items) // 2
    _TAGGED_THIS_MINUTE.clear()
    for k, v in items[keep_from:]:
        _TAGGED_THIS_MINUTE[k] = v


# Market-type heuristic for the in-game best-line check.  The
# predictions table doesn't carry the canonical market_type column the
# pre-game candidate dict ships with, so we default to ``moneyline``
# (the only kind ``gate_best_line`` currently grades — spreads/totals
# are scoped out pending per-leg matching).  Operators can override
# per-market via runtime_settings if a particular league needs a
# different default.
_DEFAULT_LIVE_BEST_LINE_MARKET_TYPE = "moneyline"


async def _evaluate_best_line_for_live_row(
    row: dict[str, Any],
) -> str | None:
    """Run the cross-book best-line gate against one live-eligible row.

    Task #835: every in-game re-score should get a fresh cross-book
    check against the 30s ``line_snapshots`` window — that's the
    whole point of the live-mode variant.  Runs against the latest
    persisted Kalshi price for the market, with ``is_live=True`` so
    :func:`app.risk.gates.gate_best_line` uses the tight freshness
    budget + the (market_id, kind) dedupe key.

    Returns the gate's ``details['decision']`` string when the gate
    ran, ``None`` when we skipped (missing data, gate import error).
    All errors are swallowed — the loop's tagging path must never be
    blocked by a feed-side failure.
    """
    market_id = row.get("market_id")
    league = row.get("league")
    home = row.get("home_team")
    away = row.get("away_team")
    price = row.get("kalshi_price")
    if not (market_id and league and home and away and price is not None):
        return None
    try:
        from ..risk.gates import RiskContext, gate_best_line
    except Exception as e:  # pragma: no cover — defensive import guard
        log.debug("in_game_loop best_line import failed: %s", e)
        return None
    try:
        ctx = RiskContext(
            market_id=str(market_id),
            side="YES",
            vertical="sports",
            league=str(league),
            market_type=_DEFAULT_LIVE_BEST_LINE_MARKET_TYPE,
            matchup=row.get("matchup"),
            current_price=float(price),
            model_probability=(
                float(row["model_yes_prob"])
                if row.get("model_yes_prob") is not None
                else None
            ),
            edge=None,
            confidence_band_low=None,
            confidence_band_high=None,
            agreement_rate=None,
            swarm_approved=True,
            swarm_recommended_action=None,
            debate_id=None,
            intended_quantity=0,
            intended_notional_usd=0.0,
            bankroll_usd=None,
            open_positions_count=0,
            open_positions_notional_usd=0.0,
            open_positions_per_vertical_usd={},
            operator_overrides={},
            market_cooldown_until=None,
            home_team=str(home),
            away_team=str(away),
            is_live=True,
            sportradar_game_id=(
                str(row["game_id"]) if row.get("game_id") else None
            ),
        )
        result = await gate_best_line(ctx)
    except Exception as e:  # pragma: no cover — feed-side failures must not break loop
        log.warning(
            "in_game_loop best_line check failed market=%s: %s",
            market_id,
            e,
        )
        return None
    decision = (result.details or {}).get("decision")
    return str(decision) if decision else None


async def _predict_live_for_market(
    row: dict[str, Any],
) -> tuple[float | None, dict[str, Any]]:
    """Dispatch a live prediction for ``row``.

    Returns ``(probability, metadata)``.  ``probability`` is None when
    no per-sport predictor is wired for the row's league (the loop
    treats that as "tag is_live, but no model update this cycle"),
    or when the wired predictor itself returns None (insufficient
    signal — see e.g. ``soccer_live.predict_live_yes``).

    Per-sport routing:
      * Soccer (task #857) — ``app.learning.soccer_live`` shared
        across the seven mapped leagues (EPL / LALIGA / SERIEA /
        BUNDES / LIGUE1 / MLS / UCL).  Ships in shadow mode: the
        loop's per-sport enable flag (``risk.live_loop_enabled_soccer``)
        is False by default so this code path is exercised by the
        operator-driven rollout, not by an automatic flip.
      * NBA (task #878) — ``app.learning.nba_live``.  Same shadow-
        mode rollout: ``risk.live_loop_enabled_nba`` ships False and
        is flipped via the operator console once Section 9 of the
        daily honesty report shows the NBA pre-vs-in-game Brier
        delta is within 0.02 of the pre-game model.
      * Other sports — no wired predictor yet (per-sport in-game
        wiring lands as follow-up tasks once each sport's shadow
        Brier crosses the 0.02-of-pre-game criterion).
    """
    league = (row.get("league") or "").strip().upper()
    if _soccer_live.is_soccer_league(league):
        try:
            return await _soccer_live.predict_live_yes(row)
        except Exception as e:  # pragma: no cover — defensive
            log.warning(
                "soccer_live predict failed for market=%s league=%s: %s",
                row.get("market_id"), league, e,
            )
            return None, {"source": "soccer_live", "error": str(e)[:240]}
    if _nba_live.is_nba_league(league):
        try:
            return await _nba_live.predict_live_yes(row)
        except Exception as e:  # pragma: no cover — defensive
            log.warning(
                "nba_live predict failed for market=%s league=%s: %s",
                row.get("market_id"), league, e,
            )
            return None, {"source": "nba_live", "error": str(e)[:240]}
    return None, {"source": None, "reason": f"no wired predictor for {league}"}


async def _persist_live_model_prob(
    market_id: str, probability: float, metadata: dict[str, Any]
) -> bool:
    """Update ``predictions.model_yes_prob`` with a freshly-computed
    live probability.  Returns True when the row was updated.

    The orchestrator's next cycle will pick this value up and build
    its ``RiskContext`` with ``is_live=True`` (because the same row
    carries the ``is_live`` tag this loop already sets), so the
    ``gate_stale_live_state`` + ``gate_live_kelly_multiplier`` live
    gates apply.

    Best-effort: failures are logged at WARN and treated as a no-op
    so a sick row can never abort the cycle.
    """
    if not market_id:
        return False
    try:
        p = max(0.0, min(1.0, float(probability)))
    except (TypeError, ValueError):
        return False
    try:
        result = await db.execute(
            """
            UPDATE predictions
               SET model_yes_prob = $2,
                   metadata = coalesce(metadata, '{}'::jsonb)
                              || jsonb_build_object('live_prediction', $3::jsonb)
             WHERE kalshi_market_id = $1
            """,
            market_id,
            p,
            json.dumps(metadata or {}, default=str),
        )
        if isinstance(result, str):
            parts = result.split()
            if len(parts) == 2 and parts[0].upper() == "UPDATE":
                try:
                    return int(parts[1]) > 0
                except ValueError:
                    return False
        return False
    except Exception as e:  # pragma: no cover — defensive
        log.warning(
            "in_game_loop _persist_live_model_prob(%s) failed: %s",
            market_id, e,
        )
        return False


# ---------------------------------------------------------------------------
# NBA-specific live prediction path (task #855).
#
# Separate from the generic ``_predict_live_for_market`` soccer-dispatcher
# above (task #857) because NBA goes through the meta-model
# ``predictor.predict(league="NBA")`` chain and routes the result through
# the live-only risk gates (``gate_stale_live_state`` +
# ``gate_live_kelly_multiplier``) before persistence — the soccer path
# stamps directly via the metadata jsonb column.  Both paths coexist in
# ``run_in_game_cycle``; their league checks are disjoint so they never
# double-process a row.
# ---------------------------------------------------------------------------


async def _predict_live_for_market_nba(
    row: dict[str, Any],
) -> dict[str, Any] | None:
    """Score one live NBA in-game candidate through the predictor chain.

    Returns a dict::

        {
            "probability": <calibrated YES prob 0..1>,
            "model_name": <active predictor name>,
            "model_version": <active predictor version>,
            "feature_inputs": <NBA live feature snapshot>,
            "live_anchor": <live score-diff anchor probability>,
        }

    or ``None`` when the league isn't NBA, when the predictor isn't
    loaded, or when the call errors out (errors are swallowed and
    surfaced to the loop's ``errors`` list rather than aborting).

    Routes through the meta-model with ``league="NBA"`` so the per-
    league calibrator the daily honesty report's Brier-split tracks
    is the same one applied here — the comparison the operator gates
    the per-sport flip on is apples-to-apples.
    """
    league = (row.get("league") or "").upper().strip()
    if league.lower() != "nba":
        return None
    try:
        from .. import predictor as _predictor
        from ..orchestrator.features import build_features
    except Exception as e:  # pragma: no cover — defensive
        log.debug("in_game_loop predictor import failed: %s", e)
        return None

    live_state = {
        "home_score": row.get("home_score"),
        "away_score": row.get("away_score"),
        "period": row.get("period"),
        "clock": row.get("clock"),
        "status": row.get("status"),
    }
    cand = nba_live_features.build_nba_live_candidate(row, live_state=live_state)
    try:
        fv = build_features(
            cand,
            consensus=cand.get("consensus_probability"),
            current_price=cand.get("kalshi_price"),
            edge=None,
        )
    except Exception as e:
        log.debug("in_game_loop build_features failed for %s: %s",
                  row.get("market_id"), e)
        return None

    try:
        pred = await _predictor.predict(list(fv.values), league="NBA")
    except _predictor.PredictorUnavailable:
        # Cold cache — the loop just won't write a probability this
        # cycle, identical to the pre-wired stub behaviour.
        return None
    except Exception as e:
        log.warning(
            "in_game_loop predictor.predict failed for %s: %s",
            row.get("market_id"), e,
        )
        return None

    return {
        "probability": float(pred.probability),
        "model_name": pred.model_name,
        "model_version": pred.model_version,
        "feature_inputs": cand.get("live_state"),
        "live_anchor": cand.get("live_state", {}).get("live_anchor"),
    }
async def _predict_live_via_registry(
    row: dict[str, Any], *, sport: str | None = None
) -> float | None:
    """Per-sport live-prediction dispatch (task #856).

    Sports registered in :data:`_LIVE_FEATURE_BUILDERS` (currently
    NHL) build a live feature vector from ``row`` and call the same
    canonical ``predict`` chain the orchestrator uses — so the returned
    YES probability is already calibrated through the per-league
    calibrator stack (champion + challenger via ``apply_both``).

    Sports without a registered builder fall through to ``None`` —
    matching the historical stub so per-sport rollout stays opt-in.

    Returns ``None`` instead of raising on:
      * Unregistered sport.
      * Predictor not loaded (cold container, model artifacts missing).
      * Any exception inside the predict chain — best-effort by design
        so a sick model can't stall the loop or wedge the trading
        path; the orchestrator falls back to the pre-game probability
        and the live risk gates still apply via the ``is_live`` tag.
    """
    sp = _normalize_sport(sport or row.get("sport") or "")
    if not sp:
        # Best-effort: infer from the row's league.
        league_in = (row.get("league") or "").strip().upper()
        for cand_sport, leagues in SPORT_LEAGUES.items():
            if league_in in {l.upper() for l in leagues}:
                sp = cand_sport
                break
    builder = _LIVE_FEATURE_BUILDERS.get(sp)
    if builder is None:
        return None
    try:
        _names, values = builder(row)
    except Exception as e:  # pragma: no cover — defensive
        log.warning(
            "in_game_loop live feature build (%s) failed for %s: %s",
            sp, row.get("market_id"), e,
        )
        return None
    try:
        from .. import predictor as _predictor
    except Exception:  # pragma: no cover — defensive
        return None
    league_for_calibrator = (row.get("league") or sp.upper()).upper()
    try:
        result = await _predictor.predict(
            values, league=league_for_calibrator
        )
    except Exception as e:
        # PredictorUnavailable / PredictorInputError land here.  We
        # swallow so the loop keeps tagging is_live for the dashboard
        # tile + risk gates even while the per-sport model is down.
        log.debug(
            "in_game_loop predict (%s) failed for %s: %s",
            sp, row.get("market_id"), e,
        )
        return None
    prob = float(result.probability)
    if prob != prob or prob in (float("inf"), float("-inf")):
        return None
    if prob < 0.0 or prob > 1.0:
        return None
    return prob


async def _persist_live_model_yes_prob(
    market_id: str, prob: float
) -> bool:
    """Stamp the in-game calibrated YES probability onto the matching
    ``predictions`` row (NBA path, task #855) — also flips
    ``is_live=true`` in the same UPDATE so the daily honesty report's
    pre-vs-in-game Brier split (Section 9) scores against the live
    prediction.

    Overwrites any prior value (the in-game loop is meant to refresh
    the probability as the game progresses — unlike the orchestrator's
    pre-game ``_persist_model_yes_prob`` which only stamps NULLs).
    Returns True when the UPDATE flipped a row, False otherwise.
    """
    try:
        result = await db.execute(
            """
            UPDATE predictions
               SET model_yes_prob = $2,
                   is_live = true
             WHERE kalshi_market_id = $1
               AND settled_at IS NULL
            """,
            market_id, float(prob),
        )
        if isinstance(result, str):
            parts = result.split()
            if len(parts) == 2 and parts[0].upper() == "UPDATE":
                try:
                    return int(parts[1]) > 0
                except ValueError:
                    return False
        return False
    except Exception as e:  # pragma: no cover — defensive
        log.warning(
            "in_game_loop persist model_yes_prob failed for %s: %s",
            market_id, e,
        )
        return False


async def _persist_live_model_yes_prob_only(
    market_id: str, prob: float
) -> bool:
    """Registry-path persist (task #856 — NHL and follow-ups).

    Distinct from :func:`_persist_live_model_yes_prob` because the
    registry path runs alongside ``_tag_is_live`` (which already
    flipped ``is_live=true`` for fresh rows), so the persist UPDATE
    must NOT also set ``is_live`` — otherwise the dashboard tile
    double-counts and the NHL test's separate "tag" vs "persist"
    UPDATE assertions fail.

    Best-effort: a failing UPDATE must NOT take down the loop.
    Returns True when the row was actually flipped.
    """
    if not market_id:
        return False
    try:
        p = float(prob)
    except (TypeError, ValueError):
        return False
    if p != p or p in (float("inf"), float("-inf")):
        return False
    if not (0.0 <= p <= 1.0):
        return False
    try:
        result = await db.execute(
            """
            UPDATE predictions
               SET model_yes_prob = $2
             WHERE kalshi_market_id = $1
               AND settled_at IS NULL
            """,
            market_id, p,
        )
    except Exception as e:  # pragma: no cover — defensive
        log.debug(
            "in_game_loop persist live model_yes_prob failed for %s: %s",
            market_id, e,
        )
        return False
    if isinstance(result, str):
        parts = result.split()
        if len(parts) == 2 and parts[0].upper() == "UPDATE":
            try:
                return int(parts[1]) > 0
            except ValueError:
                return False
    return False


def _build_live_risk_context(
    row: dict[str, Any], live_prob: float
) -> "Any":
    """Construct the minimal :class:`RiskContext` the live-only gates
    (``gate_stale_live_state``, ``gate_live_kelly_multiplier``)
    actually read from.

    Keeps the import lazy so unit tests that monkeypatch ``db`` away
    don't have to also patch the risk module's transitive imports.
    """
    from ..risk.gates import RiskContext  # local import to avoid cycle

    league = (row.get("league") or "").upper() or None
    kalshi_price = row.get("kalshi_price")
    try:
        kp = float(kalshi_price) if kalshi_price is not None else None
    except (TypeError, ValueError):
        kp = None
    edge: float | None = None
    if kp is not None:
        try:
            edge = float(live_prob) - kp
        except (TypeError, ValueError):
            edge = None
    return RiskContext(
        market_id=str(row.get("market_id") or ""),
        side="YES",
        vertical="sports",
        league=league,
        market_type="moneyline",
        matchup=None,
        current_price=kp,
        model_probability=float(live_prob),
        edge=edge,
        confidence_band_low=None,
        confidence_band_high=None,
        agreement_rate=None,
        swarm_approved=True,
        swarm_recommended_action="BUY_YES",
        debate_id=None,
        intended_quantity=0,
        intended_notional_usd=0.0,
        bankroll_usd=None,
        open_positions_count=0,
        open_positions_notional_usd=0.0,
        open_positions_per_vertical_usd={},
        operator_overrides={},
        market_cooldown_until=None,
        game_time=row.get("game_time"),
        # The in-game routing path is the whole point of this gate
        # chain — every candidate we send through here is, by
        # definition, a live re-evaluation.
        is_live=True,
        sportradar_game_id=str(row.get("game_id") or "") or None,
    )


async def _route_live_prediction_through_gates(
    row: dict[str, Any], live_prob: float
) -> tuple[bool, str | None]:
    """Run the in-game candidate through the two live-only gates
    (``gate_stale_live_state`` then ``gate_live_kelly_multiplier``).

    Returns ``(passed, reject_reason)``.  ``gate_live_kelly_multiplier``
    halves the intended size in place and always passes, so the only
    rejection path here is ``stale_live_state``.

    Failure to construct/import the gates path is treated as a soft
    pass: the loop's existing `errors` list captures the issue while
    the prediction still gets persisted so the shadow-mode rollout
    keeps producing data for the Brier-split.
    """
    try:
        from ..risk import gates as _gates
    except Exception as e:  # pragma: no cover — defensive
        log.debug("in_game_loop gates import failed: %s", e)
        return True, None
    try:
        ctx = _build_live_risk_context(row, live_prob)
    except Exception as e:  # pragma: no cover — defensive
        log.debug("in_game_loop ctx build failed: %s", e)
        return True, None
    try:
        stale = await _gates.gate_stale_live_state(ctx)
    except Exception as e:
        log.warning("in_game_loop stale_live_state raised: %s", e)
        return True, None
    if not stale.passed:
        return False, stale.reason or "stale_live_state"
    try:
        await _gates.gate_live_kelly_multiplier(ctx)
    except Exception as e:  # pragma: no cover — defensive
        log.debug("in_game_loop live_kelly_multiplier raised: %s", e)
    return True, None


async def run_in_game_cycle(
    sport: str, *, now: datetime | None = None
) -> InGameCycleResult:
    """One scheduled tick of the in-game loop for ``sport``.

    Steps:

      1. Honor the per-sport enabled flag (default False).
      2. Enforce the per-sport cadence floor via the in-memory
         ``_LAST_RUN_AT`` map.
      3. Fetch open predictions whose game is currently live.
      4. Tag ``predictions.is_live = true`` for any row not yet tagged.
      5. Return a structured result the scheduler / dashboard can use.

    Restart-safe: cadence enforcement is in-memory so a restart just
    resets the clock; the work is idempotent (same-minute re-run tags
    no new rows because the UPDATE filters on ``is_live = false``).
    """
    started_perf = time.perf_counter()
    started_at = (now or datetime.now(timezone.utc)).isoformat()
    sp = _normalize_sport(sport)
    cadence = await cadence_seconds(sp)
    result = InGameCycleResult(
        sport=sp,
        started_at=started_at,
        cadence_seconds=cadence,
    )

    enabled = await is_enabled(sp)
    result.enabled = enabled
    if not enabled:
        result.skipped_reason = (
            f"risk.live_loop_enabled_{sp} is false (ships disabled until "
            "per-sport shadow Brier within 0.02 of pre-game model)"
        )
        result.completed_at = datetime.now(timezone.utc).isoformat()
        result.duration_seconds = time.perf_counter() - started_perf
        return result

    last_at = _LAST_RUN_AT.get(sp)
    monotonic = time.monotonic()
    if last_at is not None and (monotonic - last_at) < cadence:
        wait_remaining = cadence - (monotonic - last_at)
        result.skipped_reason = (
            f"cadence floor: {wait_remaining:.1f}s remaining of "
            f"{cadence:.1f}s window"
        )
        result.completed_at = datetime.now(timezone.utc).isoformat()
        result.duration_seconds = time.perf_counter() - started_perf
        return result
    _LAST_RUN_AT[sp] = monotonic

    rows = await _fetch_live_eligible(sp)
    result.candidates_scanned = len(rows)
    if not rows:
        result.completed_at = datetime.now(timezone.utc).isoformat()
        result.duration_seconds = time.perf_counter() - started_perf
        return result

    bucket = _minute_bucket(now)
    to_tag: list[str] = []
    rows_to_check: list[dict[str, Any]] = []
    fresh_rows: list[dict[str, Any]] = []
    to_predict: list[dict[str, Any]] = []
    has_builder = sp in _LIVE_FEATURE_BUILDERS
    for r in rows:
        mid = r.get("market_id")
        if not mid:
            continue
        result.candidates_live += 1
        # Cross-book best-line evaluation MUST run on every re-score
        # (task #835) — it is intentionally decoupled from the
        # minute-bucket tagging dedupe below.  Tagging is suppressed
        # for same-minute repeats because the dashboard tile would
        # double-count an already-flipped row; the best-line gate has
        # its own (kind, market_id) alert dedupe and its own fresh
        # 30s line_snapshots window, so re-running it every cycle is
        # both safe and the whole point of the live loop.
        rows_to_check.append(r)
        key = (str(mid), bucket)
        if key in _TAGGED_THIS_MINUTE:
            result.candidates_skipped_duplicate += 1
            continue
        _TAGGED_THIS_MINUTE[key] = monotonic
        fresh_rows.append(r)
        if not r.get("is_live"):
            to_tag.append(str(mid))
        if has_builder:
            to_predict.append(r)

    flipped = await _tag_is_live(to_tag)
    result.candidates_tagged = flipped
    _gc_tagged_cache()

    # Task #835 — fire the cross-book best-line gate against every
    # market we just re-scored.  Runs on EVERY eligible row this tick
    # (no minute-bucket suppression — see comment above) so a live
    # market that re-evaluates every 30s actually gets a fresh cross-
    # book check at the gate's tight 30s freshness window.  Operator-
    # visible alert flooding is prevented at a different layer:
    # _emit_best_line_alert dedupes on (kind, market_id) for 5 min.
    # Wrapped in a top-level try so a gate-side regression cannot
    # prevent the loop's tagging path from returning a clean result.
    for r in rows_to_check:
        try:
            decision = await _evaluate_best_line_for_live_row(r)
        except Exception as e:  # pragma: no cover — defensive
            log.warning(
                "in_game_loop best_line evaluation crashed market=%s: %s",
                r.get("market_id"),
                e,
            )
            continue
        if decision is None:
            continue
        result.best_line_checks += 1
        if decision == "block_traditional_better":
            result.best_line_blocks += 1
        elif decision == "reroute_polymarket":
            result.best_line_reroutes += 1

    # Per-sport live prediction wiring — generic dispatcher path
    # (task #857: soccer is the first sport with a wired predictor).
    # Other sports' dispatchers return (None, _) so the loop only
    # tags rows + drives dashboard counts.  Failures on individual
    # rows are captured per-row and never abort the cycle.
    for r in fresh_rows:
        mid = r.get("market_id")
        try:
            prob_meta = await _predict_live_for_market(r)
        except Exception as e:  # pragma: no cover — defensive
            result.errors.append(
                f"predict_live {mid}: {type(e).__name__}: {e}"[:240]
            )
            continue
        prob, meta = prob_meta
        if prob is None:
            continue
        result.live_predictions_computed += 1
        if await _persist_live_model_prob(str(mid), prob, meta):
            result.live_predictions_persisted += 1

    # NBA-specific predictor path (task #855: NBA goes first on the
    # meta-model route).  Disjoint from the soccer dispatcher above
    # by league check.  Scores each live row through
    # ``predictor.predict(league="NBA")``, routes through the live-
    # only gate chain (gate_stale_live_state +
    # gate_live_kelly_multiplier), and stamps the calibrated YES
    # probability via the model_yes_prob + is_live UPDATE so the
    # daily honesty report's pre-vs-in-game Brier split (Section 9)
    # scores the live prediction.
    if sp in _PREDICT_LIVE_SUPPORTED_SPORTS:
        for r in rows:
            mid = r.get("market_id")
            if not mid:
                continue
            try:
                pred = await _predict_live_for_market_nba(r)
            except Exception as e:
                result.errors.append(
                    f"predict {mid}: {type(e).__name__}: {e}"
                )
                continue
            if pred is None:
                continue
            prob = pred.get("probability")
            if prob is None:
                continue
            result.predictions_made += 1
            passed, reason = await _route_live_prediction_through_gates(
                r, float(prob)
            )
            if not passed:
                result.gate_rejections += 1
                log.info(
                    "in_game_loop %s rejected by live gate: %s",
                    mid, reason,
                )
                continue
            persisted = await _persist_live_model_yes_prob(
                str(mid), float(prob)
            )
            if persisted:
                result.predictions_persisted += 1

    # Per-sport live re-evaluation via the feature-builder registry
    # (task #856: NHL is the first sport on this path).  For sports
    # with a registered live feature builder, run the predictor +
    # calibrator chain on the freshest game state and stamp the new
    # probability back onto ``predictions`` so the orchestrator's
    # next sweep picks it up.  The orchestrator constructs a
    # ``RiskContext`` with ``is_live=True`` (driven by the
    # ``predictions.is_live`` tag we just flipped) so the live gates
    # ``stale_live_state`` and ``live_kelly_multiplier`` apply
    # automatically — no extra wiring needed here.  Best-effort per
    # row: a sick prediction must not take down the loop.
    for r in to_predict:
        mid = r.get("market_id")
        if not mid:
            continue
        try:
            prob = await _predict_live_via_registry(r, sport=sp)
        except Exception as e:  # pragma: no cover — defensive
            result.errors.append(
                f"predict {mid}: {type(e).__name__}: {e}"[:240]
            )
            continue
        if prob is None:
            continue
        result.candidates_predicted += 1
        try:
            wrote = await _persist_live_model_yes_prob_only(
                str(mid), prob
            )
        except Exception as e:  # pragma: no cover — defensive
            result.errors.append(
                f"persist {mid}: {type(e).__name__}: {e}"[:240]
            )
            continue
        if wrote:
            result.candidates_prob_updated += 1

    result.completed_at = datetime.now(timezone.utc).isoformat()
    result.duration_seconds = time.perf_counter() - started_perf
    return result


def _reset_state_for_tests() -> None:
    """Test hook: clear in-memory cadence + idempotency state."""
    _LAST_RUN_AT.clear()
    _TAGGED_THIS_MINUTE.clear()
