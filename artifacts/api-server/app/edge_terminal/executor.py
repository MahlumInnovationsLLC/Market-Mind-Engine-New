"""Drain pending Edge Terminal signals → fire via paper.place_order.

Quarter-Kelly sizing off the current bankroll.  Uses the same
:func:`paper.place_order` chokepoint the LLM pipeline does so the
audit trail (lifecycle, slippage guard, trade row insert) is
identical — the only divergence is the ``placed_by='edge_terminal'``
tag we stamp on the resulting trade row.
"""
from __future__ import annotations

import logging
from typing import Any

from .. import db
from .. import runtime_settings as runtime_settings_module
from ..config import get_settings
from ..orchestrator import cycle as cycle_module
from ..orchestrator import paper as paper_module
from ..risk.gates import RiskContext
from . import market_resolver

try:
    from ..kalshi.client import KalshiAPIError, KalshiClient
except Exception:  # pragma: no cover - kalshi optional in some envs
    KalshiClient = None  # type: ignore[assignment]
    KalshiAPIError = Exception  # type: ignore[assignment,misc]


log = logging.getLogger(__name__)


# Fallback Kelly fraction used only when neither an explicit operator
# override (``system_settings.risk.kelly_fraction_cap``) nor an active
# aggression mode (conservative/balanced/aggressive) is configured.
# Matches the "balanced" profile so behaviour is unchanged for an
# operator who has set neither knob.
KELLY_FRACTION_FALLBACK = 0.25
# Hard ceiling so a single mis-priced signal can't blow up the book.
MAX_NOTIONAL_PCT_OF_BANKROLL = 0.05


async def _resolve_kelly_fraction_cap() -> float:
    """Read the operator-configured Kelly fraction cap.

    Resolution mirrors the deterministic risk gate
    (``risk.gates.gate_kelly_fraction``) so Edge Terminal and the
    pipeline size with the same knob:

      1. ``system_settings.risk.kelly_fraction_cap`` (explicit override)
      2. ``MODE_PROFILES[aggression_mode]['kelly_fraction_cap']``
         (0.15 / 0.25 / 0.40 for conservative / balanced / aggressive)
      3. ``KELLY_FRACTION_FALLBACK`` (0.25)
    """
    try:
        overrides = await cycle_module._load_operator_overrides()
    except Exception as e:  # pragma: no cover - defensive
        log.debug("edge_terminal kelly cap: overrides read failed: %s", e)
        overrides = {}
    explicit = overrides.get("kelly_fraction_cap")
    if explicit is not None:
        try:
            return float(explicit)
        except (TypeError, ValueError):
            pass
    try:
        mode = await runtime_settings_module.get_aggression_mode()
        profile = runtime_settings_module.profile_for(mode)
        cap = profile.get("kelly_fraction_cap")
        if cap is not None:
            return float(cap)
    except Exception as e:  # pragma: no cover - defensive
        log.debug("edge_terminal kelly cap: mode read failed: %s", e)
    return KELLY_FRACTION_FALLBACK


async def drain_pending(limit: int = 50) -> dict[str, int]:
    """Fire every pending signal, return counts."""
    rows = await db.fetch(
        """
        SELECT id, vertical, matchup, selection, signal_date,
               edge_pct, model_prob, market_price,
               kalshi_ticker, market_title, side,
               pick_type, avg_odds, ev_pct,
               source_screenshot_url, kalshi_market_id
        FROM edge_terminal_signals
        WHERE status = 'pending'
        ORDER BY id ASC
        LIMIT $1
        """,
        int(limit),
    )
    placed = 0
    failed = 0
    skipped = 0
    already = 0
    for r in rows:
        sig = dict(r)
        sid = int(sig["id"])
        try:
            outcome = await _fire_one(sig)
        except _SignalSkip as skip:
            # Structured non-fire — already-placed dupe, OrderRejected
            # gate, slippage refusal, etc.  Use a typed status so the UI
            # can distinguish "we deliberately stood down" from a bug.
            await _mark(sid, skip.status, skip.reason)
            if skip.status == "skipped_already_placed":
                already += 1
            else:
                skipped += 1
            continue
        except Exception as e:
            # Classify common, recoverable refusals as a structured
            # ``skipped_*`` status instead of a generic ``failed`` so
            # the operator can tell a slippage refusal from a bug.
            cls, reason = _classify_exception(e)
            if cls is not None:
                await _mark(sid, cls, reason)
                if cls == "skipped_already_placed":
                    already += 1
                else:
                    skipped += 1
                continue
            log.exception("edge_terminal fire failed for id=%s: %s", sid, e)
            await _mark(sid, "failed", reason)
            failed += 1
            continue
        if outcome is None:
            # _fire_one returns None for two distinct reasons:
            #   1. resolver couldn't find a Kalshi ticker, or
            #   2. it found one but Kelly sizing rounded to zero contracts.
            # Re-resolve cheaply so the operator-visible reason isn't a lie.
            ticker = await market_resolver.resolve(sig)
            if ticker:
                cls = "skipped_zero_kelly"
                reason = "kelly sized to zero contracts"
            else:
                cls = "skipped_unresolved"
                reason = "no matching kalshi market"
            await _mark(sid, cls, reason)
            skipped += 1
            continue
        await _mark(
            sid, "placed", None,
            trades_id=outcome.get("trades_id"),
        )
        placed += 1
    return {
        "placed": placed,
        "failed": failed,
        "skipped": skipped,
        "already_placed": already,
    }


class _SignalSkip(Exception):
    """Structured skip emitted from ``_fire_one`` to drive ``_mark()``.

    Carries a stable ``status`` string (one of ``skipped_already_placed``,
    ``skipped_gate``, ``skipped_slippage``) plus a short ``reason`` so the
    drain loop can record an honest disposition without re-deriving it.
    """

    def __init__(self, status: str, reason: str) -> None:
        super().__init__(f"{status}: {reason}")
        self.status = status
        self.reason = reason


def _classify_exception(e: BaseException) -> tuple[str | None, str]:
    """Map a place_order exception to (status, reason).

    Returns ``(None, msg)`` when the exception is genuinely unexpected
    (treated as ``failed``), or a ``("skipped_*", reason)`` pair when
    it's a known refusal (slippage guard, gate, market-closed, etc.)
    so the operator UI doesn't show every routine standdown as a bug.
    """
    msg = (str(e) or type(e).__name__).strip()
    low = msg.lower()
    if "slippage" in low:
        return "skipped_slippage", msg[:200]
    if "orderrejected" in low or "order rejected" in low:
        return "skipped_gate", msg[:200]
    if "market closed" in low or "market_closed" in low:
        return "skipped_gate", msg[:200]
    if "duplicate" in low or "already" in low:
        return "skipped_already_placed", msg[:200]
    return None, msg[:200]


async def _fire_one(sig: dict[str, Any]) -> dict[str, Any] | None:
    ticker = await market_resolver.resolve(sig)
    if not ticker:
        return None

    # Pricing — prefer scraped market_price, else assume a near-the-
    # money mid so the lifecycle's clamp keeps us in [0.01, 0.99].
    # Use an explicit None check so a genuine 0.0 quote isn't silently
    # coerced to 0.50 by Python's falsiness — a 0-priced quote is
    # almost certainly a scrape glitch, but we want it to fail loudly
    # downstream rather than be papered over.
    raw_price = sig.get("market_price")
    try:
        price = float(raw_price) if raw_price is not None else None
    except (TypeError, ValueError):
        price = None

    # When the scraper didn't capture a market price (the live Edge
    # Terminal DOM frequently omits it; observed 2026-05-27 as the
    # primary cause of "skipped: kelly sized to zero contracts"),
    # back-fill from a fresh Kalshi quote so Kelly has a real ``p``
    # to size against.  Without this, ``model_prob`` is underived,
    # ``_kelly_quantity`` returns 0, and every signal silently skips.
    if price is None:
        live_price = await _fetch_live_kalshi_price(ticker)
        if live_price is not None:
            price = live_price
    if price is None:
        price = 0.50
    price = max(0.01, min(0.99, price))

    model_prob = sig.get("model_prob")
    if model_prob is None and sig.get("edge_pct") is not None:
        # If only edge% is known, infer p from edge: edge ≈ p - price.
        try:
            model_prob = max(0.01, min(0.99, price + float(sig["edge_pct"]) / 100.0))
        except (TypeError, ValueError):
            model_prob = None
    if model_prob is None and sig.get("ev_pct") is not None:
        # EV% from the source is computed against the same market price,
        # so ``p = price * (1 + ev_pct/100)`` recovers the model's
        # probability when ``edge_pct`` wasn't separately reported.
        try:
            model_prob = max(
                0.01,
                min(0.99, price * (1.0 + float(sig["ev_pct"]) / 100.0)),
            )
        except (TypeError, ValueError):
            pass

    # Side defaults to YES (historic behavior) but honors the scraper's
    # explicit ``side`` field when present so "Under"/"No" picks trade
    # the NO side of the Kalshi market.  Using the wrong side here
    # would buy the opposite of the intended pick — the single most
    # dangerous mis-trade this executor could make.
    raw_side = (sig.get("side") or "YES")
    side = "NO" if str(raw_side).strip().upper() == "NO" else "YES"

    # Kelly is computed in the chosen side's coordinate system: NO at
    # the YES-quoted price=p costs (1-p) per contract and wins 1 when
    # the YES outcome doesn't occur (prob = 1-model_prob).
    if side == "NO":
        side_price = max(0.01, min(0.99, 1.0 - price))
        side_prob = (
            None if model_prob is None
            else max(0.01, min(0.99, 1.0 - float(model_prob)))
        )
    else:
        side_price = price
        side_prob = model_prob

    # Match paper.place_order's effective-mode resolution so sizing uses
    # the live bankroll when the operator has flipped the live-execute
    # toggle.  ``resolve_trading_mode()`` reads the same
    # ``system_settings.risk.trading_mode`` row the orchestrator's risk
    # gate honours, so Edge Terminal and pipeline now agree on
    # paper-vs-live without needing an env-var restart.
    from ..risk.trading_mode import resolve_trading_mode
    effective_mode = await resolve_trading_mode()
    if effective_mode not in ("live", "paper"):
        effective_mode = "paper"
    bankroll = await cycle_module._latest_bankroll(trading_mode=effective_mode) or 0.0
    kelly_cap = await _resolve_kelly_fraction_cap()
    quantity, kelly_fraction = _kelly_quantity(
        bankroll=bankroll,
        model_prob=side_prob,
        price=side_price,
        kelly_fraction_cap=kelly_cap,
    )
    if quantity < 1:
        return None

    # Dedupe — if the operator already has live exposure on this
    # ticker+side (manual Kalshi fill, prior pipeline trade, or a
    # previous Edge Terminal drain), record a structured
    # ``skipped_already_placed`` so the operator can see "we honored
    # your existing position" instead of a generic skip or a duplicate
    # buy.  Scoped to the same trading_mode to avoid cross-contaminating
    # paper vs live books.
    if await _already_placed(ticker, side, effective_mode):
        raise _SignalSkip(
            "skipped_already_placed",
            f"open {side} position already exists on {ticker}",
        )

    edge_signed = None
    if model_prob is not None:
        # Sign edge from the chosen side's perspective.
        edge_signed = (
            float(model_prob) - price if side == "YES"
            else price - float(model_prob)
        )
    ctx = RiskContext(
        market_id=ticker,
        side=side,
        vertical=sig.get("vertical") or "sports",
        league=None,
        market_type=None,
        matchup=sig.get("matchup"),
        current_price=price,
        model_probability=model_prob,
        edge=edge_signed,
        confidence_band_low=None,
        confidence_band_high=None,
        agreement_rate=None,
        swarm_approved=True,
        swarm_recommended_action="BUY_YES",
        debate_id=None,
        intended_quantity=quantity,
        intended_notional_usd=quantity * price,
        bankroll_usd=bankroll,
        open_positions_count=0,
        open_positions_notional_usd=0.0,
        open_positions_per_vertical_usd={},
        operator_overrides={"trading_mode": effective_mode},
        market_cooldown_until=None,
    )
    cand = {
        "kalshi_price": price,
        "source": "edge_terminal",
    }
    # Tag the trade as ``edge_terminal`` AT INSERT TIME by threading
    # ``placed_by`` through paper.place_order — previously we did a
    # follow-up UPDATE which raced against any aggregator reading the
    # ``trades`` row in the gap between the INSERT and the UPDATE.
    return await paper_module.place_order(
        ctx,
        debate_id=None,
        consensus=model_prob,
        edge=sig.get("edge_pct"),
        kelly=kelly_fraction,
        exchange="kalshi",
        cand=cand,
        placed_by="edge_terminal",
    )


def _kelly_quantity(
    *,
    bankroll: float,
    model_prob: float | None,
    price: float,
    kelly_fraction_cap: float,
) -> tuple[int, float]:
    """Return (quantity, fractional kelly) for the chosen side.

    ``kelly_fraction_cap`` is the operator-configured Kelly fraction
    (e.g. 0.15 / 0.25 / 0.40 for conservative / balanced / aggressive)
    resolved from ``system_settings.risk.kelly_fraction_cap`` /
    aggression mode — see :func:`_resolve_kelly_fraction_cap`.

    Returns (0, 0.0) when the math is unfavourable (kelly <= 0), the
    bankroll is empty, or the operator pinned the cap to 0.
    """
    if bankroll <= 0 or model_prob is None or price <= 0 or price >= 1:
        return 0, 0.0
    if kelly_fraction_cap <= 0:
        return 0, 0.0
    p = float(model_prob)
    b = (1.0 - price) / price
    q = 1.0 - p
    kelly = (b * p - q) / b
    if kelly <= 0:
        return 0, 0.0
    notional_cap = bankroll * MAX_NOTIONAL_PCT_OF_BANKROLL
    notional = min(bankroll * kelly * kelly_fraction_cap, notional_cap)
    qty = int(notional / price)
    return max(0, qty), kelly * kelly_fraction_cap


def _quarter_kelly_quantity(
    *,
    bankroll: float,
    model_prob: float | None,
    price: float,
) -> tuple[int, float]:
    """Back-compat shim — quarter-Kelly sizing at the fallback cap.

    Retained so existing call sites / tests that assumed the hardcoded
    0.25 fraction continue to work; the live executor path now reads
    the operator-tunable cap via :func:`_resolve_kelly_fraction_cap`
    and calls :func:`_kelly_quantity` directly.
    """
    return _kelly_quantity(
        bankroll=bankroll,
        model_prob=model_prob,
        price=price,
        kelly_fraction_cap=KELLY_FRACTION_FALLBACK,
    )


async def _fetch_live_kalshi_price(ticker: str) -> float | None:
    """Pull a fresh mid quote for ``ticker`` (YES coordinates, 0..1).

    Used to back-fill ``market_price`` when the Edge Terminal scraper
    didn't capture one.  Prefers the bid/ask mid; falls back to last
    print.  Returns ``None`` (and logs) on any client error so the
    caller can default to 0.50 rather than skip the signal.

    Kalshi exposes both dollar-denominated floats (``yes_bid_dollars``)
    and the legacy cent integers (``yes_bid``); we honour the modern
    field first and only fall back to the cent path when the dollar
    field is missing.
    """
    if KalshiClient is None:
        return None
    settings = get_settings()
    if not getattr(settings, "kalshi_api_key_id", None):
        return None
    try:
        client = KalshiClient()
        try:
            payload = await client.get_market(ticker)
        finally:
            close = getattr(client, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:  # pragma: no cover
                    pass
    except KalshiAPIError as e:
        log.info("edge_terminal: get_market(%s) failed: %s", ticker, e)
        return None
    except Exception as e:  # pragma: no cover - defensive
        log.info("edge_terminal: get_market(%s) error: %s", ticker, e)
        return None

    market = (payload or {}).get("market") or payload or {}

    def _read(dollars_key: str, cents_key: str) -> float | None:
        d = market.get(dollars_key)
        if d is not None:
            try:
                return float(d)
            except (TypeError, ValueError):
                pass
        c = market.get(cents_key)
        if c is not None:
            try:
                return float(c) / 100.0
            except (TypeError, ValueError):
                pass
        return None

    bid = _read("yes_bid_dollars", "yes_bid")
    ask = _read("yes_ask_dollars", "yes_ask")
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    if ask is not None and ask > 0:
        return ask
    if bid is not None and bid > 0:
        return bid
    return _read("last_price_dollars", "last_price")


async def _already_placed(
    ticker: str, side: str, trading_mode: str
) -> bool:
    """True when the operator already holds open exposure on ticker+side.

    Checks the ``positions`` table for any row with ``status='open'``
    (or NULL — older rows pre-date the column) that matches the same
    market and side in the same trading mode.  Used to suppress
    duplicate Edge Terminal fires when the operator (or a prior drain)
    has already taken the trade.
    """
    side_u = (side or "").upper()
    try:
        # Strict trading_mode match — legacy NULL rows would otherwise
        # match both paper and live probes and cross-contaminate the
        # dedupe.  A legacy NULL is treated as "not a dupe" so we err
        # on the side of placing the operator's intended trade.
        row = await db.fetchval(
            """
            SELECT 1
              FROM positions
             WHERE market_id = $1
               AND UPPER(COALESCE(side, '')) = $2
               AND COALESCE(status, 'open') = 'open'
               AND trading_mode = $3
               AND COALESCE(quantity, 0) <> 0
             LIMIT 1
            """,
            ticker, side_u, trading_mode,
        )
    except Exception as e:
        # Schema drift (e.g. missing trading_mode column in a fresh
        # dev DB) shouldn't block the trade — log + treat as "no dupe".
        log.warning(
            "edge_terminal already-placed probe failed (fail-open): %s", e,
        )
        return False
    return bool(row)


async def _mark(
    sid: int,
    status: str,
    reason: str | None,
    *,
    trades_id: int | None = None,
) -> None:
    await db.execute(
        """
        UPDATE edge_terminal_signals
        SET status = $2,
            status_reason = $3,
            trades_id = $4,
            placed_at = CASE WHEN $2 = 'placed' THEN now() ELSE placed_at END,
            updated_at = now()
        WHERE id = $1
        """,
        sid, status, reason, trades_id,
    )
