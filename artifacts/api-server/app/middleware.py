"""ASGI middleware: per-request id + structured access logging."""
from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

import structlog

from .observability import bind_request_id, clear_request_context

REQUEST_ID_HEADER = "x-request-id"

_log = structlog.stdlib.get_logger("axiom.api.access")


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Inject/propagate ``x-request-id`` and emit a JSON access log line.

    * If the inbound request carries an ``x-request-id`` header, we reuse it
      verbatim (capped at 128 chars to keep log volume bounded).  Otherwise
      a fresh uuid4 hex is minted.
    * The id is bound to ``contextvars`` so any downstream
      ``logging.getLogger(...).info(...)`` call (in the request handler, in
      a background ``asyncio.create_task``, in a structlog logger) carries
      the same correlation id.
    * The id is echoed back on the response so the client can stitch a
      single trace front-to-back.
    """

    def __init__(self, app: ASGIApp, *, header: str = REQUEST_ID_HEADER) -> None:
        super().__init__(app)
        self._header = header

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        incoming = request.headers.get(self._header)
        if incoming:
            incoming = incoming.strip()[:128] or None
        request_id = bind_request_id(incoming)

        route = request.url.path
        method = request.method
        structlog.contextvars.bind_contextvars(route=route, method=method)

        start = time.perf_counter()
        status_code = 500
        try:
            response: Response = await call_next(request)
            status_code = response.status_code
            response.headers[self._header] = request_id
            return response
        finally:
            latency_ms = round((time.perf_counter() - start) * 1000.0, 2)
            _log.info(
                "http_request",
                method=method,
                route=route,
                status_code=status_code,
                latency_ms=latency_ms,
            )
            clear_request_context()
