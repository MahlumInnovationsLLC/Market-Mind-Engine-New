"""Single-cycle execution: discover candidates → swarm → 12 gates → place."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .. import db, margin_regressor, predictor, verticals as vertical_registry
from ..models import live_calibrator
from ..commodities import predictor as commodities_predictor
from ..tech_ai import predictor as tech_ai_predictor
from ..tech_ai.tickers import InvalidTechAITicker, parse_tech_ai_ticker
from ..commodities.tickers import (
    InvalidCommodityTicker,
    parse_commodity_ticker,
)
from ..crypto import predictor as crypto_predictor
from ..crypto.tickers import InvalidCryptoTicker, parse_crypto_ticker
from ..weather import predictor as weather_predictor
from ..weather import precip_predictor as weather_precip_predictor
from ..weather.tickers import (
    InvalidTemperatureTicker,
    parse_temperature_ticker,
)
from ..weather.precip_tickers import (
    InvalidPrecipitationTicker,
    parse_precipitation_ticker,
)
from ..config import get_settings
from ..elo import attach_elo_to_candidate
from ..kalshi import lifecycle as kalshi_lifecycle
from ..predictor_n_way import plackett_luce_from_mapping
from ..risk import RiskContext, run_all_gates
from ..sports import coerce_to_ratings, get_field, get_league_config
from ..runtime_settings import get_aggression_mode
from ..swarm import run_debate
from ..swarm.streams import llm_log_hub
from .features import build_features
from .paper import place_order
from .venue_router import route_for_candidate
from .streams import pipeline_event_hub


log = logging.getLogger(__name__)


# Default sizing knobs — operator overrides table can override per cycle.
DEFAULT_INTENDED_QUANTITY = 10  # contracts
DEFAULT_MIN_PRICE = 0.05
DEFAULT_MAX_PRICE = 0.95

# Default TTL (seconds) for a resting order before the cycle-start sweep
# will cancel it. Overridable via AXIOM_RESTING_ORDER_MAX_AGE_SECS, via
# `risk.resting_order_max_age_secs` in system_settings, or per-vertical
# via `risk.resting_order_max_age_secs.<vertical>`.
DEFAULT_RESTING_ORDER_MAX_AGE_SECS = 30


# Defensive datetime comparison: `markets.game_time` is stored as a
# naive UTC ``timestamp without time zone`` (asyncpg returns it as a
# tz-naive ``datetime``), but ``datetime.now(timezone.utc)`` is
# tz-aware.  Comparing them directly raises
# ``TypeError: can't compare offset-naive and offset-aware datetimes``
# and aborts the entire cycle (no ``cycle_complete`` event), which
# strands sports/crypto cycles in flight.  This helper normalizes a
# naive value to UTC before the compare so the in-progress check is
# bullet-proof regardless of whether a feed populated the column with
# an aware or naive datetime.
def _game_time_in_past(gt: Any) -> bool:
    if gt is None:
        return False
    if not isinstance(gt, datetime):
        return False
    if gt.tzinfo is None:
        gt = gt.replace(tzinfo=timezone.utc)
    return gt <= datetime.now(timezone.utc)


# Vertical → league filter is now driven by the pluggable
# `app.verticals` registry.  `_league_filter` stays as a thin wrapper
# so existing call sites (and tests that monkeypatch it) keep working.
def _league_filter(vertical: str) -> list[str]:
    return vertical_registry.league_filter(vertical)


@dataclass
class CycleResult:
    cycle_id: int | None
    vertical: str
    started_at: str
    completed_at: str | None = None
    duration_seconds: float = 0.0
    picks_evaluated: int = 0
    trades_placed: int = 0
    fresh_debates: int = 0
    errors: list[str] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)


async def _load_operator_overrides() -> dict[str, Any]:
    """Read all `system_settings` rows whose key starts with 'risk.' and
    expose them to the gates as a flat dict.  Best-effort: a missing
    table or empty result is treated as no overrides."""
    try:
        rows = await db.fetch(
            "SELECT key, value FROM system_settings WHERE key LIKE 'risk.%'"
        )
    except Exception as e:
        log.debug("operator overrides read failed (best-effort): %s", e)
        return {}
    out: dict[str, Any] = {}
    for r in rows:
        k = (r["key"] or "")[len("risk."):]
        v = r["value"]
        if v is None:
            continue
        # try numeric coercion so gates can do float math without conversion
        try:
            out[k] = float(v) if "." in str(v) else int(v)
            continue
        except Exception:
            pass
        out[k] = v
    return out


def _resolve_resting_max_age(vertical: str, overrides: dict[str, Any]) -> float:
    """Resolve the max-age TTL (seconds) for resting orders in `vertical`.

    Precedence (first hit wins):
      1. `risk.resting_order_max_age_secs.<vertical>` system_settings row
      2. `risk.resting_order_max_age_secs` system_settings row (global)
      3. `AXIOM_RESTING_ORDER_MAX_AGE_SECS` env var
      4. `DEFAULT_RESTING_ORDER_MAX_AGE_SECS` module constant

    Non-positive values disable the sweep (treated as +inf).
    """
    import os as _os
    per_vert_key = f"resting_order_max_age_secs.{vertical}"
    candidates: list[Any] = [
        overrides.get(per_vert_key),
        overrides.get("resting_order_max_age_secs"),
        _os.environ.get("AXIOM_RESTING_ORDER_MAX_AGE_SECS"),
        DEFAULT_RESTING_ORDER_MAX_AGE_SECS,
    ]
    for v in candidates:
        if v is None or v == "":
            continue
        try:
            age = float(v)
        except (TypeError, ValueError):
            continue
        return age
    return float(DEFAULT_RESTING_ORDER_MAX_AGE_SECS)


async def _cancel_stale_resting_orders(
    vertical: str, overrides: dict[str, Any]
) -> dict[str, int]:
    """Cycle-start sweep: cancel resting orders older than the per-vertical
    TTL before new candidates are evaluated.

    Cancel failures are swallowed (logged + counted) so a flaky exchange
    can't park the orchestrator. Returns a small stats dict the caller
    can fold into the cycle's telemetry.

    When `max_age` <= 0 the sweep is disabled (no-op) — used by operators
    who want to opt a vertical out of auto-cancellation entirely.
    """
    max_age = _resolve_resting_max_age(vertical, overrides)
    kalshi_lifecycle.record_stale_resting_scan()
    if max_age <= 0:
        return {"scanned": 0, "cancelled": 0, "failed": 0, "max_age": int(max_age)}
    try:
        resting = await kalshi_lifecycle.list_resting_orders()
    except Exception as e:  # pragma: no cover — defensive
        log.warning("list_resting_orders failed (%s); skipping sweep", e)
        return {"scanned": 0, "cancelled": 0, "failed": 0, "max_age": int(max_age)}
    now = datetime.now(timezone.utc)
    cancelled = 0
    failed = 0
    scanned = 0
    for rec in resting:
        # Only touch orders relevant to this cycle's vertical so each
        # vertical's sweep is isolated; the per-vertical override can
        # intentionally extend the TTL without bleeding into others.
        if getattr(rec.request, "vertical", None) and rec.request.vertical != vertical:
            continue
        scanned += 1
        age_secs = (now - rec.created_at).total_seconds()
        if age_secs <= max_age:
            continue
        try:
            await kalshi_lifecycle.cancel_order(rec.order_id)
        except Exception as e:  # noqa: BLE001 — cycle must proceed on failure
            failed += 1
            err = f"{type(e).__name__}: {e}"[:200]
            kalshi_lifecycle.record_stale_resting_cancel_failed(rec.order_id, err)
            log.warning(
                "stale resting cancel failed for %s (age=%.1fs, vertical=%s): %s",
                rec.order_id, age_secs, vertical, err,
            )
            continue
        cancelled += 1
        kalshi_lifecycle.record_stale_resting_cancelled(rec.order_id)
    return {
        "scanned": scanned,
        "cancelled": cancelled,
        "failed": failed,
        "max_age": int(max_age),
    }


async def _latest_bankroll(trading_mode: str = "live") -> float | None:
    """Most recent bankroll, in dollars, scoped to ``trading_mode``.

    Prefers `bankroll_events` (fed by the continuous Kalshi balance sync —
    see `app.bankroll_sync`) so the floor gate always reflects the live
    exchange balance. Falls back to legacy `bankroll_snapshots` when the
    events table is empty (e.g. during the very first boot before the
    sync loop has ticked).

    The ``trading_mode`` filter (Task #752 step 1) prevents paper-mode
    bankroll deltas from leaking into the live cycle's risk gates and
    vice versa.  Default ``"live"`` is the safe fallback for callers
    that haven't been threaded yet — the legacy single-ledger world
    was effectively all-live.
    """
    mode = trading_mode if trading_mode in ("paper", "live") else "live"
    try:
        v = await db.fetchval(
            "SELECT balance_cents FROM bankroll_events "
            "WHERE trading_mode = $1 "
            "ORDER BY id DESC LIMIT 1",
            mode,
        )
        if v is not None:
            return float(int(v)) / 100.0
    except Exception as e:
        log.debug("bankroll_events read failed: %s", e)
    # `bankroll_snapshots` predates the trading_mode split and is only
    # used as a last-resort fallback on an empty events table.  It is
    # historically a live-only ledger; for paper-mode callers we skip
    # the fallback entirely so paper bankroll never returns the live
    # snapshot total.
    if mode != "live":
        return None
    try:
        v = await db.fetchval(
            "SELECT balance::float FROM bankroll_snapshots ORDER BY date DESC LIMIT 1"
        )
        return float(v) if v is not None else None
    except Exception as e:
        log.debug("bankroll snapshot read failed: %s", e)
        return None


async def _open_position_aggregates(
    trading_mode: str = "live",
) -> tuple[
    int,
    float,
    dict[str, float],
    dict[str, float],
    dict[str, float],
    dict[str, float],
    dict[str, float],
]:
    """Returns (count, total_notional, per_vertical, per_league, per_matchup,
    per_sunday_window, per_event).

    `per_event` is keyed `LEAGUE:event_id` (event_id derived from the
    debate's `game_id`, which combat & individual sports re-use as the
    fight-card / GP / tournament identifier — see task #197).

    Per-league and per-matchup buckets are derived from `positions` joined
    onto the originating debate's `league` / `matchup` (via
    `swarm_debates`).  The Sunday-window bucket is keyed by league and
    only populated when the position's game falls in the league's Sunday
    concentration window — see `app.sports.leagues.is_sunday_window`.

    Best-effort: if any join fails (e.g. the schema hasn't been migrated
    on a fresh dev DB), the per-league / per-matchup / Sunday buckets
    come back empty and the gates short-circuit their sub-checks, leaving
    behavior identical to the legacy aggregate.
    """
    mode = trading_mode if trading_mode in ("paper", "live") else "live"
    try:
        rows = await db.fetch(
            """
            SELECT p.vertical,
                   count(*)::int                                AS n,
                   coalesce(sum(p.quantity * p.avg_price), 0)::float AS notional
              FROM positions p
             WHERE coalesce(p.status, 'open') = 'open'
               AND p.trading_mode = $1
             GROUP BY p.vertical
            """,
            mode,
        )
    except Exception as e:
        log.debug("open position aggregate failed: %s", e)
        return 0, 0.0, {}, {}, {}, {}, {}
    total_n = 0
    total_notional = 0.0
    per_vertical: dict[str, float] = {}
    for r in rows:
        v = (r["vertical"] or "sports")
        total_n += int(r["n"])
        total_notional += float(r["notional"])
        per_vertical[v] = per_vertical.get(v, 0.0) + float(r["notional"])

    per_league: dict[str, float] = {}
    per_matchup: dict[str, float] = {}
    per_sunday: dict[str, float] = {}
    per_event: dict[str, float] = {}
    try:
        from ..sports import is_sunday_window as _is_sunday_window
        league_rows = await db.fetch(
            """
            SELECT coalesce(d.league, '')                            AS league,
                   coalesce(d.matchup, '')                           AS matchup,
                   coalesce(d.game_id, '')                           AS event_id,
                   coalesce(sum(p.quantity * p.avg_price), 0)::float AS notional,
                   max(g.scheduled_start)                            AS game_time
              FROM positions p
              LEFT JOIN swarm_debates d ON d.id = p.debate_id
              LEFT JOIN games g ON g.game_id = d.game_id
             WHERE coalesce(p.status, 'open') = 'open'
               AND p.trading_mode = $1
               AND coalesce(d.league, '') <> ''
             GROUP BY d.league, d.matchup, d.game_id
            """,
            mode,
        )
        for r in league_rows:
            league = str(r["league"] or "").upper()
            if not league:
                continue
            notional = float(r["notional"] or 0.0)
            per_league[league] = per_league.get(league, 0.0) + notional
            matchup = str(r["matchup"] or "").strip()
            if matchup:
                key = f"{league}:{matchup}"
                per_matchup[key] = per_matchup.get(key, 0.0) + notional
            # Per-event bucket (task #197): combat & individual sports
            # group many markets/matchups under a single event_id (UFC
            # fight card, F1 GP weekend, tennis tournament, PGA event).
            # We re-use `swarm_debates.game_id` as the event identifier
            # — the feed populates it with the card / GP / tournament
            # key for these leagues.
            event_id = str(r["event_id"] or "").strip()
            if event_id:
                ekey = f"{league}:{event_id}"
                per_event[ekey] = per_event.get(ekey, 0.0) + notional
            game_time = r["game_time"]
            if game_time is not None and _is_sunday_window(league, game_time):
                per_sunday[league] = per_sunday.get(league, 0.0) + notional
    except Exception as e:
        log.debug("per-league position aggregate failed: %s", e)

    return (
        total_n, total_notional, per_vertical,
        per_league, per_matchup, per_sunday, per_event,
    )


async def _today_trade_counts_per_league(
    trading_mode: str = "live",
) -> dict[str, int]:
    """Returns the per-league count of trades executed during the current
    UTC day, joined to `swarm_debates.league` so soccer's per-matchday
    cap (task #196) can throttle churn.

    Scoped to ``trading_mode`` (Task #752 step 1) so the daily-trade
    gate of a live cycle isn't tripped by paper-mode placement churn
    and vice versa.

    Best-effort: empty dict on any DB failure so the daily-trade gate
    short-circuits to a no-op rather than killing the cycle.
    """
    mode = trading_mode if trading_mode in ("paper", "live") else "live"
    try:
        rows = await db.fetch(
            """
            SELECT upper(coalesce(d.league, ''))    AS league,
                   count(*)::int                    AS n
              FROM trades t
              LEFT JOIN swarm_debates d ON d.id = t.debate_id
             WHERE ((t.executed_at at time zone 'UTC')::date)
                   = (now() at time zone 'UTC')::date
               AND t.trading_mode = $1
               AND coalesce(d.league, '') <> ''
             GROUP BY upper(coalesce(d.league, ''))
            """,
            mode,
        )
    except Exception as e:
        log.debug("today trade counts per league failed: %s", e)
        return {}
    out: dict[str, int] = {}
    for r in rows:
        league = str(r["league"] or "").upper()
        if league:
            out[league] = int(r["n"])
    return out


async def _persist_model_yes_prob(
    market_id: str | None, prob: float | None
) -> None:
    """Stamp the model's YES probability onto the matching ``predictions``
    row so the crypto accuracy report can score the row directly without
    a lateral join into ``swarm_debates`` (task #646).

    Idempotent: writes only when the new value is real (a finite float
    in ``[0, 1]``) and the row doesn't already carry one — re-running
    the cycle for the same market never overwrites the original
    probability we traded against.  Best-effort: a sick UPDATE must NOT
    take down the trading loop, so failures are logged at debug.

    No-ops when ``market_id`` is missing (verticals whose discovery
    feeds don't materialise a ``predictions`` row keyed on the Kalshi
    ticker — e.g. some sports producers — simply have nothing to
    stamp).
    """
    if not market_id:
        return
    if prob is None:
        return
    try:
        p = float(prob)
    except (TypeError, ValueError):
        return
    if not (0.0 <= p <= 1.0):
        return
    try:
        await db.execute(
            """
            UPDATE predictions
               SET model_yes_prob = $2
             WHERE kalshi_market_id = $1
               AND model_yes_prob IS NULL
            """,
            market_id, p,
        )
    except Exception as e:  # pragma: no cover — defensive
        log.debug(
            "model_yes_prob persist failed for %s: %s", market_id, e,
        )


async def _market_cooldown(market_id: str) -> datetime | None:
    try:
        v = await db.fetchval(
            """
            SELECT cooldown_until
              FROM watchdog_market_cooldowns
             WHERE market_id = $1
             ORDER BY cooldown_until DESC
             LIMIT 1
            """,
            market_id,
        )
        return v
    except Exception as e:
        log.debug("market cooldown lookup failed: %s", e)
        return None


async def _candidate_predictions(vertical: str, limit: int) -> list[dict[str, Any]]:
    """Pull recent predictions that don't yet have a fresh swarm debate.

    Uses kalshi_market_id when present so each row maps to a real market.
    Skips rows whose game has already started.  Avoids candidates we've
    already debated in the last 15 minutes — those go through the
    `_cached_debates` path instead, which doesn't pay LLM cost.
    """
    leagues = _league_filter(vertical)
    try:
        if leagues:
            rows = await db.fetch(
                """
                SELECT p.id, p.game_id, p.league, p.kalshi_market_id, p.kalshi_price,
                       p.home_win_prob, p.confidence, p.edge, p.game_time,
                       g.home_team, g.away_team
                  FROM predictions p
                  LEFT JOIN games g ON g.game_id = p.game_id
                 WHERE p.kalshi_market_id IS NOT NULL
                   AND p.league = ANY($2)
                   AND (p.game_time IS NULL OR p.game_time > now())
                   AND NOT EXISTS (
                       SELECT 1 FROM swarm_debates d
                        WHERE d.game_id = p.game_id
                          AND d.created_at > now() - interval '15 minutes'
                   )
                   -- task #832: skip candidates the operator has
                   -- snoozed via the Top-Edges tile.  Expired snoozes
                   -- (snoozed_until <= now()) are NOT suppressed.
                   AND NOT EXISTS (
                       SELECT 1 FROM prediction_snoozes s
                        WHERE s.prediction_id = p.id
                          AND s.snoozed_until > now()
                   )
                 ORDER BY abs(coalesce(p.edge, 0)) DESC NULLS LAST
                 LIMIT $1
                """,
                limit,
                leagues,
            )
        else:
            rows = await db.fetch(
                """
                SELECT p.id, p.game_id, p.league, p.kalshi_market_id, p.kalshi_price,
                       p.home_win_prob, p.confidence, p.edge, p.game_time,
                       g.home_team, g.away_team
                  FROM predictions p
                  LEFT JOIN games g ON g.game_id = p.game_id
                 WHERE p.kalshi_market_id IS NOT NULL
                   AND (p.game_time IS NULL OR p.game_time > now())
                   AND NOT EXISTS (
                       SELECT 1 FROM swarm_debates d
                        WHERE d.game_id = p.game_id
                          AND d.created_at > now() - interval '15 minutes'
                   )
                   -- task #832: skip snoozed candidates (see above).
                   AND NOT EXISTS (
                       SELECT 1 FROM prediction_snoozes s
                        WHERE s.prediction_id = p.id
                          AND s.snoozed_until > now()
                   )
                 ORDER BY abs(coalesce(p.edge, 0)) DESC NULLS LAST
                 LIMIT $1
                """,
                limit,
            )
        return [dict(r) for r in rows]
    except Exception as e:
        log.debug("candidate prediction read failed: %s", e)
        return []


async def _cached_debates(
    vertical: str, limit: int, aggression_mode: str | None = None
) -> list[dict[str, Any]]:
    """Recent approved debates that haven't been traded today.

    Filters `approved = true` here (not just at gate_swarm_approval) so
    we don't churn through obviously-rejected debates inside the gate
    pipeline.  gate_swarm_approval still re-checks for safety.

    When `aggression_mode` is supplied, also discards cached debates whose
    `game_context.aggression_mode` doesn't match — otherwise an operator
    flipping Conservative -> Aggressive could keep trading on stale
    moderator/gatekeeper verdicts that were biased for the prior mode.
    """
    leagues = _league_filter(vertical)
    mode_clause = (
        "AND COALESCE(sd.game_context->>'aggression_mode', 'balanced') = $%d"
    )
    try:
        if leagues:
            params: list[Any] = [limit, leagues]
            sql = """
                SELECT sd.id            AS debate_id,
                       sd.game_id, sd.matchup, sd.league, sd.market_type,
                       sd.consensus_probability, sd.confidence_band_low, sd.confidence_band_high,
                       sd.agreement_rate, sd.approved, sd.recommended_action,
                       sd.game_context,
                       g.scheduled_start AS game_time,
                       g.home_team, g.away_team
                  FROM swarm_debates sd
                  LEFT JOIN games g ON g.game_id = sd.game_id
                 WHERE sd.created_at > now() - interval '60 minutes'
                   AND sd.approved = true
                   AND sd.league = ANY($2)
                   AND NOT EXISTS (
                       SELECT 1 FROM trades t
                        WHERE t.executed_at::date = (now() at time zone 'utc')::date
                          AND t.market_id IS NOT NULL
                          AND (
                            t.market_id = (sd.game_context->>'kalshi_market_id')
                            OR t.market_id = sd.game_id
                          )
                   )
                """
            if aggression_mode:
                params.append(aggression_mode)
                sql += "\n                   " + (mode_clause % len(params))
            sql += "\n                 ORDER BY sd.created_at DESC\n                 LIMIT $1\n                "
            rows = await db.fetch(sql, *params)
        else:
            params = [limit]
            sql = """
                SELECT sd.id            AS debate_id,
                       sd.game_id, sd.matchup, sd.league, sd.market_type,
                       sd.consensus_probability, sd.confidence_band_low, sd.confidence_band_high,
                       sd.agreement_rate, sd.approved, sd.recommended_action,
                       sd.game_context,
                       g.scheduled_start AS game_time,
                       g.home_team, g.away_team
                  FROM swarm_debates sd
                  LEFT JOIN games g ON g.game_id = sd.game_id
                 WHERE sd.created_at > now() - interval '60 minutes'
                   AND sd.approved = true
                   AND NOT EXISTS (
                       SELECT 1 FROM trades t
                        WHERE t.executed_at::date = (now() at time zone 'utc')::date
                          AND t.market_id IS NOT NULL
                          AND (
                            t.market_id = (sd.game_context->>'kalshi_market_id')
                            OR t.market_id = sd.game_id
                          )
                   )
                """
            if aggression_mode:
                params.append(aggression_mode)
                sql += "\n                   " + (mode_clause % len(params))
            sql += "\n                 ORDER BY sd.created_at DESC\n                 LIMIT $1\n                "
            rows = await db.fetch(sql, *params)
        return [dict(r) for r in rows]
    except Exception as e:
        log.debug("cached debate read failed: %s", e)
        return []


def _resolve_market_id(d: dict[str, Any]) -> str | None:
    """Return the canonical Kalshi market id for the candidate, or None.

    We deliberately do NOT fall back to `game_id` — placing trades keyed
    to a non-exchange identifier would break settlement, position
    aggregation, and the future live submission path (#4).  Candidates
    without a real market id are skipped by the cycle.
    """
    ctx = d.get("game_context") or {}
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except Exception:
            ctx = {}
    return (
        d.get("kalshi_market_id")
        or (ctx.get("kalshi_market_id") if isinstance(ctx, dict) else None)
    )


def _fresh_debate_candidate(
    pred: dict[str, Any], debate: dict[str, Any]
) -> dict[str, Any]:
    """Build the orchestrator candidate dict from a freshly-completed
    swarm debate plus its source `predictions` row.

    Critically, this carries `home_team` / `away_team` from the
    prediction (joined onto the `games` table in
    `_candidate_predictions`) into `game_context` so the downstream
    `attach_elo_to_candidate` enrichment can actually populate
    team_elo / opponent_elo / recent_form_delta on the fresh-debate
    path.  Without this the debate's game_context only carried market
    ids and the Elo lookup silently no-op'd (task #130).
    """
    return {
        "debate_id": debate.get("debate_id"),
        # Streaming UUID surfaced by run_debate so the orchestrator can
        # publish post-debate events that the live SwarmTranscript can
        # correlate back to its open card (task #613).  Cached debates
        # have no stream id — they were run in a prior session — so
        # downstream code keys off the presence of this field to
        # decide whether the live transcript can render the update.
        "stream_debate_id": debate.get("stream_debate_id"),
        "game_id": debate.get("game_id"),
        "matchup": debate.get("matchup"),
        "league": debate.get("league"),
        "market_type": debate.get("market_type"),
        "consensus_probability": debate.get("consensus_probability"),
        "confidence_band_low": debate.get("confidence_band_low"),
        "confidence_band_high": debate.get("confidence_band_high"),
        "agreement_rate": debate.get("agreement_rate"),
        "approved": debate.get("approved"),
        "recommended_action": debate.get("recommended_action"),
        # Carry kickoff onto the candidate so RiskContext.game_time is
        # populated for the fresh-debate path too — drives pre-kickoff
        # blackout + Sunday-window cap evaluation.
        "game_time": pred.get("game_time"),
        # Top-level home/away (attach_elo_to_candidate accepts either
        # location) so the elo enrichment fires even when an upstream
        # consumer ignores game_context.
        "home_team": pred.get("home_team"),
        "away_team": pred.get("away_team"),
        "game_context": {
            "kalshi_market_id": pred.get("kalshi_market_id"),
            "kalshi_price": pred.get("kalshi_price"),
            "home_team": pred.get("home_team"),
            "away_team": pred.get("away_team"),
        },
    }


async def _publish(event: dict[str, Any]) -> None:
    await pipeline_event_hub.publish(event)


async def _persist_and_broadcast_n_way_sketch(
    cand: dict[str, Any], live_pred: dict[str, Any] | None
) -> None:
    """Persist the per-entrant Plackett-Luce sketch onto the debate row
    and mirror it onto the LLM-logs websocket so the live SwarmTranscript
    can render the same field-probability table the recent-debates panel
    shows (tasks #213 + #613).

    Best-effort: a sick swarm_debates UPDATE or websocket publish must
    NOT take down the trading loop.  Splits cleanly on the two pieces:

    - The DB UPDATE keys off the persisted DB id (`cand["debate_id"]`).
    - The websocket event keys off the streaming UUID
      (`cand["stream_debate_id"]`) — same id every per-debate event on
      `/api/ws/llm-logs` carries, so the live transcript can attach the
      sketch to the active card.  Cached debates carry no stream id
      (they were run in a prior session), so the websocket publish is
      skipped — there is no live transcript card for them.
    """
    if live_pred is None:
        return
    sketch = live_pred.get("n_way_sketch")
    if not isinstance(sketch, dict):
        return
    persisted_id = cand.get("debate_id")
    if persisted_id is not None:
        try:
            # Namespace the audit data under a single key so we never
            # collide with other game_context fields.  The entrant
            # identifier already lives inside the sketch.
            await db.execute(
                """
                UPDATE swarm_debates
                   SET game_context = coalesce(game_context, '{}'::jsonb)
                                      || jsonb_build_object(
                                            'plackett_luce_sketch', $2::jsonb
                                         )
                 WHERE id = $1
                """,
                int(persisted_id),
                json.dumps(sketch),
            )
        except Exception as e:  # pragma: no cover — defensive
            log.debug(
                "plackett-luce sketch persist failed for debate %s: %s",
                persisted_id, e,
            )
    stream_id = cand.get("stream_debate_id")
    if stream_id:
        try:
            event: dict[str, Any] = {
                "type": "debate_n_way_sketch",
                "debate_id": str(stream_id),
                "sketch": sketch,
            }
            if persisted_id is not None:
                event["persisted_debate_id"] = int(persisted_id)
            await llm_log_hub.publish(event)
        except Exception as e:  # pragma: no cover — defensive
            log.debug(
                "plackett-luce sketch publish failed for debate %s: %s",
                stream_id, e,
            )


async def _predict_for_crypto_binary(cand: dict[str, Any]) -> dict[str, Any] | None:
    """Route a crypto-binary candidate through `app.crypto.predictor`.

    Returns the prediction's `as_dict()` payload (so the cycle's
    downstream telemetry/gates see the same shape they get from the
    generic predictor) or None when the ticker can't be parsed or the
    OHLCV window is missing.  Both failure modes degrade to swarm-only
    signals — same fallback the meta-model path uses.
    """
    market_id = _resolve_market_id(cand) or cand.get("kalshi_market_id")
    if not market_id:
        return None
    try:
        ticker = parse_crypto_ticker(str(market_id))
    except InvalidCryptoTicker as e:
        log.debug("crypto ticker parse failed for %s: %s", market_id, e)
        return None
    try:
        pred = await crypto_predictor.predict_crypto_binary(
            ticker, league=cand.get("league") or "CRYPTO"
        )
    except crypto_predictor.CryptoPredictionError as e:
        log.debug("crypto predict failed for %s: %s", market_id, e)
        return None
    except Exception as e:  # pragma: no cover — defensive
        log.warning("crypto predict crashed for %s: %s", market_id, e)
        return None
    payload = pred.as_dict()
    payload["feature_inputs"] = payload.get("raw", {}).get("features", {})
    payload["feature_count"] = payload.get("raw", {}).get("feature_count")
    return payload


async def _predict_for_temperature_binary(cand: dict[str, Any]) -> dict[str, Any] | None:
    """Route a temperature candidate (KXHIGH/KXTEMP/KXLOW) through the
    GRU + EMOS pipeline (task #246).

    Returns the prediction's `as_dict()` payload (so the cycle's
    downstream telemetry/gates see the same shape they get from the
    generic predictor) or None when the ticker can't be parsed (e.g.
    KXRAIN — which falls through to the meta-model) or the multi-source
    payload didn't surface any usable forecasts.
    """
    market_id = _resolve_market_id(cand) or cand.get("kalshi_market_id")
    if not market_id:
        return None
    try:
        ticker = parse_temperature_ticker(str(market_id))
    except InvalidTemperatureTicker as e:
        log.debug("temperature ticker parse failed for %s: %s", market_id, e)
        return None
    try:
        pred = await weather_predictor.predict_temperature_binary(
            ticker, league=cand.get("league") or "WEATHER"
        )
    except weather_predictor.TemperaturePredictionError as e:
        log.debug("temperature predict failed for %s: %s", market_id, e)
        return None
    except Exception as e:  # pragma: no cover — defensive
        log.warning("temperature predict crashed for %s: %s", market_id, e)
        return None
    payload = pred.as_dict()
    payload["feature_inputs"] = payload.get("raw", {}).get("ensemble", {})
    payload["feature_count"] = (
        (payload.get("raw", {}).get("ensemble", {}) or {}).get("n", 0)
    )
    return payload


async def _predict_for_precipitation_binary(cand: dict[str, Any]) -> dict[str, Any] | None:
    """Route a KXRAIN precipitation candidate through the gamma/logistic
    EMOS pipeline (task #255).

    Returns the prediction's `as_dict()` payload (so the cycle's
    downstream telemetry/gates see the same shape they get from the
    generic predictor) or None when the ticker can't be parsed (i.e.
    it's not a KXRAIN ticker — KXHIGH/KXTEMP/KXLOW are handled by the
    temperature predictor; anything else falls through to the meta-
    model) or the multi-source payload didn't surface any usable
    precipitation forecasts.
    """
    market_id = _resolve_market_id(cand) or cand.get("kalshi_market_id")
    if not market_id:
        return None
    try:
        ticker = parse_precipitation_ticker(str(market_id))
    except InvalidPrecipitationTicker as e:
        log.debug("precipitation ticker parse failed for %s: %s", market_id, e)
        return None
    try:
        pred = await weather_precip_predictor.predict_precipitation_binary(
            ticker, league=cand.get("league") or "WEATHER"
        )
    except weather_precip_predictor.PrecipitationPredictionError as e:
        log.debug("precipitation predict failed for %s: %s", market_id, e)
        return None
    except Exception as e:  # pragma: no cover — defensive
        log.warning("precipitation predict crashed for %s: %s", market_id, e)
        return None
    payload = pred.as_dict()
    payload["feature_inputs"] = payload.get("raw", {}).get("ensemble", {})
    payload["feature_count"] = (
        (payload.get("raw", {}).get("ensemble", {}) or {}).get("n", 0)
    )
    return payload


async def _predict_for_commodities_binary(cand: dict[str, Any]) -> dict[str, Any] | None:
    """Route a commodities-binary candidate through `app.commodities.predictor`.

    Mirrors `_predict_for_crypto_binary`: parse the Kalshi ticker,
    call the per-contract LGB+XGB stack, fold the calibrated YES
    probability into the standard cycle telemetry shape.  Both failure
    modes (parse, missing OHLCV window) degrade to swarm-only signals.
    """
    market_id = _resolve_market_id(cand) or cand.get("kalshi_market_id")
    if not market_id:
        return None
    try:
        ticker = parse_commodity_ticker(str(market_id))
    except InvalidCommodityTicker as e:
        log.debug("commodity ticker parse failed for %s: %s", market_id, e)
        return None
    try:
        pred = await commodities_predictor.predict_commodity_binary(
            ticker, league=cand.get("league") or "COMMODITIES"
        )
    except commodities_predictor.CommodityPredictionError as e:
        log.debug("commodity predict failed for %s: %s", market_id, e)
        return None
    except Exception as e:  # pragma: no cover — defensive
        log.warning("commodity predict crashed for %s: %s", market_id, e)
        return None
    payload = pred.as_dict()
    payload["feature_inputs"] = payload.get("raw", {}).get("features", {})
    payload["feature_count"] = payload.get("raw", {}).get("feature_count")
    return payload


async def _predict_for_tech_ai_binary(cand: dict[str, Any]) -> dict[str, Any] | None:
    """Route a tech-AI candidate through `app.tech_ai.predictor`.

    Mirrors the commodities/crypto helpers — parse the Kalshi ticker
    into a `TechAITicker`, run the Bayesian evidence model, fold the
    calibrated YES probability into the standard cycle telemetry
    shape.  Failure modes (parse error, sick evidence cache) degrade
    to swarm-only signals exactly like every other vertical.
    """
    market_id = _resolve_market_id(cand) or cand.get("kalshi_market_id")
    if not market_id:
        return None
    try:
        ticker = parse_tech_ai_ticker(str(market_id))
    except InvalidTechAITicker as e:
        log.debug("tech-ai ticker parse failed for %s: %s", market_id, e)
        return None
    try:
        pred = await tech_ai_predictor.predict_tech_ai_binary(
            ticker, league=cand.get("league") or "TECH_AI"
        )
    except tech_ai_predictor.TechAIPredictionError as e:
        log.debug("tech-ai predict failed for %s: %s", market_id, e)
        return None
    except Exception as e:  # pragma: no cover — defensive
        log.warning("tech-ai predict crashed for %s: %s", market_id, e)
        return None
    payload = pred.as_dict()
    payload["feature_inputs"] = payload.get("raw", {}).get("features", {})
    payload["feature_count"] = payload.get("raw", {}).get("feature_count")
    return payload


async def _predict_for_elections_binary(cand: dict[str, Any]) -> dict[str, Any] | None:
    """Route an elections candidate through `app.elections.predictor`.

    Mirrors the tech-AI helper — parse the Kalshi ticker into an
    `ElectionTicker`, run the poll-of-polls model, fold the
    calibrated YES probability into the standard cycle telemetry
    shape.  Failure modes (parse error, sick election_polls cache)
    degrade to swarm-only signals.
    """
    market_id = _resolve_market_id(cand) or cand.get("kalshi_market_id")
    if not market_id:
        return None
    try:
        from ..elections.tickers import (
            InvalidElectionTicker as _InvalidElectionTicker,
            parse_election_ticker as _parse_election_ticker,
        )
        from ..elections import predictor as _elections_predictor
    except Exception as e:  # pragma: no cover — defensive
        log.warning("elections module import failed: %s", e)
        return None
    try:
        ticker = _parse_election_ticker(str(market_id))
    except _InvalidElectionTicker as e:
        log.debug("elections ticker parse failed for %s: %s", market_id, e)
        return None
    try:
        pred = await _elections_predictor.predict_election_binary(
            ticker, league=cand.get("league") or "ELECTIONS"
        )
    except _elections_predictor.ElectionPredictionError as e:
        log.debug("elections predict failed for %s: %s", market_id, e)
        return None
    except Exception as e:  # pragma: no cover — defensive
        log.warning("elections predict crashed for %s: %s", market_id, e)
        return None
    payload = pred.as_dict()
    payload["feature_inputs"] = payload.get("raw", {}).get("features", {})
    payload["feature_count"] = payload.get("raw", {}).get("feature_count")
    return payload


async def _predict_for_events_econ_binary(cand: dict[str, Any]) -> dict[str, Any] | None:
    """Route an events-econ candidate through `app.events_econ.predictor`.

    Mirrors the elections / tech-AI helper — parse the Kalshi ticker
    into an `EventsEconTicker`, run the consensus-prior model, fold
    the calibrated YES probability into the standard cycle telemetry
    shape.  Failure modes (parse error, sick econ_releases cache)
    degrade to swarm-only signals.
    """
    market_id = _resolve_market_id(cand) or cand.get("kalshi_market_id")
    if not market_id:
        return None
    try:
        from ..events_econ.tickers import (
            InvalidEventsEconTicker as _InvalidEventsEconTicker,
            parse_events_econ_ticker as _parse_events_econ_ticker,
        )
        from ..events_econ import predictor as _events_econ_predictor
    except Exception as e:  # pragma: no cover — defensive
        log.warning("events-econ module import failed: %s", e)
        return None
    try:
        ticker = _parse_events_econ_ticker(str(market_id))
    except _InvalidEventsEconTicker as e:
        log.debug("events-econ ticker parse failed for %s: %s", market_id, e)
        return None
    # Pass the candidate's current Kalshi YES mid into the predictor
    # so it can fold the pre-release market into the consensus-prior
    # probability via a log-odds Bayesian blend.  Source the mid from
    # any of: top-level current_price (set on fresh debate inputs),
    # top-level kalshi_price (set on cached candidate rows from
    # `_candidate_predictions`), or game_context blob (set when the
    # candidate was assembled from a fresh debate).  Without this
    # fall-through the log-odds blend in `predict_events_econ_binary`
    # never fires in live cycle flow — same regression we fixed in
    # `_predict_for_entertainment_binary`.
    kalshi_mid_raw = cand.get("current_price") or cand.get("kalshi_price")
    if kalshi_mid_raw is None:
        ctx_blob = cand.get("game_context") or {}
        if isinstance(ctx_blob, str):
            try:
                ctx_blob = json.loads(ctx_blob)
            except Exception:
                ctx_blob = {}
        if isinstance(ctx_blob, dict):
            kalshi_mid_raw = ctx_blob.get("kalshi_price")
    try:
        kalshi_mid = float(kalshi_mid_raw) if kalshi_mid_raw is not None else None
    except (TypeError, ValueError):
        kalshi_mid = None
    try:
        pred = await _events_econ_predictor.predict_events_econ_binary(
            ticker,
            league=cand.get("league") or "EVENTS_ECON",
            kalshi_mid=kalshi_mid,
        )
    except _events_econ_predictor.EventsEconPredictionError as e:
        log.debug("events-econ predict failed for %s: %s", market_id, e)
        return None
    except Exception as e:  # pragma: no cover — defensive
        log.warning("events-econ predict crashed for %s: %s", market_id, e)
        return None
    payload = pred.as_dict()
    payload["feature_inputs"] = payload.get("raw", {}).get("features", {})
    payload["feature_count"] = payload.get("raw", {}).get("feature_count")
    return payload


async def _predict_for_entertainment_binary(cand: dict[str, Any]) -> dict[str, Any] | None:
    """Route an entertainment candidate through `app.entertainment.predictor`.

    Mirrors the events-econ helper — parse the Kalshi ticker into an
    `EntertainmentTicker`, run the hybrid model, fold the calibrated
    YES probability into the cycle telemetry shape.  Failure modes
    (parse error, sick entertainment_signals cache) degrade to swarm-
    only signals.
    """
    market_id = _resolve_market_id(cand) or cand.get("kalshi_market_id")
    if not market_id:
        return None
    try:
        from ..entertainment.tickers import (
            InvalidEntertainmentTicker as _InvalidEntertainmentTicker,
            parse_entertainment_ticker as _parse_entertainment_ticker,
        )
        from ..entertainment import predictor as _entertainment_predictor
    except Exception as e:  # pragma: no cover — defensive
        log.warning("entertainment module import failed: %s", e)
        return None
    try:
        ticker = _parse_entertainment_ticker(str(market_id))
    except _InvalidEntertainmentTicker as e:
        log.debug("entertainment ticker parse failed for %s: %s", market_id, e)
        return None
    # Source the Kalshi mid from any of: top-level current_price (set
    # on fresh debate inputs), top-level kalshi_price (set on cached
    # candidate rows from `_candidate_predictions`), or game_context
    # blob (set when the candidate was assembled from a fresh debate).
    # Without this fall-through the log-odds blend in
    # `predict_entertainment_binary` never fires in live cycle flow.
    kalshi_mid_raw = cand.get("current_price") or cand.get("kalshi_price")
    if kalshi_mid_raw is None:
        ctx_blob = cand.get("game_context") or {}
        if isinstance(ctx_blob, str):
            try:
                ctx_blob = json.loads(ctx_blob)
            except Exception:
                ctx_blob = {}
        if isinstance(ctx_blob, dict):
            kalshi_mid_raw = ctx_blob.get("kalshi_price")
    try:
        kalshi_mid = float(kalshi_mid_raw) if kalshi_mid_raw is not None else None
    except (TypeError, ValueError):
        kalshi_mid = None
    try:
        pred = await _entertainment_predictor.predict_entertainment_binary(
            ticker,
            league=cand.get("league") or "ENTERTAINMENT",
            kalshi_mid=kalshi_mid,
        )
    except _entertainment_predictor.EntertainmentPredictionError as e:
        log.debug("entertainment predict failed for %s: %s", market_id, e)
        return None
    except Exception as e:  # pragma: no cover — defensive
        log.warning("entertainment predict crashed for %s: %s", market_id, e)
        return None
    payload = pred.as_dict()
    payload["feature_inputs"] = payload.get("raw", {}).get("features", {})
    payload["feature_count"] = payload.get("raw", {}).get("feature_count")
    return payload


async def _predict_for_wnba_player_props(
    cand: dict[str, Any],
) -> dict[str, Any] | None:
    """Route a WNBA player-prop candidate through the dedicated
    predictor (task #823).

    The candidate must carry ``player`` (str), ``stat`` (one of the
    supported PROP_MARKETS) and ``line`` (float).  Optional:
    ``side`` (default 'over'), ``opponent_team``, ``pace_estimate``,
    ``kalshi_mid``.  Returns ``None`` when any required field is
    missing OR when the per-player baseline sample is below the
    configured minimum — in either case the orchestrator falls back
    to the generic meta-model so the cycle isn't blocked.
    """
    player = cand.get("player")
    stat = cand.get("stat") or cand.get("market")
    line = cand.get("line")
    if not (player and stat and line is not None):
        return None
    try:
        from ..wnba.predictor import predict_wnba_player_prop
    except Exception as e:  # pragma: no cover — should never hit
        log.debug("wnba predictor import failed: %s", e)
        return None
    try:
        prediction = await predict_wnba_player_prop(
            player=str(player),
            stat=str(stat),
            line=float(line),
            side=str(cand.get("side") or "over"),
            opponent_team=cand.get("opponent_team"),
            pace_estimate=cand.get("pace_estimate"),
            kalshi_mid=cand.get("kalshi_mid"),
        )
    except Exception as e:
        log.debug("wnba predictor call failed for %s: %s", player, e)
        return None
    if prediction is None:
        return None
    payload = prediction.as_dict()
    payload["feature_inputs"] = {
        "player": player, "stat": stat, "line": line,
        "side": cand.get("side") or "over",
        "opponent_team": cand.get("opponent_team"),
    }
    return payload


async def _predict_for_candidate(
    cand: dict[str, Any],
    *,
    consensus: float | None,
    current_price: float | None,
    edge: float | None,
    vertical: str = "sports",
) -> dict[str, Any] | None:
    """Run the active canonical predictor on a candidate's signals.

    Builds the meta-model's expected 32-feature vector from the
    candidate dict via `build_features()` so the live probability is
    produced from the same schema the trainer used.  The predictor
    service still auto-pads/truncates if a future model has a different
    width — this just gives it a sane positional input to start from.

    Returns the prediction's `as_dict()` payload (with an extra
    `feature_inputs` key carrying the named features that were actually
    present on the candidate), or None when the predictor isn't loaded
    yet (cold cache) or the call fails.  Errors are swallowed and
    logged at debug — a sick model must NOT take down the trading loop,
    the loop just falls back to swarm-only signals.
    """
    # Verticals can opt into a specialist predictor via
    # `VerticalSpec.predictor_hook`.  The crypto-binary vertical wires
    # its OHLCV ensemble + GRU stack via the "crypto_binary" hook,
    # bypassing the generic meta-model entirely.
    spec = vertical_registry.get_spec(vertical)
    if spec is not None and spec.predictor_hook == "crypto_binary":
        return await _predict_for_crypto_binary(cand)
    if spec is not None and spec.predictor_hook == "commodities_binary":
        return await _predict_for_commodities_binary(cand)
    if spec is not None and spec.predictor_hook == "tech_ai_evidence":
        return await _predict_for_tech_ai_binary(cand)
    if spec is not None and spec.predictor_hook == "elections_poll_of_polls":
        return await _predict_for_elections_binary(cand)
    if spec is not None and spec.predictor_hook == "events_econ_consensus_prior":
        return await _predict_for_events_econ_binary(cand)
    if spec is not None and spec.predictor_hook == "entertainment_hybrid":
        return await _predict_for_entertainment_binary(cand)
    if spec is not None and spec.predictor_hook == "wnba_player_props":
        wnba_payload = await _predict_for_wnba_player_props(cand)
        # Fall through to the meta-model when the candidate doesn't
        # carry the player/stat/line trio (e.g. the operator opted the
        # vertical on but the discovery feed hasn't yet been wired —
        # task #824).  The generic meta-model is a strict downgrade
        # but it keeps the cycle moving instead of returning None.
        if wnba_payload is not None:
            return wnba_payload
    if spec is not None and spec.predictor_hook == "weather_temp_gru_emos":
        weather_payload = await _predict_for_temperature_binary(cand)
        if weather_payload is not None:
            return weather_payload
        # KXRAIN: route to the precipitation EMOS pipeline (task #255)
        # before falling through to the generic meta-model so precip
        # markets get a properly-calibrated multi-source forecast.
        weather_payload = await _predict_for_precipitation_binary(cand)
        if weather_payload is not None:
            return weather_payload
        # Anything else (unknown weather ticker shape) -> fall through
        # to the generic meta-model.

    # N-way tournament markets (task #197) — leagues that flag
    # `LeagueConfig.n_way_market=True` (ATP / WTA / F1 / PGA) and ship
    # an `n_way_field` mapping `{entrant: rating}` on the candidate are
    # priced through the closed-form Plackett-Luce module instead of
    # the team-sport meta-model (which is undefined for N>2 fields).
    # The candidate's `entrant` field selects which leg's YES
    # probability to surface.  Falls through to the meta-model when
    # the field/entrant isn't supplied so live trading isn't blocked
    # while the feed is being wired (follow-up #214).
    league = cand.get("league")
    league_cfg = get_league_config(league) if league else None
    # Enrichment: candidates persisted via the standard
    # `_candidate_predictions` SELECT don't carry n_way_field /
    # entrant directly (no DB column).  Pull the field from the
    # process-local ratings cache populated by the Sportradar
    # normalizer, and derive `entrant` from the candidate's
    # `home.name` / `team` / `selection` field which the predictions
    # row already records as the YES side of the outright market.
    n_way_field_raw = cand.get("n_way_field")
    if n_way_field_raw is None and league_cfg is not None and league_cfg.n_way_market:
        n_way_field_raw = get_field(cand.get("game_id") or cand.get("event_id"))
    n_way_field = coerce_to_ratings(n_way_field_raw)
    entrant = (
        cand.get("entrant")
        or cand.get("side_entrant")
        or cand.get("selection")
        or cand.get("team")
        or ((cand.get("home") or {}).get("name") if isinstance(cand.get("home"), dict) else None)
    )
    if (
        league_cfg is not None
        and league_cfg.n_way_market
        and isinstance(n_way_field, dict)
        and entrant in n_way_field
    ):
        try:
            ratings = {str(k): float(v) for k, v in n_way_field.items()}
            probs = plackett_luce_from_mapping(ratings)
            yes_prob = float(probs[entrant])
            return {
                "probability": yes_prob,
                "model_name": "plackett_luce_n_way",
                "model_version": "1",
                "feature_inputs": {
                    "n_way_field_size": len(ratings),
                    "entrant": entrant,
                    "entrant_rating": ratings[entrant],
                },
                "feature_count": len(ratings),
                "n_way_probabilities": probs,
                # Full audit sketch persisted onto the debate row so
                # analysts can reproduce the per-entrant probabilities
                # without re-pulling the (process-local) ratings cache.
                "n_way_sketch": {
                    "model": "plackett_luce",
                    "entrant": entrant,
                    "entrant_rating": ratings[entrant],
                    "entrant_probability": yes_prob,
                    "field_size": len(ratings),
                    "ratings": ratings,
                    "probabilities": probs,
                },
            }
        except Exception as e:  # pragma: no cover - never kill the loop
            log.debug(
                "n-way predict failed for %s/%s: %s",
                league, cand.get("game_id"), e,
            )
            # Fall through to the team-sport meta-model on any failure.

    fv = build_features(
        cand,
        consensus=consensus,
        current_price=current_price,
        edge=edge,
    )
    try:
        pred = await predictor.predict(
            fv.values,
            league=cand.get("league"),
        )
    except predictor.PredictorUnavailable:
        return None
    except Exception as e:  # pragma: no cover — model-specific failures
        log.debug("live predict failed for %s: %s", cand.get("game_id"), e)
        return None
    payload = pred.as_dict()
    payload["feature_inputs"] = fv.present
    payload["feature_count"] = len(fv.values)
    # Spread / total tickers benefit from a continuous-quantity model:
    # blend the binary baseline with the per-league margin regressor
    # so the live probability picks up the regressor's edge while
    # keeping every signal the meta-model sees on top.  Best-effort —
    # any failure leaves the baseline untouched.
    try:
        adj = margin_regressor.adjust_probability(
            float(payload["probability"]), cand,
        )
    except Exception as e:  # pragma: no cover — never let this kill the loop
        log.debug("margin regressor adjust failed for %s: %s", cand.get("game_id"), e)
        adj = None
    if adj is not None:
        payload["baseline_probability"] = payload["probability"]
        payload["probability"] = adj["blended"]
        payload["margin_adjustment"] = adj
    return payload


async def run_cycle(vertical: str = "sports") -> CycleResult:
    """Execute one full trading cycle for `vertical`.

    Cycle stages emit pipeline events the dashboard can render in real
    time:  cycle_start → candidate_start → swarm_start → gate × N →
    candidate_complete (… repeat …) → cycle_complete.
    """
    settings = get_settings()
    started = time.perf_counter()
    started_at = datetime.now(timezone.utc).isoformat()

    active_model = predictor.active_model_info()
    cycle_settings_snapshot = {
        "trading_mode": settings.trading_mode,
        "cycle_interval_seconds": settings.cycle_interval_seconds,
        "max_fresh_debates_per_cycle": settings.max_fresh_debates_per_cycle,
        "vertical": vertical,
        "active_model": active_model,
    }

    # Open the cycle row up front so dashboards see in-flight cycles
    # immediately rather than only after completion.
    cycle_id: int | None = None
    try:
        cycle_id = await db.fetchval(
            """
            INSERT INTO trading_cycles (started_at, vertical, picks_evaluated,
                                        trades_placed, errors, settings_snapshot)
            VALUES (now(), $1, 0, 0, NULL, $2::jsonb)
            RETURNING id
            """,
            vertical,
            json.dumps(cycle_settings_snapshot),
        )
    except Exception as e:
        log.warning("trading_cycles open failed (cycle still runs): %s", e)

    result = CycleResult(
        cycle_id=int(cycle_id) if cycle_id is not None else None,
        vertical=vertical,
        started_at=started_at,
    )
    await _publish({
        "type": "cycle_start",
        "cycle_id": result.cycle_id,
        "vertical": vertical,
        "started_at": started_at,
        "settings": cycle_settings_snapshot,
    })

    overrides = await _load_operator_overrides()
    # Inject the operator-tunable aggression mode so the gates can use
    # it as the *default* threshold (explicit `risk.*` overrides still
    # win — see `risk.gates._mode_default`).
    aggression_mode = await get_aggression_mode()
    overrides.setdefault("aggression_mode", aggression_mode)
    # Effective trading mode honors the operator override too — same
    # contract as gate_kill_switch.
    effective_mode = (
        overrides.get("trading_mode") or settings.trading_mode or "paper"
    ).lower()
    # Age-based sweep: cancel resting orders older than the per-vertical
    # TTL before *anything else* happens in the cycle. A resting order
    # from cycle N can fill against cycle N+1's view of the world (new
    # edge, new persona disagreement, new drift signal) — we'd rather
    # cancel and re-submit fresh if still warranted. Runs even when
    # `effective_mode == "halt"` so halting the loop does not leave
    # already-resting orders live against an arbitrarily stale thesis.
    # Cancel failures are swallowed so a flaky venue cannot park the
    # orchestrator.
    try:
        sweep_stats = await _cancel_stale_resting_orders(vertical, overrides)
        if sweep_stats.get("cancelled") or sweep_stats.get("failed"):
            await _publish({
                "type": "stale_resting_sweep",
                "cycle_id": result.cycle_id,
                "vertical": vertical,
                **sweep_stats,
            })
        # Run the rolling-baseline anomaly evaluator + persist this
        # cycle's counters (task #711). Order matters: evaluate FIRST
        # against prior history (24h excluding this cycle), then record
        # after — otherwise the just-inserted row dilutes the z-score it
        # is itself being scored against. Both calls fail open so a
        # telemetry write or alert dispatch failure never blocks the
        # cycle.
        try:
            from . import stale_cancel_baseline as _sc_baseline
            anomaly = await _sc_baseline.evaluate_and_alert(
                vertical, overrides, sweep_stats, cycle_id=result.cycle_id,
            )
            if anomaly.get("alerts"):
                await _publish({
                    "type": "stale_cancel_anomaly",
                    "cycle_id": result.cycle_id,
                    "vertical": vertical,
                    "alerts": anomaly["alerts"],
                    "volume": anomaly.get("volume"),
                    "failure": anomaly.get("failure"),
                })
            await _sc_baseline.record_observation(
                vertical=vertical,
                cycle_id=result.cycle_id,
                scanned=int(sweep_stats.get("scanned") or 0),
                cancelled=int(sweep_stats.get("cancelled") or 0),
                failed=int(sweep_stats.get("failed") or 0),
                max_age=int(sweep_stats.get("max_age") or 0),
            )
            # Retention sweep: prunes stale_cancel_observations rows
            # older than the configured window (7 days). The helper is
            # day-throttled per worker process so the DELETE only
            # actually executes once per 24h regardless of how often
            # the cycle ticks; in-between calls return immediately.
            # Wired here (rather than via a separate scheduler) so
            # retention is automatically active as soon as the cycle
            # is — one fewer moving part for ops.
            await _sc_baseline.maybe_gc_observations()
        except Exception as e:  # pragma: no cover — defensive
            log.warning(
                "stale_cancel baseline/anomaly path crashed (cycle continues): %s",
                e,
            )
        # Notification dedupe retention: prune `notification_dedupe`
        # rows whose TTL expired more than the configured retention
        # window ago (default 7 days, override via
        # AXIOM_NOTIFICATION_DEDUPE_RETENTION_DAYS). Day-throttled per
        # worker process so the DELETE only actually executes once
        # per 24h regardless of cycle cadence. Wired here (rather than
        # via a separate scheduler) so retention is automatically
        # active as soon as the cycle is — daily_summary ALSO runs the
        # GC on its own scheduler, but operators can disable that and
        # we still want bounded growth.
        try:
            from ..notifications import alerts as _alerts_gc
            await _alerts_gc.maybe_gc_dedupe()
        except Exception as e:  # pragma: no cover — defensive
            log.warning(
                "notification_dedupe gc crashed (cycle continues): %s", e,
            )
    except Exception as e:  # pragma: no cover — defensive: sweep must never block the cycle
        log.warning("stale resting sweep crashed (cycle continues): %s", e)

    if effective_mode == "halt":
        result.errors.append("trading_mode=halt — cycle skipped placement stage")
        await _publish({
            "type": "cycle_halted",
            "cycle_id": result.cycle_id,
            "reason": "trading_mode=halt",
        })
        await _close_cycle(result, started)
        return result

    # Task #752 step 1: thread the cycle's resolved trading_mode into
    # every read-side risk aggregate so paper-mode bankroll deltas /
    # open positions / today-trade churn never leak into a live cycle's
    # gates and vice versa.  The cycle's ``effective_mode`` is one of
    # paper|live by this point (halt was already short-circuited above).
    bankroll = await _latest_bankroll(trading_mode=effective_mode)
    (
        open_n,
        open_notional,
        per_vertical,
        per_league,
        per_matchup,
        per_sunday_window,
        per_event,
    ) = await _open_position_aggregates(trading_mode=effective_mode)
    trades_today_per_league = await _today_trade_counts_per_league(
        trading_mode=effective_mode
    )

    # Best-effort cap-bucket warn check: page the on-call when any
    # NFL exposure bucket (per-league, per-game, Sunday-window)
    # crosses the warn threshold (default 80%) so they hear about
    # it before the next bet is silently blocked. Dedup is handled
    # inside the helper so it cannot spam every cycle. Scheduled as
    # a fire-and-forget task to avoid adding Postmark latency to the
    # hot path; failures are swallowed.
    try:
        from ..notifications.cap_warnings import check_nfl_cap_warnings
        asyncio.create_task(check_nfl_cap_warnings())
    except Exception as e:  # pragma: no cover — defensive
        log.debug("cap-warn check failed to schedule: %s", e)

    # Best-effort tail-risk burst-rate check (task #746): page the
    # on-call when either tail-risk gate logs more than the operator-
    # tunable threshold of vetoes inside the rolling 60-minute window.
    # Dedup is handled inside the helper so a sustained burst pages
    # once per TTL.  Scheduled as a fire-and-forget task to keep the
    # hot path off Postmark latency; failures are swallowed.
    try:
        from ..notifications.tail_risk_burst import check_tail_risk_burst
        asyncio.create_task(check_tail_risk_burst())
    except Exception as e:  # pragma: no cover — defensive
        log.debug("tail-risk burst check failed to schedule: %s", e)

    # Best-effort live-state staleness check (task #862): page the
    # on-call when a sport's oldest sportradar_live_state row has
    # exceeded 2× the per-sport freshness budget for N consecutive
    # checks. Dedup + recovery notice are handled inside the helper.
    # Scheduled as a fire-and-forget task to keep the hot path off
    # Postmark latency; failures are swallowed.
    try:
        from ..notifications.live_state_staleness import (
            check_live_state_staleness,
        )
        asyncio.create_task(check_live_state_staleness())
    except Exception as e:  # pragma: no cover — defensive
        log.debug("live-state staleness check failed to schedule: %s", e)

    # 1) Cached approved debates first (cheap — no fresh LLM cost).
    cached = await _cached_debates(vertical, limit=10, aggression_mode=aggression_mode)
    # 2) Then up to N fresh swarm debates if budget allows.
    fresh_pool = await _candidate_predictions(
        vertical, limit=settings.max_fresh_debates_per_cycle
    )

    candidates: list[dict[str, Any]] = []
    candidates.extend(cached)

    # Run fresh debates serially to bound concurrent LLM cost.
    for pred in fresh_pool:
        if not (settings.openai_api_key and settings.anthropic_api_key):
            result.errors.append("swarm keys missing — skipped fresh debates")
            break
        try:
            d = await run_debate({
                "game_id": pred["game_id"],
                "league": pred["league"],
                "market_type": "moneyline",
                "side": "YES",
                "current_price": float(pred["kalshi_price"]) if pred.get("kalshi_price") is not None else None,
                "context": (
                    f"home_win_prob={pred['home_win_prob']} "
                    f"edge={pred['edge']} confidence={pred['confidence']}"
                ),
                # Persisted into swarm_debates.game_context so settlement
                # can join debates -> markets -> persona Brier updates.
                # asyncpg returns numeric columns as ``Decimal``; cast
                # to float here so the downstream JSON serializer in
                # ``run_debate`` (and the swarm_debates.game_context
                # jsonb persist) doesn't choke with
                # ``Object of type Decimal is not JSON serializable``.
                # Mirrors the float() conversion already applied to
                # ``current_price`` two lines above.
                "kalshi_market_id": pred.get("kalshi_market_id"),
                "kalshi_price": float(pred["kalshi_price"]) if pred.get("kalshi_price") is not None else None,
                # Carried so the persisted swarm_debates.game_context
                # row holds the team names — that's what the Elo
                # enrichment (`attach_elo_to_candidate`) keys off when
                # the cached-debate path rehydrates this debate later.
                "home_team": pred.get("home_team"),
                "away_team": pred.get("away_team"),
                "aggression_mode": aggression_mode,
                # Drives swarm.runner persona_profile resolution via the
                # vertical registry (default profile for legacy verticals).
                "vertical": vertical,
            })
            result.fresh_debates += 1
            candidates.append(_fresh_debate_candidate(pred, d))
        except Exception as e:
            result.errors.append(f"fresh debate failed for {pred.get('game_id')}: {e}")
            log.warning("fresh debate failed: %s", e)

    # 2.5) Best-effort enrichment: attach internal Elo features to each
    # candidate's game_context so sports predictors and the dashboard
    # see team_elo / opponent_elo / recent_form_delta alongside the
    # other inputs.  Silently no-ops when team names or league are
    # missing or the elo tables aren't reachable.
    for cand in candidates:
        try:
            await attach_elo_to_candidate(cand)
        except Exception as e:
            log.debug("elo enrichment failed for %s: %s", cand.get("game_id"), e)
        # Spread/Total candidates also need the closing line on
        # game_context so the margin regressor has something to
        # compare its expected margin/total against.  Best-effort —
        # silent no-op if the line table is empty for the game.
        try:
            await margin_regressor.attach_lines_to_candidate(cand)
        except Exception as e:
            log.debug("line enrichment failed for %s: %s", cand.get("game_id"), e)

    # Edge Terminal input-mode exclusivity (task #844): when the operator
    # has selected ``edge_terminal`` as the input source, the LLM
    # pipeline must NOT place trades — only the scraper executor fires.
    # Short-circuit BEFORE the candidate loop so we don't waste CPU
    # running 12 gates per candidate just to throw the result away.
    try:
        from ..edge_terminal import control as _et_control
        _input_mode = await _et_control.get_input_mode()
    except Exception:  # pragma: no cover — defensive
        _input_mode = "pipeline"
    _edge_terminal_active = _input_mode == "edge_terminal"

    # 3) Evaluate every candidate against the 12 gates.
    for cand in candidates:
        result.picks_evaluated += 1
        market_id = _resolve_market_id(cand)
        if not market_id:
            result.errors.append("candidate without market_id skipped")
            continue
        side = "YES" if (cand.get("recommended_action") or "").upper() != "BUY_NO" else "NO"
        consensus = cand.get("consensus_probability")
        # current_price falls back to consensus when the underlying market price
        # isn't carried on the cached debate row — gates with no price degrade
        # to PASS so we don't kill the loop on missing pricing data.
        ctx_blob = cand.get("game_context") or {}
        if isinstance(ctx_blob, str):
            try:
                ctx_blob = json.loads(ctx_blob)
            except Exception:
                ctx_blob = {}
        current_price = (
            float(ctx_blob["kalshi_price"])
            if isinstance(ctx_blob, dict) and ctx_blob.get("kalshi_price") is not None
            else None
        )
        # Pre-compute the swarm-derived edge so the feature builder gets
        # the *same* signed-edge the gates will see further down.
        pre_edge: float | None = None
        if consensus is not None and current_price is not None:
            pre_edge = (
                float(consensus) - float(current_price)
                if side == "YES"
                else float(current_price) - float(consensus)
            )
        # Live model probability from the loaded canonical predictor.
        # Built from the candidate dict via `build_features()` so the
        # vector matches the meta-model's training-time schema.
        live_pred = await _predict_for_candidate(
            cand,
            consensus=consensus,
            current_price=current_price,
            edge=pre_edge,
            vertical=vertical,
        )
        # N-way tournament markets (task #213): persist the per-entrant
        # Plackett-Luce sketch onto the debate row's `game_context` AND
        # mirror it onto the LLM-logs websocket so the live transcript
        # can render the same field-probability table the recent-debates
        # panel shows (task #613).  Best-effort: failures must NOT take
        # down the trading loop.
        await _persist_and_broadcast_n_way_sketch(cand, live_pred)
        # If swarm consensus is missing, fall back to the live model
        # probability so the gates still get a real number to chew on —
        # storing the live probability "alongside" the cached consensus
        # so the dashboard can render both per candidate.
        effective_consensus = consensus
        if effective_consensus is None and live_pred is not None:
            effective_consensus = live_pred["probability"]
        # Stamp the model's YES probability inline on the source
        # ``predictions`` row so the crypto accuracy report (task #580)
        # can join settled rows directly without a lateral
        # ``swarm_debates`` lookup (task #646).  Persist the
        # pre-calibration value so live writes share the same semantic
        # as the historical backfill — both reflect the model's raw
        # belief at scoring time, before the per-vertical isotonic
        # adjustment.  No-op for verticals whose feeds don't write a
        # ``predictions`` row keyed on ``kalshi_market_id`` and for
        # rows that already carry a probability (idempotent).
        await _persist_model_yes_prob(market_id, effective_consensus)
        # Route the model probability through the freshly-retrained
        # per-vertical isotonic calibrator (online-learning loop, task
        # #88).  Bypasses cleanly when no retrained snapshot has landed
        # yet for this vertical — gates then see the raw probability,
        # exactly the behavior before this loader existed.  Edge is
        # recomputed from the (possibly-calibrated) value below so the
        # gatekeeper's edge math stays consistent with what it sees as
        # the model probability.
        pre_calibration_consensus = effective_consensus
        if effective_consensus is not None:
            try:
                effective_consensus = await live_calibrator.apply(
                    vertical, float(effective_consensus)
                )
            except Exception as e:  # pragma: no cover — defensive
                log.debug(
                    "live_calibrator apply failed for %s: %s",
                    cand.get("game_id"), e,
                )
        edge = pre_edge
        if (
            effective_consensus is not None
            and current_price is not None
            and (
                edge is None
                or pre_calibration_consensus != effective_consensus
            )
        ):
            edge = (
                float(effective_consensus) - float(current_price)
                if side == "YES"
                else float(current_price) - float(effective_consensus)
            )
        intended_qty = int(overrides.get("intended_quantity", DEFAULT_INTENDED_QUANTITY))
        intended_notional = intended_qty * float(current_price or 0.0)
        cooldown_until = await _market_cooldown(market_id)

        # Commodities matchup propagation (task #198).  The 11-pair
        # correlation sub-check on `gate_per_vertical_cap` keys off
        # `open_positions_per_matchup_usd['COMMODITIES:<SYMBOL>']`, so
        # synthesise the canonical commodity symbol from the ticker
        # whenever the upstream candidate doesn't already carry one.
        # Without this the gate quietly under-counts correlated
        # exposure and degrades to the per-vertical aggregate only.
        if vertical == "commodities" and not cand.get("matchup"):
            try:
                cand["matchup"] = parse_commodity_ticker(str(market_id)).symbol
            except InvalidCommodityTicker:
                pass

        # Upstream missing-league instrumentation (task #771).
        #
        # `gate_uncalibrated_vertical` already fail-closes when
        # `ctx.league` is missing (state_error path → REJECT unless
        # the per-vertical raw-mode override is set), but a feed bug
        # that silently drops the league field would just look like
        # an ordinary calibrator-gate rejection on the dashboard —
        # the same "silent EV leak" shape the gate is meant to close.
        #
        # Before constructing the RiskContext we log a WARN with
        # vertical + market_id and persist a `risk_gate_trips` marker
        # row (gate_name='missing_league_on_candidate') so the daily
        # honesty report's new "Missing-league candidates (24h)"
        # section surfaces the count alongside raw-mode trades.
        # We deliberately do NOT skip the candidate — the existing
        # gate is already fail-closed; this is purely instrumentation
        # so the upstream feed bug becomes loud (see runbook §21).
        _cand_league = cand.get("league")
        _league_missing = (
            _cand_league is None
            or (isinstance(_cand_league, str) and not _cand_league.strip())
        )
        if _league_missing:
            log.warning(
                "candidate missing league: vertical=%s market_id=%s "
                "(gate_uncalibrated_vertical will fail-closed; see runbook §21)",
                vertical,
                market_id,
            )
            try:
                import json as _json_ml
                await db.execute(
                    """
                    INSERT INTO risk_gate_trips
                        (gate_name, market_id, vertical, side,
                         intended_quantity, current_price, reason, details)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
                    """,
                    "missing_league_on_candidate",
                    str(market_id),
                    vertical,
                    side,
                    int(intended_qty) if intended_qty is not None else None,
                    float(current_price) if current_price is not None else None,
                    "candidate reached risk gates without a league set",
                    _json_ml.dumps({
                        "vertical": vertical,
                        "market_id": str(market_id),
                        "matchup": cand.get("matchup"),
                        "game_id": cand.get("game_id"),
                    }),
                )
            except Exception as _ml_e:  # pragma: no cover — persist is best-effort
                log.warning(
                    "missing_league_on_candidate persist failed for %s: %s",
                    market_id,
                    _ml_e,
                )

        ctx = RiskContext(
            market_id=str(market_id),
            side=side,
            vertical=vertical,
            league=cand.get("league"),
            market_type=cand.get("market_type"),
            matchup=cand.get("matchup"),
            current_price=current_price,
            model_probability=float(effective_consensus) if effective_consensus is not None else None,
            edge=edge,
            confidence_band_low=cand.get("confidence_band_low"),
            confidence_band_high=cand.get("confidence_band_high"),
            agreement_rate=cand.get("agreement_rate"),
            swarm_approved=bool(cand.get("approved")),
            swarm_recommended_action=cand.get("recommended_action"),
            debate_id=cand.get("debate_id"),
            intended_quantity=intended_qty,
            intended_notional_usd=intended_notional,
            bankroll_usd=bankroll,
            open_positions_count=open_n,
            open_positions_notional_usd=open_notional,
            open_positions_per_vertical_usd=per_vertical,
            operator_overrides=overrides,
            market_cooldown_until=cooldown_until,
            home_team=cand.get("home_team"),
            away_team=cand.get("away_team"),
            open_positions_per_league_usd=per_league,
            open_positions_per_matchup_usd=per_matchup,
            open_positions_sunday_window_usd=per_sunday_window,
            trades_today_per_league=trades_today_per_league,
            game_time=cand.get("game_time"),
            # Per-event concentration + live-action blackout (task #197).
            # `event_id` falls back to the candidate's game_id so combat
            # & individual sports — which group many markets per card /
            # GP / tournament — cap their per-event exposure correctly.
            # Legacy team-sport candidates carry game_id == matchup-id
            # so the per-event sub-check is a stricter mirror of the
            # per-game cap (no behavior change since per_event_cap_pct
            # is None for them).
            event_id=cand.get("event_id") or cand.get("game_id"),
            open_positions_per_event_usd=per_event,
            # Derive `event_in_progress` from the candidate's game_time
            # when the feed hasn't tagged it explicitly: a candidate
            # whose start time has passed is treated as in-progress so
            # the live-action blackout (task #197) can reject it.  The
            # candidate query filters `game_time > now()` for typical
            # team-sport flows so this only fires when feeds widen the
            # filter to surface in-play events for combat & individual
            # sports — exactly the scenario the blackout protects.
            event_in_progress=(
                bool(cand.get("event_in_progress", False))
                or _game_time_in_past(cand.get("game_time"))
            ),
        )

        await _publish({
            "type": "candidate_start",
            "cycle_id": result.cycle_id,
            "vertical": vertical,
            "market_id": ctx.market_id,
            "side": ctx.side,
            "debate_id": ctx.debate_id,
            "model_probability": ctx.model_probability,
            "current_price": ctx.current_price,
            "edge": ctx.edge,
            "swarm_consensus": float(consensus) if consensus is not None else None,
            "live_model_probability": (
                live_pred["probability"] if live_pred else None
            ),
            "live_model_version": (
                live_pred["model_version"] if live_pred else None
            ),
            "live_model_name": (
                live_pred["model_name"] if live_pred else None
            ),
            "live_model_feature_count": (
                live_pred.get("feature_count") if live_pred else None
            ),
            "live_model_feature_inputs": (
                live_pred.get("feature_inputs") if live_pred else None
            ),
            # N-way pricing audit: surface the per-entrant Plackett-Luce
            # sketch in real time so dashboards can render every leg's
            # probability alongside the one this candidate is trading.
            "n_way_sketch": (
                live_pred.get("n_way_sketch") if live_pred else None
            ),
            # When the spread/total margin regressor adjusted the
            # baseline, surface its components so operators can see
            # which signal is moving the live probability.
            "margin_adjustment": (
                live_pred.get("margin_adjustment") if live_pred else None
            ),
            "baseline_probability": (
                live_pred.get("baseline_probability") if live_pred else None
            ),
        })

        async def _on_gate_event(ev: dict[str, Any]) -> None:
            await _publish({
                "type": "gate",
                "cycle_id": result.cycle_id,
                "vertical": vertical,
                "market_id": ctx.market_id,
                "side": ctx.side,
                "debate_id": ctx.debate_id,
                **ev,
            })

        passed_all, gate_results = await run_all_gates(ctx, on_event=_on_gate_event)
        if not passed_all:
            await _publish({
                "type": "candidate_complete",
                "cycle_id": result.cycle_id,
                "market_id": ctx.market_id,
                "side": ctx.side,
                "debate_id": ctx.debate_id,
                "outcome": "rejected",
                "rejecting_gate": gate_results[-1].name,
                "reason": gate_results[-1].reason,
            })
            continue

        # All 12 gates passed → place paper trade.
        try:
            # Edge Terminal input-mode exclusivity (task #844): the
            # input mode was sampled once at the top of the cycle into
            # ``_edge_terminal_active`` so we don't re-query per
            # candidate.  We still surface the decision per-candidate
            # for the dashboard's "why didn't this fire?" view.
            if _edge_terminal_active:
                results_out.append({
                    "market_id": ctx.market_id,
                    "side": ctx.side,
                    "decision": "skipped_input_mode_edge_terminal",
                    "gates": [gr.to_dict() for gr in gate_results],
                })
                continue
            kelly = next(
                (gr.details.get("kelly") for gr in gate_results if gr.name == "kelly_fraction"),
                None,
            )
            # Honor best-line gate's reroute hint (task #828) — when
            # gate_best_line discovered Polymarket has a meaningfully
            # better price than Kalshi for the same sports market it
            # sets ctx.venue_override, which overrides the default
            # venue_router pick for THIS candidate only.
            if ctx.venue_override:
                venues = list(ctx.venue_override)
            else:
                venues = route_for_candidate(cand, side=ctx.side)
            placements: list[dict[str, Any]] = []
            placement_errors: list[str] = []
            league_key = (cand.get("league") or "").upper()
            event_id_val = cand.get("event_id") or cand.get("game_id")
            from ..sports import is_sunday_window as _isw
            sunday_anchor = ctx.game_time or datetime.now(timezone.utc)
            sunday_match = bool(league_key) and _isw(league_key, sunday_anchor)
            for venue in venues:
                try:
                    pl = await place_order(
                        ctx,
                        debate_id=ctx.debate_id,
                        consensus=ctx.model_probability,
                        edge=ctx.edge,
                        kelly=kelly,
                        exchange=venue,
                        cand=cand,
                    )
                except Exception as venue_err:
                    placement_errors.append(f"{venue}: {venue_err}")
                    log.warning(
                        "place_order failed for %s on %s: %s",
                        ctx.market_id, venue, venue_err,
                    )
                    continue
                placements.append(pl)
                # Per-venue accounting: each successful placement adds a
                # *separate* position with its own notional, so risk-cap
                # aggregates must increment per placement (not per
                # candidate). Without this, fan-out to two venues would
                # double real exposure but only count as one trade —
                # letting subsequent same-cycle candidates evade caps.
                result.trades_placed += 1
                open_n += 1
                open_notional += intended_notional
                per_vertical[vertical] = (
                    per_vertical.get(vertical, 0.0) + intended_notional
                )
                if league_key:
                    per_league[league_key] = (
                        per_league.get(league_key, 0.0) + intended_notional
                    )
                    trades_today_per_league[league_key] = (
                        trades_today_per_league.get(league_key, 0) + 1
                    )
                    matchup_val = cand.get("matchup")
                    if matchup_val:
                        mkey = f"{league_key}:{matchup_val}"
                        per_matchup[mkey] = (
                            per_matchup.get(mkey, 0.0) + intended_notional
                        )
                    if event_id_val:
                        ekey = f"{league_key}:{event_id_val}"
                        per_event[ekey] = (
                            per_event.get(ekey, 0.0) + intended_notional
                        )
                    if sunday_match:
                        per_sunday_window[league_key] = (
                            per_sunday_window.get(league_key, 0.0) + intended_notional
                        )
            if not placements:
                # All venues errored — surface as a place_error so the
                # cycle records it consistently with the prior single-
                # venue behavior.
                raise RuntimeError(
                    "all venues failed: " + "; ".join(placement_errors)
                )
            # Pick a representative placement (first successful) for legacy
            # consumers that expect a single `placement` dict, but also
            # publish the full list under `placements` for venue-aware UIs.
            placement = placements[0]
            if len(placements) > 1:
                placement = {
                    **placement,
                    "venues": [p["exchange"] for p in placements],
                }
            await _publish({
                "type": "candidate_complete",
                "cycle_id": result.cycle_id,
                "market_id": ctx.market_id,
                "side": ctx.side,
                "debate_id": ctx.debate_id,
                "outcome": "placed",
                "placement": placement,
                "placements": placements,
                "venues": [p["exchange"] for p in placements],
            })
        except Exception as e:
            result.errors.append(f"place_order failed for {ctx.market_id}: {e}")
            log.warning("place_order failed: %s", e)
            await _publish({
                "type": "candidate_complete",
                "cycle_id": result.cycle_id,
                "market_id": ctx.market_id,
                "side": ctx.side,
                "debate_id": ctx.debate_id,
                "outcome": "place_error",
                "reason": str(e),
            })

    await _close_cycle(result, started)
    return result


async def _close_cycle(result: CycleResult, started: float) -> None:
    result.duration_seconds = time.perf_counter() - started
    result.completed_at = datetime.now(timezone.utc).isoformat()
    if result.cycle_id is not None:
        try:
            await db.execute(
                """
                UPDATE trading_cycles
                   SET completed_at = now(),
                       picks_evaluated = $2,
                       trades_placed = $3,
                       errors = $4
                 WHERE id = $1
                """,
                result.cycle_id,
                result.picks_evaluated,
                result.trades_placed,
                ("; ".join(result.errors))[:8000] if result.errors else None,
            )
        except Exception as e:
            log.warning("trading_cycles close failed: %s", e)
    await _publish({
        "type": "cycle_complete",
        "cycle_id": result.cycle_id,
        "vertical": result.vertical,
        "completed_at": result.completed_at,
        "duration_seconds": result.duration_seconds,
        "picks_evaluated": result.picks_evaluated,
        "trades_placed": result.trades_placed,
        "fresh_debates": result.fresh_debates,
        "errors": result.errors,
    })
