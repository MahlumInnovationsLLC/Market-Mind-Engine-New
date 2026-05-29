"""Edge Terminal scraper-input mode (task #844).

Public surface used by the rest of the api-server:

  * :func:`get_input_mode` / :func:`set_input_mode` — read/write the
    ``app.input_mode`` system setting that switches between the LLM
    pipeline and the Edge Terminal scraper.
  * :func:`start` / :func:`stop` — lifespan hooks; idempotent.
  * :func:`get_status` — surface for the dashboard's stale-poller
    indicator (>120s grey + last error).
  * :func:`self_check` — on-demand "can the scraper log in and see
    badged tiles?" probe used by the admin self-check button (task #950).
"""
from __future__ import annotations

from .control import (
    INPUT_MODE_EDGE_TERMINAL,
    INPUT_MODE_PIPELINE,
    VALID_INPUT_MODES,
    get_input_mode,
    set_input_mode,
)
from .poller import get_status, start, stop
from .scraper import self_check


__all__ = [
    "INPUT_MODE_EDGE_TERMINAL",
    "INPUT_MODE_PIPELINE",
    "VALID_INPUT_MODES",
    "get_input_mode",
    "set_input_mode",
    "get_status",
    "start",
    "stop",
    "self_check",
]
