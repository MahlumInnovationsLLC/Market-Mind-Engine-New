"""Polymarket CLOB WebSocket subscriber.

Connects to the public market data WS, subscribes to book/price/trade
channels for a configured set of asset (token) ids, and forwards every
parsed message to the in-process bus.

The user-channel (private fills/orders) requires the L2 auth headers
inside the subscribe payload — this implementation defaults to the
market channel which is unauthenticated. Pass `auth=True` to upgrade.

The subscriber owns one background task that auto-reconnects with
capped exponential backoff. It never raises out to callers.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Iterable

import websockets
from websockets.exceptions import ConnectionClosed

from ..config import get_settings
from . import auth as polymarket_auth
from .bus import bus

log = logging.getLogger(__name__)


class PolymarketWebSocket:
    def __init__(
        self,
        *,
        asset_ids: Iterable[str] | None = None,
        market_ids: Iterable[str] | None = None,
        channel: str = "market",
    ) -> None:
        self._settings = get_settings()
        self._asset_ids = list(asset_ids or [])
        self._market_ids = list(market_ids or [])
        self._channel = channel  # "market" (public) or "user" (auth required)
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._connected = False
        self._last_error: str | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_error(self) -> str | None:
        return self._last_error

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="polymarket-ws")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
        self._task = None
        self._connected = False

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._run_once()
                backoff = 1.0
            except Exception as e:
                self._connected = False
                self._last_error = str(e)
                log.warning("Polymarket WS error: %s (reconnect in %.1fs)", e, backoff)
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 30.0)

    async def _run_once(self) -> None:
        url = self._settings.polymarket_ws_url.rstrip("/") + "/" + self._channel
        log.info("Polymarket WS connecting %s", url)
        async with websockets.connect(
            url,
            ping_interval=20,
            ping_timeout=20,
            max_size=4 * 1024 * 1024,
        ) as ws:
            self._connected = True
            self._last_error = None
            await self._subscribe(ws)
            await bus.publish({"type": "ws_status", "exchange": "polymarket", "connected": True})
            try:
                while not self._stop.is_set():
                    raw = await ws.recv()
                    try:
                        if isinstance(raw, (str, bytes, bytearray)):
                            # Orderbook snapshots can be 1-4 MB; parse them
                            # in a thread so the event loop stays responsive.
                            if len(raw) > 16_384:
                                msg = await asyncio.to_thread(json.loads, raw)
                            else:
                                msg = json.loads(raw)
                        else:
                            msg = raw
                    except json.JSONDecodeError:
                        log.warning("Polymarket WS non-JSON frame ignored")
                        continue
                    await bus.publish({"type": "polymarket_event", "msg": msg})
            except ConnectionClosed:
                pass
            finally:
                self._connected = False
                await bus.publish({"type": "ws_status", "exchange": "polymarket", "connected": False})

    async def _subscribe(self, ws) -> None:
        sub: dict = {"type": "subscribe"}
        if self._channel == "market":
            sub["assets_ids"] = self._asset_ids
            sub["markets"] = self._market_ids
        else:
            # user channel: needs L2 auth in the subscribe payload
            s = self._settings
            ts = int(time.time())
            headers = polymarket_auth.sign_request(
                api_key=s.polymarket_api_key or "",
                api_secret=s.polymarket_api_secret or "",
                passphrase=s.polymarket_api_passphrase or "",
                address=s.polymarket_wallet_address or "",
                method="GET",
                path="/ws",
                body="",
                ts=ts,
            )
            sub["auth"] = {
                "apiKey": headers["POLY_API_KEY"],
                "secret": s.polymarket_api_secret or "",
                "passphrase": headers["POLY_PASSPHRASE"],
                "timestamp": headers["POLY_TIMESTAMP"],
                "signature": headers["POLY_SIGNATURE"],
            }
            sub["markets"] = self._market_ids
        await ws.send(json.dumps(sub))


_singleton: PolymarketWebSocket | None = None


def get_ws_subscriber() -> PolymarketWebSocket | None:
    return _singleton


async def maybe_start_ws(
    *,
    asset_ids: Iterable[str] | None = None,
    market_ids: Iterable[str] | None = None,
) -> PolymarketWebSocket | None:
    """Start the WS subscriber. Idempotent. The market channel works
    without credentials; if no asset/market ids are provided we skip
    starting because the WS would have nothing to subscribe to."""
    global _singleton
    asset_ids = list(asset_ids or [])
    market_ids = list(market_ids or [])
    if not asset_ids and not market_ids:
        log.info("Polymarket WS not started — no asset/market ids configured")
        return None
    if _singleton is None:
        _singleton = PolymarketWebSocket(asset_ids=asset_ids, market_ids=market_ids)
    await _singleton.start()
    return _singleton


async def stop_ws() -> None:
    global _singleton
    if _singleton is not None:
        await _singleton.stop()
        _singleton = None
