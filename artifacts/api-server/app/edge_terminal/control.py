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
the row).  Boot calls :func:`ensure_seeded` which is a no-op when the
row already exists, so an operator-flipped value survives restarts.
"""
from __future__ import annotations

import logging
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
    on read-failure or empty row."""
    try:
        v = await db.fetchval(
            "SELECT value FROM system_settings WHERE key = $1",
            _SETTING_KEY,
        )
    except Exception as e:  # pragma: no cover — defensive
        log.warning("input_mode read failed (defaulting edge_terminal): %s", e)
        return INPUT_MODE_EDGE_TERMINAL
    s = (v or "").strip().lower()
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
    await db.execute(
        """
        INSERT INTO system_settings (key, value, updated_at)
        VALUES ($1, $2, now())
        ON CONFLICT (key) DO UPDATE
          SET value = EXCLUDED.value,
              updated_at = EXCLUDED.updated_at
        """,
        _SETTING_KEY,
        s,
    )
    return s


async def ensure_seeded() -> str:
    """Boot-time enforcement that ``app.input_mode`` is
    ``edge_terminal``.

    Task #844 requires the app to default to Edge Terminal mode on
    every boot/redeploy, regardless of any prior operator override.
    Operators can still flip back to ``pipeline`` at runtime via the
    POST /api/edge-terminal/input-mode endpoint; that change persists
    only for the lifetime of the running process.
    """
    try:
        await set_input_mode(INPUT_MODE_EDGE_TERMINAL)
    except Exception as e:  # pragma: no cover — defensive
        log.warning("input_mode boot seed failed: %s", e)
    return INPUT_MODE_EDGE_TERMINAL
