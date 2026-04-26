"""Kalshi REST client (async, httpx-based).

Each call signs the request with the operator's RSA private key and parses
Kalshi's modern "dollar-era" responses (cents are integers, dollars are floats).

The client is intentionally thin: it returns the parsed JSON dict for endpoints
where Kalshi may add fields, and small typed dataclasses for the bits we need
(balance, position summary).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..config import get_settings
from ..http_retry import retry_request
from . import auth

log = logging.getLogger(__name__)


def _parse_position(row: dict[str, Any]) -> float:
    """Read a Kalshi position size from a ``/portfolio/positions`` row.

    Kalshi's modern payload uses ``position_fp`` (a string-encoded float
    that supports fractional contracts via partial-fill aggregation).
    The legacy ``position`` integer field is left in the response for
    back-compat but is **always ``0`` for accounts that have any
    fractional activity** — so reading it first silently hides every
    open position.  We honour ``position_fp`` first and fall back to
    the legacy field only when ``position_fp`` is missing.
    """
    fp = row.get("position_fp")
    if fp is not None and fp != "":
        try:
            return float(fp)
        except (TypeError, ValueError):
            pass
    legacy = row.get("position")
    try:
        return float(legacy or 0)
    except (TypeError, ValueError):
        return 0.0


def _dollars_or_cents(
    row: dict[str, Any], *, dollars_key: str, cents_key: str
) -> int:
    """Return a cent-denominated amount from a Kalshi position row.

    Kalshi's modern payload ships dollar-denominated string fields
    (e.g. ``market_exposure_dollars = "9.620000"``) alongside the
    legacy integer-cents fields (which are zero for accounts with any
    fractional activity).  Prefer the dollar field, converting via
    ``round(dollars * 100)``, and fall back to the legacy cents field
    when the dollar field is absent.
    """
    dval = row.get(dollars_key)
    if dval is not None and dval != "":
        try:
            return int(round(float(dval) * 100))
        except (TypeError, ValueError):
            pass
    try:
        return int(row.get(cents_key) or 0)
    except (TypeError, ValueError):
        return 0


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
    # Signed contracts (positive=YES, negative=NO).  Kalshi's newer payload
    # uses ``position_fp`` (a string-encoded float to support fractional
    # contracts), so this field is a float to preserve precision for
    # markets that sweep partial fills.  Legacy ``position`` (integer)
    # is honoured as a fallback when ``position_fp`` is absent.
    position: float
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
                    position=_parse_position(row),
                    market_exposure_cents=_dollars_or_cents(
                        row, dollars_key="market_exposure_dollars", cents_key="market_exposure"
                    ),
                    realized_pnl_cents=_dollars_or_cents(
                        row, dollars_key="realized_pnl_dollars", cents_key="realized_pnl"
                    ),
                    fees_paid_cents=_dollars_or_cents(
                        row, dollars_key="fees_paid_dollars", cents_key="fees_paid"
                    ),
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

    async def list_markets(
        self,
        *,
        limit: int = 50,
        status: str | None = "open",
        series_ticker: str | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List markets via ``GET /markets``.

        ``series_ticker`` filters server-side to a single Kalshi series
        (e.g. ``KXBTC``); ``cursor`` follows the standard Kalshi
        pagination token. Both default to ``None`` so existing callers
        keep their previous behavior.
        """
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        if series_ticker:
            params["series_ticker"] = series_ticker
        if cursor:
            params["cursor"] = cursor
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

    async def get_orderbook(self, ticker: str) -> dict[str, Any]:
        """Pull a one-shot orderbook snapshot for ``ticker``.

        Used by the lifecycle slippage guard as a fallback when the
        WS-fed in-process cache is missing or stale beyond the
        configured freshness window. Kalshi returns
        ``{"orderbook": {"yes": [[price, size], ...], "no": [...]}}``;
        this method returns the parsed body unchanged so the caller
        decides how to apply it (typically ``orderbook_cache.apply_snapshot``).
        """
        return await self._request("GET", f"/markets/{ticker}/orderbook")

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

# Cadence for the background re-probe loop.  10 minutes by default —
# slow enough to add no meaningful load to Kalshi, fast enough to catch
# mid-session credential drift (rotated keys, KALSHI_ENV flipped on a
# running instance) within a short window.
KALSHI_CLUSTER_PROBE_INTERVAL_SECONDS = max(
    60, int(os.environ.get("AXIOM_KALSHI_CLUSTER_PROBE_INTERVAL_SECONDS") or "600")
)
# Hard floor on how often the probe will actually hit the wire — even
# if a caller invokes ``cluster_probe()`` directly (e.g. from a debug
# endpoint) we never re-probe more than once per this window.  The
# boot path bypasses this with ``force=True``.
KALSHI_CLUSTER_PROBE_MIN_INTERVAL_SECONDS = max(
    10, int(os.environ.get("AXIOM_KALSHI_CLUSTER_PROBE_MIN_INTERVAL_SECONDS") or "60")
)

_cluster_probe_state: dict[str, Any] = {"checked": False}
_cluster_probe_lock = asyncio.Lock()
_cluster_probe_task: asyncio.Task | None = None
_cluster_probe_stop = asyncio.Event()

# How long a single Kalshi cluster-mismatch alert stays deduped in
# ``notification_dedupe`` before another page can fire for the same
# (configured_env, key_belongs_to) pair.  Six hours: long enough that a
# string of restarts during a bad deploy generates one page (not one
# per restart), short enough that a key that stays misrouted overnight
# still re-pages a few times so the on-call can't claim they missed it.
KALSHI_CLUSTER_MISMATCH_ALERT_TTL_SECONDS = max(
    60,
    int(os.environ.get("AXIOM_KALSHI_CLUSTER_MISMATCH_ALERT_TTL_SECONDS") or "21600"),
)


def cluster_probe_state() -> dict[str, Any]:
    """Return the latest cluster-probe result (empty until probe has run)."""
    return dict(_cluster_probe_state)


def _iso_utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds")


def _record_cluster_probe_result(result: dict[str, Any]) -> dict[str, Any]:
    """Stamp ``result`` with ``checked_at`` / ``checked_at_epoch`` and
    publish it as the new ``_cluster_probe_state``.

    When the new verdict is the wrong-cluster case (``ok=False`` with a
    non-null ``key_belongs_to``), schedule a dedupe-gated operator alert
    so the same probe result that flips the dashboard card red also
    pages on-call. The dispatch is fire-and-forget on the running event
    loop — failures (e.g. no event loop, dedupe DB unreachable) are
    logged and never propagate to the caller.
    """
    now = time.time()
    stamped = {
        **result,
        "checked": True,
        "checked_at_epoch": now,
        "checked_at": _iso_utc(now),
    }
    _cluster_probe_state.clear()
    _cluster_probe_state.update(stamped)
    _maybe_dispatch_cluster_mismatch_alert(stamped)
    return stamped


def _maybe_dispatch_cluster_mismatch_alert(result: dict[str, Any]) -> None:
    """Schedule a wrong-cluster Postmark alert when ``result`` describes
    the wrong-cluster condition (``ok=False`` with a non-null
    ``key_belongs_to``).  Dedupe is keyed on the ``configured->detected``
    pair so a single misconfigured deploy cannot spam inboxes on every
    restart or every periodic re-probe.
    """
    if result.get("ok"):
        return
    configured = result.get("configured_env")
    detected = result.get("key_belongs_to")
    if not configured or not detected:
        return
    payload = {
        "configured_env": str(configured),
        "key_belongs_to": str(detected),
        "status": result.get("status"),
        "detail": result.get("error"),
    }
    dedupe_label = f"{payload['configured_env']}->{payload['key_belongs_to']}"

    async def _runner() -> None:
        # Lazy import to avoid a notifications -> kalshi import cycle
        # at module load time.
        from ..notifications import alerts as _alerts
        try:
            ok = await _alerts.dedupe_or_skip(
                "kalshi_cluster_mismatch",
                dedupe_label,
                ttl_seconds=KALSHI_CLUSTER_MISMATCH_ALERT_TTL_SECONDS,
            )
        except Exception as e:  # pragma: no cover — fail-open
            log.warning("kalshi cluster-mismatch dedupe failed: %s", e)
            ok = True
        if not ok:
            return
        try:
            _alerts.kalshi_cluster_mismatch(**payload)
        except Exception as e:  # pragma: no cover
            log.warning("kalshi cluster-mismatch alert dispatch failed: %s", e)

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_runner())
    except RuntimeError:
        # No running loop (sync test harness) — run inline so the
        # caller can observe the dispatch.
        try:
            asyncio.run(_runner())
        except Exception as e:  # pragma: no cover
            log.warning("inline kalshi cluster-mismatch dispatch failed: %s", e)


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


async def cluster_probe(*, force: bool = False) -> dict[str, Any]:
    """Authenticated cluster check.

    Runs once at boot and then on a slow cadence (see
    :func:`start_cluster_probe_loop`) so mid-session credential drift
    — a rotated key, ``KALSHI_ENV`` changed without a full restart —
    is caught instead of silently 401'ing the WS reconnect loop.

    Returns a dict with at least: ``ok``, ``configured_env``,
    ``key_belongs_to`` (``"prod"|"demo"|None``), ``checked``,
    ``checked_at`` (ISO-8601 UTC), ``checked_at_epoch`` (float), and
    either ``detail`` or ``error``. Never raises. The result is
    cached in :func:`cluster_probe_state` so /api/health can surface
    the latest snapshot.

    ``force=False`` (the default) honors a hard floor on how often the
    probe will actually hit the wire
    (``AXIOM_KALSHI_CLUSTER_PROBE_MIN_INTERVAL_SECONDS``, default 60s)
    so a chatty caller can't add meaningful load to Kalshi. The boot
    path passes ``force=True`` so the first check runs unconditionally.
    """
    # Short-circuit if a recent result is still warm.  This is the
    # rate-limit promised by the task: even if many callers invoke
    # ``cluster_probe()`` at once, we re-probe at most once per
    # min-interval window.  The periodic loop sleeps for much longer
    # than this, so it is unaffected.
    last_at = _cluster_probe_state.get("checked_at_epoch")
    if (
        not force
        and isinstance(last_at, (int, float))
        and (time.time() - float(last_at)) < KALSHI_CLUSTER_PROBE_MIN_INTERVAL_SECONDS
    ):
        return dict(_cluster_probe_state)

    async with _cluster_probe_lock:
        # Re-check inside the lock: another waiter may have just
        # refreshed the state while we queued.
        last_at = _cluster_probe_state.get("checked_at_epoch")
        if (
            not force
            and isinstance(last_at, (int, float))
            and (time.time() - float(last_at)) < KALSHI_CLUSTER_PROBE_MIN_INTERVAL_SECONDS
        ):
            return dict(_cluster_probe_state)
        return await _run_cluster_probe()


async def _run_cluster_probe() -> dict[str, Any]:
    """Actually hit the wire.  Caller is responsible for rate-limiting."""
    s = get_settings()
    if not s.kalshi_api_key_id or not s.kalshi_private_key:
        return _record_cluster_probe_result({
            "ok": False,
            "configured_env": s.kalshi_env,
            "key_belongs_to": None,
            "error": "missing KALSHI_API_KEY_ID or KALSHI_API_PRIVATE_KEY",
        })

    configured_env = s.kalshi_env
    configured_url = s.kalshi_api_base_url
    try:
        primary = await _signed_balance_call(configured_url)
    except Exception as e:
        return _record_cluster_probe_result({
            "ok": False,
            "configured_env": configured_env,
            "key_belongs_to": None,
            "error": f"network error probing {configured_env!r}: {e}"[:300],
        })

    status = int(primary.get("status") or 0)
    if 200 <= status < 300:
        return _record_cluster_probe_result({
            "ok": True,
            "configured_env": configured_env,
            "key_belongs_to": configured_env,
            "status": status,
            "detail": f"authenticated against cluster {configured_env!r}",
        })

    # Anything other than an auth-rejection: don't mislabel as
    # wrong-cluster. The operator likely has a different problem
    # (network, 5xx, expired key id, etc).
    if status not in (401, 403, 404):
        return _record_cluster_probe_result({
            "ok": False,
            "configured_env": configured_env,
            "key_belongs_to": None,
            "status": status,
            "error": f"http {status} from cluster {configured_env!r}: {str(primary.get('body'))[:200]}",
        })

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
        return _record_cluster_probe_result({
            "ok": False,
            "configured_env": configured_env,
            "key_belongs_to": other_env,
            "status": status,
            "error": msg,
        })
    msg = (
        f"Kalshi credentials rejected by cluster {configured_env!r} "
        f"(http {status}); the other cluster {other_env!r} also "
        f"returned http {sec_status}. Check KALSHI_API_KEY_ID and "
        f"KALSHI_API_PRIVATE_KEY."
    )
    return _record_cluster_probe_result({
        "ok": False,
        "configured_env": configured_env,
        "key_belongs_to": None,
        "status": status,
        "error": msg,
    })


# ---------------------------------------------------------------------------
# Background re-probe loop
# ---------------------------------------------------------------------------
#
# Task #311: the boot probe is not enough — if Kalshi rotates a key
# mid-session or KALSHI_ENV is flipped on a running instance, the
# cached "ok" verdict will keep reporting healthy while live calls
# 401.  A slow background re-probe keeps the cached state honest and
# refreshes the /api/health card without adding meaningful Kalshi
# load (one signed GET every 10 minutes by default).


async def _cluster_probe_loop() -> None:
    while not _cluster_probe_stop.is_set():
        try:
            await asyncio.wait_for(
                _cluster_probe_stop.wait(),
                timeout=KALSHI_CLUSTER_PROBE_INTERVAL_SECONDS,
            )
        except asyncio.TimeoutError:
            pass
        else:
            return
        try:
            res = await cluster_probe(force=True)
            if res.get("ok"):
                log.debug(
                    "kalshi cluster re-probe ok (env=%s)", res.get("configured_env")
                )
            else:
                log.warning(
                    "kalshi cluster re-probe FAILED: %s", res.get("error")
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — never let the loop die
            log.warning("kalshi cluster re-probe raised: %s", e)


async def start_cluster_probe_loop() -> None:
    """Kick off the background re-probe loop.

    Idempotent: a second call while the loop is already running is a
    no-op. The boot-time one-shot probe in ``app.main`` runs separately
    so the cached state is populated before this loop's first sleep.
    """
    global _cluster_probe_task
    if _cluster_probe_task and not _cluster_probe_task.done():
        return
    _cluster_probe_stop.clear()
    _cluster_probe_task = asyncio.create_task(
        _cluster_probe_loop(), name="kalshi-cluster-probe"
    )
    log.info(
        "kalshi cluster re-probe loop started (interval=%ss)",
        KALSHI_CLUSTER_PROBE_INTERVAL_SECONDS,
    )


async def stop_cluster_probe_loop() -> None:
    """Signal the re-probe loop to exit and wait for it."""
    global _cluster_probe_task
    _cluster_probe_stop.set()
    if _cluster_probe_task:
        _cluster_probe_task.cancel()
        try:
            await _cluster_probe_task
        except (asyncio.CancelledError, Exception):
            pass
    _cluster_probe_task = None
