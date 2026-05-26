"""Operator-visible ``app.input_mode`` system setting.

The Edge Terminal poller + dashboard read-side both consult this row
to decide whether the LLM pipeline or the scraper is authoritative.
Two-mode enum:

  * ``pipeline``       — the historic LLM swarm + 12-gate pipeline
                          owns execution.  Dashboard tiles show
                          pipeline trades.
  * ``edge_terminal`` — the scraper runs every 60 s, drops signals
                         into the dedup table, and fires through
                         :func:`paper.place_order` at-market with
                         quarter-Kelly sizing.  Dashboard tiles
                         default to Edge Terminal data; a small
                         "Show pipeline data" toggle restores the
                         old view.

Default on a fresh install is ``edge_terminal`` (migration 0055 seeds
the row).  :func:`ensure_seeded` is called on every boot and, by
default, FORCES the input mode back to ``edge_terminal`` so a fresh
redeploy can never accidentally leave the operator on the LLM
pipeline.  Operators that want their runtime override (``pipeline``)
to survive restarts can set ``AXIOM_FORCE_EDGE_TERMINAL_ON_BOOT=0``
in the environment; in that mode :func:`ensure_seeded` only seeds a
default when the row is missing.
"""
from __future__ import annotations

import logging
import os
from typing import Literal

from .. import db


log = logging.getLogger(__name__)


INPUT_MODE_PIPELINE = "pipeline"
INPUT_MODE_EDGE_TERMINAL = "edge_terminal"
VALID_INPUT_MODES = (INPUT_MODE_PIPELINE, INPUT_MODE_EDGE_TERMINAL)

InputMode = Literal["pipeline", "edge_terminal"]

_SETTING_KEY = "app.input_mode"


async def get_input_mode() -> str:
    """Return the persisted input mode, defaulting to edge_terminal
    on read-failure or empty row.

    ``system_settings.value`` is ``jsonb`` in production, so values are
    JSON-encoded (the literal ``"edge_terminal"`` with quotes).  We
    accept either form -- a bare ``edge_terminal`` text value (dev/test
    databases that never had the out-of-band ALTER) and a JSON-encoded
    ``"edge_terminal"`` from prod -- by stripping a single layer of
    JSON quoting before matching against ``VALID_INPUT_MODES``.
    """
    try:
        v = await db.fetchval(
            "SELECT value::text FROM system_settings WHERE key = $1",
            _SETTING_KEY,
        )
    except Exception as e:  # pragma: no cover — defensive
        log.warning("input_mode read failed (defaulting edge_terminal): %s", e)
        return INPUT_MODE_EDGE_TERMINAL
    s = (v or "").strip()
    # Unwrap a single layer of JSON encoding -- e.g. '"edge_terminal"' or
    # the bare 'edge_terminal' both end up as 'edge_terminal'.
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        try:
            import json as _json
            s = _json.loads(s)
        except Exception:
            s = s[1:-1]
    s = s.strip().lower()
    if s not in VALID_INPUT_MODES:
        return INPUT_MODE_EDGE_TERMINAL
    return s


async def set_input_mode(mode: str) -> str:
    """Persist ``mode`` (one of :data:`VALID_INPUT_MODES`).  Raises
    ``ValueError`` for an unknown mode so the route returns 400."""
    s = (mode or "").strip().lower()
    if s not in VALID_INPUT_MODES:
        raise ValueError(
            f"input_mode must be one of {VALID_INPUT_MODES} (got {mode!r})"
        )
    # ``system_settings.value`` is ``jsonb`` in prod -- JSON-encode the
    # plain string via ``to_jsonb($2::text)`` so the insert works on
    # either a text or jsonb column (text column happily stores the
    # serialised ``"edge_terminal"`` with quotes, which get_input_mode
    # unwraps on read).
    await db.execute(
        """
        INSERT INTO system_settings (key, value, updated_at)
        VALUES ($1, to_jsonb($2::text), now())
        ON CONFLICT (key) DO UPDATE
          SET value = EXCLUDED.value,
              updated_at = EXCLUDED.updated_at
        """,
        _SETTING_KEY,
        s,
    )
    return s


_FORCE_ON_BOOT_ENV = "AXIOM_FORCE_EDGE_TERMINAL_ON_BOOT"


def _force_on_boot() -> bool:
    raw = (os.environ.get(_FORCE_ON_BOOT_ENV) or "1").strip().lower()
    return raw in ("1", "true", "yes", "on")


async def ensure_seeded() -> str:
    """Boot-time seed for ``app.input_mode``.

    Default behaviour (``AXIOM_FORCE_EDGE_TERMINAL_ON_BOOT`` unset or
    truthy): always reset the row to ``edge_terminal`` so a redeploy
    can never leave the operator on the LLM pipeline.

    Opt-out behaviour (``AXIOM_FORCE_EDGE_TERMINAL_ON_BOOT=0``): only
    seed when the row is missing; an operator-flipped ``pipeline``
    value survives restarts.

    Always returns the resulting mode (never raises — boot must not
    fail because of a transient DB hiccup).
    """
    if _force_on_boot():
        try:
            await set_input_mode(INPUT_MODE_EDGE_TERMINAL)
        except Exception as e:  # pragma: no cover — defensive
            log.warning("input_mode boot force-seed failed: %s", e)
        return INPUT_MODE_EDGE_TERMINAL
    # Preserve existing operator value if present.
    try:
        existing = await db.fetchval(
            "SELECT value FROM system_settings WHERE key = $1",
            _SETTING_KEY,
        )
    except Exception as e:  # pragma: no cover — defensive
        log.warning("input_mode read on boot failed: %s", e)
        existing = None
    s = (existing or "").strip().lower()
    if s in VALID_INPUT_MODES:
        return s
    try:
        await set_input_mode(INPUT_MODE_EDGE_TERMINAL)
    except Exception as e:  # pragma: no cover — defensive
        log.warning("input_mode boot seed failed: %s", e)
    return INPUT_MODE_EDGE_TERMINAL
