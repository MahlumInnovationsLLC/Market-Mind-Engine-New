"""Clerk Frontend API proxy for production deployments.

Python/FastAPI port of the canonical Express middleware shipped with
the ``clerk-auth`` skill
(``.local/skills/clerk-auth/templates/api-server/src/middlewares/clerkProxyMiddleware.ts``).

The Clerk web SDK is configured with ``proxyUrl =
https://<app-domain>/api/__clerk`` in production builds (auto-injected
via ``VITE_CLERK_PROXY_URL`` at publish time).  Every Clerk request --
SDK bundle download, Frontend API calls, OAuth callbacks -- is then
issued by the browser against our own domain on that path.  We
transparently forward those requests to Clerk's Frontend API, attach
the ``Clerk-Secret-Key`` and ``Clerk-Proxy-Url`` headers Clerk requires,
and stream the upstream response back to the browser.

Without this route mounted, production sign-in renders a permanently
blank page: the SDK's bundle fetch 404s, ``ClerkProvider`` never
finishes initialising, and the ``<SignIn>`` component never paints.

Activation rules (mirroring the Express template):

* Only registered when ``CLERK_SECRET_KEY`` is set in the environment.
  Dev builds use ``VITE_CLERK_PROXY_URL=""`` so the SDK hits Clerk's
  CDN directly and never touches this path.

The proxy path is hard-coded to ``/api/__clerk`` to match the skill
contract (see ``setup-and-customization.md`` "Proxy path is hardcoded
to /api/__clerk").
"""

from __future__ import annotations

import logging
import os
from typing import Iterable

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse

from .http_retry import retry_request

log = logging.getLogger(__name__)

# Only idempotent verbs are safe to retry transparently — retrying a POST/PUT
# etc. against Clerk's Frontend API could double-apply a mutation (e.g. create
# a second session), so those stay single-shot.
_RETRYABLE_CLERK_METHODS: frozenset[str] = frozenset({"GET", "HEAD"})

CLERK_FAPI = "https://frontend-api.clerk.dev"
CLERK_PROXY_PATH = "/api/__clerk"

# Hop-by-hop headers per RFC 7230 §6.1 -- never forwarded by a proxy.
# We additionally drop ``host`` (httpx sets it from the target URL) and
# ``content-length`` (httpx recomputes it from the body we hand it).
_HOP_BY_HOP: frozenset[str] = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)


def _client_host(request: Request) -> str | None:
    """Return the public-facing hostname Clerk should see in
    ``Clerk-Proxy-Url``.

    Prefers the leftmost ``X-Forwarded-Host`` value (original client-
    facing host when multiple proxy hops appended rather than replaced
    the header) and falls back to the ``Host`` header.  Matches
    ``getClerkProxyHost`` in the canonical Express middleware.
    """

    forwarded = request.headers.get("x-forwarded-host")
    if forwarded:
        first_hop = forwarded.split(",", 1)[0].strip()
        if first_hop:
            return first_hop
    host = request.headers.get("host")
    return host.strip() if host else None


def _client_ip(request: Request) -> str | None:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",", 1)[0].strip()
        if first:
            return first
    client = request.client
    return client.host if client else None


def _forward_request_headers(
    request: Request, *, proxy_url: str, secret_key: str, client_ip: str | None
) -> dict[str, str]:
    headers: dict[str, str] = {}
    for name, value in request.headers.items():
        if name.lower() in _HOP_BY_HOP:
            continue
        headers[name] = value
    headers["Clerk-Proxy-Url"] = proxy_url
    headers["Clerk-Secret-Key"] = secret_key
    if client_ip:
        headers["X-Forwarded-For"] = client_ip
    return headers


def _forward_response_headers(
    upstream_headers: httpx.Headers,
) -> list[tuple[bytes, bytes]]:
    """Return upstream headers as raw (bytes, bytes) tuples, preserving
    duplicates.

    Collapsing into a ``dict`` would lose multi-valued headers --
    critically ``Set-Cookie``, which Clerk's session/OAuth flows rely
    on (multiple cookies, one header per cookie).  Starlette's
    ``Response`` accepts raw header tuples via ``raw_headers``, which
    keeps duplicates intact.
    """

    raw: list[tuple[bytes, bytes]] = []
    for name, value in upstream_headers.multi_items():
        if name.lower() in _HOP_BY_HOP:
            continue
        raw.append((name.lower().encode("latin-1"), value.encode("latin-1")))
    return raw


def register_clerk_proxy(app: FastAPI) -> bool:
    """Mount the Clerk proxy on ``app`` when configured.

    Returns ``True`` when the route was registered, ``False`` when the
    proxy is disabled (no ``CLERK_SECRET_KEY``) so the caller can log
    the skipped state.
    """

    secret_key = os.environ.get("CLERK_SECRET_KEY")
    if not secret_key:
        log.info(
            "clerk_proxy not registered: CLERK_SECRET_KEY not set "
            "(expected in dev, required in production)"
        )
        return False

    # Module-scoped client so we get connection pooling across requests.
    # ``timeout=None`` lets Clerk's own deadlines apply (matches the
    # Express ``http-proxy-middleware`` default behaviour).
    client = httpx.AsyncClient(
        base_url=CLERK_FAPI,
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=False,
    )

    # The httpx client lives for the process lifetime; on shutdown the
    # OS reclaims the sockets.  We intentionally do NOT register an
    # ``@app.on_event("shutdown")`` hook here because the FastAPI app
    # already uses a ``lifespan`` context manager and mixing the two
    # APIs triggers a deprecation warning -- the cleanup payoff at
    # process exit is not worth the noise.

    _METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]

    @app.api_route(
        CLERK_PROXY_PATH,
        methods=_METHODS,
        include_in_schema=False,
        name="clerk_proxy_root",
    )
    async def clerk_proxy_root(request: Request) -> Response:
        return await _do_proxy("", request)

    @app.api_route(
        CLERK_PROXY_PATH + "/{clerk_path:path}",
        methods=_METHODS,
        include_in_schema=False,
        name="clerk_proxy",
    )
    async def clerk_proxy(clerk_path: str, request: Request) -> Response:
        return await _do_proxy(clerk_path, request)

    async def _do_proxy(clerk_path: str, request: Request) -> Response:
        host = _client_host(request)
        proto = request.headers.get("x-forwarded-proto", "https")
        proxy_url = f"{proto}://{host}{CLERK_PROXY_PATH}" if host else CLERK_PROXY_PATH
        client_ip = _client_ip(request)

        target_path = "/" + clerk_path if clerk_path else "/"
        query = request.url.query
        if query:
            target_path = f"{target_path}?{query}"

        body = await request.body()
        headers = _forward_request_headers(
            request,
            proxy_url=proxy_url,
            secret_key=secret_key,
            client_ip=client_ip,
        )

        async def _do_upstream() -> httpx.Response:
            return await client.request(
                request.method,
                target_path,
                content=body if body else None,
                headers=headers,
            )

        try:
            if request.method.upper() in _RETRYABLE_CLERK_METHODS:
                upstream = await retry_request(
                    _do_upstream,
                    client_name="clerk_proxy",
                    max_attempts=3,
                )
            else:
                upstream = await _do_upstream()
        except httpx.HTTPError as exc:
            log.warning(
                "clerk_proxy upstream error path=%s err=%s",
                target_path,
                exc,
            )
            return Response(
                content=b"Upstream Clerk request failed",
                status_code=502,
                media_type="text/plain",
            )

        raw_headers = _forward_response_headers(upstream.headers)
        # Construct an empty Response, then overwrite raw_headers so
        # multi-valued headers (Set-Cookie etc.) survive intact.
        response = Response(
            content=upstream.content,
            status_code=upstream.status_code,
        )
        if not any(name == b"content-length" for name, _ in raw_headers):
            raw_headers.append(
                (b"content-length", str(len(upstream.content)).encode("latin-1"))
            )
        response.raw_headers = raw_headers
        return response

    log.info("clerk_proxy registered at %s -> %s", CLERK_PROXY_PATH, CLERK_FAPI)
    return True


__all__ = ["register_clerk_proxy", "CLERK_PROXY_PATH", "CLERK_FAPI"]
