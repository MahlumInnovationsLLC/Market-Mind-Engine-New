"""Shadow predictor — snapshot the model's calibrated YES probability
for every *active* Kalshi market on an hourly cadence.

The orchestrator only traverses markets whose feed materialised a
``predictions`` row AND whose edge gates / capital / gatekeeper let a
trade through — so the calibrator's training set is biased toward the
narrow sub-population we considered worth trading.  This module closes
that gap by writing a row into ``shadow_predictions`` for every
active market we hold a model probability for, regardless of whether
we traded it.

The implementation deliberately leverages the *existing*
``predictions.model_yes_prob`` column rather than re-running the model
+ calibrator chain from scratch:

  * ``predictions.model_yes_prob`` is stamped by the orchestrator on
    every cycle that produces a probability for a market (via
    ``cycle._persist_model_yes_prob``).  That value is *already*
    calibrated through the per-league calibrator chain, which is
    exactly what we want to feed back into the calibrator's training
    set on settlement.
  * Re-running the predictor here would either need a feature builder
    per vertical (NBA needs game features, CRYPTO needs OHLCV bars,
    weather needs grid pulls...) or would have to invent synthetic
    inputs — both of which would produce garbage probabilities the
    calibrator would learn from.

This also means the shadow loop captures markets the orchestrator
*considered* but didn't trade (gate failures, edge below threshold,
duplicate-trade exemption, etc.) — which is the entire population we
want unbiased calibration over.

Idempotency: each run truncates ``predicted_at`` to the current hour.
The unique index ``(market_id, predicted_at)`` makes a same-hour
re-run a no-op, so the cycle is safe to retry without producing
duplicate snapshots.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .. import db, predictor as _predictor


log = logging.getLogger(__name__)


# Per-cycle safety cap so a runaway predictions table can't OOM the
# scan or write more than a few thousand rows in one pass.  Hourly
# cadence x ~1000 active markets = ~24k rows/day, comfortably below
# any storage threshold.
DEFAULT_BATCH_LIMIT = 5_000


@dataclass
class ShadowPredictCycleResult:
    started_at: str
    completed_at: str | None = None
    duration_seconds: float = 0.0
    candidates_scanned: int = 0
    snapshots_written: int = 0
    skipped_duplicate: int = 0
    skipped_no_prob: int = 0
    errors: list[str] = field(default_factory=list)


def _hour_floor(now: datetime | None = None) -> datetime:
    """Truncate ``now`` to the current hour (UTC) for idempotency."""
    n = now or datetime.now(timezone.utc)
    return n.replace(minute=0, second=0, microsecond=0)


def _league_to_vertical(league: str | None) -> str | None:
    """Best-effort league -> vertical reverse lookup.

    Walks the registered ``VerticalSpec`` list once per call so a
    new vertical is picked up without restart.  Returns ``None`` for
    leagues that don't belong to a registered vertical (legacy /
    debug rows) — the calibrator UNION groups by league anyway, so
    a missing vertical only affects the drift-alert path.
    """
    if not league:
        return None
    try:
        from .. import verticals as _v
    except Exception:  # pragma: no cover — defensive
        return None
    lg = league.upper()
    for spec in _v.all_specs():
        if lg in {l.upper() for l in spec.leagues}:
            return spec.name
    return None


async def _fetch_active_predictions(limit: int) -> list[dict[str, Any]]:
    """Scan the live ``predictions`` table for markets we have a fresh
    model probability on and that haven't settled yet.

    Filters:
      * ``model_yes_prob IS NOT NULL`` — the orchestrator stamped a
        calibrated probability on this row, so we can shadow-track it.
      * ``settled_at IS NULL`` — we only shadow-track markets still
        open on Kalshi; settled rows already have their outcome.
      * ``kalshi_market_id IS NOT NULL`` — anchors the row to a real
        Kalshi market the settler can resolve.
      * ``updated_at > now() - interval '6 hours'`` — guards against
        stale ghost rows: a market whose probability hasn't been
        re-stamped in 6h is unlikely to settle cleanly and would
        pollute the shadow training set.

    Wider window than the `1 hour` cycle cadence so a brief
    orchestrator outage doesn't drop the corresponding shadow rows.
    """
    try:
        rows = await db.fetch(
            """
            SELECT id              AS prediction_id,
                   kalshi_market_id,
                   league,
                   model_yes_prob::float AS p_yes,
                   kalshi_price::float   AS yes_price,
                   confidence::float     AS confidence,
                   game_time
              FROM predictions
             WHERE kalshi_market_id IS NOT NULL
               AND model_yes_prob IS NOT NULL
               AND settled_at IS NULL
               AND created_at > now() - interval '6 hours'
             ORDER BY created_at DESC
             LIMIT $1
            """,
            limit,
        )
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning("[shadow_predictor] active scan failed: %s", e)
        return []


async def _insert_snapshot(
    *,
    market_id: str,
    league: str | None,
    vertical: str | None,
    yes_price: float | None,
    predicted_prob: float,
    confidence: float | None,
    predicted_at: datetime,
    metadata: dict[str, Any],
) -> bool:
    """Insert one ``shadow_predictions`` row.  Returns True when the
    row was newly inserted, False when ON CONFLICT DO NOTHING fired
    (i.e. another snapshot for this market already exists at the
    same hour).
    """
    # Pull the active predictor name/version for traceability so an
    # operator can correlate a shadow miss back to the model that
    # produced it.  Best-effort: a missing predictor info just leaves
    # the columns NULL.
    model_name: str | None = None
    model_version: str | None = None
    try:
        info = _predictor.active_model_info()
        if info:
            model_name = info.get("name")
            mv = info.get("version")
            model_version = str(mv) if mv is not None else None
    except Exception:  # pragma: no cover — defensive
        pass

    try:
        # `RETURNING id` is None when ON CONFLICT DO NOTHING fires —
        # asyncpg's `fetchval` then returns None too, which is our
        # signal that the row was a same-hour duplicate.
        row_id = await db.fetchval(
            """
            INSERT INTO shadow_predictions (
                market_id, league, vertical, kalshi_yes_price,
                predicted_prob, uncalibrated_prob, confidence,
                model_name, model_version,
                predicted_at, source, metadata
            ) VALUES (
                $1, $2, $3, $4,
                $5, NULL, $6,
                $7, $8,
                $9, 'shadow', $10::jsonb
            )
            ON CONFLICT (market_id, predicted_at) DO NOTHING
            RETURNING id
            """,
            market_id,
            league,
            vertical,
            yes_price,
            predicted_prob,
            confidence,
            model_name,
            model_version,
            predicted_at,
            json.dumps(metadata or {}),
        )
        return row_id is not None
    except Exception as e:
        log.warning(
            "[shadow_predictor] insert failed for market=%s: %s", market_id, e
        )
        return False


async def run_cycle(*, limit: int = DEFAULT_BATCH_LIMIT) -> ShadowPredictCycleResult:
    """One pass: snapshot every active market's calibrated probability.

    Failures on individual rows are captured per-row and never abort
    the cycle — the goal is "best-effort coverage" so a single
    sick market can't stall the whole shadow loop.
    """
    started = time.perf_counter()
    started_at = datetime.now(timezone.utc).isoformat()
    cr = ShadowPredictCycleResult(started_at=started_at)

    rows = await _fetch_active_predictions(limit)
    cr.candidates_scanned = len(rows)
    if not rows:
        cr.duration_seconds = round(time.perf_counter() - started, 3)
        cr.completed_at = datetime.now(timezone.utc).isoformat()
        return cr

    bucket_at = _hour_floor()

    for r in rows:
        market_id = r.get("kalshi_market_id")
        prob = r.get("p_yes")
        if not market_id or prob is None:
            cr.skipped_no_prob += 1
            continue
        try:
            p = float(prob)
        except (TypeError, ValueError):
            cr.skipped_no_prob += 1
            continue
        if not (0.0 <= p <= 1.0):
            cr.skipped_no_prob += 1
            continue

        league = (r.get("league") or None) or None
        if league is not None:
            league = str(league).upper()
        vertical = _league_to_vertical(league)
        yes_price = r.get("yes_price")
        confidence = r.get("confidence")

        meta = {
            "prediction_id": int(r.get("prediction_id"))
                if r.get("prediction_id") is not None else None,
            "game_time": (
                r.get("game_time").isoformat()
                if hasattr(r.get("game_time"), "isoformat")
                else None
            ),
        }
        try:
            wrote = await _insert_snapshot(
                market_id=str(market_id),
                league=league,
                vertical=vertical,
                yes_price=float(yes_price) if yes_price is not None else None,
                predicted_prob=p,
                confidence=float(confidence) if confidence is not None else None,
                predicted_at=bucket_at,
                metadata=meta,
            )
        except Exception as e:
            cr.errors.append(f"{market_id}: {type(e).__name__}: {e}")
            continue
        if wrote:
            cr.snapshots_written += 1
        else:
            cr.skipped_duplicate += 1

    cr.duration_seconds = round(time.perf_counter() - started, 3)
    cr.completed_at = datetime.now(timezone.utc).isoformat()
    log.info(
        "[shadow_predictor] cycle done scanned=%d wrote=%d dup=%d errors=%d duration=%.2fs",
        cr.candidates_scanned, cr.snapshots_written,
        cr.skipped_duplicate, len(cr.errors), cr.duration_seconds,
    )
    return cr
