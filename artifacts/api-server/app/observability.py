"""Structured JSON logging + request-id correlation.

Wires structlog onto the stdlib logging root so every existing
``logging.getLogger(__name__).info(...)`` call automatically becomes a JSON
line that includes whatever ``request_id`` (and other request metadata) is
bound to the current ``contextvars.Context``.

Background tasks spawned with ``asyncio.create_task`` from inside a request
handler inherit that context for free — that is how a single
``request_id`` follows a trace through API → swarm → gates → execution.
"""
from __future__ import annotations

import logging
import sys
import uuid
from contextvars import ContextVar
from typing import Any

import structlog

# ---------------------------------------------------------------------------
# Per-request context
# ---------------------------------------------------------------------------

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def new_request_id() -> str:
    """Generate a fresh request id (uuid4 hex, 32 chars)."""
    return uuid.uuid4().hex


def get_request_id() -> str | None:
    """Return the request id bound to the current context, if any."""
    return request_id_var.get()


def bind_request_id(request_id: str | None) -> str:
    """Bind ``request_id`` to the current context.

    If ``request_id`` is falsy, a fresh one is generated.  The structlog
    contextvars binding is also refreshed so the value shows up on every
    subsequent log line in this context.
    """
    rid = request_id or new_request_id()
    request_id_var.set(rid)
    structlog.contextvars.bind_contextvars(request_id=rid)
    return rid


def clear_request_context() -> None:
    """Tear down per-request structlog contextvars (call at request end)."""
    structlog.contextvars.clear_contextvars()
    request_id_var.set(None)


# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------

_CONFIGURED = False


def _request_id_processor(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Ensure every log line carries a ``request_id`` field (possibly null)."""
    if "request_id" not in event_dict:
        event_dict["request_id"] = request_id_var.get()
    return event_dict


def configure_logging(level: int = logging.INFO) -> None:
    """Install JSON logging on the stdlib root logger and configure structlog.

    Idempotent — safe to call from app startup *and* from test fixtures.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        _request_id_processor,
        structlog.processors.add_log_level,
        timestamper,
    ]

    structlog.configure(
        processors=shared_processors
        + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    # Drop any handlers installed by a prior ``logging.basicConfig`` call.
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    # Quiet noisy uvicorn loggers — they would otherwise duplicate the access
    # log lines emitted by our middleware.
    for noisy in ("uvicorn.access",):
        logging.getLogger(noisy).handlers = []
        logging.getLogger(noisy).propagate = True

    _CONFIGURED = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger bound to ``name`` (or the calling module)."""
    return structlog.stdlib.get_logger(name)
