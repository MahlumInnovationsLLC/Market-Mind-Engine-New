"""Nightly mining job: correlate UW signals against settled outcomes.

For each (vertical × signal_type × aggregator) candidate we:

  1. Pull settled trades for the vertical (with ``actual_outcome`` from
     ``trade_postmortems`` and ``model_prob`` from ``trades``).
  2. For each trade, compute the candidate signal value at trade-entry
     time by counting / summing UW rows over a lookback window ending at
     the trade's ``created_at``.
  3. Run the IC + Sharpe + monotonicity stats over the (value, residual)
     pairs and upsert the result into ``autoresearch_features`` keyed by
     a stable name. Existing ``status`` is preserved (so a previously
     promoted or rejected candidate survives the next mining pass).

Reads only. Writes only to ``autoresearch_features``. Safe to run
concurrently with the predictor / feed pollers.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

from .. import db
from . import stats, store


log = logging.getLogger("axiom.autoresearch.mining")


# Aggregators describe HOW to roll a stream of UW rows into a single
# scalar per trade. ``window_seconds`` is how far back from trade entry
# we look; ``op`` is how we collapse the rows.
@dataclass(frozen=True)
class _Aggregator:
    name: str
    window_seconds: int
    op: str  # "count" | "sum_premium"


_DEFAULT_AGGREGATORS: tuple[_Aggregator, ...] = (
    _Aggregator(name="count_1h",  window_seconds=3600,  op="count"),
    _Aggregator(name="count_24h", window_seconds=86400, op="count"),
    _Aggregator(name="sum_premium_24h", window_seconds=86400, op="sum_premium"),
)

_DEFAULT_SIGNAL_TYPES: tuple[str, ...] = ("flow_alert", "darkpool_print")


def _trade_lookback_days() -> int:
    return max(7, int(os.environ.get("AXIOM_AUTORESEARCH_LOOKBACK_DAYS") or "30"))


def _min_samples() -> int:
    return max(20, int(os.environ.get("AXIOM_AUTORESEARCH_MIN_SAMPLES") or "30"))


@dataclass
class MiningResult:
    started_at: str
    completed_at: str
    duration_seconds: float
    verticals_examined: int = 0
    candidates_evaluated: int = 0
    candidates_persisted: int = 0
    insufficient_samples: int = 0
    errors: list[str] = field(default_factory=list)


async def _list_verticals() -> list[str]:
    try:
        async with db.acquire() as c:
            rows = await c.fetch(
                """
                SELECT DISTINCT vertical
                  FROM trades
                 WHERE vertical IS NOT NULL
                   AND settled_at IS NOT NULL
                """,
            )
        return [str(r["vertical"]) for r in rows if r["vertical"]]
    except Exception as e:
        log.warning("autoresearch _list_verticals failed: %s", e)
        return []


async def _settled_trades(vertical: str, lookback_days: int) -> list[dict[str, Any]]:
    """Trades that settled in the lookback window for the given vertical.

    Returns ``model_prob`` aligned to the side actually taken (so YES
    trades use ``model_prob``, NO trades use ``1 - model_prob``) and the
    realised binary outcome on that side. The caller treats
    ``y - p_side`` as the residual the meta-model left on the table.
    """
    try:
        async with db.acquire() as c:
            rows = await c.fetch(
                """
                SELECT
                  coalesce(t.trade_id, t.id::text) AS trade_id,
                  t.created_at                     AS created_at,
                  lower(coalesce(t.side, ''))      AS side_lc,
                  t.model_prob                     AS model_prob,
                  pm.actual_outcome                AS actual_outcome
                  FROM trades t
                  JOIN trade_postmortems pm
                    ON pm.trade_id = coalesce(t.trade_id, t.id::text)
                 WHERE t.vertical = $1
                   AND t.settled_at IS NOT NULL
                   AND t.settled_at > now() - ($2::int || ' days')::interval
                   AND t.model_prob IS NOT NULL
                   AND pm.actual_outcome IS NOT NULL
                   AND t.created_at IS NOT NULL
                """,
                vertical, lookback_days,
            )
    except Exception as e:
        log.warning("autoresearch trades fetch failed for %s: %s", vertical, e)
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            p = float(r["model_prob"])
            y = int(r["actual_outcome"])
        except (TypeError, ValueError):
            continue
        side = (r["side_lc"] or "").strip()
        if side in {"no", "n", "sell"}:
            p_side = 1.0 - p
            y_side = 1 - y
        else:
            p_side = p
            y_side = y
        out.append({
            "trade_id": r["trade_id"],
            "created_at": r["created_at"],
            "p_side": p_side,
            "y_side": y_side,
        })
    return out


async def _aggregate_signal(
    *,
    signal_type: str,
    agg: _Aggregator,
    end_at: datetime,
) -> float:
    """Roll the UW stream up to ``end_at`` into a single scalar.

    We aggregate *globally* (not per-ticker) since most verticals are
    sports markets with no ticker linkage; the hypothesis is that
    market-wide options-flow / dark-pool intensity correlates with
    short-term outcome surprises. A future task can specialise this for
    crypto / equity verticals where tickers are present.
    """
    if end_at.tzinfo is None:
        end_at = end_at.replace(tzinfo=timezone.utc)
    start_at = end_at.timestamp() - agg.window_seconds
    try:
        async with db.acquire() as c:
            if agg.op == "count":
                row = await c.fetchrow(
                    """
                    SELECT count(*)::int AS v
                      FROM uw_signals
                     WHERE signal_type = $1
                       AND occurred_at >= to_timestamp($2)
                       AND occurred_at <  $3
                    """,
                    signal_type, start_at, end_at,
                )
                return float(row["v"] or 0)
            if agg.op == "sum_premium":
                # ``payload`` is jsonb. UW exposes premium under several
                # field names depending on endpoint; coalesce a few.
                row = await c.fetchrow(
                    """
                    SELECT coalesce(sum(
                        coalesce(
                          (payload->>'premium')::numeric,
                          (payload->>'total_premium')::numeric,
                          (payload->>'value')::numeric,
                          0
                        )
                    ), 0)::float AS v
                      FROM uw_signals
                     WHERE signal_type = $1
                       AND occurred_at >= to_timestamp($2)
                       AND occurred_at <  $3
                    """,
                    signal_type, start_at, end_at,
                )
                return float(row["v"] or 0.0)
    except Exception as e:
        log.debug("uw aggregate failed (%s/%s): %s", signal_type, agg.name, e)
        return 0.0
    return 0.0


async def _evaluate_candidate(
    *,
    vertical: str,
    signal_type: str,
    aggregator: _Aggregator,
    trades: Sequence[dict[str, Any]],
) -> dict[str, Any] | None:
    """Compute (value, residual) pairs for the candidate and score them."""
    if len(trades) < _min_samples():
        return None
    values: list[float] = []
    residuals: list[float] = []
    for t in trades:
        v = await _aggregate_signal(
            signal_type=signal_type,
            agg=aggregator,
            end_at=t["created_at"],
        )
        values.append(v)
        residuals.append(float(t["y_side"]) - float(t["p_side"]))
    summary = stats.evaluate(values, residuals)
    summary["vertical"] = vertical
    summary["signal_type"] = signal_type
    summary["aggregator"] = aggregator.name
    return summary


async def run_mining(
    *,
    verticals: Sequence[str] | None = None,
    signal_types: Sequence[str] = _DEFAULT_SIGNAL_TYPES,
    aggregators: Sequence[_Aggregator] = _DEFAULT_AGGREGATORS,
) -> MiningResult:
    """Mine UW signals across verticals and persist candidates.

    Errors are collected per-vertical so a single bad query doesn't
    abort the whole pass.
    """
    started = datetime.now(tz=timezone.utc)
    res = MiningResult(
        started_at=started.isoformat(timespec="seconds"),
        completed_at=started.isoformat(timespec="seconds"),
        duration_seconds=0.0,
    )
    vert_list = list(verticals) if verticals is not None else await _list_verticals()
    res.verticals_examined = len(vert_list)
    lookback = _trade_lookback_days()

    for vertical in vert_list:
        try:
            trades = await _settled_trades(vertical, lookback)
        except Exception as e:
            res.errors.append(f"{vertical}: trades fetch: {type(e).__name__}: {e}")
            continue
        if len(trades) < _min_samples():
            res.insufficient_samples += 1
            log.info(
                "autoresearch %s: %d trades < min_samples=%d, skipping",
                vertical, len(trades), _min_samples(),
            )
            continue
        for sig in signal_types:
            for agg in aggregators:
                res.candidates_evaluated += 1
                try:
                    summary = await _evaluate_candidate(
                        vertical=vertical,
                        signal_type=sig,
                        aggregator=agg,
                        trades=trades,
                    )
                except Exception as e:
                    res.errors.append(
                        f"{vertical}/{sig}/{agg.name}: {type(e).__name__}: {e}"
                    )
                    continue
                if summary is None:
                    res.insufficient_samples += 1
                    continue
                name = store.feature_name(vertical, sig, agg.name)
                row = await store.upsert_metrics(
                    name=name,
                    vertical=vertical,
                    signal_type=sig,
                    aggregator=agg.name,
                    ic=float(summary["ic"]),
                    sharpe=float(summary["sharpe"]),
                    monotonicity=float(summary["monotonicity"]),
                    n_samples=int(summary["n"]),
                    metadata={"window_seconds": agg.window_seconds, "op": agg.op},
                )
                if row is not None:
                    res.candidates_persisted += 1

    completed = datetime.now(tz=timezone.utc)
    res.completed_at = completed.isoformat(timespec="seconds")
    res.duration_seconds = (completed - started).total_seconds()
    log.info(
        "autoresearch mining done: verticals=%d evaluated=%d persisted=%d errors=%d",
        res.verticals_examined, res.candidates_evaluated,
        res.candidates_persisted, len(res.errors),
    )
    return res
