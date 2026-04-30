"""Paper-mode order lifecycle.

Writes a row into `trades` (with the routed exchange tag) for the
operator dashboard, and **also dispatches through the chosen venue's
lifecycle** (`app.kalshi.lifecycle.submit_order` /
`app.polymarket.lifecycle.submit_order`) so the lifecycle audit trail
(slippage guards, dedupe, etc.) and live-mode flow run for both venues.

Live-mode Polymarket signing is owned by a separate task; if a live order
routes to Polymarket it surfaces as an `OrderRejected` from the Polymarket
lifecycle rather than silently no-op'ing.
"""
from __future__ import annotations

import json
import logging
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any

from .. import db
from ..config import get_settings
from ..kalshi.market_titles import resolve_titles as resolve_market_titles
from ..risk.gates import RiskContext
from ..sports.leagues import get_league_config
from .n_way import derive_n_way_competitor
from .three_way import derive_three_way_leg


log = logging.getLogger(__name__)


# Cached resolution of the bot's "owning" operator clerk_user_id
# (task #297). Stamped onto every bot-initiated `OrderRequest.context`
# so per-user trade-confirmation emails fire end-to-end on live fills.
# (False, None) = "not yet resolved this process"; (True, x) = resolved
# (x is None when neither the env var nor the DB fallback yielded one).
_OPERATOR_USER_ID_CACHE: tuple[bool, str | None] = (False, None)


async def _resolve_operator_user_id() -> str | None:
    """Return the clerk_user_id stamped onto bot-initiated orders.

    Resolution order:
      1. ``AXIOM_OPERATOR_USER_ID`` env var (preferred — explicit).
      2. The first ``is_admin = true`` row in ``app_users`` ordered
         by ``first_seen_at`` — the bootstrap admin is the operator
         on a single-tenant deployment, so this keeps confirmations
         flowing on installs that haven't set the env var.
      3. ``None`` when neither is available — submission still
         proceeds; the lifecycle's ``handle_ws_fill`` notification
         path treats a missing ``user_id`` as a silent no-op
         (covered by the existing trade-confirmation tests).

    Cached for the process lifetime so the bot loop does not re-query
    the admin table on every cycle. Tests that mutate the env or DB
    can clear the cache via :func:`_reset_operator_user_id_cache`.
    """
    global _OPERATOR_USER_ID_CACHE
    resolved_flag, cached = _OPERATOR_USER_ID_CACHE
    if resolved_flag:
        return cached
    explicit = (get_settings().operator_user_id or "").strip()
    if explicit:
        _OPERATOR_USER_ID_CACHE = (True, explicit)
        return explicit
    fallback: str | None = None
    db_failed = False
    try:
        row = await db.fetchval(
            "SELECT clerk_user_id FROM app_users "
            "WHERE is_admin = true "
            "ORDER BY first_seen_at NULLS LAST LIMIT 1"
        )
        fallback = str(row) if row else None
    except Exception as e:  # pragma: no cover — defensive
        log.debug("operator user_id fallback lookup failed: %s", e)
        db_failed = True
    # Only cache when we got an authoritative answer. A transient DB
    # error must not pin the cache to None for the process lifetime —
    # the next bot order will retry.
    if not db_failed:
        _OPERATOR_USER_ID_CACHE = (True, fallback)
    if fallback is None:
        log.warning(
            "no operator user_id resolved — bot-initiated orders will "
            "ship without context['user_id'] and trade-confirmation "
            "emails will not fire. Set AXIOM_OPERATOR_USER_ID or "
            "promote a row in app_users to is_admin=true."
        )
    return fallback


def _reset_operator_user_id_cache() -> None:
    """Test helper — drop the cached operator clerk_user_id."""
    global _OPERATOR_USER_ID_CACHE
    _OPERATOR_USER_ID_CACHE = (False, None)


def _gen_paper_trade_id() -> str:
    # Distinguish paper trade ids from live ones at a glance.
    return f"paper_{int(datetime.now(timezone.utc).timestamp())}_{secrets.token_hex(4)}"


def _resolve_polymarket_ids(
    cand: dict | None, ctx: RiskContext, *, paper: bool
) -> tuple[str, str]:
    """Return (market_id, token_id) for a Polymarket order.

    Polymarket orders need both the conditionId (market) and the ERC1155
    outcome token id. Both come from the candidate dict when an upstream
    feed has already mapped Kalshi <-> Polymarket. In paper mode we
    synthesise placeholders if absent so the simulated lifecycle still
    runs; in live mode we refuse rather than send a malformed order.
    """
    market_id = (
        (cand or {}).get("polymarket_market_id")
        or (cand or {}).get("polymarket_condition_id")
        or ctx.market_id
        or ""
    )
    token_id = (
        (cand or {}).get("polymarket_token_id")
        or (cand or {}).get("polymarket_yes_token_id" if ctx.side.upper() == "YES"
                            else "polymarket_no_token_id")
        or ""
    )
    if not paper:
        if not market_id:
            raise RuntimeError(
                "Polymarket routing requires polymarket_market_id on the candidate"
            )
        if not token_id:
            raise RuntimeError(
                "Polymarket routing requires polymarket_token_id on the candidate"
            )
    if not market_id:
        market_id = ctx.market_id or f"paper-{uuid.uuid4().hex[:8]}"
    if not token_id:
        token_id = f"paper-{ctx.side.lower()}-{uuid.uuid4().hex[:8]}"
    return str(market_id), str(token_id)


async def _dispatch_via_lifecycle(
    *,
    exchange: str,
    ctx: RiskContext,
    cand: dict | None,
    side_norm: str,
    quantity: int,
    price: float,
    paper: bool,
    consensus: float | None,
    edge: float | None,
) -> dict[str, Any]:
    """Submit through the chosen venue's lifecycle.submit_order.

    Returns a small dict describing the resulting OrderRecord (or raises
    OrderRejected / RuntimeError, which the caller surfaces as a venue
    failure).
    """
    # Lifecycle expects integer cents in 1..99 for limit orders.
    cents = int(round(price * 100))
    cents = max(1, min(99, cents))
    context: dict[str, Any] = {
        "model_consensus": consensus,
        "model_edge": edge,
        "vertical": ctx.vertical or "sports",
    }
    # Stamp the operator's clerk_user_id so the lifecycle's
    # `handle_ws_fill` notification path can resolve a recipient and
    # deliver per-user trade-confirmation emails on bot-driven live
    # fills (task #297). Resolution is cached for the process lifetime
    # so this lookup is effectively free after the first call.
    operator_uid = await _resolve_operator_user_id()
    if operator_uid:
        context["user_id"] = operator_uid
    if exchange == "kalshi":
        from ..kalshi import lifecycle as kalshi_lifecycle

        req = kalshi_lifecycle.OrderRequest(
            market_ticker=str(ctx.market_id or ""),
            side="yes" if side_norm == "yes" else "no",
            action="buy",
            count=quantity,
            limit_price_cents=cents,
            type="limit",
            time_in_force="GTC",
            vertical=ctx.vertical or "sports",
            context=context,
        )
        rec = await kalshi_lifecycle.submit_order(req, force_paper=paper)
        return {
            "exchange_order_id": rec.exchange_order_id,
            "lifecycle_state": rec.state,
            "filled_count": rec.filled_count,
            "avg_fill_price_cents": rec.avg_fill_price_cents,
            "client_order_id": rec.order_id,
        }

    # exchange == "polymarket"
    from ..polymarket import lifecycle as polymarket_lifecycle

    market_id, token_id = _resolve_polymarket_ids(cand, ctx, paper=paper)
    req = polymarket_lifecycle.OrderRequest(
        market_id=market_id,
        token_id=token_id,
        side="yes" if side_norm == "yes" else "no",
        action="buy",
        count=quantity,
        limit_price_cents=cents,
        type="limit",
        time_in_force="GTC",
        vertical=ctx.vertical or "sports",
        context=context,
    )
    rec = await polymarket_lifecycle.submit_order(req, force_paper=paper)
    return {
        "exchange_order_id": rec.exchange_order_id,
        "lifecycle_state": rec.state,
        "filled_count": rec.filled_count,
        "avg_fill_price_cents": rec.avg_fill_price_cents,
        "client_order_id": rec.order_id,
        "polymarket_market_id": market_id,
        "polymarket_token_id": token_id,
    }


async def place_order(
    ctx: RiskContext,
    *,
    debate_id: int | None,
    consensus: float | None,
    edge: float | None,
    kelly: float | None,
    exchange: str = "kalshi",
    cand: dict | None = None,
) -> dict[str, Any]:
    """Place a paper (or live) trade routed to ``exchange``.

    Dispatches through the chosen venue's ``lifecycle.submit_order``
    (Kalshi or Polymarket) so the venue's audit trail / slippage guard /
    dedupe gate run, then writes the operator-dashboard `trades` row
    tagged with the routed exchange.

    ``exchange`` defaults to ``"kalshi"`` so existing Kalshi-only behavior
    is preserved when the operator hasn't flipped ``AXIOM_VENUE_ROUTING``.
    Returns a dict the orchestrator publishes on the pipeline stream and
    persists alongside the cycle record.

    Raises ``RuntimeError`` (DB write) / ``OrderRejected`` (lifecycle) so
    the cycle counts the trade as a venue failure rather than a silent
    no-op.
    """
    exchange = (exchange or "kalshi").strip().lower()
    if exchange not in ("kalshi", "polymarket"):
        raise ValueError(
            f"place_order exchange must be 'kalshi' or 'polymarket' (got {exchange!r})"
        )
    settings = get_settings()
    # Effective mode honors the operator override carried on the
    # RiskContext (`risk.trading_mode` in system_settings) so flipping
    # live via the dashboard takes effect immediately and matches the
    # source of truth used by run_cycle's halt check.
    override_mode = None
    if isinstance(ctx.operator_overrides, dict):
        override_mode = ctx.operator_overrides.get("trading_mode")
    mode = (override_mode or settings.trading_mode or "paper").strip().lower()
    paper = mode != "live"

    trade_id = _gen_paper_trade_id()
    action = "buy"
    side_norm = "yes" if ctx.side.upper() == "YES" else "no"
    quantity = max(1, int(ctx.intended_quantity))
    # Prefer the venue-specific quote when present so a Polymarket-routed
    # order isn't priced off a Kalshi mid (and vice-versa). Falls back to
    # ctx.current_price (historically the Kalshi mid) when the candidate
    # only carries one side's quote.
    venue_price: float | None = None
    if cand is not None:
        if exchange == "polymarket":
            raw = cand.get("polymarket_price")
        else:
            raw = cand.get("kalshi_price")
        try:
            venue_price = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            venue_price = None
    price = float(venue_price if venue_price is not None else (ctx.current_price or 0.0))

    market_title: str | None = None
    if ctx.market_id:
        try:
            titles = await resolve_market_titles([ctx.market_id])
            market_title = titles.get(ctx.market_id)
        except Exception as e:
            log.warning("resolve market title failed for %s: %s", ctx.market_id, e)

    # 3-way (soccer h/d/a) leg derivation (task #444).  Stamp the leg
    # on the trade's `metadata` jsonb at insert-time so the settlement
    # pipeline can apply the correct per-leg outcome on a draw — without
    # this, a 3-way market settled with a draw would silently fall back
    # to the binary `outcome_yes` and mis-score the home/away legs.
    # Gated on the league's `three_way_market` flag so non-3-way verticals
    # (NBA/NFL/etc.) do not get spurious leg tags from titles that happen
    # to mention "draw" / a team name.
    metadata: dict[str, Any] = {}
    try:
        cfg = get_league_config(ctx.league)
        if cfg is not None and cfg.three_way_market:
            leg = derive_three_way_leg(
                market_title=market_title, candidate=cand,
            )
            if leg is not None:
                metadata["three_way_leg"] = leg
    except Exception as e:  # pragma: no cover — never let this block insert
        log.debug(
            "three_way_leg derivation failed for %s: %s", ctx.market_id, e,
        )

    # N-way (tennis / F1 / PGA outright) competitor derivation (task
    # #616).  Stamp the competitor identifier on the trade's `metadata`
    # jsonb at insert-time so the settlement pipeline can apply the
    # correct per-leg outcome on the actual race / tournament winner —
    # without this, an ATP/F1/PGA outright market settled by Kalshi
    # would silently fall back to the binary `outcome_yes` and the
    # losing competitors' legs would be mis-scored as model misses
    # rather than the deterministic NO they actually are.  Gated on
    # the league's `n_way_market` flag so non-n-way verticals do not
    # get spurious competitor tags from titles that happen to mention
    # an entrant-shaped substring.
    try:
        cfg = get_league_config(ctx.league)
        if cfg is not None and cfg.n_way_market:
            comp = derive_n_way_competitor(
                market_title=market_title, candidate=cand,
            )
            if isinstance(comp, dict):
                metadata.update(comp)
    except Exception as e:  # pragma: no cover — never let this block insert
        log.debug(
            "n_way_competitor derivation failed for %s: %s", ctx.market_id, e,
        )
    metadata_json = json.dumps(metadata) if metadata else None

    # Submit through the chosen venue's lifecycle FIRST so a rejection
    # (slippage, dedupe, kill switch, missing polymarket id) prevents a
    # ghost row in the trades table.
    lifecycle_result = await _dispatch_via_lifecycle(
        exchange=exchange,
        ctx=ctx,
        cand=cand,
        side_norm=side_norm,
        quantity=quantity,
        price=price,
        paper=paper,
        consensus=consensus,
        edge=edge,
    )

    try:
        new_id = await db.fetchval(
            """
            INSERT INTO trades (
                trade_id, market_id, market_title, vertical, side, action,
                quantity, price, fees, pnl,
                trading_mode, exchange, executed_at,
                model_prob, model_edge, kelly_fraction,
                metadata, placed_by
            ) VALUES (
                $1, $2, $3, $4, $5, $6,
                $7, $8, 0, NULL,
                $9, $10, now(),
                $11, $12, $13,
                COALESCE($14::jsonb, '{}'::jsonb), $15
            )
            RETURNING id
            """,
            trade_id,
            ctx.market_id,
            market_title,
            ctx.vertical or "sports",
            side_norm,
            action,
            quantity,
            price,
            "paper" if paper else "live",
            exchange,
            consensus,
            edge,
            kelly,
            metadata_json,
            # The orchestrator path inserts trades on behalf of the
            # autotrader / cycle / swarm.  Tag every row with
            # ``placed_by='orchestrator'`` so the analytics filter
            # can split system trades from manual UI trades.  Migration
            # 0039 added the column.
            "orchestrator",
        )
    except Exception as e:
        log.error("trade insert failed: %s", e)
        raise RuntimeError(f"trade insert failed: {e}") from e

    return {
        "trade_id": trade_id,
        "trades_id": int(new_id) if new_id is not None else None,
        "market_id": ctx.market_id,
        "market_title": market_title,
        "side": side_norm,
        "quantity": quantity,
        "price": price,
        "trading_mode": "paper" if paper else "live",
        "exchange": exchange,
        "debate_id": debate_id,
        "lifecycle": lifecycle_result,
        "metadata": metadata or None,
    }
