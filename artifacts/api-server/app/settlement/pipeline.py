"""Single-trade settlement pipeline.

Atomic flow per (market_id, trade) pair:
  1. compute realized PnL (cents-based for kalshi/yes-no, value-based otherwise)
  2. UPDATE trades SET pnl, settled_at  (idempotent — skip if already set)
  3. UPDATE positions SET status='closed', closed_at, realized_pnl (idempotent)
  4. INSERT trade_postmortems        (idempotent on (trade_id, market_id))
  5. update swarm persona Brier from any matched swarm_debate
  6. accumulate vertical_daily_pnl  (UPSERT on (report_date, vertical))
  7. append calibration_snapshots  + raise model_health_alerts on drift
  8. INSERT online_learning_buffer (idempotent on trade_id)

A `CycleResult` aggregates counters for the periodic loop.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from .. import db
from ..feeds import espn_soccer
from ..sports.leagues import get_league_config
from .streams import settlement_event_hub


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Swallowed-exception accounting (task #925).
#
# Five sites in this module deliberately swallow exceptions so a flaky
# write (postmortem, online-learning buffer, position close, EMOS stamp,
# canary decrement) can never break the *settlement* of the trade
# itself.  That is still correct — settlement is the contract — but the
# previous code logged a warning and moved on, so a steady stream of
# failures was invisible to the on-call.  We now bump a per-site counter
# and emit a structured log line with enough context to grep.  Tests in
# ``tests/test_settlement_pipeline.py`` inject failures at each site and
# assert the counter advanced.
#
# These are intentionally process-local counters: settlement is a
# single-process loop, the dashboard reads via ``settlement_swallow_counts()``,
# and we don't want a transient logging glitch to take settle_market down.
# ---------------------------------------------------------------------------

_SWALLOW_SITES: tuple[str, ...] = (
    "canary_decrement",
    "postmortem_insert",
    "online_learning_buffer_insert",
    "precip_emos_stamp",
    "position_close",
)

_swallow_counts: dict[str, int] = {site: 0 for site in _SWALLOW_SITES}


def settlement_swallow_counts() -> dict[str, int]:
    """Snapshot of swallowed-exception counters keyed by site name."""
    return dict(_swallow_counts)


def _record_swallow(site: str, exc: BaseException, *, context: str = "") -> None:
    """Bump the per-site counter and emit a structured warning.

    ``site`` MUST be one of ``_SWALLOW_SITES`` — passing anything else
    is a programmer error (the counter dict would silently grow without
    appearing in the dashboard's known-key list).
    """
    if site not in _swallow_counts:  # pragma: no cover — programmer error
        log.error("settlement._record_swallow unknown site=%r", site)
        return
    _swallow_counts[site] += 1
    log.warning(
        "settlement swallowed %s exception: %s: %s%s",
        site,
        type(exc).__name__,
        exc,
        f" [{context}]" if context else "",
    )


# ---------------------------------------------------------------------------
# 3-way (soccer h/d/a) helpers
# ---------------------------------------------------------------------------

# Canonical 3-way outcome labels used across the settlement, persona
# Brier and postmortem paths.  Soccer markets ship as three independent
# YES/NO legs (home, draw, away); each leg's binary outcome is derived
# from the actual match result via `outcome_yes_for_three_way_leg`.
THREE_WAY_OUTCOMES: tuple[str, ...] = ("home", "draw", "away")


def normalize_three_way_result(result: str | None) -> str | None:
    """Lower-case and validate a 3-way match result.  Returns None when
    the input doesn't match one of the canonical outcomes so callers
    can transparently fall back to the binary path."""
    if result is None:
        return None
    r = str(result).strip().lower()
    # Accept a few common synonyms so feed integrations don't have to
    # normalise upstream.
    aliases = {
        "h": "home", "home_win": "home", "home win": "home", "1": "home",
        "d": "draw", "tie": "draw", "x": "draw",
        "a": "away", "away_win": "away", "away win": "away", "2": "away",
    }
    r = aliases.get(r, r)
    return r if r in THREE_WAY_OUTCOMES else None


def outcome_yes_for_three_way_leg(result_3way: str, leg: str) -> int:
    """Map a 3-way match result (home/draw/away) to the YES/NO outcome
    for a single leg of that market.

    Each soccer match is structured as three independent YES/NO Kalshi
    markets — the home leg pays YES iff home wins, the draw leg pays YES
    iff the match ends level, and the away leg pays YES iff away wins.
    A draw therefore settles BOTH the home and away legs as NO without
    being a "model miss" for a persona that correctly leaned toward a
    draw on the draw leg.
    """
    r = normalize_three_way_result(result_3way)
    l = normalize_three_way_result(leg)
    if r is None:
        raise ValueError(f"unknown 3-way result: {result_3way!r}")
    if l is None:
        raise ValueError(f"unknown 3-way leg: {leg!r}")
    return 1 if r == l else 0


def resolve_three_way_leg(trade: dict[str, Any]) -> str | None:
    """Best-effort extraction of which leg (home/draw/away) a soccer
    trade was placed on.  Inspects the trade's metadata jsonb first
    (canonical key: `three_way_leg`), falling back to the trade's
    `side` column when that already encodes a leg name."""
    md = trade.get("metadata")
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except Exception:
            md = None
    if isinstance(md, dict):
        for key in ("three_way_leg", "outcome_3way", "leg"):
            v = md.get(key)
            if isinstance(v, str):
                norm = normalize_three_way_result(v)
                if norm is not None:
                    return norm
    side = trade.get("side")
    if isinstance(side, str):
        return normalize_three_way_result(side)
    return None


# ---------------------------------------------------------------------------
# N-way (tennis / F1 / PGA outright) helpers
# ---------------------------------------------------------------------------


def resolve_n_way_competitor(trade: dict[str, Any]) -> dict[str, str] | None:
    """Best-effort extraction of which competitor an n-way outright
    trade was placed on.

    Mirrors :func:`resolve_three_way_leg` for combat / individual sports
    (ATP / WTA / F1 / PGA — leagues whose ``LeagueConfig.n_way_market``
    flag is ``True``).  Returns a dict carrying any combination of
    ``{"competitor_id": ..., "competitor_name": ...}`` or ``None`` when
    no per-leg identifier was stamped onto the trade's metadata jsonb.

    Inspects the trade's metadata jsonb only — unlike the three-way
    helper there's no ``side`` fallback because n-way trades all ship
    with ``side`` = "yes"/"no" and the per-competitor identity lives
    exclusively in the metadata stamp written at insert-time by
    :func:`app.orchestrator.n_way.derive_n_way_competitor`.
    """
    md = trade.get("metadata")
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except Exception:
            md = None
    if not isinstance(md, dict):
        return None
    out: dict[str, str] = {}
    for id_key in ("competitor_id", "entrant_id", "competitor", "entrant"):
        v = md.get(id_key)
        if isinstance(v, str) and v.strip():
            out["competitor_id"] = v.strip()
            break
    for name_key in ("competitor_name", "entrant_name"):
        v = md.get(name_key)
        if isinstance(v, str) and v.strip():
            out["competitor_name"] = v.strip()
            break
    return out or None


def outcome_yes_for_n_way_competitor(
    *,
    winning_competitor_id: str | None,
    winning_competitor_name: str | None,
    leg: dict[str, str],
) -> int:
    """Map an n-way race / tournament winner to the YES/NO outcome
    for a single trade's per-competitor leg.

    Each tournament-winner market is structured as one independent
    YES/NO Kalshi market per competitor — the leg pays YES iff the
    bound competitor wins, NO otherwise.  Compares on ``competitor_id``
    when both sides supply one (canonical match), falling back to a
    case-insensitive ``competitor_name`` compare so a backfilled name
    still scores even when the id is missing on either side.

    Raises ``ValueError`` when neither the winner nor the leg provides
    enough information to make a comparison — caller should treat that
    as "no per-leg outcome available" and fall back to the binary
    ``outcome_yes`` rather than guessing.
    """
    win_id = (winning_competitor_id or "").strip().lower()
    win_name = (winning_competitor_name or "").strip().lower()
    leg_id = (leg.get("competitor_id") or "").strip().lower()
    leg_name = (leg.get("competitor_name") or "").strip().lower()
    if not (win_id or win_name):
        raise ValueError("winner identity required")
    if not (leg_id or leg_name):
        raise ValueError("leg identity required")
    if win_id and leg_id:
        return 1 if win_id == leg_id else 0
    if win_name and leg_name:
        return 1 if win_name == leg_name else 0
    # Mixed-form compare: id on one side, name on the other.  Treat as
    # a non-match (NO) — we cannot prove identity across forms without
    # a join table, and an over-eager YES would mis-credit a loser.
    return 0


# ---------------------------------------------------------------------------
# Result + helpers
# ---------------------------------------------------------------------------


@dataclass
class CycleResult:
    started_at: str
    completed_at: str | None = None
    duration_seconds: float = 0.0
    markets_examined: int = 0
    markets_settled: int = 0
    trades_settled: int = 0
    postmortems_written: int = 0
    persona_metrics_updated: int = 0
    alerts_raised: int = 0
    errors: list[str] = field(default_factory=list)


async def _publish(event: dict[str, Any]) -> None:
    await settlement_event_hub.publish(event)


def _to_unit_price(price: float | None) -> float | None:
    """Kalshi prices are stored as cents (0-100) for prediction markets,
    but we also see legacy decimals (0-1).  Normalize to a 0-1 unit
    price for PnL math.  Anything > 1 is assumed cents."""
    if price is None:
        return None
    p = float(price)
    if p > 1.0:
        return p / 100.0
    return p


def _trade_pnl(side: str | None, quantity: float | None, price: float | None,
               outcome_yes: int) -> float:
    """Realized PnL on a single fill given binary outcome (1=YES wins, 0=NO wins).

    `price` is always the YES-side unit price as stored by `orchestrator/paper.py`
    (`price = ctx.current_price`).  Each contract pays $1 if its side wins.

    NO contract cost basis is therefore `q * (1 - yes_price)`, NOT `q * yes_price`.
    """
    if quantity is None or price is None:
        return 0.0
    q = float(quantity)
    yes_p = _to_unit_price(price) or 0.0
    side_lc = (side or "").lower()
    if side_lc in ("yes", "y", "buy"):
        cost = q * yes_p
        payout = q * (1.0 if outcome_yes == 1 else 0.0)
    elif side_lc in ("no", "n", "sell"):
        cost = q * (1.0 - yes_p)
        payout = q * (1.0 if outcome_yes == 0 else 0.0)
    else:
        return 0.0
    return round(payout - cost, 6)


# ---------------------------------------------------------------------------
# Trade-level settlement
# ---------------------------------------------------------------------------


async def _settle_one_trade(
    trade: dict[str, Any], outcome_yes: int, settle_price: float | None,
    *, result_3way: str | None = None, leg: str | None = None,
    n_way_winner: dict[str, str] | None = None,
    n_way_competitor: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Settle a single un-settled trade row.  Returns a result dict.

    `result_3way`/`leg` carry the soccer h/d/a context when the trade
    belongs to a 3-way market — they are written into the postmortem
    details so downstream analytics can distinguish a draw outcome on
    a home/away leg from a "model miss" on a true 2-way moneyline.

    `n_way_winner`/`n_way_competitor` carry the analogous context for
    n-way outright markets (ATP / WTA / F1 / PGA): the winning entrant
    and the per-trade competitor leg — also written into the postmortem
    so a losing-competitor leg is stored as a deterministic NO rather
    than a "model miss" against a binary moneyline frame.
    """
    tid = trade["id"]
    pnl = _trade_pnl(trade.get("side"), trade.get("quantity"), trade.get("price"),
                     outcome_yes)
    # `RETURNING id` lets us tell whether this UPDATE actually
    # transitioned the trade from unsettled → settled.  A duplicate
    # settlement (replay, retry, race between the live websocket and
    # the polling fallback) hits the `settled_at IS NULL` predicate as
    # a no-op and returns no row; we use that signal below to skip the
    # canary decrement so a single canary trade can't drain more than
    # one slot from the counter.
    try:
        first_settle_row = await db.fetchrow(
            """
            UPDATE trades
               SET pnl = $2,
                   settled_at = now()
             WHERE id = $1
               AND settled_at IS NULL
             RETURNING id
            """,
            tid, pnl,
        )
    except Exception as e:
        return {"trade_id": tid, "ok": False, "error": f"trade update: {e}"}

    is_first_settle = first_settle_row is not None

    # Canary counter accounting (Task #752 step 5).  Decrement is
    # idempotent-on-noop and swallows DB errors internally — a flaky
    # ``system_settings`` write must not block the settlement of an
    # otherwise-clean trade.  Only canary-tagged live trades trigger
    # the decrement; everything else is a fast metadata-shape check.
    # We *also* gate on the first-settle flag so a duplicate settle of
    # the same canary trade can't drain two slots.
    if is_first_settle:
        try:
            from ..risk import canary as _canary
            await _canary.decrement_on_settle(trade)
        except Exception as e:  # pragma: no cover — defensive
            _record_swallow("canary_decrement", e, context=f"trade_id={tid}")

    # Post-mortem (idempotent on trade_id — relies on the
    # `trade_postmortems_trade_id_key` unique constraint so concurrent
    # settlement re-runs collapse silently instead of racing the
    # pre-check and emitting duplicate-key warnings).
    try:
        await db.execute(
            """
            INSERT INTO trade_postmortems (
                trade_id, market_id, league, market_type, vertical,
                error_category, predicted_prob, actual_outcome, model_edge,
                pnl, hour_of_day, day_of_week, severity, details
            ) VALUES (
                $1, $2, $3, $4, $5,
                $6, $7, $8, $9,
                $10, $11, $12, $13, $14::jsonb
            )
            ON CONFLICT ON CONSTRAINT trade_postmortems_trade_id_key DO NOTHING
            """,
            trade.get("trade_id") or str(tid),
            trade.get("market_id"),
            trade.get("league"),
            (trade.get("metadata") or {}).get("market_type") if isinstance(trade.get("metadata"), dict) else None,
            trade.get("vertical"),
            "win" if pnl > 0 else ("loss" if pnl < 0 else "flat"),
            trade.get("model_prob"),
            outcome_yes,
            trade.get("model_edge") or trade.get("edge"),
            pnl,
            datetime.now(timezone.utc).hour,
            datetime.now(timezone.utc).weekday(),
            abs(pnl),
            json.dumps({
                "settle_price": settle_price,
                "side": trade.get("side"),
                "quantity": float(trade.get("quantity") or 0),
                "price": float(trade.get("price") or 0),
                "exchange": trade.get("exchange"),
                "trading_mode": trade.get("trading_mode"),
                # 3-way h/d/a context — present only for soccer-vertical
                # trades; null otherwise so legacy 2-way analytics keep
                # working unchanged.
                "three_way_result": result_3way,
                "three_way_leg": leg,
                # N-way (tennis / F1 / PGA outright) context — present
                # only for combat / individual-sport trades; null
                # otherwise so legacy analytics keep working unchanged.
                "n_way_winner_id": (n_way_winner or {}).get("competitor_id"),
                "n_way_winner_name": (n_way_winner or {}).get("competitor_name"),
                "n_way_competitor_id": (n_way_competitor or {}).get("competitor_id"),
                "n_way_competitor_name": (n_way_competitor or {}).get("competitor_name"),
            }),
        )
    except Exception as e:
        _record_swallow("postmortem_insert", e, context=f"trade_id={tid}")

    # Online learning buffer (idempotent on trade_id — relies on the
    # `online_learning_buffer_pkey` unique constraint so settlement
    # re-runs no longer race the pre-check and emit duplicate-key
    # warnings for the same trade).
    try:
        await db.execute(
            """
            INSERT INTO online_learning_buffer (
                trade_id, market_id, league, vertical, actual_outcome,
                predicted_prob, edge_at_entry, pnl, has_full_features, processed
            ) VALUES (
                $1, $2, $3, $4, $5,
                $6, $7, $8, $9, false
            )
            ON CONFLICT ON CONSTRAINT online_learning_buffer_pkey DO NOTHING
            """,
            trade.get("trade_id") or str(tid),
            trade.get("market_id"),
            trade.get("league"),
            trade.get("vertical"),
            outcome_yes,
            trade.get("model_prob"),
            trade.get("model_edge") or trade.get("edge"),
            pnl,
            trade.get("model_prob") is not None,
        )
    except Exception as e:
        _record_swallow(
            "online_learning_buffer_insert", e, context=f"trade_id={tid}",
        )

    return {
        "trade_id": tid,
        "ok": True,
        "pnl": pnl,
        "outcome_yes": outcome_yes,
    }


async def _stamp_precip_emos_observation(
    market_id: str, outcome_yes: int,
) -> None:
    """Stamp the realized YES/NO outcome onto a KXRAIN observation row.

    The predictor (``app.weather.precip_predictor``) snapshots the
    ensemble mean / variance into ``precip_emos_observations`` at
    trade-creation time. At settlement we close the loop by marking
    the row settled with the canonical Kalshi YES outcome — the EMOS
    refit fitter handles the above/below direction flip itself, so
    here we just record the raw market-level outcome.

    Best-effort: this table only feeds the offline refit loop and the
    settlement path must never fail because of an observability write.
    The UPDATE is a no-op for any market we never logged a prediction
    for (older markets, non-KXRAIN, etc.) — the WHERE clause below
    only matches actual KXRAIN observation rows.
    """
    try:
        await db.execute(
            """
            UPDATE precip_emos_observations
               SET actual_outcome_yes = $2,
                   settled_at         = COALESCE(settled_at, now()),
                   updated_at         = now()
             WHERE market_id = $1
            """,
            market_id, int(outcome_yes),
        )
    except Exception as e:
        _record_swallow(
            "precip_emos_stamp", e, context=f"market_id={market_id}",
        )


async def _close_position(market_id: str, outcome_yes: int) -> None:
    """Mark all open positions on this market as closed."""
    try:
        await db.execute(
            """
            UPDATE positions
               SET status = 'closed',
                   closed_at = now(),
                   current_price = $2
             WHERE market_id = $1
               AND coalesce(status, 'open') = 'open'
            """,
            market_id, float(outcome_yes),
        )
    except Exception as e:
        _record_swallow(
            "position_close", e, context=f"market_id={market_id}",
        )


# ---------------------------------------------------------------------------
# Persona Brier update from any swarm debate that produced this trade
# ---------------------------------------------------------------------------


async def _update_personas_for_market(
    market_id: str, outcome_yes: int, *, result_3way: str | None = None,
) -> int:
    """For every recent swarm_debate associated with this market that has
    NOT already been settled, update `swarm_analyst_metrics` (running
    Brier + accuracy) per persona using a Bayesian online update:
    new_brier = (n*old_brier + (p - y)^2) / (n+1).

    Per-debate idempotency: the `swarm_debates.outcome IS NULL` filter
    guarantees re-running settle_market on the same market never
    double-counts persona predictions.  Returns the number of
    (persona, league, market_type) rows touched.
    """
    try:
        debates = await db.fetch(
            """
            SELECT id, league, market_type, analyst_verdicts, outcome,
                   game_context
              FROM swarm_debates
             WHERE ((game_context->>'kalshi_market_id') = $1 OR game_id = $1)
               AND outcome IS NULL
             ORDER BY created_at DESC
             LIMIT 5
            """,
            market_id,
        )
    except Exception as e:
        log.debug("debate lookup failed for %s: %s", market_id, e)
        return 0

    touched = 0
    # Stamp a richer label on 3-way markets so per-persona analytics
    # can break out draw outcomes from straight WIN/LOSS rows.  The
    # binary Brier update below is unchanged — a persona that leaned
    # NO on the home leg and saw a draw is already correctly rewarded
    # with a tight squared-error against y=0 — but the label lets the
    # operator slice persona accuracy by 3-way result class without
    # joining back to trade_postmortems.
    if result_3way is not None:
        outcome_label = (
            f"{result_3way.upper()}_RESULT_{'WIN' if outcome_yes == 1 else 'LOSS'}"
        )
    else:
        outcome_label = "WIN" if outcome_yes == 1 else "LOSS"
    for d in debates:
        verdicts = d["analyst_verdicts"]
        if isinstance(verdicts, str):
            try:
                verdicts = json.loads(verdicts)
            except Exception:
                continue
        if not isinstance(verdicts, list):
            continue
        # Atomic claim: stamp outcome only if still NULL.  If 0 rows
        # were updated, another worker already processed this debate
        # and we must NOT update persona metrics for it again.
        try:
            stamped = await db.execute(
                "UPDATE swarm_debates SET outcome=$2 WHERE id=$1 AND outcome IS NULL",
                d["id"], outcome_label,
            )
            # asyncpg returns "UPDATE n" — if n==0, skip persona updates.
            if isinstance(stamped, str) and stamped.endswith(" 0"):
                continue
        except Exception as e:
            log.debug("debate outcome stamp failed: %s", e)
            continue

        league = d["league"] or "ALL"
        mtype = d["market_type"] or "moneyline"
        # Per task #194 the persona Brier table is scoped per-vertical
        # so a sports-Brier history doesn't leak into a crypto cycle.
        # The vertical is recorded on the debate's game_context jsonb;
        # legacy rows missing the field are treated as 'sports' which
        # matches the migration 0005 backfill.
        gctx = d["game_context"]
        if isinstance(gctx, str):
            try:
                gctx = json.loads(gctx)
            except Exception:
                gctx = None
        debate_vertical = "sports"
        if isinstance(gctx, dict):
            v_raw = gctx.get("vertical")
            if isinstance(v_raw, str) and v_raw.strip():
                debate_vertical = v_raw.strip().lower()
        for v in verdicts:
            if not isinstance(v, dict):
                continue
            # Live debates (swarm/runner.py) write the key as "persona";
            # legacy/cold-storage debates use "analyst_id".  Accept both
            # so settlement updates work across the entire history.
            persona_id = v.get("persona") or v.get("analyst_id")
            prob = v.get("probability")
            conf = v.get("confidence")
            if not persona_id or prob is None:
                continue
            try:
                prob_f = float(prob)
                # Brier squared error against actual binary outcome.
                sq_err = (prob_f - float(outcome_yes)) ** 2
                # Persona was directionally correct iff their probability
                # for YES > 0.5 matches the actual YES outcome.
                directional = 1 if (prob_f >= 0.5) == (outcome_yes == 1) else 0
                conf_f = float(conf) if conf is not None else None
                # UPSERT-via-merge: read existing row, compute weighted update.
                existing = await db.fetchrow(
                    """
                    SELECT id, total_predictions, correct_predictions, brier_score,
                           directional_accuracy, avg_confidence
                      FROM swarm_analyst_metrics
                     WHERE analyst_id = $1
                       AND coalesce(vertical, 'sports') = $4
                       AND league = $2
                       AND market_type = $3
                       AND coalesce(home_away, 'all') = 'all'
                       AND coalesce(spread_bucket, 'all') = 'all'
                       AND coalesce(injury_context, 'all') = 'all'
                     LIMIT 1
                    """,
                    persona_id, league, mtype, debate_vertical,
                )
                if existing is None:
                    await db.execute(
                        """
                        INSERT INTO swarm_analyst_metrics
                            (analyst_id, vertical, league, market_type, home_away,
                             spread_bucket, injury_context,
                             total_predictions, correct_predictions,
                             brier_score, directional_accuracy, avg_confidence,
                             last_updated)
                        VALUES ($1, $8, $2, $3, 'all', 'all', 'all',
                                1, $4, $5, $6, $7, now())
                        """,
                        persona_id, league, mtype,
                        directional, sq_err, float(directional), conf_f or 0.0,
                        debate_vertical,
                    )
                    touched += 1
                else:
                    n_old = int(existing["total_predictions"] or 0)
                    n_new = n_old + 1
                    correct_old = int(existing["correct_predictions"] or 0)
                    brier_old = float(existing["brier_score"] or 0.0)
                    acc_old = float(existing["directional_accuracy"] or 0.0)
                    conf_old = float(existing["avg_confidence"] or 0.0)
                    brier_new = (n_old * brier_old + sq_err) / n_new
                    acc_new = (n_old * acc_old + directional) / n_new
                    conf_new = (
                        (n_old * conf_old + (conf_f or 0.0)) / n_new
                        if conf_f is not None
                        else conf_old
                    )
                    await db.execute(
                        """
                        UPDATE swarm_analyst_metrics
                           SET total_predictions   = $2,
                               correct_predictions = $3,
                               brier_score         = $4,
                               directional_accuracy= $5,
                               avg_confidence      = $6,
                               last_updated        = now()
                         WHERE id = $1
                        """,
                        existing["id"], n_new, correct_old + directional,
                        brier_new, acc_new, conf_new,
                    )
                    touched += 1
            except Exception as e:
                log.debug("persona metric update failed (%s): %s", persona_id, e)
    return touched


# ---------------------------------------------------------------------------
# Daily PnL aggregation
# ---------------------------------------------------------------------------


async def _accumulate_daily_pnl(vertical: str | None) -> None:
    """Recompute today's row for the given vertical from `trades` and
    UPSERT into `vertical_daily_pnl`.  Cheap because it only scans
    today's trades."""
    if not vertical:
        vertical = "sports"
    today = datetime.now(timezone.utc).date()
    try:
        agg = await db.fetchrow(
            """
            SELECT
              count(*)::int                                         AS n,
              count(*) FILTER (WHERE pnl > 0)::int                  AS wins,
              count(*) FILTER (WHERE pnl <= 0)::int                 AS losses,
              coalesce(sum(pnl), 0)::float                          AS pnl,
              coalesce(avg(coalesce(model_edge, edge)), 0)::float   AS avg_edge
              FROM trades
             WHERE vertical = $1
               AND ((settled_at AT TIME ZONE 'UTC')::date = $2
                    OR (executed_at AT TIME ZONE 'UTC')::date = $2)
               AND pnl IS NOT NULL
            """,
            vertical, today,
        )
        n = int(agg["n"] or 0)
        if n == 0:
            return
        win_rate = (float(agg["wins"]) / n) * 100.0 if n else 0.0
        # cumulative_pnl = previous day's cumulative + today's pnl
        prev_cum = await db.fetchval(
            """
            SELECT cumulative_pnl::float FROM vertical_daily_pnl
             WHERE vertical = $1 AND report_date < $2
             ORDER BY report_date DESC LIMIT 1
            """,
            vertical, today,
        )
        cumulative = float(prev_cum or 0.0) + float(agg["pnl"])
        # UPSERT — match production schema (no unique index, so check first)
        existing_id = await db.fetchval(
            "SELECT id FROM vertical_daily_pnl WHERE vertical=$1 AND report_date=$2 LIMIT 1",
            vertical, today,
        )
        if existing_id is None:
            await db.execute(
                """
                INSERT INTO vertical_daily_pnl
                    (report_date, vertical, trades_count, wins, losses,
                     daily_pnl, cumulative_pnl, win_rate, avg_edge)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                today, vertical, n, int(agg["wins"]), int(agg["losses"]),
                float(agg["pnl"]), cumulative, win_rate, float(agg["avg_edge"]),
            )
        else:
            await db.execute(
                """
                UPDATE vertical_daily_pnl
                   SET trades_count   = $2,
                       wins           = $3,
                       losses         = $4,
                       daily_pnl      = $5,
                       cumulative_pnl = $6,
                       win_rate       = $7,
                       avg_edge       = $8
                 WHERE id = $1
                """,
                existing_id, n, int(agg["wins"]), int(agg["losses"]),
                float(agg["pnl"]), cumulative, win_rate, float(agg["avg_edge"]),
            )
    except Exception as e:
        log.warning("daily pnl accumulate failed for %s: %s", vertical, e)


# ---------------------------------------------------------------------------
# Calibration snapshot + drift alert
# ---------------------------------------------------------------------------


# Brier above this is "drift" warning territory; > MCE_HARD => critical.
BRIER_DRIFT_WARN = 0.27
BRIER_DRIFT_CRIT = 0.32


async def _calibration_and_drift(vertical: str | None) -> int:
    """Compute rolling Brier on settled trades for this vertical against
    each trade's *true market outcome* (sourced from `trade_postmortems.actual_outcome`,
    which is the YES/NO resolution we just wrote — NOT trade profitability),
    append a `calibration_snapshots` row, and raise a `model_health_alerts`
    row if the Brier exceeds drift thresholds.  Returns # alerts raised.

    For YES-side trades the prediction probability is `model_prob` directly.
    For NO-side  trades it is `1 - model_prob`, since `model_prob` always
    represents P(YES) per the gatekeeper convention.
    """
    if not vertical:
        return 0
    try:
        agg = await db.fetchrow(
            """
            WITH settled AS (
              SELECT
                t.model_prob::float                 AS p_yes,
                lower(coalesce(t.side, ''))         AS side_lc,
                pm.actual_outcome::int              AS y
                FROM trades t
                JOIN trade_postmortems pm
                  ON pm.trade_id = coalesce(t.trade_id, t.id::text)
               WHERE t.vertical = $1
                 AND t.settled_at IS NOT NULL
                 AND t.settled_at > now() - interval '24 hours'
                 AND t.model_prob IS NOT NULL
                 AND pm.actual_outcome IS NOT NULL
            ), aligned AS (
              SELECT
                CASE WHEN side_lc IN ('no','n','sell') THEN 1.0 - p_yes ELSE p_yes END AS p_side,
                CASE WHEN side_lc IN ('no','n','sell') THEN 1 - y       ELSE y       END AS y_side
                FROM settled
            )
            SELECT count(*)::int                                 AS n,
                   avg((p_side - y_side) * (p_side - y_side))::float AS brier,
                   avg(abs(p_side - y_side))::float                  AS mae
              FROM aligned
            """,
            vertical,
        )
        n = int(agg["n"] or 0)
        if n == 0:
            return 0
        brier = float(agg["brier"] or 0.0)
        mae = float(agg["mae"] or 0.0)
        await db.execute(
            """
            INSERT INTO calibration_snapshots
                (vertical, sub_category, ece, mce, brier_score, reliability,
                 resolution, sample_count, calibrator_state, drift_status, config)
            VALUES ($1, '24h', $2, $3, $4, NULL, NULL, $5,
                    '{}'::jsonb,
                    jsonb_build_object('brier_warn', $6::float, 'brier_crit', $7::float),
                    '{}'::jsonb)
            """,
            vertical, mae, mae, brier, n, BRIER_DRIFT_WARN, BRIER_DRIFT_CRIT,
        )
    except Exception as e:
        log.warning("calibration snapshot failed for %s: %s", vertical, e)
        return 0

    raised = 0
    if brier >= BRIER_DRIFT_CRIT:
        sev, thr = "critical", BRIER_DRIFT_CRIT
    elif brier >= BRIER_DRIFT_WARN:
        sev, thr = "warning", BRIER_DRIFT_WARN
    else:
        return 0
    # Avoid alert spam: only raise if no unack'd brier_drift alert in last hour.
    try:
        recent = await db.fetchval(
            """
            SELECT 1 FROM model_health_alerts
             WHERE vertical = $1 AND alert_type = 'brier_drift'
               AND created_at > now() - interval '1 hour'
             LIMIT 1
            """,
            vertical,
        )
        if not recent:
            msg = f"{vertical} 24h Brier {brier:.3f} >= {thr:.3f} ({sev})"
            await db.execute(
                """
                INSERT INTO model_health_alerts
                    (vertical, alert_type, severity, metric_name, metric_value,
                     threshold, message, acknowledged)
                VALUES ($1, 'brier_drift', $2, 'brier_score', $3, $4, $5, false)
                ON CONFLICT (id) DO NOTHING
                """,
                vertical, sev, brier, thr, msg,
            )
            raised = 1
            # Email the operator about the drift event.  Best-effort.
            try:
                from ..notifications import alerts as _alerts
                # Persistent dedupe survives settlement re-runs of the
                # same vertical at the same severity within the hour.
                # The model_health_alerts row insert above is the
                # primary suppressor, but a settle-re-run that crosses
                # the hour boundary would otherwise fire a duplicate.
                if await _alerts.dedupe_or_skip(
                    "drift", f"{vertical}:{sev}", ttl_seconds=3600
                ):
                    _alerts.drift(
                        vertical=vertical,
                        metric="brier_score_24h",
                        value=brier,
                        threshold=thr,
                        note=f"severity={sev}; {msg}",
                    )
            except Exception as e:  # pragma: no cover
                log.warning("drift alert dispatch failed: %s", e)
    except Exception as e:
        log.warning("alert insert failed for %s: %s", vertical, e)
    return raised


async def _calibration_and_drift_shadow(vertical: str | None) -> int:
    """Same 24h Brier drift computation as :func:`_calibration_and_drift`,
    but driven by ``shadow_learning_buffer`` rather than the live
    trades x postmortems join.

    The shadow loop (task #753) snapshots the model's calibrated YES
    probability for *every* active Kalshi market the orchestrator
    considered, so this helper measures drift on the unbiased
    universe — catching regressions even when no trade was placed.

    Alerts raised here use the same ``alert_type='brier_drift'`` so
    the existing retrain consumer dedupes naturally, but the message
    is prefixed ``[shadow]`` so an operator skimming the alert feed
    can immediately distinguish "trades drifting" from "shadow
    universe drifting" — the latter often catches regressions a few
    hours earlier.

    The snapshot row carries ``config.source = 'shadow'`` so the
    operator dashboard's calibration history doesn't get confused
    when both sources fire snapshots within the same window.
    """
    if not vertical:
        return 0
    # Preflight: a fresh dev DB without migration 0043 wouldn't have
    # the buffer table.  ``to_regclass`` returns NULL when the table
    # is missing.  We MUST gate at the Python layer (not in WHERE)
    # because Postgres resolves FROM-clause relations at parse time.
    try:
        present = await db.fetchval(
            "SELECT to_regclass('public.shadow_learning_buffer') IS NOT NULL"
        )
    except Exception as e:
        log.warning("shadow drift preflight failed for %s: %s", vertical, e)
        return 0
    if not present:
        return 0

    try:
        agg = await db.fetchrow(
            """
            WITH settled AS (
              SELECT predicted_prob::float AS p_side,
                     actual_outcome::int   AS y_side
                FROM shadow_learning_buffer
               WHERE vertical = $1
                 AND settled_at IS NOT NULL
                 AND settled_at > now() - interval '24 hours'
                 AND predicted_prob IS NOT NULL
                 AND actual_outcome IS NOT NULL
            )
            SELECT count(*)::int                                    AS n,
                   avg((p_side - y_side) * (p_side - y_side))::float AS brier,
                   avg(abs(p_side - y_side))::float                  AS mae
              FROM settled
            """,
            vertical,
        )
        n = int(agg["n"] or 0)
        if n == 0:
            return 0
        brier = float(agg["brier"] or 0.0)
        mae = float(agg["mae"] or 0.0)
        await db.execute(
            """
            INSERT INTO calibration_snapshots
                (vertical, sub_category, ece, mce, brier_score, reliability,
                 resolution, sample_count, calibrator_state, drift_status, config)
            VALUES ($1, '24h_shadow', $2, $3, $4, NULL, NULL, $5,
                    '{}'::jsonb,
                    jsonb_build_object('brier_warn', $6::float,
                                       'brier_crit', $7::float),
                    jsonb_build_object('source', 'shadow'))
            """,
            vertical, mae, mae, brier, n, BRIER_DRIFT_WARN, BRIER_DRIFT_CRIT,
        )
    except Exception as e:
        log.warning("shadow calibration snapshot failed for %s: %s", vertical, e)
        return 0

    if brier >= BRIER_DRIFT_CRIT:
        sev, thr = "critical", BRIER_DRIFT_CRIT
    elif brier >= BRIER_DRIFT_WARN:
        sev, thr = "warning", BRIER_DRIFT_WARN
    else:
        return 0
    raised = 0
    # Dedupe across both shadow- and trade-driven alerts: we look up
    # any unack'd brier_drift in the last hour for this vertical.
    # The retrain consumer keys on (vertical, alert_type), so two
    # back-to-back alerts (one shadow, one trade) would trigger two
    # retrains — undesirable.  The trade path's identical guard at
    # line 794 ensures whichever path fires first wins the hour.
    try:
        recent = await db.fetchval(
            """
            SELECT 1 FROM model_health_alerts
             WHERE vertical = $1 AND alert_type = 'brier_drift'
               AND created_at > now() - interval '1 hour'
             LIMIT 1
            """,
            vertical,
        )
        if not recent:
            msg = (
                f"[shadow] {vertical} 24h Brier {brier:.3f} "
                f">= {thr:.3f} ({sev})"
            )
            await db.execute(
                """
                INSERT INTO model_health_alerts
                    (vertical, alert_type, severity, metric_name, metric_value,
                     threshold, message, acknowledged)
                VALUES ($1, 'brier_drift', $2, 'brier_score_shadow', $3, $4, $5, false)
                ON CONFLICT (id) DO NOTHING
                """,
                vertical, sev, brier, thr, msg,
            )
            raised = 1
            try:
                from ..notifications import alerts as _alerts
                if await _alerts.dedupe_or_skip(
                    "drift", f"shadow:{vertical}:{sev}", ttl_seconds=3600
                ):
                    _alerts.drift(
                        vertical=f"{vertical} (shadow)",
                        metric="brier_score_24h_shadow",
                        value=brier,
                        threshold=thr,
                        note=f"severity={sev}; {msg}",
                    )
            except Exception as e:  # pragma: no cover
                log.warning("shadow drift alert dispatch failed: %s", e)
    except Exception as e:
        log.warning("shadow alert insert failed for %s: %s", vertical, e)
    return raised


# ---------------------------------------------------------------------------
# Public: settle a market end-to-end
# ---------------------------------------------------------------------------


async def settle_market(
    market_id: str, *, outcome_yes: int, settle_price: float | None = None,
    result_3way: str | None = None,
    n_way_winner: dict[str, str] | None = None,
    match_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Settle every un-settled trade against `market_id` with the given
    binary outcome.  Cascades through positions, postmortems, persona
    metrics, daily PnL, calibration, and the online learning buffer.

    `outcome_yes` is the canonical per-leg binary outcome the Kalshi
    market resolved to (1=YES, 0=NO).  For soccer / cup competitions
    that ship as 3-way h/d/a markets, callers may additionally pass
    `result_3way` ("home" / "draw" / "away") so each trade's per-leg
    outcome is derived from the actual match result + the trade's
    `three_way_leg` metadata — a draw correctly settles BOTH the home
    and away legs as NO without being treated as a 2-way moneyline
    miss in the persona Brier path.  When the league config does NOT
    flag the market as 3-way, `result_3way` is ignored and the binary
    `outcome_yes` is used unchanged for every trade (legacy behaviour).

    For ATP / WTA / F1 / PGA outright markets (leagues whose
    `n_way_market` flag is True), callers may pass `n_way_winner` —
    a ``{"competitor_id": ..., "competitor_name": ...}`` dict naming
    the actual race / tournament winner.  Each trade's per-leg outcome
    is then derived from the trade's stamped `competitor_id` (see
    :func:`resolve_n_way_competitor`) so a winner outcome correctly
    settles the winning leg as YES and every losing-competitor leg as
    NO without treating the latter as model misses.  When the league
    is not flagged n-way OR no winner is supplied, the binary
    `outcome_yes` is used unchanged.

    Returns a result dict the API endpoint can serialize.
    """
    if outcome_yes not in (0, 1):
        raise ValueError("outcome_yes must be 0 or 1")
    result_3way = normalize_three_way_result(result_3way)
    n_way_winner = (
        {k: v for k, v in n_way_winner.items() if isinstance(v, str) and v.strip()}
        if isinstance(n_way_winner, dict)
        else None
    ) or None

    await _publish({
        "type": "market_settle_start",
        "market_id": market_id,
        "outcome_yes": outcome_yes,
        "result_3way": result_3way,
        "n_way_winner": n_way_winner,
    })

    try:
        trades = await db.fetch(
            """
            SELECT id, trade_id, market_id, vertical, league, side, action,
                   quantity::float AS quantity, price::float AS price,
                   model_prob, model_edge, edge, exchange, trading_mode,
                   metadata
              FROM trades
             WHERE market_id = $1
               AND settled_at IS NULL
            """,
            market_id,
        )
    except Exception as e:
        await _publish({
            "type": "market_settle_error", "market_id": market_id, "error": str(e),
        })
        raise

    settled: list[dict[str, Any]] = []
    verticals: set[str] = set()
    # Aggregate position-level P&L across every trade row that resolved
    # this market so we can fire a single settlement push per position
    # (task #132) instead of one per partial fill.
    position_pnl = 0.0
    position_vertical: str | None = None
    any_trade_settled = False
    # Track per-trade legs so the persona Brier path can score each
    # debate against the actual per-leg outcome on 3-way markets.
    trade_outcomes: dict[Any, int] = {}
    for t in trades:
        td = dict(t)
        # Per-trade outcome derivation: when the league is configured
        # as a 3-way market AND the caller supplied a 3-way result, use
        # the trade's leg metadata to decide whether THIS leg pays out.
        # Falls back to the binary `outcome_yes` for any trade whose
        # leg can't be resolved (preserves legacy 2-way behaviour for
        # rows persisted before the leg metadata convention landed).
        trade_leg: str | None = None
        trade_competitor: dict[str, str] | None = None
        trade_outcome_yes = outcome_yes
        cfg = get_league_config(td.get("league"))
        if result_3way is not None and cfg is not None and cfg.three_way_market:
            trade_leg = resolve_three_way_leg(td)
            if trade_leg is not None:
                try:
                    trade_outcome_yes = outcome_yes_for_three_way_leg(
                        result_3way, trade_leg
                    )
                except ValueError:
                    # Defensive — normalize_three_way_result already
                    # validated `result_3way`, so this can only fire
                    # if `trade_leg` is malformed.  Fall back to the
                    # binary outcome rather than skipping the trade.
                    trade_outcome_yes = outcome_yes
        # Same shape for n-way outright markets (task #616): when the
        # league is flagged `n_way_market` AND the caller supplied a
        # winner, score this trade's leg against the actual winner.
        elif (
            n_way_winner is not None
            and cfg is not None
            and cfg.n_way_market
        ):
            trade_competitor = resolve_n_way_competitor(td)
            if trade_competitor is not None:
                try:
                    trade_outcome_yes = outcome_yes_for_n_way_competitor(
                        winning_competitor_id=n_way_winner.get("competitor_id"),
                        winning_competitor_name=n_way_winner.get("competitor_name"),
                        leg=trade_competitor,
                    )
                except ValueError:
                    # Defensive — caller-supplied winner had no id /
                    # name; fall back to the binary outcome rather than
                    # skipping the trade.
                    trade_outcome_yes = outcome_yes
        res = await _settle_one_trade(
            td, trade_outcome_yes, settle_price,
            result_3way=result_3way, leg=trade_leg,
            n_way_winner=n_way_winner, n_way_competitor=trade_competitor,
        )
        trade_outcomes[td.get("id")] = trade_outcome_yes
        settled.append(res)
        if td.get("vertical"):
            verticals.add(td["vertical"])
            if position_vertical is None:
                position_vertical = td["vertical"]
        if res.get("ok"):
            any_trade_settled = True
            try:
                position_pnl += float(res.get("pnl") or 0.0)
            except (TypeError, ValueError):
                pass
        await _publish({
            "type": "trade_settled",
            "market_id": market_id,
            "trade_id": td["id"],
            **{k: v for k, v in res.items() if k != "trade_id"},
        })

    # Mobile push: one notification per resolved position, carrying the
    # aggregated realized P&L. Best-effort — settlement must never fail
    # because the push channel is degraded.
    if any_trade_settled:
        try:
            from ..notifications import push as _push
            _push.push_settlement(
                market_ticker=market_id,
                vertical=position_vertical,
                pnl=position_pnl,
                outcome_yes=outcome_yes,
            )
        except Exception as e:
            log.warning("settlement push dispatch failed for market %s: %s",
                        market_id, e)

    # Per-leg persona outcome: when this market is one leg of a 3-way
    # soccer fixture and the caller supplied `result_3way`, the persona
    # Brier must be scored against the per-leg outcome derived from the
    # actual match result — NOT the binary `outcome_yes` the caller may
    # have passed (which a non-3-way-aware caller can mis-set).  Every
    # trade on a single Kalshi market belongs to the same leg, so we
    # take the most common per-trade outcome from `trade_outcomes` and
    # use that as the canonical per-leg outcome for persona scoring.
    persona_outcome_yes = outcome_yes
    if result_3way is not None and trade_outcomes:
        outcome_counts: dict[int, int] = {}
        for v in trade_outcomes.values():
            outcome_counts[v] = outcome_counts.get(v, 0) + 1
        # mode() picks the most-frequent value; ties resolve to YES (1)
        # so a market with mixed legs (which shouldn't happen but might
        # under data drift) errs on the safer side of recording a WIN.
        persona_outcome_yes = max(outcome_counts.items(), key=lambda kv: (kv[1], kv[0]))[0]
    # Close the EMOS observation loop for KXRAIN markets — see
    # task #280. This UPDATE is a no-op for any market the predictor
    # never logged, so it's safe to call unconditionally and avoids
    # leaking the KXRAIN ticker shape into the settlement path.
    await _stamp_precip_emos_observation(market_id, outcome_yes)
    await _close_position(market_id, persona_outcome_yes)
    persona_touched = await _update_personas_for_market(
        market_id, persona_outcome_yes, result_3way=result_3way,
    )
    # If we updated any persona metrics, refresh the in-memory weight
    # cache so the very next swarm debate reflects the new Brier scores.
    # Without this the gatekeeper would keep using startup-time weights
    # until the process restarts — i.e. no live online learning.
    if persona_touched > 0:
        try:
            from ..swarm import refresh_persona_weights
            await refresh_persona_weights()
        except Exception as e:
            log.warning("persona weight cache refresh failed: %s", e)
    alerts_raised = 0
    for v in verticals:
        await _accumulate_daily_pnl(v)
        alerts_raised += await _calibration_and_drift(v)

    result = {
        "market_id": market_id,
        "outcome_yes": outcome_yes,
        "result_3way": result_3way,
        "n_way_winner": n_way_winner,
        "trades_settled": len([s for s in settled if s.get("ok")]),
        "persona_metrics_updated": persona_touched,
        "verticals": sorted(verticals),
        "alerts_raised": alerts_raised,
        "details": settled,
    }
    # Soccer 3-way match summary — surfaces home/away team names and
    # final scoreline on the operator console's recent-settlements
    # panel so an operator can confirm the per-leg derivation matched
    # the real result without leaving the dashboard.  Absent for non-
    # 3-way markets and for soccer markets settled before Sportradar
    # publishes the final score.
    if match_summary is not None:
        for k in ("home_team", "away_team", "home_score", "away_score"):
            v = match_summary.get(k)
            if v is not None:
                result[k] = v
    await _publish({"type": "market_settle_complete", **{k: v for k, v in result.items() if k != "details"}})
    return result


# ---------------------------------------------------------------------------
# Cycle: scan for kalshi positions whose market is final and settle them
# ---------------------------------------------------------------------------


async def _open_kalshi_market_ids(limit: int = 50) -> list[str]:
    """Discover candidate kalshi markets to settle.  Unions two sources:
      (a) currently-open positions on kalshi
      (b) un-settled kalshi trades that no longer have a corresponding
          open position (data drift / external system inconsistencies).
    Without (b) those orphaned trades would never auto-settle.
    """
    try:
        rows = await db.fetch(
            """
            SELECT market_id FROM (
              SELECT DISTINCT market_id FROM positions
               WHERE coalesce(status, 'open') = 'open'
                 AND exchange = 'kalshi'
                 AND market_id IS NOT NULL
              UNION
              SELECT DISTINCT market_id FROM trades
               WHERE settled_at IS NULL
                 AND exchange = 'kalshi'
                 AND market_id IS NOT NULL
            ) m
            LIMIT $1
            """,
            limit,
        )
        return [r["market_id"] for r in rows]
    except Exception as e:
        log.warning("open positions read failed: %s", e)
        return []


async def _league_for_market(market_id: str) -> str | None:
    """Best-effort lookup of the league a kalshi market belongs to.

    Inspects the trades table first (richest source — every fill on
    this market carries a league tag in production) then falls back to
    the most recent ``swarm_debates`` row that referenced the market.
    Returns ``None`` when neither source knows the league, in which
    case the caller treats the market as non-3-way and skips the
    sportradar lookup.
    """
    try:
        row = await db.fetchrow(
            """
            SELECT league
              FROM trades
             WHERE market_id = $1
               AND league IS NOT NULL
             LIMIT 1
            """,
            market_id,
        )
        if row and row["league"]:
            return str(row["league"])
    except Exception as e:
        log.debug("league lookup (trades) failed for %s: %s", market_id, e)
    try:
        row = await db.fetchrow(
            """
            SELECT league
              FROM swarm_debates
             WHERE ((game_context->>'kalshi_market_id') = $1 OR game_id = $1)
               AND league IS NOT NULL
             ORDER BY created_at DESC
             LIMIT 1
            """,
            market_id,
        )
        if row and row["league"]:
            return str(row["league"])
    except Exception as e:
        log.debug("league lookup (debates) failed for %s: %s", market_id, e)
    return None


# Sportradar status enum values that mean "match has finished and the
# scoreline is final".  Mirrors the set already accepted by the
# soccer-extended live-state poller (see feeds/sportradar.py).
_SOCCER_FINAL_STATUSES = {"closed", "ended", "complete", "finished"}


async def _sportradar_three_way_result(market_id: str) -> str | None:
    """Resolve the 3-way (home/draw/away) match result for a soccer
    kalshi market by joining through the swarm_debates → game_id link
    into the ``sportradar_live_state`` cache the soccer-extended live
    poller already maintains.

    Returns one of ``"home"``, ``"draw"`` or ``"away"`` once the match
    has finalised with a scoreline; ``None`` otherwise (no debate /
    no live-state row / not yet final / scoreline missing).
    """
    summary = await _sportradar_soccer_match_summary(market_id)
    if summary is None:
        return None
    return summary.get("result")  # type: ignore[return-value]


async def _sportradar_soccer_match_summary(
    market_id: str,
) -> dict[str, Any] | None:
    """Resolve the full soccer match summary for a kalshi market —
    h/d/a result plus the home/away team names and final scoreline —
    by joining ``swarm_debates`` (for ``game_id`` + ``league``) into
    the ``sportradar_live_state`` and ``sportradar_schedules`` caches.

    Returns ``{"result", "home_team", "away_team", "home_score",
    "away_score", "status"}`` once the match has finalised with a
    scoreline; ``None`` if there is no debate/league mapping, the
    market is not a 3-way soccer leg, the live-state row is missing,
    the match is not yet final, or the scoreline is unavailable.

    The recent-settlements panel and the trade-detail page both call
    this so a single query encodes the "is the match final + here is
    the scoreboard" contract for soccer markets.
    """
    try:
        debate = await db.fetchrow(
            """
            SELECT game_id, league
              FROM swarm_debates
             WHERE ((game_context->>'kalshi_market_id') = $1 OR game_id = $1)
               AND game_id IS NOT NULL
             ORDER BY created_at DESC
             LIMIT 1
            """,
            market_id,
        )
    except Exception as e:
        log.debug("3-way debate lookup failed for %s: %s", market_id, e)
        return None
    if not debate:
        return None
    cfg = get_league_config(debate.get("league"))
    if cfg is None or not cfg.three_way_market:
        return None
    game_id = debate["game_id"]
    try:
        row = await db.fetchrow(
            """
            SELECT status, home_score, away_score
              FROM sportradar_live_state
             WHERE game_id = $1
             ORDER BY fetched_at DESC
             LIMIT 1
            """,
            game_id,
        )
    except Exception as e:
        log.debug("sportradar live-state read failed for %s: %s", game_id, e)
        return None
    if not row:
        return None
    status = (row["status"] or "").lower()
    if status not in _SOCCER_FINAL_STATUSES:
        return None
    home = row["home_score"]
    away = row["away_score"]
    if home is None or away is None:
        return None
    try:
        h = int(home)
        a = int(away)
    except (TypeError, ValueError):
        return None
    if h > a:
        result = "home"
    elif h < a:
        result = "away"
    else:
        result = "draw"
    # Look up the team names from the schedules cache.  Best-effort —
    # missing names just leave the labels empty so the UI can fall back
    # to "Home / Away" placeholders without breaking.
    home_team: str | None = None
    away_team: str | None = None
    try:
        sched = await db.fetchrow(
            """
            SELECT home_team, away_team
              FROM sportradar_schedules
             WHERE game_id = $1
             ORDER BY updated_at DESC
             LIMIT 1
            """,
            game_id,
        )
        if sched is not None:
            home_team = sched["home_team"] or None
            away_team = sched["away_team"] or None
    except Exception as e:
        log.debug("sportradar schedules read failed for %s: %s", game_id, e)
    return {
        "result": result,
        "home_team": home_team,
        "away_team": away_team,
        "home_score": h,
        "away_score": a,
        "status": status,
    }


# ---------------------------------------------------------------------------
# Backup soccer settlement source — Task #769.
#
# Sportradar is the primary signal but a single-source settlement loop is
# one outage away from mis-settling a stack of bets.  When we're about to
# settle a soccer market on Sportradar alone (Kalshi hasn't published
# YES/NO yet), we additionally consult ESPN's free public scoreboard for
# the same match and require the two sources to agree before auto-settling.
# Disagreements — or "primary stale + secondary unavailable" cases — are
# parked on the `soccer_settlement_review` queue for an operator decision
# instead of being silently auto-settled.
# ---------------------------------------------------------------------------


# How long after kickoff we'll wait for Sportradar to publish a final
# scoreline before treating "no primary verdict yet" as stale enough to
# require an explicit review when the secondary is also unavailable.  90'
# regulation + 30' extra time + 15' handover headroom.
_PRIMARY_STALE_AFTER_SECS: float = 3 * 3600.0

# Dashboard tile RED threshold — operator hasn't resolved a queued
# soccer settlement review within this many seconds.  Surfaced via
# ``app.dashboard.get_soccer_settlement_review_summary`` and the
# ``/api/dashboard/soccer-settlement-review`` endpoint.
SOCCER_REVIEW_RED_THRESHOLD_SECS: float = 6 * 3600.0


async def _market_debate_game_id(
    market_id: str,
) -> tuple[str | None, str | None]:
    """Return ``(game_id, league)`` from the most recent debate row."""
    try:
        row = await db.fetchrow(
            """
            SELECT game_id, league
              FROM swarm_debates
             WHERE ((game_context->>'kalshi_market_id') = $1 OR game_id = $1)
               AND game_id IS NOT NULL
             ORDER BY created_at DESC
             LIMIT 1
            """,
            market_id,
        )
    except Exception as e:
        log.debug("debate game_id lookup failed for %s: %s", market_id, e)
        return (None, None)
    if not row:
        return (None, None)
    return (row.get("game_id"), row.get("league"))


async def _market_scheduled_age_seconds(market_id: str) -> float | None:
    """Seconds since the soccer market's match was scheduled to kick off.

    ``None`` when no schedules row is on file.  Used to decide whether a
    missing primary verdict is stale enough to demand explicit review.
    """
    try:
        row = await db.fetchrow(
            """
            SELECT s.scheduled
              FROM swarm_debates d
              JOIN sportradar_schedules s ON s.game_id = d.game_id
             WHERE ((d.game_context->>'kalshi_market_id') = $1 OR d.game_id = $1)
               AND d.game_id IS NOT NULL
             ORDER BY d.created_at DESC, s.updated_at DESC
             LIMIT 1
            """,
            market_id,
        )
    except Exception as e:
        log.debug("scheduled-age lookup failed for %s: %s", market_id, e)
        return None
    if not row or row.get("scheduled") is None:
        return None
    sched = row["scheduled"]
    if isinstance(sched, datetime):
        if sched.tzinfo is None:
            sched = sched.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - sched).total_seconds()
    return None


async def _espn_soccer_match_summary(
    market_id: str,
) -> dict[str, Any] | None:
    """ESPN-side counterpart to :func:`_sportradar_soccer_match_summary`.

    Joins the same ``swarm_debates`` → ``sportradar_schedules`` chain to
    recover the league + home/away team names + scheduled kickoff, then
    asks the ESPN public scoreboard for the same shape Sportradar
    returns.  ``None`` when:

      * the league has no ESPN slug wired up
      * the schedules cache lacks team names
      * ESPN itself returned no final match
    """
    game_id, league = await _market_debate_game_id(market_id)
    if not league or not game_id:
        return None
    cfg = get_league_config(league)
    if cfg is None or not cfg.three_way_market:
        return None
    if league not in espn_soccer.supported_leagues():
        return None
    try:
        sched = await db.fetchrow(
            """
            SELECT home_team, away_team, scheduled
              FROM sportradar_schedules
             WHERE game_id = $1
             ORDER BY updated_at DESC
             LIMIT 1
            """,
            game_id,
        )
    except Exception as e:
        log.debug("schedules read for ESPN fallback failed (%s): %s", game_id, e)
        return None
    if not sched or not sched["home_team"] or not sched["away_team"]:
        return None
    return await espn_soccer.fetch_match_summary(
        league=league,
        home_team=sched["home_team"],
        away_team=sched["away_team"],
        scheduled_at=sched.get("scheduled"),
    )


async def _resolve_soccer_settlement(
    *,
    market_id: str,
    league: str,
    sportradar_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    """Decide whether to auto-settle this soccer market or queue it.

    Decision matrix (``primary`` = Sportradar, ``fallback`` = ESPN)::

      primary final    fallback final    → settle on agreement, queue on
                                            disagreement
      primary final    fallback unavail. → settle on primary alone (single
                                            source acceptable when primary
                                            is itself final)
      primary unavail. fallback final    → queue if primary stale
                                            (kickoff > 3h ago); otherwise
                                            skip and wait
      primary unavail. fallback unavail. → skip (wait for either source)

    Returns a dict with keys::

      action:            "settle" | "queue" | "skip"
      result_3way:       str | None              (only set when action=settle)
      match_summary:     dict | None             (the source picked for settle)
      primary:           dict | None             (sportradar payload)
      fallback:          dict | None             (espn payload)
      disagreement_kind: str | None              (only when action=queue)
    """
    out: dict[str, Any] = {
        "action": "skip",
        "result_3way": None,
        "match_summary": None,
        "primary": sportradar_summary,
        "fallback": None,
        "disagreement_kind": None,
    }

    fallback_summary: dict[str, Any] | None = None
    # Only call ESPN when the league has a slug wired up; saves an HTTP
    # round-trip for non-supported leagues (currently a no-op since every
    # 3-way league is supported, but the supported_leagues() check keeps
    # the contract honest if we add a new league before wiring its slug).
    if league and league.upper() in espn_soccer.supported_leagues():
        try:
            fallback_summary = await _espn_soccer_match_summary(market_id)
        except Exception as e:  # pragma: no cover — fallback is best-effort
            log.info(
                "ESPN fallback lookup failed for %s: %s — treating as unavailable",
                market_id, e,
            )
            fallback_summary = None
    out["fallback"] = fallback_summary

    primary_final = (
        isinstance(sportradar_summary, dict)
        and sportradar_summary.get("result") in THREE_WAY_OUTCOMES
    )
    fallback_final = (
        isinstance(fallback_summary, dict)
        and fallback_summary.get("result") in THREE_WAY_OUTCOMES
    )

    if primary_final and fallback_final:
        if sportradar_summary["result"] == fallback_summary["result"]:
            out["action"] = "settle"
            out["result_3way"] = sportradar_summary["result"]
            out["match_summary"] = sportradar_summary
        else:
            out["action"] = "queue"
            out["disagreement_kind"] = "disagree"
        return out

    if primary_final and not fallback_final:
        # Single-source settlement is acceptable when the primary itself
        # has a final scoreline; ESPN being slow / down does not by
        # itself warrant a hold.
        out["action"] = "settle"
        out["result_3way"] = sportradar_summary["result"]
        out["match_summary"] = sportradar_summary
        return out

    if fallback_final and not primary_final:
        # Primary hasn't reported a final result yet.  If the match was
        # scheduled long enough ago that the primary is clearly stale,
        # queue for review so an operator can sign off on settling from
        # the secondary alone.  Otherwise skip and wait for the primary.
        age = await _market_scheduled_age_seconds(market_id)
        if age is not None and age > _PRIMARY_STALE_AFTER_SECS:
            out["action"] = "queue"
            out["disagreement_kind"] = "primary_stale_secondary_final"
        return out

    # Neither source has a final result yet.  Two sub-cases:
    #
    #   * primary stale + secondary down — queue for review so the
    #     market doesn't sit unsettled forever when both upstreams are
    #     misbehaving.  Operator can resolve manually from the
    #     dashboard once they've verified the scoreline out-of-band.
    #     This is the "secondary down + primary stale → queued" half
    #     of Task #769's done criteria.
    #   * primary fresh — nothing to do this cycle, both sources may
    #     just be slow.
    age = await _market_scheduled_age_seconds(market_id)
    if age is not None and age > _PRIMARY_STALE_AFTER_SECS:
        out["action"] = "queue"
        out["disagreement_kind"] = "primary_stale_secondary_down"
    return out


async def _has_open_soccer_review(market_id: str) -> bool:
    try:
        n = await db.fetchval(
            """
            SELECT 1 FROM soccer_settlement_review
             WHERE market_id = $1 AND resolved_at IS NULL
             LIMIT 1
            """,
            market_id,
        )
    except Exception as e:
        log.debug("open soccer review check failed for %s: %s", market_id, e)
        return False
    return bool(n)


async def enqueue_soccer_review(
    *,
    market_id: str,
    league: str | None,
    game_id: str | None,
    primary: dict[str, Any] | None,
    fallback: dict[str, Any] | None,
    disagreement_kind: str,
) -> int | None:
    """Insert (idempotent on open market) into ``soccer_settlement_review``.

    Returns the new row id, or ``None`` when an open row already covers
    this market or the insert failed.  Wrapped in its own helper so the
    admin module can re-queue from outside the cycle (and for testing).
    """
    if await _has_open_soccer_review(market_id):
        return None
    try:
        row = await db.fetchrow(
            """
            INSERT INTO soccer_settlement_review
                (market_id, game_id, league,
                 primary_source, primary_payload,
                 fallback_source, fallback_payload,
                 disagreement_kind)
            VALUES ($1, $2, $3,
                    'sportradar', $4::jsonb,
                    'espn',       $5::jsonb,
                    $6)
            RETURNING id
            """,
            market_id,
            game_id,
            league,
            json.dumps(primary, default=str) if primary else None,
            json.dumps(fallback, default=str) if fallback else None,
            disagreement_kind,
        )
    except Exception as e:
        # Race with the partial unique index — another cycle just wrote
        # the same row.  Treat as a successful idempotent no-op.
        log.info("soccer review enqueue for %s skipped: %s", market_id, e)
        return None
    return int(row["id"]) if row else None


# Allowed values for the admin resolve endpoint.  Two source-pick
# verdicts plus three manual overrides covering every 3-way outcome.
SOCCER_REVIEW_RESOLUTIONS: frozenset[str] = frozenset({
    "sportradar", "fallback", "manual_home", "manual_draw", "manual_away",
})


async def apply_soccer_review_resolution(
    *,
    review_id: int,
    actor_user_id: str,
    actor_email: str | None,
    resolution: str,
    notes: str | None = None,
) -> dict[str, Any]:
    """Resolve one queued soccer review and route through ``settle_market``.

    Writes an ``admin_audit_log`` row before flipping the review row
    closed so a settle-time crash still leaves an operator-visible
    trail.  ``settle_market`` is idempotent on (market_id, trade_id) —
    re-resolving a partially-settled row is safe.
    """
    if resolution not in SOCCER_REVIEW_RESOLUTIONS:
        raise ValueError(f"unknown soccer review resolution: {resolution!r}")

    review = await db.fetchrow(
        """
        SELECT id, market_id, game_id, league,
               primary_payload, fallback_payload, disagreement_kind,
               resolved_at, resolution, resolved_result, settle_completed_at
          FROM soccer_settlement_review
         WHERE id = $1
        """,
        int(review_id),
    )
    if review is None:
        raise ValueError(f"soccer review {review_id} not found")
    # Allow retries when the row was previously closed by an attempt
    # that completed the resolve+audit step but failed to finish the
    # downstream ``settle_market`` call.  ``settle_completed_at`` only
    # gets stamped once settle returns successfully — if it's NULL on
    # an already-resolved row we may safely re-run with the same (or
    # a corrected) resolution.  ``settle_market`` is idempotent on
    # (market_id, trade_id), so a re-run after a partial settle is
    # safe.
    already_settled = (
        review["resolved_at"] is not None
        and review.get("settle_completed_at") is not None
    )
    if already_settled:
        raise ValueError(f"soccer review {review_id} already resolved")

    def _decode(v: Any) -> dict[str, Any] | None:
        if v is None:
            return None
        if isinstance(v, dict):
            return v
        if isinstance(v, (str, bytes, bytearray)):
            try:
                return json.loads(v)
            except (ValueError, TypeError):
                return None
        return None

    primary = _decode(review["primary_payload"])
    fallback = _decode(review["fallback_payload"])

    if resolution == "sportradar":
        if not primary or primary.get("result") not in THREE_WAY_OUTCOMES:
            raise ValueError(
                "cannot resolve via sportradar — primary payload missing result"
            )
        result_3way = primary["result"]
        chosen = primary
    elif resolution == "fallback":
        if not fallback or fallback.get("result") not in THREE_WAY_OUTCOMES:
            raise ValueError(
                "cannot resolve via fallback — fallback payload missing result"
            )
        result_3way = fallback["result"]
        chosen = fallback
    else:  # manual_home / manual_draw / manual_away
        result_3way = resolution.removeprefix("manual_")
        # Synthesize a match_summary so settle_market's per-leg path
        # still has team names (used in postmortem narrative).  Borrow
        # them from whichever source had them, else leave None.
        ref = primary or fallback or {}
        chosen = {
            "result": result_3way,
            "home_team": ref.get("home_team"),
            "away_team": ref.get("away_team"),
            "home_score": ref.get("home_score"),
            "away_score": ref.get("away_score"),
            "status": "manual",
            "source": "manual",
        }

    market_id = review["market_id"]

    # Audit BEFORE we mutate so even a downstream crash leaves a trail.
    try:
        await db.execute(
            """
            INSERT INTO admin_audit_log
                (actor_user_id, actor_email,
                 target_user_id, target_email,
                 action, before_value, after_value)
            VALUES ($1, $2, $3, $4,
                    'soccer_settlement_review_resolve',
                    $5::jsonb, $6::jsonb)
            """,
            actor_user_id,
            actor_email,
            actor_user_id,  # target_user_id NOT NULL — actor is the closest fit
            actor_email,
            json.dumps(
                {
                    "review_id": int(review_id),
                    "market_id": market_id,
                    "league": review["league"],
                    "game_id": review["game_id"],
                    "disagreement_kind": review["disagreement_kind"],
                    "primary": primary,
                    "fallback": fallback,
                },
                default=str,
            ),
            json.dumps(
                {
                    "resolution": resolution,
                    "result_3way": result_3way,
                    "notes": notes,
                },
                default=str,
            ),
        )
    except Exception as e:
        log.warning(
            "soccer review audit log insert failed for %s: %s", review_id, e
        )

    # Route back through the canonical settle path FIRST so a transient
    # downstream failure (DB blip, kalshi timeout) doesn't leave the
    # review row marked closed-but-unsettled.  ``settle_market`` is
    # idempotent on (market_id, trade_id), so a retry that re-runs
    # this is safe.  Stamp ``resolved_at`` + ``resolution`` only after
    # settle returns; stamp ``settle_completed_at`` at the same time
    # so a future retry can tell the difference between "previously
    # resolved fully" and "previously resolved but settle crashed".
    settle_res = await settle_market(
        market_id,
        outcome_yes=0,
        settle_price=None,
        result_3way=result_3way,
        match_summary=chosen,
    )

    # Settle succeeded — flip the row closed and stamp the completion
    # marker.  Use ``COALESCE`` on resolved_at so a retry that comes in
    # after a previous attempt set resolved_at but failed before
    # settle_completed_at preserves the original audit timestamp.
    await db.execute(
        """
        UPDATE soccer_settlement_review
           SET resolved_at         = COALESCE(resolved_at, now()),
               resolved_by         = $2,
               resolved_by_email   = $3,
               resolution          = $4,
               resolved_result     = $5,
               notes               = $6,
               settle_completed_at = now()
         WHERE id = $1
        """,
        int(review_id),
        actor_user_id,
        actor_email,
        resolution,
        result_3way,
        notes,
    )

    return {
        "ok": True,
        "review_id": int(review_id),
        "market_id": market_id,
        "resolution": resolution,
        "result_3way": result_3way,
        "trades_settled": int(settle_res.get("trades_settled", 0)),
        "persona_metrics_updated": int(
            settle_res.get("persona_metrics_updated", 0)
        ),
    }


def _parse_kalshi_outcome(market: dict[str, Any]) -> tuple[int, float | None] | None:
    """Inspect a Kalshi `/markets/{ticker}` response and return
    (outcome_yes, settle_price) if the market is final, else None."""
    if not isinstance(market, dict):
        return None
    m = market.get("market") if isinstance(market.get("market"), dict) else market
    status = (m.get("status") or "").lower()
    if status not in ("settled", "finalized", "final"):
        return None
    result = (m.get("result") or "").lower()
    if result == "yes":
        return 1, _to_unit_price(m.get("settlement_value"))
    if result == "no":
        return 0, _to_unit_price(m.get("settlement_value"))
    # Sometimes the response carries `expiration_value` instead.
    ev = m.get("expiration_value")
    if ev is not None:
        try:
            return (1 if int(ev) >= 50 else 0), _to_unit_price(ev)
        except Exception:
            return None
    return None


async def run_cycle(*, market_limit: int = 50) -> CycleResult:
    """One settlement sweep: poll kalshi for each open position's
    market and settle any that are final.  Honors the AXIOM_TRADING_MODE
    halt setting only insofar as it keeps polling cheap; settlement
    must keep running even when trading is halted, otherwise the
    Bayesian feedback loop stalls.
    """
    started = time.perf_counter()
    started_at = datetime.now(timezone.utc).isoformat()
    cr = CycleResult(started_at=started_at)
    await _publish({"type": "cycle_start", "started_at": started_at})

    market_ids = await _open_kalshi_market_ids(limit=market_limit)
    cr.markets_examined = len(market_ids)
    if not market_ids:
        cr.duration_seconds = time.perf_counter() - started
        cr.completed_at = datetime.now(timezone.utc).isoformat()
        await _publish({"type": "cycle_complete", **_summary(cr)})
        return cr

    # Lazy import so test environments without Kalshi creds still work.
    try:
        from ..kalshi.client import KalshiClient, KalshiAPIError
        from ..config import get_settings
        s = get_settings()
        if not s.kalshi_api_key_id or not s.kalshi_private_key:
            cr.errors.append("kalshi credentials missing — cycle no-op")
            cr.duration_seconds = time.perf_counter() - started
            cr.completed_at = datetime.now(timezone.utc).isoformat()
            await _publish({"type": "cycle_complete", **_summary(cr)})
            return cr
    except Exception as e:
        cr.errors.append(f"kalshi import failed: {e}")
        cr.duration_seconds = time.perf_counter() - started
        cr.completed_at = datetime.now(timezone.utc).isoformat()
        await _publish({"type": "cycle_complete", **_summary(cr)})
        return cr

    try:
        async with KalshiClient(settings=s, timeout=8.0) as kc:
            for ticker in market_ids:
                try:
                    raw = await kc.get_market(ticker)
                except KalshiAPIError as e:
                    cr.errors.append(f"{ticker}: kalshi {e.status}")
                    raw = None
                except Exception as e:
                    cr.errors.append(f"{ticker}: {e}")
                    raw = None
                parsed = _parse_kalshi_outcome(raw) if raw is not None else None

                # Consult the existing Sportradar soccer-extended feed
                # for a 3-way (home/draw/away) match result so soccer
                # markets settle on the richer per-leg path.  This lets
                # the pipeline fire postmortems + persona Brier updates
                # for every soccer trade — including ones whose Kalshi
                # leg has not yet been published — and ensures a draw
                # correctly settles BOTH the home and away YES legs as
                # NO without being treated as a 2-way moneyline miss.
                #
                # When Kalshi has NOT yet published a YES/NO outcome and
                # we'd be settling a soccer market on Sportradar alone,
                # cross-check the result against the ESPN public
                # scoreboard via :func:`_resolve_soccer_settlement` so a
                # Sportradar outage cannot mis-settle bets (Task #769).
                # Disagreements / "primary stale + secondary unavailable"
                # cases are parked on ``soccer_settlement_review`` for an
                # operator to resolve from the dashboard.
                result_3way: str | None = None
                match_summary: dict[str, Any] | None = None
                queued_for_review = False
                try:
                    league = await _league_for_market(ticker)
                    cfg = get_league_config(league) if league else None
                    if cfg is not None and cfg.three_way_market:
                        primary_summary = await _sportradar_soccer_match_summary(ticker)
                        if parsed is not None:
                            # Kalshi has the canonical YES/NO outcome —
                            # honour it directly; the agreement check
                            # only gates Sportradar-only settlement.
                            match_summary = primary_summary
                            if primary_summary is not None:
                                result_3way = primary_summary.get("result")
                        else:
                            decision = await _resolve_soccer_settlement(
                                market_id=ticker,
                                league=league or "",
                                sportradar_summary=primary_summary,
                            )
                            action = decision["action"]
                            if action == "settle":
                                match_summary = decision["match_summary"]
                                result_3way = decision["result_3way"]
                            elif action == "queue":
                                queued_for_review = True
                                game_id, _ = await _market_debate_game_id(ticker)
                                review_id = await enqueue_soccer_review(
                                    market_id=ticker,
                                    league=league,
                                    game_id=game_id,
                                    primary=decision["primary"],
                                    fallback=decision["fallback"],
                                    disagreement_kind=str(
                                        decision["disagreement_kind"]
                                    ),
                                )
                                cr.errors.append(
                                    f"{ticker}: soccer settlement queued for review "
                                    f"({decision['disagreement_kind']}, "
                                    f"review_id={review_id})"
                                )
                            # action == "skip" → fall through, neither
                            # parsed nor result_3way set, cycle skips.
                except Exception as e:
                    log.debug("3-way result lookup failed for %s: %s", ticker, e)

                if queued_for_review:
                    continue

                if parsed is not None:
                    outcome_yes, settle_price = parsed
                elif result_3way is not None:
                    # Kalshi hasn't published its YES/NO resolution yet
                    # but Sportradar has the final score for this 3-way
                    # soccer market.  Pass a binary fallback of 0 — the
                    # per-leg derivation in `settle_market` overrides it
                    # for every trade whose `three_way_leg` metadata is
                    # populated (which is the canonical case for soccer
                    # candidates persisted under task #210).
                    outcome_yes, settle_price = 0, None
                else:
                    continue

                try:
                    res = await settle_market(
                        ticker,
                        outcome_yes=outcome_yes,
                        settle_price=settle_price,
                        result_3way=result_3way,
                        match_summary=match_summary,
                    )
                    cr.markets_settled += 1
                    cr.trades_settled += int(res.get("trades_settled", 0))
                    cr.postmortems_written += int(res.get("trades_settled", 0))
                    cr.persona_metrics_updated += int(res.get("persona_metrics_updated", 0))
                    cr.alerts_raised += int(res.get("alerts_raised", 0))
                except Exception as e:
                    cr.errors.append(f"{ticker} settle: {e}")
    except Exception as e:
        cr.errors.append(f"cycle outer: {e}")

    cr.duration_seconds = time.perf_counter() - started
    cr.completed_at = datetime.now(timezone.utc).isoformat()
    await _publish({"type": "cycle_complete", **_summary(cr)})
    return cr


def _summary(cr: CycleResult) -> dict[str, Any]:
    return {
        "started_at": cr.started_at,
        "completed_at": cr.completed_at,
        "duration_seconds": cr.duration_seconds,
        "markets_examined": cr.markets_examined,
        "markets_settled": cr.markets_settled,
        "trades_settled": cr.trades_settled,
        "postmortems_written": cr.postmortems_written,
        "persona_metrics_updated": cr.persona_metrics_updated,
        "alerts_raised": cr.alerts_raised,
        "errors": cr.errors,
    }
