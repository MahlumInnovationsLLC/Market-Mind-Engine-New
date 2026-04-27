"""One-shot backfill: stamp historical model YES probabilities onto
``predictions`` rows so the rolling crypto accuracy report (task #580)
no longer needs the ``swarm_debates`` lateral join (task #646).

The accuracy report originally read the model probability via a
``LATERAL`` join into ``swarm_debates`` keyed on either
``swarm_debates.game_id == predictions.game_id`` or
``swarm_debates.game_context->>'kalshi_market_id'``.  Once the
orchestrator started writing the probability inline (see
``app.orchestrator.cycle._persist_model_yes_prob``) the report could
read it from ``predictions.model_yes_prob`` directly — but only for
rows scored after the column landed.  Legacy crypto rows still have
``model_yes_prob IS NULL`` and would silently fall into the report's
``n_unscored`` bucket.

This module closes that gap: it copies the most-recent
``swarm_debates.consensus_probability`` for the same Kalshi market
onto each unsettled-or-settled prediction row that's still missing a
probability.  Operators can trigger it from
``POST /api/admin/crypto/backfill-probabilities`` and watch progress
on ``GET /api/admin/crypto/backfill-probabilities-status``.

Design notes:
  * **Idempotent** — the UPDATE is guarded by ``model_yes_prob IS
    NULL`` so re-running the backfill never overwrites a row the
    orchestrator already stamped.  The match shape mirrors the SQL
    ``accuracy_report._WINDOW_SQL`` used to run, so legacy rows that
    used to surface in the report are exactly the rows we backfill.
  * **Scoped to one league per run** — the default league is
    ``CRYPTO`` (task #580 was crypto-only).  The runner accepts an
    explicit ``league`` so the same machinery can be reused for other
    verticals later without copy-pasting.
  * **Single-flight** via an :class:`asyncio.Lock` — a second POST
    while a backfill is in progress returns 409.
  * **Persistent status** in ``system_settings`` so the operator
    panel survives restarts (mirrors the entertainment backfill).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .. import db


log = logging.getLogger(__name__)


# Default league — task #646 is scoped to crypto, but the runner is
# parametric so the same scaffold can backfill another vertical later
# without forking the module.  Kept in sync with
# ``app.crypto.accuracy_report._LEAGUE``.
DEFAULT_LEAGUE: str = "CRYPTO"


# Persistent status snapshot key.  Mirrors the convention used by
# ``app.entertainment.backfill._STATUS_KEY`` so the operator panel can
# load the most recent run after a restart with a single
# ``system_settings`` lookup.
_STATUS_KEY: str = "crypto.probability_backfill.last_run"


# UPDATE statement: copy the most recent debate's consensus probability
# onto every prediction row in the league that still has no inline
# probability.  The lateral join shape matches the original
# ``accuracy_report._WINDOW_SQL`` so the rows we backfill are exactly
# the rows the report used to surface via the join.  Returns the row
# count via ``RETURNING 1`` aggregated into a count by the caller.
_BACKFILL_SQL = """
UPDATE predictions p
   SET model_yes_prob = sd.consensus_probability,
       updated_at     = now()
  FROM (
    SELECT p2.id                      AS prediction_id,
           sd2.consensus_probability  AS consensus_probability
      FROM predictions p2
      JOIN LATERAL (
          SELECT consensus_probability
            FROM swarm_debates
           WHERE consensus_probability IS NOT NULL
             AND (
               (game_context->>'kalshi_market_id') = p2.kalshi_market_id
               OR game_id = p2.game_id
             )
           ORDER BY created_at DESC
           LIMIT 1
      ) sd2 ON true
     WHERE p2.league = $1
       AND p2.model_yes_prob IS NULL
  ) sd
 WHERE p.id = sd.prediction_id
   AND p.model_yes_prob IS NULL
"""


# Quick coverage probe used by both the dry-run path and the post-run
# summary.  ``unscored`` here means "settled, no inline probability" —
# the same definition the accuracy report uses to populate
# ``n_unscored``, so the two numbers stay in lockstep.
_COVERAGE_SQL = """
SELECT
    count(*) FILTER (
        WHERE settled_at IS NOT NULL
    )                                                AS settled_total,
    count(*) FILTER (
        WHERE settled_at IS NOT NULL
          AND model_yes_prob IS NOT NULL
    )                                                AS settled_scored,
    count(*) FILTER (
        WHERE settled_at IS NOT NULL
          AND model_yes_prob IS NULL
    )                                                AS settled_unscored
  FROM predictions
 WHERE league = $1
"""


@dataclass
class BackfillStatus:
    """In-process status snapshot.

    ``rows_updated`` is the count of prediction rows the UPDATE actually
    touched on the most recent run.  ``coverage_before`` /
    ``coverage_after`` carry the pre/post-run "settled rows still
    missing a probability" tallies so the operator can confirm the run
    actually closed the gap (and not just bumped a few rows).
    """

    state: str = "idle"            # idle | running | completed | failed
    league: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    duration_seconds: float = 0.0
    rows_updated: int = 0
    coverage_before: dict[str, int] | None = None
    coverage_after: dict[str, int] | None = None
    last_error: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "league": self.league,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_seconds": round(self.duration_seconds, 3),
            "rows_updated": self.rows_updated,
            "coverage_before": (
                dict(self.coverage_before)
                if self.coverage_before is not None else None
            ),
            "coverage_after": (
                dict(self.coverage_after)
                if self.coverage_after is not None else None
            ),
            "last_error": self.last_error,
        }


_status = BackfillStatus()
_lock = asyncio.Lock()


def is_running() -> bool:
    return _lock.locked()


def get_status() -> dict[str, Any]:
    return _status.snapshot()


async def _coverage(league: str) -> dict[str, int]:
    """Pre/post-run coverage tally.  Soft-fails to zeroes so a transient
    DB blip doesn't fail the whole run — the UPDATE itself is the
    authoritative source for ``rows_updated``."""
    try:
        row = await db.fetchrow(_COVERAGE_SQL, league)
    except Exception as e:  # noqa: BLE001 — defensive
        log.warning(
            "probability backfill coverage probe failed for %s: %s",
            league, e,
        )
        return {"settled_total": 0, "settled_scored": 0, "settled_unscored": 0}
    if row is None:
        return {"settled_total": 0, "settled_scored": 0, "settled_unscored": 0}
    return {
        "settled_total": int(row["settled_total"] or 0),
        "settled_scored": int(row["settled_scored"] or 0),
        "settled_unscored": int(row["settled_unscored"] or 0),
    }


def _parse_rowcount(result: Any) -> int:
    """asyncpg's ``execute`` returns the command tag (e.g. ``"UPDATE 7"``).
    Be permissive so a future driver change that returns an int directly
    still surfaces the right count instead of crashing the runner.
    """
    if isinstance(result, int):
        return result
    if isinstance(result, str) and result.startswith("UPDATE "):
        try:
            return int(result.split(" ", 1)[1])
        except (IndexError, ValueError):
            return 0
    return 0


async def _persist_status() -> None:
    """Write the current snapshot to ``system_settings`` so the operator
    panel can render the last completed run after an api-server restart.
    Best-effort: persistence failure must never fail a backfill.
    """
    try:
        await db.execute(
            """
            INSERT INTO system_settings (key, value, updated_at)
            VALUES ($1, $2, now())
            ON CONFLICT (key) DO UPDATE
               SET value = EXCLUDED.value,
                   updated_at = now()
            """,
            _STATUS_KEY,
            json.dumps(_status.snapshot()),
        )
    except Exception as e:  # noqa: BLE001 — defensive
        log.debug("probability backfill status persist failed: %s", e)


async def load_persisted_status() -> dict[str, Any] | None:
    """Read the most recently persisted status snapshot back out so the
    operator panel survives api-server restarts.  Returns ``None`` when
    no run has ever completed."""
    try:
        raw = await db.fetchval(
            "SELECT value FROM system_settings WHERE key = $1",
            _STATUS_KEY,
        )
    except Exception as e:  # noqa: BLE001 — defensive
        log.debug("probability backfill status load failed: %s", e)
        return None
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None
    return None


async def run_backfill(*, league: str = DEFAULT_LEAGUE) -> dict[str, Any]:
    """Run one backfill pass for ``league`` and return the final status.

    Single-flight: a second concurrent call blocks on the module-level
    lock instead of running in parallel — the underlying UPDATE is
    idempotent but running two of them at once would just waste DB
    cycles.

    Returns the final ``BackfillStatus.snapshot()`` so the admin
    endpoint can echo it back to the caller without re-reading the
    module global.
    """
    league = (league or DEFAULT_LEAGUE).strip().upper()
    if not league:
        raise ValueError("league must be a non-empty string")

    async with _lock:
        global _status
        _status = BackfillStatus(
            state="running",
            league=league,
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        started = time.perf_counter()
        try:
            _status.coverage_before = await _coverage(league)
            try:
                result = await db.execute(_BACKFILL_SQL, league)
            except Exception as e:
                _status.state = "failed"
                _status.last_error = f"{type(e).__name__}: {e}"[:500]
                log.exception(
                    "probability backfill UPDATE crashed for %s", league,
                )
                _status.completed_at = datetime.now(timezone.utc).isoformat()
                _status.duration_seconds = time.perf_counter() - started
                await _persist_status()
                return _status.snapshot()
            _status.rows_updated = _parse_rowcount(result)
            _status.coverage_after = await _coverage(league)
            _status.state = "completed"
        finally:
            _status.completed_at = datetime.now(timezone.utc).isoformat()
            _status.duration_seconds = time.perf_counter() - started
            await _persist_status()
        return _status.snapshot()


def reset_status_for_tests() -> None:
    """Drop the in-process snapshot — wired into pytest fixtures."""
    global _status
    _status = BackfillStatus()


__all__ = [
    "BackfillStatus",
    "DEFAULT_LEAGUE",
    "get_status",
    "is_running",
    "load_persisted_status",
    "reset_status_for_tests",
    "run_backfill",
]
