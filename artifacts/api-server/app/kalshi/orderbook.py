"""In-process orderbook cache fed by Kalshi WS messages.

The WS subscriber (`app.kalshi.ws.KalshiWebSocket`) publishes every parsed
frame onto the in-process `bus` as ``{"type": "kalshi_event", "msg": ...}``.
This module subscribes to that stream and maintains a per-ticker view of
the resting bids on each side.

Two channel payloads matter:

* ``orderbook_snapshot`` — full level set, replaces our cached view.
* ``orderbook_delta``    — single (price, side, delta) edit applied on top.

Kalshi quotes a binary contract as two half-books, both expressed as
RESTING BIDS:

* ``yes`` book — bids willing to BUY YES at ``yes_price`` cents.
* ``no``  book — bids willing to BUY NO  at ``no_price``  cents.

A taker who wants to BUY a side has to cross the *opposite* half-book
(YES + NO sum to $1.00 on a binary contract). Concretely:

  BUY YES of N contracts → match against the no-book; cheapest taker
    yes-price = ``100 - max(no_price)``; the worst (highest) yes-price
    you pay equals ``100 - min(no_price walked)``.
  BUY NO  of N contracts → mirror image: walk the yes-book from the
    highest yes-price down; worst no-price paid = ``100 - min(yes_price)``.
  SELL YES of N → hit the yes-book bids; worst (lowest) price received
    = lowest yes-price walked.
  SELL NO  of N → hit the no-book bids; worst received = lowest no-price.

`worst_fill_price` encapsulates these rules so the lifecycle guard can
just ask "buying N contracts of YES — what's the worst price I'd pay?"
without having to know the binary-book transform.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .bus import bus

log = logging.getLogger(__name__)


__all__ = [
    "OrderbookCache",
    "orderbook_cache",
    "start_orderbook_consumer",
    "stop_orderbook_consumer",
]


Side = str  # "yes" | "no"


class OrderbookCache:
    """Per-ticker level book.  Levels are ``{price_cents: size}`` dicts."""

    def __init__(self) -> None:
        # ticker -> side -> {price_cents: size}
        self._books: dict[str, dict[str, dict[int, int]]] = {}
        # ticker -> monotonic-ish wall-clock seconds of last mutation.
        # Used by the lifecycle slippage guard to decide whether the
        # cached view is fresh enough to trust before a live submit.
        self._updated_at: dict[str, float] = {}
        self._lock = asyncio.Lock()

    # ---- mutation -------------------------------------------------------

    @staticmethod
    def _empty_book() -> dict[str, dict[int, int]]:
        return {"yes": {}, "no": {}}

    @staticmethod
    def _normalize_levels(levels: Any) -> dict[int, int]:
        """Accept either ``[[price, size], ...]`` or ``[{"price":p,"size":s}]``."""
        out: dict[int, int] = {}
        if not levels:
            return out
        for lvl in levels:
            if isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
                p, s = lvl[0], lvl[1]
            elif isinstance(lvl, dict):
                p = lvl.get("price") or lvl.get("price_cents")
                s = lvl.get("size") or lvl.get("count") or lvl.get("quantity")
            else:
                continue
            try:
                pi = int(p)
                si = int(s)
            except (TypeError, ValueError):
                continue
            if 1 <= pi <= 99 and si > 0:
                out[pi] = si
        return out

    async def apply_snapshot(
        self,
        ticker: str,
        *,
        yes: Any = None,
        no: Any = None,
    ) -> None:
        async with self._lock:
            self._books[ticker] = {
                "yes": self._normalize_levels(yes),
                "no": self._normalize_levels(no),
            }
            self._updated_at[ticker] = time.time()

    async def apply_delta(
        self,
        ticker: str,
        *,
        side: Side,
        price_cents: int,
        delta: int,
    ) -> None:
        if side not in ("yes", "no"):
            return
        try:
            price_cents = int(price_cents)
            delta = int(delta)
        except (TypeError, ValueError):
            return
        if not (1 <= price_cents <= 99):
            return
        async with self._lock:
            book = self._books.setdefault(ticker, self._empty_book())
            level = book[side]
            cur = level.get(price_cents, 0)
            new = cur + delta
            if new <= 0:
                level.pop(price_cents, None)
            else:
                level[price_cents] = new
            self._updated_at[ticker] = time.time()

    async def clear(self, ticker: str | None = None) -> None:
        async with self._lock:
            if ticker is None:
                self._books.clear()
                self._updated_at.clear()
            else:
                self._books.pop(ticker, None)
                self._updated_at.pop(ticker, None)

    # ---- freshness ------------------------------------------------------

    async def last_updated_at(self, ticker: str) -> float | None:
        """Wall-clock seconds when the cache last saw any mutation for
        ``ticker`` (snapshot or delta). ``None`` when the cache has
        never received a frame for this market.
        """
        async with self._lock:
            return self._updated_at.get(ticker)

    async def age_seconds(self, ticker: str) -> float | None:
        """Seconds since the last cache mutation for ``ticker``, or
        ``None`` if never seen.
        """
        async with self._lock:
            ts = self._updated_at.get(ticker)
            if ts is None:
                return None
            return max(0.0, time.time() - ts)

    async def is_fresh(self, ticker: str, max_age_seconds: float) -> bool:
        """``True`` when the cache has data for ``ticker`` newer than
        ``max_age_seconds``. ``max_age_seconds <= 0`` means "any cached
        snapshot counts as fresh" (operator-disabled freshness check).
        """
        age = await self.age_seconds(ticker)
        if age is None:
            return False
        if max_age_seconds <= 0:
            return True
        return age <= float(max_age_seconds)

    # ---- queries --------------------------------------------------------

    async def snapshot(self, ticker: str) -> dict[str, dict[int, int]] | None:
        async with self._lock:
            book = self._books.get(ticker)
            if book is None:
                return None
            return {"yes": dict(book["yes"]), "no": dict(book["no"])}

    async def worst_fill_price(
        self,
        ticker: str,
        *,
        action: str,
        side: Side,
        count: int,
    ) -> tuple[int | None, int]:
        """Project the worst-case taker price for ``count`` contracts of
        ``side`` using ``action`` ("buy" | "sell").

        Returns ``(worst_price_cents, filled)`` expressed in the *taker's*
        own units (yes-price for side="yes", no-price for side="no"):

        * BUY  — ``worst_price`` is the highest cents you'd pay.
        * SELL — ``worst_price`` is the lowest cents you'd receive.

        Returns ``(None, 0)`` when the cache has no book for the ticker.
        Caller decides whether that's allow-or-block — the lifecycle guard
        treats an unknown book as "skip" (degraded-allow) and an
        explicitly empty book as zero liquidity.
        """
        if side not in ("yes", "no") or count <= 0:
            return None, 0
        action = (action or "").lower()
        if action not in ("buy", "sell"):
            return None, 0
        async with self._lock:
            book = self._books.get(ticker)
            if book is None:
                return None, 0
            # Choose the half-book a taker actually crosses:
            #   BUY  this side -> opposite half-book (binary transform)
            #   SELL this side -> same half-book (hit the standing bids)
            if action == "buy":
                level = dict(book["no" if side == "yes" else "yes"])
                cross = True  # prices need 100 - p transform
            else:
                level = dict(book[side])
                cross = False
        # Both BUY and SELL takers eat the *highest* resting bid first
        # (best price for the resting side ⇒ best price for the taker too:
        # BUY YES at 100-no_price wants max(no_price); SELL YES at
        # yes_price wants max(yes_price)).
        prices = sorted(level.keys(), reverse=True)
        remaining = int(count)
        last: int | None = None
        filled = 0
        for p in prices:
            avail = level[p]
            take = min(avail, remaining)
            if take <= 0:
                continue
            filled += take
            remaining -= take
            last = p
            if remaining <= 0:
                break
        if last is None:
            return None, 0
        # ``last`` is the *deepest* (lowest) bid we had to walk to.  Convert
        # to the taker's-units worst-case price.
        if cross:
            # BUY taker: paid in (100 - bid_price); deepest bid was lowest,
            # so worst-cost = 100 - last (the highest taker price).
            return (100 - int(last)), filled
        # SELL taker: received bid_price; deepest bid was lowest, so worst
        # received = last itself.
        return int(last), filled

    async def fillable_within_price(
        self,
        ticker: str,
        *,
        action: str,
        side: Side,
        count: int,
        worst_acceptable_price_cents: int,
    ) -> int:
        """How many of ``count`` contracts the book can absorb without
        walking past ``worst_acceptable_price_cents`` (in taker units).

        * BUY  — accept levels whose per-contract cost is ``<= ceiling``.
        * SELL — accept levels whose per-contract proceeds are ``>= floor``.

        Returns 0 when the cache has no book for the ticker, the requested
        side is empty, or no level satisfies the price constraint. The
        caller (slippage guard) uses this to decide whether a smaller
        slice of the order would still respect the configured edge-erosion
        threshold.
        """
        if side not in ("yes", "no") or count <= 0:
            return 0
        action = (action or "").lower()
        if action not in ("buy", "sell"):
            return 0
        async with self._lock:
            book = self._books.get(ticker)
            if book is None:
                return 0
            if action == "buy":
                level = dict(book["no" if side == "yes" else "yes"])
                cross = True
            else:
                level = dict(book[side])
                cross = False
        ceiling = int(worst_acceptable_price_cents)
        prices = sorted(level.keys(), reverse=True)
        remaining = int(count)
        filled = 0
        for p in prices:
            if cross:
                # BUY taker pays (100 - p). Accept while taker_cost <= ceiling.
                if (100 - int(p)) > ceiling:
                    break
            else:
                # SELL taker receives p. Accept while p >= floor.
                if int(p) < ceiling:
                    break
            avail = level[p]
            take = min(avail, remaining)
            if take <= 0:
                continue
            filled += take
            remaining -= take
            if remaining <= 0:
                break
        return filled


# --- module-level singleton + bus consumer ---------------------------------

orderbook_cache = OrderbookCache()
_consumer_task: asyncio.Task | None = None


def _extract_payload(msg: dict[str, Any]) -> dict[str, Any] | None:
    """Pull the inner Kalshi payload from a bus message."""
    if not isinstance(msg, dict) or msg.get("type") != "kalshi_event":
        return None
    inner = msg.get("msg")
    if not isinstance(inner, dict):
        return None
    return inner


async def _route_one(inner: dict[str, Any]) -> None:
    channel = inner.get("type") or inner.get("channel") or ""
    payload = inner.get("msg") or inner.get("data") or {}
    if not isinstance(payload, dict):
        return
    ticker = (
        payload.get("market_ticker")
        or payload.get("ticker")
        or payload.get("marketTicker")
    )
    if not ticker:
        return
    if channel == "orderbook_snapshot":
        await orderbook_cache.apply_snapshot(
            str(ticker),
            yes=payload.get("yes") or payload.get("yes_levels"),
            no=payload.get("no") or payload.get("no_levels"),
        )
    elif channel == "orderbook_delta":
        side = payload.get("side")
        price = payload.get("price") or payload.get("price_cents")
        delta = payload.get("delta") or payload.get("size") or payload.get("count")
        if side is None or price is None or delta is None:
            return
        await orderbook_cache.apply_delta(
            str(ticker),
            side=str(side),
            price_cents=int(price),
            delta=int(delta),
        )


async def _consume(q: asyncio.Queue) -> None:
    try:
        while True:
            msg = await q.get()
            inner = _extract_payload(msg)
            if inner is None:
                continue
            try:
                await _route_one(inner)
            except Exception as e:  # pragma: no cover — defensive
                log.warning("orderbook cache route error: %s", e)
    finally:
        await bus.unsubscribe(q)


async def start_orderbook_consumer() -> asyncio.Task:
    """Idempotently start the background consumer that mirrors WS
    orderbook events into the cache.  Returns the task handle.

    Subscription is performed synchronously before the task is spawned,
    so messages published immediately after this call cannot be missed
    by a not-yet-scheduled consumer.
    """
    global _consumer_task
    if _consumer_task is not None and not _consumer_task.done():
        return _consumer_task
    q = await bus.subscribe()
    _consumer_task = asyncio.create_task(_consume(q), name="kalshi-orderbook-cache")
    return _consumer_task


async def stop_orderbook_consumer() -> None:
    global _consumer_task
    if _consumer_task is None:
        return
    _consumer_task.cancel()
    try:
        await _consumer_task
    except (asyncio.CancelledError, Exception):
        pass
    _consumer_task = None
