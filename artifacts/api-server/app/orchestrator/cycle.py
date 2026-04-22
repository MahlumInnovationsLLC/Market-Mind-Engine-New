"""Single-cycle execution: discover candidates → swarm → 12 gates → place."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .. import db, predictor
from ..config import get_settings
from ..risk import RiskContext, run_all_gates
from ..swarm import run_debate
from .features import build_features
from .paper import place_order
from .streams import pipeline_event_hub


log = logging.getLogger(__name__)


# Default sizing knobs — operator overrides table can override per cycle.
DEFAULT_INTENDED_QUANTITY = 10  # contracts
DEFAULT_MIN_PRICE = 0.05
DEFAULT_MAX_PRICE = 0.95


# Map verticals → list of league codes used for SQL filtering.
# `predictions.league` and `swarm_debates.league` carry the league code
# (e.g. "NBA", "NFL").  An empty list means "no filter" — used for
# verticals like 'sports' that span every supported league.  Future
# verticals (weather, crypto, politics) get explicit league prefixes
# once their feeds land.
VERTICAL_LEAGUES: dict[str, list[str]] = {
    "sports": [],  # all leagues
    "weather": ["WEATHER"],
    "crypto": ["CRYPTO"],
    "politics": ["POLITICS"],
}


def _league_filter(vertical: str) -> list[str]:
    return VERTICAL_LEAGUES.get(vertical.lower(), [])


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


async def _latest_bankroll() -> float | None:
    try:
        v = await db.fetchval(
            "SELECT balance::float FROM bankroll_snapshots ORDER BY date DESC LIMIT 1"
        )
        return float(v) if v is not None else None
    except Exception as e:
        log.debug("bankroll snapshot read failed: %s", e)
        return None


async def _open_position_aggregates() -> tuple[int, float, dict[str, float]]:
    """Returns (count, total_notional, per_vertical_notional)."""
    try:
        rows = await db.fetch(
            """
            SELECT vertical,
                   count(*)::int                                AS n,
                   coalesce(sum(quantity * avg_price), 0)::float AS notional
              FROM positions
             WHERE coalesce(status, 'open') = 'open'
             GROUP BY vertical
            """
        )
    except Exception as e:
        log.debug("open position aggregate failed: %s", e)
        return 0, 0.0, {}
    total_n = 0
    total_notional = 0.0
    per_vertical: dict[str, float] = {}
    for r in rows:
        v = (r["vertical"] or "sports")
        total_n += int(r["n"])
        total_notional += float(r["notional"])
        per_vertical[v] = per_vertical.get(v, 0.0) + float(r["notional"])
    return total_n, total_notional, per_vertical


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
                       p.home_win_prob, p.confidence, p.edge, p.game_time
                  FROM predictions p
                 WHERE p.kalshi_market_id IS NOT NULL
                   AND p.league = ANY($2)
                   AND (p.game_time IS NULL OR p.game_time > now())
                   AND NOT EXISTS (
                       SELECT 1 FROM swarm_debates d
                        WHERE d.game_id = p.game_id
                          AND d.created_at > now() - interval '15 minutes'
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
                       p.home_win_prob, p.confidence, p.edge, p.game_time
                  FROM predictions p
                 WHERE p.kalshi_market_id IS NOT NULL
                   AND (p.game_time IS NULL OR p.game_time > now())
                   AND NOT EXISTS (
                       SELECT 1 FROM swarm_debates d
                        WHERE d.game_id = p.game_id
                          AND d.created_at > now() - interval '15 minutes'
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


async def _cached_debates(vertical: str, limit: int) -> list[dict[str, Any]]:
    """Recent approved debates that haven't been traded today.

    Filters `approved = true` here (not just at gate_swarm_approval) so
    we don't churn through obviously-rejected debates inside the gate
    pipeline.  gate_swarm_approval still re-checks for safety.
    """
    leagues = _league_filter(vertical)
    try:
        if leagues:
            rows = await db.fetch(
                """
                SELECT id            AS debate_id,
                       game_id, matchup, league, market_type,
                       consensus_probability, confidence_band_low, confidence_band_high,
                       agreement_rate, approved, recommended_action,
                       game_context
                  FROM swarm_debates sd
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
                 ORDER BY sd.created_at DESC
                 LIMIT $1
                """,
                limit,
                leagues,
            )
        else:
            rows = await db.fetch(
                """
                SELECT id            AS debate_id,
                       game_id, matchup, league, market_type,
                       consensus_probability, confidence_band_low, confidence_band_high,
                       agreement_rate, approved, recommended_action,
                       game_context
                  FROM swarm_debates sd
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
                 ORDER BY sd.created_at DESC
                 LIMIT $1
                """,
                limit,
            )
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


async def _publish(event: dict[str, Any]) -> None:
    await pipeline_event_hub.publish(event)


async def _predict_for_candidate(
    cand: dict[str, Any],
    *,
    consensus: float | None,
    current_price: float | None,
    edge: float | None,
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
    fv = build_features(
        cand,
        consensus=consensus,
        current_price=current_price,
        edge=edge,
    )
    try:
        pred = await predictor.predict(fv.values)
    except predictor.PredictorUnavailable:
        return None
    except Exception as e:  # pragma: no cover — model-specific failures
        log.debug("live predict failed for %s: %s", cand.get("game_id"), e)
        return None
    payload = pred.as_dict()
    payload["feature_inputs"] = fv.present
    payload["feature_count"] = len(fv.values)
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
    # Effective trading mode honors the operator override too — same
    # contract as gate_kill_switch.
    effective_mode = (
        overrides.get("trading_mode") or settings.trading_mode or "paper"
    ).lower()
    if effective_mode == "halt":
        result.errors.append("trading_mode=halt — cycle skipped placement stage")
        await _publish({
            "type": "cycle_halted",
            "cycle_id": result.cycle_id,
            "reason": "trading_mode=halt",
        })
        await _close_cycle(result, started)
        return result

    bankroll = await _latest_bankroll()
    open_n, open_notional, per_vertical = await _open_position_aggregates()

    # 1) Cached approved debates first (cheap — no fresh LLM cost).
    cached = await _cached_debates(vertical, limit=10)
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
                "current_price": float(pred["kalshi_price"]) if pred["kalshi_price"] else None,
                "context": (
                    f"home_win_prob={pred['home_win_prob']} "
                    f"edge={pred['edge']} confidence={pred['confidence']}"
                ),
                # Persisted into swarm_debates.game_context so settlement
                # can join debates -> markets -> persona Brier updates.
                "kalshi_market_id": pred.get("kalshi_market_id"),
                "kalshi_price": pred.get("kalshi_price"),
            })
            result.fresh_debates += 1
            candidates.append({
                "debate_id": d.get("debate_id"),
                "game_id": d.get("game_id"),
                "matchup": d.get("matchup"),
                "league": d.get("league"),
                "market_type": d.get("market_type"),
                "consensus_probability": d.get("consensus_probability"),
                "confidence_band_low": d.get("confidence_band_low"),
                "confidence_band_high": d.get("confidence_band_high"),
                "agreement_rate": d.get("agreement_rate"),
                "approved": d.get("approved"),
                "recommended_action": d.get("recommended_action"),
                "game_context": {
                    "kalshi_market_id": pred.get("kalshi_market_id"),
                    "kalshi_price": pred.get("kalshi_price"),
                },
            })
        except Exception as e:
            result.errors.append(f"fresh debate failed for {pred.get('game_id')}: {e}")
            log.warning("fresh debate failed: %s", e)

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
        )
        # If swarm consensus is missing, fall back to the live model
        # probability so the gates still get a real number to chew on —
        # storing the live probability "alongside" the cached consensus
        # so the dashboard can render both per candidate.
        effective_consensus = consensus
        if effective_consensus is None and live_pred is not None:
            effective_consensus = live_pred["probability"]
        edge = pre_edge
        if edge is None and effective_consensus is not None and current_price is not None:
            edge = (
                float(effective_consensus) - float(current_price)
                if side == "YES"
                else float(current_price) - float(effective_consensus)
            )
        intended_qty = int(overrides.get("intended_quantity", DEFAULT_INTENDED_QUANTITY))
        intended_notional = intended_qty * float(current_price or 0.0)
        cooldown_until = await _market_cooldown(market_id)

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
            kelly = next(
                (gr.details.get("kelly") for gr in gate_results if gr.name == "kelly_fraction"),
                None,
            )
            placement = await place_order(
                ctx,
                debate_id=ctx.debate_id,
                consensus=ctx.model_probability,
                edge=ctx.edge,
                kelly=kelly,
            )
            result.trades_placed += 1
            # Update local aggregates so subsequent candidates in the
            # SAME cycle see the new exposure.
            open_n += 1
            open_notional += intended_notional
            per_vertical[vertical] = per_vertical.get(vertical, 0.0) + intended_notional
            await _publish({
                "type": "candidate_complete",
                "cycle_id": result.cycle_id,
                "market_id": ctx.market_id,
                "side": ctx.side,
                "debate_id": ctx.debate_id,
                "outcome": "placed",
                "placement": placement,
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
