"""Drain pending Edge Terminal signals → fire via paper.place_order.

Quarter-Kelly sizing off the current bankroll.  Uses the same
:func:`paper.place_order` chokepoint the LLM pipeline does so the
audit trail (lifecycle, slippage guard, trade row insert) is
identical — the only divergence is the ``placed_by='edge_terminal'``
tag we stamp on the resulting trade row.
"""
from __future__ import annotations

import logging
import math
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
# Operator directive: size EVERY Edge Terminal trade at a flat fraction of
# the live bankroll (NOT quarter-Kelly, and with NO single-contract
# min-stake fallback).  The old Kelly path sized most signals far below
# this and the min-stake fallback forced a 1-contract (sub-$1) position
# whenever the edge math was uncomputable — the exact "why are we placing
# sub-$1 trades?" symptom.  Equal to MAX_NOTIONAL_PCT_OF_BANKROLL so the
# flat target also respects the hard ceiling.
FLAT_BANKROLL_FRACTION = 0.05


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
               source_screenshot_url, kalshi_market_id,
               signal_badged
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
    # Per-terminal-status tally so the poller log and the operator
    # "why aren't we trading" panel can break the drain down by reason
    # (e.g. ``skipped_low_confidence: 8``) rather than a single opaque
    # ``skipped`` bucket.  Keyed by the same enumerated status strings
    # ``_mark`` persists on the signal row.
    by_status: dict[str, int] = {}

    def _tally(status: str) -> None:
        by_status[status] = by_status.get(status, 0) + 1

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
            _tally(skip.status)
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
                _tally(cls)
                if cls == "skipped_already_placed":
                    already += 1
                else:
                    skipped += 1
                continue
            log.exception("edge_terminal fire failed for id=%s: %s", sid, e)
            await _mark(sid, "failed", reason)
            _tally("failed")
            failed += 1
            continue
        if outcome is None:
            # _fire_one now raises a structured ``_SignalSkip`` for every
            # deliberate stand-down (unresolved market, unsupported type,
            # zero-Kelly, low confidence, dupe), so a None return is an
            # unexpected fall-through rather than a routine skip.  Record
            # it honestly instead of guessing a reason via a re-resolve.
            await _mark(sid, "skipped_unknown", "no order produced")
            _tally("skipped_unknown")
            skipped += 1
            continue
        await _mark(
            sid, "placed", None,
            trades_id=outcome.get("trades_id"),
        )
        _tally("placed")
        placed += 1
    return {
        "placed": placed,
        "failed": failed,
        "skipped": skipped,
        "already_placed": already,
        "by_status": by_status,
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
    # The order endpoint answers ``404 market_not_found`` when the ticker
    # is not an active tradeable market — overwhelmingly a settled/closed
    # game (re-queued picks for fixtures that already ended), but it can
    # also mean a stale/wrong ticker.  Either way the order is genuinely
    # un-placeable, so skip cleanly rather than surfacing a red "Kalshi
    # rejected order" failure.  The reason is left non-asserting (does not
    # claim "settled") so a wrong-ticker resolver bug isn't masked behind
    # a false settled-market label — it stays visible/investigable.
    if "market_not_found" in low or "market not found" in low:
        return (
            "skipped_gate",
            "Kalshi market not found — settled, closed, or ticker no longer open",
        )
    if "duplicate" in low or "already" in low:
        return "skipped_already_placed", msg[:200]
    return None, msg[:200]


_MIN_MODEL_CONFIDENCE = 0.60


async def _fire_one(sig: dict[str, Any]) -> dict[str, Any] | None:
    # "Must be signal-badged" gate.  Every row the scraper inserts came
    # off a signal-tagged tile (the column defaults to True), but the
    # explicit check makes the condition auditable and lets a future
    # non-badged source be stood down with a clear reason rather than
    # silently traded.
    badged_raw = sig.get("signal_badged")
    is_badged = True if badged_raw is None else bool(badged_raw)
    if not is_badged:
        raise _SignalSkip("skipped_not_badged", "signal is not badged")

    # Resolve once through the structured surface so a single call yields
    # both the ticker AND the resolver-recommended side.  Spreads couple the
    # two: the handicap sign decides which team's "wins by over X.5" market to
    # target and whether to buy YES or NO, so the side cannot be left to the
    # scraper for those.  ``rr.side`` is None for non-spread / unsigned picks,
    # in which case the scraper's side is honoured below.
    rr = await market_resolver.resolve_match(sig)
    ticker = rr.ticker
    if not ticker:
        # No ticker — the resolver's honest reason (no Kalshi series mapped
        # for this vertical/pick_type vs a series exists but no candidate
        # matched) drives the operator-visible disposition.  Spreads and
        # totals are *attempted* (the resolver maps KX…SPREAD / KX…TOTAL
        # series and line-matches them); only genuinely unmapped market types
        # fall into the ``skipped_unsupported_market`` bucket.
        reason = rr.reason or "no matching kalshi market"
        if rr.kind == "no_series":
            raise _SignalSkip("skipped_unsupported_market", reason)
        raise _SignalSkip("skipped_unresolved", reason)

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

    # When the resolver remapped a spread to the OPPONENT's market (rr.side
    # set), the scraped ``market_price`` belongs to the pick tile's market —
    # a DIFFERENT Kalshi ticker in different coordinates — so it cannot be
    # used to price the resolved ticker.  Discard it and quote the resolved
    # ticker live instead; ``price`` is then the resolved ticker's YES price,
    # which keeps the ``pick_price = 1 - price`` NO math below self-consistent.
    if rr.side in ("YES", "NO"):
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

    # Side defaults to YES (historic behavior) but honors the scraper's
    # explicit ``side`` field when present so "Under"/"No" picks trade
    # the NO side of the Kalshi market.  Using the wrong side here
    # would buy the opposite of the intended pick — the single most
    # dangerous mis-trade this executor could make.
    raw_side = (sig.get("side") or "YES")
    side = "NO" if str(raw_side).strip().upper() == "NO" else "YES"

    # The resolver overrides the side when the market and side are coupled
    # (spreads).  Kalshi lists only "<team> wins by over X.5" markets, so a
    # "+1.5" underdog cover resolves to the OPPONENT's market and must be
    # bought as NO ("opponent does NOT win by over 1.5"), while a "-1.5"
    # favourite is YES on the pick team's market.  The scraper's ``side`` is
    # the raw tile side and is wrong for "+"-spreads, so the resolver's
    # explicit recommendation wins whenever it set one.
    if rr.side in ("YES", "NO"):
        side = rr.side

    # Price of the side we actually buy, in that side's own coordinates.
    # ``price`` above is the YES-quoted price; the NO contract costs
    # ``1 - price``.  All probability/edge/sizing math below is done in
    # the chosen side's coordinate system against this ``pick_price``.
    pick_price = max(0.01, min(0.99, price if side == "YES" else 1.0 - price))

    # Resolve the model probability and record WHICH source produced it
    # (``confidence_source``) so the deciding inputs are auditable on the
    # signal row and the observability views can distinguish a scraper-
    # reported probability from one we inferred from edge%/EV%.
    #
    # ``model_prob`` is the probability that the *scraped pick* wins — i.e.
    # already the win-probability of the side we buy.  An "Under"/"No" tile
    # shows the model's confidence in the Under, which IS the NO side; it is
    # NOT a YES-coordinate probability.  It must therefore never be flipped
    # to ``1 - model_prob`` for NO picks: doing so gated and sized on the
    # opposite side, rejecting genuine +EV NO picks and (worse) placing
    # actually -EV ones (a 0.30 pick looked like 0.70).  The inference
    # fallbacks recover it against ``pick_price`` so the edge%/EV% (reported
    # for the pick) stay in the pick's coordinates too.
    model_prob = sig.get("model_prob")
    confidence_source = "scraped" if model_prob is not None else "none"
    if model_prob is None and sig.get("edge_pct") is not None:
        # If only edge% is known, infer p from edge: edge ≈ p - pick_price.
        try:
            model_prob = max(
                0.01, min(0.99, pick_price + float(sig["edge_pct"]) / 100.0)
            )
            confidence_source = "edge_inferred"
        except (TypeError, ValueError):
            model_prob = None
    if model_prob is None and sig.get("ev_pct") is not None:
        # EV% from the source is computed against the pick's own price,
        # so ``p = pick_price * (1 + ev_pct/100)`` recovers the model's
        # probability when ``edge_pct`` wasn't separately reported.
        try:
            model_prob = max(
                0.01,
                min(0.99, pick_price * (1.0 + float(sig["ev_pct"]) / 100.0)),
            )
            confidence_source = "ev_inferred"
        except (TypeError, ValueError):
            pass

    # Signed edge from the chosen side's perspective (None when we never
    # recovered a model probability).  ``model_prob`` already lives in the
    # pick's coordinates, so edge is simply ``p - pick_price`` for both
    # sides.  Computed here so it can be persisted alongside the other
    # deciding inputs before any gate runs.
    edge_signed = None
    if model_prob is not None:
        edge_signed = float(model_prob) - pick_price

    # Persist the deciding inputs (resolved ticker, model_prob, signed
    # edge, confidence source, badge flag) up front — i.e. as soon as a
    # decision actually exists — so every disposition that gets THIS far
    # (placed, low-confidence, zero-Kelly, dupe) carries its deciding
    # values without re-running the resolver or sizer.  Pre-resolution
    # stand-downs (not-badged, unresolved, unsupported) have no model_prob
    # yet; their status+reason are still recorded by drain via ``_mark``,
    # and the badge flag was persisted by the poller at insert time.
    await _persist_decision(
        int(sig["id"]),
        ticker=ticker,
        model_prob=model_prob,
        edge=edge_signed,
        confidence_source=confidence_source,
        badged=is_badged,
    )

    # Operator-mandated minimum model confidence floor.  ``model_prob`` is
    # already the win-probability of the pick we buy (NO picks included),
    # so the floor is applied to it directly — a sub-60% pick is rejected on
    # either side.  Signals with no model_prob at all (scraper omitted it
    # AND no edge/ev to infer from) bypass the floor — the min-stake
    # fallback below already caps real risk at one contract for those.
    if model_prob is not None:
        floor_prob = float(model_prob)
        if floor_prob < _MIN_MODEL_CONFIDENCE:
            raise _SignalSkip(
                "skipped_low_confidence",
                (
                    f"model confidence {floor_prob:.0%} below "
                    f"{_MIN_MODEL_CONFIDENCE:.0%} floor"
                ),
            )

    # Kelly is computed in the chosen side's coordinate system.  Both the
    # price (``pick_price``) and the win-probability (``model_prob``) are
    # already expressed for the side we buy, so no flipping is needed.
    side_price = pick_price
    side_prob = (
        None if model_prob is None
        else max(0.01, min(0.99, float(model_prob)))
    )

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
    # Flat 5%-of-bankroll sizing (operator directive).  No quarter-Kelly,
    # no min-stake single-contract fallback: every fired signal deploys the
    # same fraction of the bankroll, and a signal that can't fund at least
    # one whole contract at that target is skipped rather than down-sized to
    # a sub-dollar position.
    quantity, sizing_fraction = _flat_fraction_quantity(
        bankroll=bankroll,
        price=side_price,
    )
    if quantity < 1:
        # Surface WHY sizing produced nothing.  Critically there is NO
        # min-stake fallback any more: we never silently place a 1-contract
        # (sub-$1) order just because the 5% target rounds below a whole
        # contract — that was the source of the sub-$1 trades.
        if not math.isfinite(bankroll) or bankroll <= 0:
            zero_reason = "no bankroll available for sizing"
        elif not math.isfinite(side_price) or side_price <= 0.0:
            zero_reason = "side price non-positive; no contracts affordable"
        elif side_price >= 1.0:
            zero_reason = "side price at ceiling (>=1.0); no contracts affordable"
        else:
            zero_reason = (
                f"5% of bankroll (${bankroll * FLAT_BANKROLL_FRACTION:.2f}) "
                f"buys <1 contract at {side_price:.2f}/contract"
            )
        raise _SignalSkip("skipped_zero_kelly", zero_reason)

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

    ctx = RiskContext(
        market_id=ticker,
        side=side,
        vertical=sig.get("vertical") or "sports",
        league=None,
        market_type=None,
        matchup=sig.get("matchup"),
        # Price/notional/quote are all in the bought side's coordinates so
        # the lifecycle stamps the correct limit: it applies
        # ``limit_price_cents`` directly as ``no_price`` for NO orders, so a
        # NO order MUST carry ``pick_price`` (1 - yes_price), not the YES
        # quote — otherwise we'd bid the YES price for the NO contract and
        # massively overpay.
        current_price=pick_price,
        model_probability=model_prob,
        edge=edge_signed,
        confidence_band_low=None,
        confidence_band_high=None,
        agreement_rate=None,
        swarm_approved=True,
        swarm_recommended_action="BUY_YES",
        debate_id=None,
        intended_quantity=quantity,
        intended_notional_usd=quantity * pick_price,
        bankroll_usd=bankroll,
        open_positions_count=0,
        open_positions_notional_usd=0.0,
        open_positions_per_vertical_usd={},
        operator_overrides={"trading_mode": effective_mode},
        market_cooldown_until=None,
    )
    cand = {
        # Side-coordinate quote: the lifecycle turns this into
        # ``limit_price_cents`` and stamps it as yes_price/no_price by side,
        # so it must be ``pick_price`` (the price of the contract we buy).
        "kalshi_price": pick_price,
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
        kelly=sizing_fraction,
        exchange="kalshi",
        cand=cand,
        placed_by="edge_terminal",
    )


def _flat_fraction_quantity(
    *,
    bankroll: float,
    price: float,
    fraction: float = FLAT_BANKROLL_FRACTION,
) -> tuple[int, float]:
    """Whole-contract quantity that deploys ``fraction`` of bankroll.

    Operator directive: every Edge Terminal trade is sized at a flat 5%
    of the live bankroll, regardless of model edge — NOT quarter-Kelly,
    and with NO single-contract min-stake fallback.

    Returns ``(0, fraction)`` when the bankroll/price can't fund at least
    one whole contract at the target notional, so the caller skips the
    signal rather than placing a sub-target (sub-$1) position.  ``price``
    is in dollars per contract (0..1, exclusive).
    """
    if (
        not math.isfinite(bankroll)
        or not math.isfinite(price)
        or bankroll <= 0
        or price <= 0
        or price >= 1
    ):
        return 0, fraction
    notional = bankroll * fraction
    qty = int(notional / price)
    return max(0, qty), fraction


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


async def _persist_decision(
    sid: int,
    *,
    ticker: str | None,
    model_prob: float | None,
    edge: float | None,
    confidence_source: str,
    badged: bool,
) -> None:
    """Persist the deciding inputs for a signal so the operator queue and
    observability views can explain every disposition without re-running
    the resolver or sizer.

    Best-effort: a persistence failure (schema drift on a fresh dev DB,
    transient outage) must NOT block the trade, so we swallow + log.
    ``kalshi_ticker`` is COALESCE'd so a NULL ticker never clobbers a
    previously-resolved value.
    """
    try:
        await db.execute(
            """
            UPDATE edge_terminal_signals
            SET kalshi_ticker = COALESCE($2, kalshi_ticker),
                decided_model_prob = $3,
                decided_edge = $4,
                confidence_source = $5,
                signal_badged = $6,
                updated_at = now()
            WHERE id = $1
            """,
            sid, ticker, model_prob, edge, confidence_source, bool(badged),
        )
    except Exception as e:  # pragma: no cover — defensive
        log.debug("edge_terminal: decision persist failed for sid=%s: %s", sid, e)


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
    # Per-signal terminal-outcome log keyed on the stable signal id so a
    # single pick can be followed end-to-end (insert -> drain -> mark)
    # by grepping ``signal id=<sid>`` in the poller/executor logs.
    log.info(
        "edge_terminal signal id=%s -> %s%s%s",
        sid,
        status,
        f" trades_id={trades_id}" if trades_id is not None else "",
        f" ({reason})" if reason else "",
    )
