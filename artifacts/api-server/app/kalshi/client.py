"""Kalshi REST client (async, httpx-based).

Each call signs the request with the operator's RSA private key and parses
Kalshi's modern "dollar-era" responses (cents are integers, dollars are floats).

The client is intentionally thin: it returns the parsed JSON dict for endpoints
where Kalshi may add fields, and small typed dataclasses for the bits we need
(balance, position summary).
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


class KalshiAPIError(RuntimeError):
    def __init__(self, status: int, body: Any, message: str = "") -> None:
        super().__init__(message or f"Kalshi API error {status}: {body!r}")
        self.status = status
        self.body = body


@dataclass
class Balance:
    balance_cents: int
    payout_cents: int
    raw: dict[str, Any]

    @property
    def balance_dollars(self) -> float:
        return self.balance_cents / 100.0


@dataclass
class PositionSummary:
    market_ticker: str
    position: int          # signed contracts (positive=YES, negative=NO)
    market_exposure_cents: int
    realized_pnl_cents: int
    fees_paid_cents: int
    raw: dict[str, Any]


class KalshiClient:
    """Async REST client. Construct once and reuse; closes its httpx client on `aclose()`."""

    def __init__(self, *, settings=None, timeout: float = 15.0) -> None:
        self._settings = settings or get_settings()
        if not self._settings.kalshi_api_key_id or not self._settings.kalshi_private_key:
            raise auth.KalshiAuthError(
                "Kalshi credentials missing — set KALSHI_API_KEY_ID and KALSHI_API_PRIVATE_KEY"
            )
        self._http = httpx.AsyncClient(timeout=timeout)
        self._base = self._settings.kalshi_api_base_url.rstrip("/")
        # Path prefix for signing (e.g. "/trade-api/v2")
        self._base_path = urlsplit(self._base).path or ""

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "KalshiClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    # ---- internal -------------------------------------------------------

    def _sign_path(self, sub_path: str) -> str:
        # Kalshi signs the full path ("/trade-api/v2/..."), not just the suffix.
        return f"{self._base_path}{sub_path}"

    async def _request(
        self,
        method: str,
        sub_path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        # Idempotent reads (GET) get retry-with-backoff on transient
        # 5xx / network errors via the shared helper.  Mutating verbs
        # (POST/PUT/PATCH/DELETE) keep the previous single-shot
        # behaviour to avoid double-submitting orders.
        sign_path = self._sign_path(sub_path)
        url = f"{self._base}{sub_path}"
        is_idempotent = method.upper() == "GET"

        async def _do() -> httpx.Response:
            # Re-sign on every attempt: Kalshi includes a timestamp in
            # the signed payload so a stale signature is rejected on
            # retry.
            headers = auth.sign_request(
                key_id=self._settings.kalshi_api_key_id or "",
                private_key_pem=self._settings.kalshi_private_key or "",
                method=method,
                path=sign_path,
            )
            return await self._http.request(
                method, url, params=params, json=json, headers=headers
            )

        try:
            if is_idempotent:
                r = await retry_request(_do, client_name="kalshi", max_attempts=4)
            else:
                r = await _do()
        except httpx.HTTPError as e:
            raise KalshiAPIError(0, None, f"Kalshi network error: {e}") from e
        try:
            body: Any = r.json() if r.content else None
        except ValueError:
            body = r.text
        if r.status_code >= 400:
            raise KalshiAPIError(r.status_code, body)
        return body

    # ---- public endpoints ----------------------------------------------

    async def exchange_status(self) -> dict[str, Any]:
        return await self._request("GET", "/exchange/status")

    async def get_balance(self) -> Balance:
        data = await self._request("GET", "/portfolio/balance")
        # Kalshi returns "balance" in cents; "payout" sometimes present.
        bal_cents = int(data.get("balance") or 0)
        payout_cents = int(data.get("payout") or 0)
        return Balance(balance_cents=bal_cents, payout_cents=payout_cents, raw=data)

    async def get_positions(self, *, limit: int = 100) -> list[PositionSummary]:
        data = await self._request("GET", "/portfolio/positions", params={"limit": limit})
        out: list[PositionSummary] = []
        for row in (data or {}).get("market_positions", []) or []:
            out.append(
                PositionSummary(
                    market_ticker=row.get("ticker") or row.get("market_ticker") or "",
                    position=int(row.get("position") or 0),
                    market_exposure_cents=int(row.get("market_exposure") or 0),
                    realized_pnl_cents=int(row.get("realized_pnl") or 0),
                    fees_paid_cents=int(row.get("fees_paid") or 0),
                    raw=row,
                )
            )
        return out

    async def get_market(self, ticker: str) -> dict[str, Any]:
        return await self._request("GET", f"/markets/{ticker}")

    async def get_market_candlesticks(
        self,
        ticker: str,
        *,
        start_ts: int,
        end_ts: int,
        period_interval: int = 1,
    ) -> dict[str, Any]:
        """Fetch market price history (candlesticks) for ``ticker``.

        Kalshi exposes per-market candlesticks at
        ``GET /series/{series_ticker}/markets/{ticker}/candlesticks``.
        ``period_interval`` is the bar size in minutes — Kalshi accepts
        ``1``, ``60``, or ``1440``. ``start_ts`` / ``end_ts`` are unix
        epoch seconds (the venue caps the returned window to ~5000
        bars per call).

        The returned dict carries a ``candlesticks`` list whose entries
        have at least ``end_period_ts`` and a ``price`` block with
        ``open``/``high``/``low``/``close``/``mean`` in cents (0..100).
        Callers that just want a market-vs-model overlay should use
        ``price.mean`` (or ``price.close`` when ``mean`` is null —
        Kalshi nulls it for empty windows).
        """
        series_ticker = (ticker or "").split("-", 1)[0]
        if not series_ticker or "-" not in (ticker or ""):
            raise KalshiAPIError(
                0, None, f"cannot derive series ticker from {ticker!r}"
            )
        params = {
            "start_ts": int(start_ts),
            "end_ts": int(end_ts),
            "period_interval": int(period_interval),
        }
        return await self._request(
            "GET",
            f"/series/{series_ticker}/markets/{ticker}/candlesticks",
            params=params,
        )

    async def list_markets(self, *, limit: int = 50, status: str | None = "open") -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        return await self._request("GET", "/markets", params=params)

    async def submit_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Submit a new order. Caller is responsible for validating payload shape.

        Kalshi's `POST /portfolio/orders` expects:
          { ticker, action: "buy"|"sell", side: "yes"|"no",
            type: "limit"|"market", count, yes_price?|no_price?,
            client_order_id, time_in_force? }
        """
        return await self._request("POST", "/portfolio/orders", json=payload)

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        return await self._request("DELETE", f"/portfolio/orders/{order_id}")

    async def get_order(self, order_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/portfolio/orders/{order_id}")

    async def get_order_by_client_id(
        self, client_order_id: str
    ) -> dict[str, Any]:
        """Look up an order by its caller-supplied ``client_order_id``.

        Used by the submit-recovery path: when ``POST /portfolio/orders``
        returns a timeout / 5xx, we don't know whether the venue accepted
        the order. Every submit attaches a UUID ``client_order_id``, so
        we can ask Kalshi "do you have an order with this id?" and apply
        the venue's truth locally.

        Returns ``{"found": bool, "order": dict|None}``. Never raises
        the "not found" case — that's a normal outcome. Other HTTP
        errors (auth, 5xx on the lookup itself, network) DO propagate
        as :class:`KalshiAPIError` so the caller can decide to retry
        next cycle vs. give up.

        Kalshi's ``GET /portfolio/orders`` accepts a ``client_order_id``
        query param and returns a list filtered to that id (0 or 1
        matches). The shape is ``{"orders": [...]}``; older responses
        wrap the single order in ``{"order": {...}}`` — handle both.
        Uses the shared GET retry, so transient 5xx/network errors on
        the lookup itself are retried before surfacing.
        """
        if not client_order_id:
            return {"found": False, "order": None}
        data = await self._request(
            "GET",
            "/portfolio/orders",
            params={"client_order_id": client_order_id, "limit": 1},
        )
        body = data or {}
        # Prefer the plural "orders" list; fall back to a singular key.
        rows = body.get("orders")
        if not isinstance(rows, list):
            single = body.get("order") or body
            rows = [single] if isinstance(single, dict) and single.get("order_id") else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            # Strict match only: never promote an unrelated order, even if
            # the venue returns a single row for some other reason. If the
            # row doesn't carry our client_order_id we say "not found" and
            # let the submit retry on the next cycle.
            row_coid = row.get("client_order_id") or row.get("clientOrderId")
            if row_coid == client_order_id:
                return {"found": True, "order": row}
        return {"found": False, "order": None}

    async def list_fills(
        self,
        *,
        min_ts: int | None = None,
        limit: int = 200,
        ticker: str | None = None,
    ) -> list[dict[str, Any]]:
        """Pull recent fills from `/portfolio/fills`.

        Kalshi paginates fills with a `cursor` token. We follow it until
        either the page has no `cursor` or we've collected `limit` rows
        (whichever comes first). `min_ts` is the unix-epoch second cutoff
        — only fills at-or-after it are returned. Returns the raw fill
        dicts so the reconciler can inspect every field Kalshi adds in
        the future without needing client changes.
        """
        out: list[dict[str, Any]] = []
        params: dict[str, Any] = {"limit": min(200, max(1, int(limit)))}
        if min_ts is not None:
            params["min_ts"] = int(min_ts)
        if ticker:
            params["ticker"] = ticker
        cursor: str | None = None
        # Cap at 5 pages so a misconfigured `min_ts` can't page forever.
        for _ in range(5):
            if cursor:
                params["cursor"] = cursor
            data = await self._request("GET", "/portfolio/fills", params=params)
            page = (data or {}).get("fills") or []
            out.extend([row for row in page if isinstance(row, dict)])
            if len(out) >= limit:
                return out[:limit]
            cursor = (data or {}).get("cursor")
            if not cursor:
                break
        return out


async def health_probe() -> dict[str, Any]:
    """Cheap unauthenticated check: hit `/exchange/status` with our key.

    Returns `{ok, status?, error?}`. Never raises.
    """
    s = get_settings()
    if not s.kalshi_api_key_id or not s.kalshi_private_key:
        return {"ok": False, "error": "missing credentials"}
    try:
        async with KalshiClient(settings=s, timeout=8.0) as c:
            data = await c.exchange_status()
        return {"ok": True, "exchange_active": bool(data.get("exchange_active", True)), "raw": data}
    except KalshiAPIError as e:
        return {"ok": False, "error": f"http {e.status}: {e.body!r}"[:300]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


# ---------------------------------------------------------------------------
# Cluster auto-detect probe
# ---------------------------------------------------------------------------
#
# Task #20 root-caused a long outage to prod credentials being pointed at
# the demo cluster: every signed REST call and the WS upgrade silently
# 401'd, but ``/exchange/status`` is unauthenticated so the existing
# health check stayed green. The probe below runs once at boot, signs a
# real authenticated call (``GET /portfolio/balance``), and if it gets
# 401'd from the configured cluster it tries the *other* cluster too so
# the operator sees "your key actually belongs to <other>" instead of a
# slow drip of 401s in the reconnect loop.

KALSHI_PROD_BASE = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_DEMO_BASE = "https://demo-api.kalshi.co/trade-api/v2"

_cluster_probe_state: dict[str, Any] = {"checked": False}


def cluster_probe_state() -> dict[str, Any]:
    """Return the latest cluster-probe result (empty until probe has run)."""
    return dict(_cluster_probe_state)


async def _signed_balance_call(base_url: str) -> dict[str, Any]:
    """Sign+send ``GET /portfolio/balance`` against ``base_url``.

    Returns ``{status, body}`` for any HTTP response, or raises on
    network errors. Never raises ``KalshiAPIError``.
    """
    s = get_settings()
    base = base_url.rstrip("/")
    sub_path = "/portfolio/balance"
    sign_path = (urlsplit(base).path or "") + sub_path
    headers = auth.sign_request(
        key_id=s.kalshi_api_key_id or "",
        private_key_pem=s.kalshi_private_key or "",
        method="GET",
        path=sign_path,
    )
    async with httpx.AsyncClient(timeout=8.0) as c:
        r = await c.get(f"{base}{sub_path}", headers=headers)
    body: Any
    try:
        body = r.json() if r.content else None
    except ValueError:
        body = r.text
    return {"status": r.status_code, "body": body}


async def cluster_probe() -> dict[str, Any]:
    """Authenticated cluster check. Runs once at boot.

    Returns a dict with at least: ``ok``, ``configured_env``,
    ``key_belongs_to`` (``"prod"|"demo"|None``), and either ``detail``
    or ``error``. Never raises. The result is cached in
    :func:`cluster_probe_state` so /api/health can surface it.
    """
    s = get_settings()
    if not s.kalshi_api_key_id or not s.kalshi_private_key:
        result = {
            "ok": False,
            "configured_env": s.kalshi_env,
            "key_belongs_to": None,
            "error": "missing KALSHI_API_KEY_ID or KALSHI_API_PRIVATE_KEY",
        }
        _cluster_probe_state.clear()
        _cluster_probe_state.update({"checked": True, **result})
        return result

    configured_env = s.kalshi_env
    configured_url = s.kalshi_api_base_url
    try:
        primary = await _signed_balance_call(configured_url)
    except Exception as e:
        result = {
            "ok": False,
            "configured_env": configured_env,
            "key_belongs_to": None,
            "error": f"network error probing {configured_env!r}: {e}"[:300],
        }
        _cluster_probe_state.clear()
        _cluster_probe_state.update({"checked": True, **result})
        return result

    status = int(primary.get("status") or 0)
    if 200 <= status < 300:
        result = {
            "ok": True,
            "configured_env": configured_env,
            "key_belongs_to": configured_env,
            "status": status,
            "detail": f"authenticated against cluster {configured_env!r}",
        }
        _cluster_probe_state.clear()
        _cluster_probe_state.update({"checked": True, **result})
        return result

    # Anything other than an auth-rejection: don't mislabel as
    # wrong-cluster. The operator likely has a different problem
    # (network, 5xx, expired key id, etc).
    if status not in (401, 403, 404):
        result = {
            "ok": False,
            "configured_env": configured_env,
            "key_belongs_to": None,
            "status": status,
            "error": f"http {status} from cluster {configured_env!r}: {str(primary.get('body'))[:200]}",
        }
        _cluster_probe_state.clear()
        _cluster_probe_state.update({"checked": True, **result})
        return result

    # Auth rejected on the configured cluster. Probe the OTHER cluster
    # to see if the key actually belongs there.
    other_env = "demo" if configured_env == "prod" else "prod"
    other_url = KALSHI_DEMO_BASE if other_env == "demo" else KALSHI_PROD_BASE
    try:
        secondary = await _signed_balance_call(other_url)
        sec_status = int(secondary.get("status") or 0)
    except Exception as e:
        log.warning("kalshi cluster cross-probe of %s failed: %s", other_env, e)
        secondary = {"status": 0, "body": f"network error: {e}"}
        sec_status = 0

    if 200 <= sec_status < 300:
        msg = (
            f"Kalshi credentials look valid but were rejected by cluster "
            f"{configured_env!r} — the key authenticates against "
            f"{other_env!r}. Set KALSHI_ENV={other_env}."
        )
        result = {
            "ok": False,
            "configured_env": configured_env,
            "key_belongs_to": other_env,
            "status": status,
            "error": msg,
        }
    else:
        msg = (
            f"Kalshi credentials rejected by cluster {configured_env!r} "
            f"(http {status}); the other cluster {other_env!r} also "
            f"returned http {sec_status}. Check KALSHI_API_KEY_ID and "
            f"KALSHI_API_PRIVATE_KEY."
        )
        result = {
            "ok": False,
            "configured_env": configured_env,
            "key_belongs_to": None,
            "status": status,
            "error": msg,
        }
    _cluster_probe_state.clear()
    _cluster_probe_state.update({"checked": True, **result})
    return result
