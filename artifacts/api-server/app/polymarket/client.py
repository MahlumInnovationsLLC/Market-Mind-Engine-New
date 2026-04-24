"""Polymarket CLOB REST client (async, httpx-based).

Thin wrapper that signs each request with L2 HMAC headers and parses the
CLOB's JSON responses. Public market endpoints (e.g. `/markets`) work
without auth; private endpoints (`/balance-allowance`, `/orders`, `/order`)
require the L2 credentials.

Polymarket prices are decimals in `[0, 1]`. To keep parity with the
Kalshi-flavoured lifecycle (which deals in 1..99 cents), helpers convert
between the two representations.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..config import get_settings
from ..http_retry import retry_request
from . import auth

log = logging.getLogger(__name__)


class PolymarketAPIError(RuntimeError):
    def __init__(self, status: int, body: Any, message: str = "") -> None:
        super().__init__(message or f"Polymarket API error {status}: {body!r}")
        self.status = status
        self.body = body


@dataclass
class Balance:
    """USDC balance/allowance for the configured wallet, in cents.

    The CLOB returns balances as integer micro-USDC strings; we convert
    to whole cents so the surface looks like Kalshi's `Balance`.
    """
    balance_cents: int
    allowance_cents: int
    raw: dict[str, Any]

    @property
    def balance_dollars(self) -> float:
        return self.balance_cents / 100.0


@dataclass
class PositionSummary:
    market_id: str          # condition id (e.g. "0xabc…")
    token_id: str           # ERC1155 outcome token id
    outcome: str            # "Yes" / "No" / outcome label as reported
    size: float             # signed contracts
    avg_price: float        # decimal probability 0..1
    realized_pnl_cents: int
    raw: dict[str, Any]


def _to_cents(decimal_price: Any) -> int:
    try:
        return int(round(float(decimal_price) * 100.0))
    except (TypeError, ValueError):
        return 0


class PolymarketClient:
    """Async REST client. Construct once and reuse; closes its httpx client on `aclose()`."""

    def __init__(self, *, settings=None, timeout: float = 15.0) -> None:
        self._settings = settings or get_settings()
        self._http = httpx.AsyncClient(timeout=timeout)
        self._base = self._settings.polymarket_api_base_url.rstrip("/")
        self._base_path = urlsplit(self._base).path or ""

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "PolymarketClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    # ---- internal -------------------------------------------------------

    def _have_creds(self) -> bool:
        s = self._settings
        return bool(
            s.polymarket_api_key
            and s.polymarket_api_secret
            and s.polymarket_api_passphrase
            and s.polymarket_wallet_address
        )

    def _sign_path(self, sub_path: str) -> str:
        return f"{self._base_path}{sub_path}"

    async def _request(
        self,
        method: str,
        sub_path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        require_auth: bool = True,
    ) -> Any:
        url = f"{self._base}{sub_path}"
        body_str = ""
        if json is not None:
            import json as _json
            body_str = _json.dumps(json, separators=(",", ":"))
        if require_auth and not self._have_creds():
            raise auth.PolymarketAuthError(
                "Polymarket credentials missing — set POLYMARKET_API_KEY, "
                "POLYMARKET_API_SECRET, POLYMARKET_API_PASSPHRASE and "
                "POLYMARKET_WALLET_ADDRESS"
            )

        # Idempotent reads (GET) get retry-with-backoff via the shared
        # helper; mutating verbs keep the previous single-shot
        # behaviour to avoid duplicate order submission.  The L2 HMAC
        # signature is regenerated on every attempt because Polymarket
        # bakes the current timestamp into the signed payload — a
        # signature reused after the retry-after delay would be
        # rejected as stale, defeating the retry.
        is_idempotent = method.upper() == "GET"

        async def _do() -> httpx.Response:
            headers: dict[str, str] = {"Accept": "application/json"}
            if json is not None:
                headers["Content-Type"] = "application/json"
            if require_auth:
                headers.update(
                    auth.sign_request(
                        api_key=self._settings.polymarket_api_key or "",
                        api_secret=self._settings.polymarket_api_secret or "",
                        passphrase=self._settings.polymarket_api_passphrase or "",
                        address=self._settings.polymarket_wallet_address or "",
                        method=method,
                        path=self._sign_path(sub_path),
                        body=body_str,
                    )
                )
            if json is not None:
                return await self._http.request(
                    method, url, params=params, content=body_str, headers=headers
                )
            return await self._http.request(method, url, params=params, headers=headers)

        try:
            if is_idempotent:
                r = await retry_request(_do, client_name="polymarket", max_attempts=4)
            else:
                r = await _do()
        except httpx.HTTPError as e:
            raise PolymarketAPIError(0, None, f"Polymarket network error: {e}") from e
        try:
            body: Any = r.json() if r.content else None
        except ValueError:
            body = r.text
        if r.status_code >= 400:
            raise PolymarketAPIError(r.status_code, body)
        return body

    # ---- public endpoints ----------------------------------------------

    async def exchange_status(self) -> dict[str, Any]:
        """CLOB has no `/exchange/status` — `/time` is the canonical liveness probe."""
        return await self._request("GET", "/time", require_auth=False)

    async def get_balance(self) -> Balance:
        """`/balance-allowance` returns USDC balance + allowance for the
        configured wallet. The CLOB returns micro-USDC strings; convert
        to cents (1 USDC = 100 cents = 1_000_000 micro-USDC)."""
        data = await self._request(
            "GET",
            "/balance-allowance",
            params={"asset_type": "COLLATERAL"},
        )
        bal_micro = int((data or {}).get("balance") or 0)
        allow_micro = int((data or {}).get("allowance") or 0)
        return Balance(
            balance_cents=bal_micro // 10_000,
            allowance_cents=allow_micro // 10_000,
            raw=data or {},
        )

    async def get_positions(self, *, limit: int = 100) -> list[PositionSummary]:
        """List open positions. Polymarket exposes positions via the
        data-api `/positions` endpoint; the CLOB itself doesn't have one,
        so this is a best-effort fetch that returns `[]` if unavailable."""
        try:
            data = await self._request(
                "GET",
                "/positions",
                params={"limit": limit, "user": self._settings.polymarket_wallet_address or ""},
            )
        except PolymarketAPIError as e:
            if e.status == 404:
                return []
            raise
        out: list[PositionSummary] = []
        rows = data if isinstance(data, list) else (data or {}).get("positions", []) or []
        for row in rows:
            out.append(
                PositionSummary(
                    market_id=str(row.get("market") or row.get("conditionId") or ""),
                    token_id=str(row.get("asset") or row.get("tokenId") or ""),
                    outcome=str(row.get("outcome") or ""),
                    size=float(row.get("size") or 0),
                    avg_price=float(row.get("avgPrice") or row.get("avg_price") or 0),
                    realized_pnl_cents=_to_cents(row.get("realizedPnl") or 0),
                    raw=row,
                )
            )
        return out

    async def get_market(self, condition_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/markets/{condition_id}", require_auth=False)

    async def get_prices_history(
        self,
        token_id: str,
        *,
        start_ts: int,
        end_ts: int,
        fidelity: int = 60,
    ) -> list[dict[str, Any]]:
        """Pull historical trade prices for a single outcome token.

        Wraps the public CLOB ``/prices-history`` endpoint, which returns
        ``{"history": [{"t": <unix>, "p": <0..1>}, ...]}`` — Polymarket's
        analogue of Kalshi's market candlesticks.

        ``fidelity`` is the bar size in minutes (1 / 60 / 1440 to match
        the Kalshi-side caller). Returns an empty list rather than
        raising if the response doesn't include a history array.
        """
        data = await self._request(
            "GET",
            "/prices-history",
            params={
                "market": token_id,
                "startTs": int(start_ts),
                "endTs": int(end_ts),
                "fidelity": int(fidelity),
            },
            require_auth=False,
        )
        if not isinstance(data, dict):
            return []
        history = data.get("history") or []
        return [row for row in history if isinstance(row, dict)]

    async def list_markets(self, *, limit: int = 50, next_cursor: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if next_cursor:
            params["next_cursor"] = next_cursor
        return await self._request("GET", "/markets", params=params, require_auth=False)

    async def submit_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Submit a new order. Caller is responsible for validating payload shape.

        Polymarket's `POST /order` expects an L1-signed (EIP-712) order
        envelope shaped roughly:
          { order: { salt, maker, signer, taker, tokenId, makerAmount,
                     takerAmount, expiration, nonce, feeRateBps, side, signatureType, signature },
            owner, orderType }

        Building/signing the L1 envelope is outside this client; live
        callers must construct and sign it before calling. Paper-mode
        never reaches this method (the lifecycle short-circuits earlier).
        """
        return await self._request("POST", "/order", json=payload)

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        return await self._request("DELETE", f"/order/{order_id}")

    async def get_order(self, order_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/data/order/{order_id}")


async def health_probe() -> dict[str, Any]:
    """Cheap unauthenticated check: hit `/time`. Never raises."""
    s = get_settings()
    try:
        async with PolymarketClient(settings=s, timeout=8.0) as c:
            data = await c.exchange_status()
        return {"ok": True, "raw": data}
    except PolymarketAPIError as e:
        return {"ok": False, "error": f"http {e.status}: {e.body!r}"[:300]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}
