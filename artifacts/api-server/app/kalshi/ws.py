"""Kalshi WebSocket subscriber.

Connects to the Kalshi v2 WS endpoint, signs the upgrade with the same
RSA-PSS scheme used for REST, subscribes to orderbook + fill channels, and
forwards every parsed message to the in-process `bus`.

The subscriber owns one background task that auto-reconnects with capped
exponential backoff. It never raises out to callers.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Iterable

import websockets
from websockets.exceptions import ConnectionClosed

from ..config import get_settings
from . import auth
from .bus import bus

log = logging.getLogger(__name__)


DEFAULT_CHANNELS = ("fill", "orderbook_snapshot", "orderbook_delta", "ticker")


class KalshiWebSocket:
    def __init__(self, *, channels: Iterable[str] = DEFAULT_CHANNELS, market_tickers: list[str] | None = None) -> None:
        self._settings = get_settings()
        self._channels = list(channels)
        self._tickers = list(market_tickers or [])
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._connected = False
        self._last_error: str | None = None
        self._cmd_id = 0

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
        self._task = asyncio.create_task(self._run(), name="kalshi-ws")

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
                backoff = 1.0  # reset after a clean session
            except Exception as e:
                self._connected = False
                self._last_error = str(e)
                # 401 on the upgrade almost always means the credential
                # belongs to the *other* cluster (prod key against demo, or
                # vice-versa). Surface that hint to operators.
                hint = ""
                if "401" in str(e):
                    hint = (
                        f" — HTTP 401 on the WS upgrade usually means the API"
                        f" key is for a different Kalshi cluster than"
                        f" KALSHI_ENV={self._settings.kalshi_env!r}"
                    )
                log.warning("Kalshi WS error: %s%s (reconnect in %.1fs)", e, hint, backoff)
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 30.0)

    async def _run_once(self) -> None:
        url = self._settings.kalshi_ws_url
        # Sign the WS upgrade. Signing path is the URL path of the WS endpoint.
        from urllib.parse import urlsplit
        sign_path = urlsplit(url).path
        headers = auth.sign_request(
            key_id=self._settings.kalshi_api_key_id or "",
            private_key_pem=self._settings.kalshi_private_key or "",
            method="GET",
            path=sign_path,
        )
        log.info("Kalshi WS connecting %s", url)
        async with websockets.connect(
            url,
            additional_headers=list(headers.items()),
            ping_interval=20,
            ping_timeout=20,
            max_size=4 * 1024 * 1024,
        ) as ws:
            self._connected = True
            self._last_error = None
            await self._subscribe(ws)
            await bus.publish({"type": "ws_status", "connected": True})
            try:
                while not self._stop.is_set():
                    raw = await ws.recv()
                    try:
                        if isinstance(raw, (str, bytes, bytearray)):
                            # Orderbook snapshots can be 1-4 MB; parse them
                            # in a thread so the event loop stays responsive.
                            # Small frames (fills, tickers, deltas) are parsed
                            # inline — the asyncio.to_thread overhead (~50 µs)
                            # would dominate for sub-16 KB payloads.
                            if len(raw) > 16_384:
                                msg = await asyncio.to_thread(json.loads, raw)
                            else:
                                msg = json.loads(raw)
                        else:
                            msg = raw
                    except json.JSONDecodeError:
                        log.warning("Kalshi WS non-JSON frame ignored")
                        continue
                    await bus.publish({"type": "kalshi_event", "msg": msg})
            except ConnectionClosed:
                pass
            finally:
                self._connected = False
                await bus.publish({"type": "ws_status", "connected": False})

    async def _subscribe(self, ws) -> None:
        self._cmd_id += 1
        params: dict = {"channels": self._channels}
        if self._tickers:
            params["market_tickers"] = self._tickers
        await ws.send(json.dumps({"id": self._cmd_id, "cmd": "subscribe", "params": params}))


_singleton: KalshiWebSocket | None = None


def get_ws_subscriber() -> KalshiWebSocket | None:
    return _singleton


async def maybe_start_ws() -> KalshiWebSocket | None:
    """Start the WS subscriber if credentials are configured. Idempotent."""
    global _singleton
    s = get_settings()
    if not s.kalshi_api_key_id or not s.kalshi_private_key:
        log.info("Kalshi WS not started — credentials missing")
        return None
    if _singleton is None:
        _singleton = KalshiWebSocket()
    await _singleton.start()
    return _singleton


async def stop_ws() -> None:
    global _singleton
    if _singleton is not None:
        await _singleton.stop()
        _singleton = None
