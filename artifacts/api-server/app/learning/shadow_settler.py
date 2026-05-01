"""Shadow settler — close the loop on shadow predictions.

For every ``shadow_predictions`` row whose ``settled_at`` is still
NULL but whose underlying Kalshi market has resolved on the live
``predictions`` table, this module:

  1. Copies ``predictions.outcome`` / ``predictions.settled_at`` onto
     the shadow row.
  2. Inserts a matching ``shadow_learning_buffer`` row so the
     per-league nightly calibrator refit can UNION it into the
     training set.

The settler relies on the fact that the *same* Kalshi market settler
(``app.feeds.kalshi_crypto_markets`` for crypto,
``app.settlement.pipeline`` for sports) already stamps the public
outcome onto the shared ``predictions`` table.  By keying the shadow
table on ``kalshi_market_id`` we get the public-outcomes feed for
free without re-implementing per-vertical resolution sources
(Kalshi resolution, scoreboard APIs, CoinGecko, etc.).

Idempotent — the unique partial index on
``shadow_learning_buffer (shadow_prediction_id)`` makes a re-run a
no-op for already-buffered rows.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .. import db


log = logging.getLogger(__name__)


# Per-cycle safety cap so a long backlog can't OOM the settler in
# one pass.  At hourly cadence this still drains ~120k rows/day,
# more than enough headroom against the predictor's write rate.
DEFAULT_BATCH_LIMIT = 5_000


@dataclass
class ShadowSettleCycleResult:
    started_at: str
    completed_at: str | None = None
    duration_seconds: float = 0.0
    candidates_scanned: int = 0
    settled: int = 0
    buffer_inserted: int = 0
    voids_skipped: int = 0
    errors: list[str] = field(default_factory=list)


# `predictions.outcome` is a *text* column (migration 0022) holding
# Kalshi's canonical resolution labels: 'yes' / 'no' / 'void'.  The
# shadow tables store outcome as integer (1=yes, 0=no), so we
# normalise here.  'void' is intentionally dropped from the training
# UNION — a voided market carries no calibration signal and would
# bias the per-league Brier score downward.  Numeric outcomes are
# accepted too so a future settler that writes 0/1 directly remains
# compatible.
def _normalize_outcome(raw: Any) -> int | None:
    """Return 1/0 for resolved outcomes, None for void / missing /
    unparseable.  Mirrors the label vocabulary established by the
    Kalshi reconciler in ``app/feeds/kalshi_crypto_markets.py`` and
    documented on the ``predictions.outcome`` column."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, (int, float)):
        as_int = int(raw)
        return as_int if as_int in (0, 1) else None
    if isinstance(raw, str):
        norm = raw.strip().lower()
        if norm in ("yes", "y", "1", "true", "t"):
            return 1
        if norm in ("no", "n", "0", "false", "f"):
            return 0
        # 'void', 'cancel', '' — no calibration signal.
        return None
    return None


async def _fetch_pending_resolutions(limit: int) -> list[dict[str, Any]]:
    """Find shadow_predictions rows whose underlying market has settled
    on the live predictions table but whose shadow row hasn't been
    stamped yet.

    The join is keyed on ``kalshi_market_id`` (the only stable
    cross-table identity for a Kalshi market).  When multiple
    ``predictions`` rows share a market_id (rare — happens only when
    the Kalshi feed re-keys mid-life), we take the most recently
    settled one via DISTINCT ON.
    """
    try:
        rows = await db.fetch(
            """
            WITH resolved AS (
              SELECT DISTINCT ON (kalshi_market_id)
                     kalshi_market_id,
                     outcome,
                     settled_at
                FROM predictions
               WHERE kalshi_market_id IS NOT NULL
                 AND settled_at IS NOT NULL
                 AND outcome IS NOT NULL
               ORDER BY kalshi_market_id, settled_at DESC
            )
            SELECT sp.id              AS shadow_id,
                   sp.market_id,
                   sp.league,
                   sp.vertical,
                   sp.predicted_prob::float    AS predicted_prob,
                   sp.uncalibrated_prob::float AS uncalibrated_prob,
                   r.outcome,
                   r.settled_at
              FROM shadow_predictions sp
              JOIN resolved r
                ON r.kalshi_market_id = sp.market_id
             WHERE sp.settled_at IS NULL
             ORDER BY sp.id
             LIMIT $1
            """,
            limit,
        )
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning("[shadow_settler] pending scan failed: %s", e)
        return []


async def _stamp_shadow(
    shadow_id: int, *, outcome: int, settled_at: datetime,
) -> bool:
    """Stamp the resolution onto the shadow_predictions row.  Returns
    True iff the row was newly stamped (i.e. our UPDATE actually
    flipped settled_at from NULL to a value)."""
    try:
        # `UPDATE ... RETURNING id` returns NULL when the WHERE clause
        # filtered the row out (e.g. another worker already stamped
        # it).  This is our idempotency hint without a separate
        # SELECT.
        rid = await db.fetchval(
            """
            UPDATE shadow_predictions
               SET settled_at = $2,
                   outcome    = $3
             WHERE id = $1
               AND settled_at IS NULL
            RETURNING id
            """,
            shadow_id, settled_at, int(outcome),
        )
        return rid is not None
    except Exception as e:
        log.warning(
            "[shadow_settler] shadow stamp failed for id=%s: %s", shadow_id, e
        )
        return False


async def _insert_buffer(
    *,
    shadow_id: int,
    market_id: str | None,
    league: str | None,
    vertical: str | None,
    predicted_prob: float | None,
    uncalibrated_prob: float | None,
    actual_outcome: int,
    settled_at: datetime,
) -> bool:
    """Insert the buffer row.  Returns True iff a new row was written;
    False on ON CONFLICT (already present from a previous cycle)."""
    if predicted_prob is None:
        # Defensive: a NULL predicted_prob would feed a NaN into the
        # calibrator UNION.  Skip cleanly.
        return False
    try:
        rid = await db.fetchval(
            """
            INSERT INTO shadow_learning_buffer (
                shadow_prediction_id, market_id, league, vertical,
                predicted_prob, uncalibrated_prob, actual_outcome,
                source, has_full_features, processed,
                settled_at
            ) VALUES (
                $1, $2, $3, $4,
                $5, $6, $7,
                'shadow', true, false,
                $8
            )
            ON CONFLICT (shadow_prediction_id)
                WHERE shadow_prediction_id IS NOT NULL
                DO NOTHING
            RETURNING id
            """,
            shadow_id, market_id, league, vertical,
            float(predicted_prob),
            float(uncalibrated_prob) if uncalibrated_prob is not None else None,
            int(actual_outcome),
            settled_at,
        )
        return rid is not None
    except Exception as e:
        log.warning(
            "[shadow_settler] buffer insert failed for shadow_id=%s: %s",
            shadow_id, e,
        )
        return False


async def run_cycle(*, limit: int = DEFAULT_BATCH_LIMIT) -> ShadowSettleCycleResult:
    """One pass: stamp resolutions onto shadow_predictions and copy
    them into shadow_learning_buffer.

    Always non-fatal: per-row failures are captured per-row and never
    abort the cycle.
    """
    started = time.perf_counter()
    started_at = datetime.now(timezone.utc).isoformat()
    cr = ShadowSettleCycleResult(started_at=started_at)

    pending = await _fetch_pending_resolutions(limit)
    cr.candidates_scanned = len(pending)
    if not pending:
        cr.duration_seconds = round(time.perf_counter() - started, 3)
        cr.completed_at = datetime.now(timezone.utc).isoformat()
        return cr

    for r in pending:
        shadow_id = int(r["shadow_id"])
        outcome = r.get("outcome")
        settled_at = r.get("settled_at")
        if outcome is None or settled_at is None:
            continue
        outcome_int = _normalize_outcome(outcome)
        if outcome_int is None:
            # Void / cancel / unparseable — these carry no calibration
            # signal.  Count them so an operator can sanity-check the
            # void rate without spelunking logs, but never raise.
            cr.voids_skipped += 1
            continue

        try:
            stamped = await _stamp_shadow(
                shadow_id, outcome=outcome_int, settled_at=settled_at,
            )
        except Exception as e:
            cr.errors.append(f"{shadow_id}: stamp {type(e).__name__}: {e}")
            continue
        if stamped:
            cr.settled += 1

        # Buffer insert is idempotent on (shadow_prediction_id), so we
        # can run it even when the stamp was a no-op (another worker
        # raced us).  This guarantees the buffer row exists even if
        # the stamp & buffer split across two cycles.
        try:
            inserted = await _insert_buffer(
                shadow_id=shadow_id,
                market_id=r.get("market_id"),
                league=r.get("league"),
                vertical=r.get("vertical"),
                predicted_prob=r.get("predicted_prob"),
                uncalibrated_prob=r.get("uncalibrated_prob"),
                actual_outcome=outcome_int,
                settled_at=settled_at,
            )
        except Exception as e:
            cr.errors.append(
                f"{shadow_id}: buffer {type(e).__name__}: {e}"
            )
            continue
        if inserted:
            cr.buffer_inserted += 1

    cr.duration_seconds = round(time.perf_counter() - started, 3)
    cr.completed_at = datetime.now(timezone.utc).isoformat()
    log.info(
        "[shadow_settler] cycle done scanned=%d settled=%d buffered=%d "
        "voids=%d errors=%d duration=%.2fs",
        cr.candidates_scanned, cr.settled, cr.buffer_inserted,
        cr.voids_skipped, len(cr.errors), cr.duration_seconds,
    )
    return cr
