"""Replay LLM predictions from ``v1_archive.llm_predictions``.

V1 logged 57K rows of debate / model calls along with the eventual
``actual_outcome`` text and a ``correct`` boolean.  This module exposes
a small read-only harness so a current model bundle can be benchmarked
against V1's historical calls — same league / category mix, same
matchups, but the outcome was decided by the actual game.

The harness is intentionally minimal: it slices the archive by filter,
groups by provider + model + league + category, and reports the win-
rate / Brier / sample-count triple.  Comparing two bundles is done by
the caller (run the replay, then call again with the new bundle's IDs)
— this keeps the harness side-effect-free and trivially testable.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Iterable

from .. import db


log = logging.getLogger("axiom.v1_archive.llm_backtest")


# Reuses the same flag the feature accessors honour — operator who
# disables the archive globally takes the backtest harness offline too.
def _use_archive() -> bool:
    return os.getenv(
        "AXIOM_USE_V1_ARCHIVE_FOR_FEATURES", "1",
    ).strip().lower() not in ("0", "false", "no", "off", "")


async def _table_present() -> bool:
    try:
        return bool(
            await db.fetchval(
                "SELECT to_regclass('v1_archive.llm_predictions') IS NOT NULL"
            )
        )
    except Exception as e:
        log.debug("v1_archive.llm_predictions preflight failed: %s", e)
        return False


@dataclass(frozen=True)
class ReplayBucket:
    """Aggregate accuracy stats for one (provider, model, league,
    category) slice of the replay."""
    provider: str
    model_id: str
    league: str
    category: str
    n: int
    n_correct: int
    win_rate: float
    brier: float | None
    avg_confidence: float | None


@dataclass(frozen=True)
class ReplayResult:
    total_rows: int
    buckets: tuple[ReplayBucket, ...]
    overall_win_rate: float
    overall_brier: float | None
    overall_avg_confidence: float | None
    filters: dict[str, Any] = field(default_factory=dict)


async def replay(
    *,
    providers: Iterable[str] | None = None,
    leagues: Iterable[str] | None = None,
    categories: Iterable[str] | None = None,
    since: Any = None,
    until: Any = None,
    limit: int | None = None,
) -> ReplayResult:
    """Run an in-place benchmark over V1's persisted LLM calls.

    All filters are optional.  Rows whose ``correct`` is NULL (V1 never
    resolved the market) are dropped from the accuracy aggregates — they
    can't contribute a signal either way.  Confidence + Brier are only
    aggregated for rows that supply a ``confidence`` value.

    Returns an empty :class:`ReplayResult` (``total_rows=0``) when the
    archive is disabled or the table is missing — callers gate on
    ``result.total_rows`` and treat an empty result as "no replay
    available", never as an error.
    """
    filters = {
        "providers": list(providers) if providers else None,
        "leagues": list(leagues) if leagues else None,
        "categories": list(categories) if categories else None,
        "since": since,
        "until": until,
        "limit": limit,
    }
    empty = ReplayResult(
        total_rows=0,
        buckets=(),
        overall_win_rate=0.0,
        overall_brier=None,
        overall_avg_confidence=None,
        filters=filters,
    )
    if not _use_archive():
        return empty
    if not await _table_present():
        return empty

    where_parts: list[str] = [
        "correct IS NOT NULL",
        "provider IS NOT NULL",
        "model_id IS NOT NULL",
    ]
    params: list[Any] = []

    def _add(clause_tmpl: str, value: Any) -> None:
        params.append(value)
        where_parts.append(clause_tmpl.format(idx=len(params)))

    if providers:
        _add("provider = ANY(${idx}::text[])", [str(p) for p in providers])
    if leagues:
        _add("upper(coalesce(league, '')) = ANY(${idx}::text[])",
             [str(l).upper() for l in leagues])
    if categories:
        _add("category = ANY(${idx}::text[])", [str(c) for c in categories])
    if since is not None:
        _add("predicted_at >= ${idx}", since)
    if until is not None:
        _add("predicted_at < ${idx}", until)

    where_sql = " AND ".join(where_parts)
    limit_sql = ""
    if limit is not None:
        params.append(int(limit))
        limit_sql = f"LIMIT ${len(params)}"

    sql = f"""
        WITH src AS (
          SELECT provider,
                 model_id,
                 coalesce(upper(league), 'UNKNOWN')  AS league,
                 coalesce(category, 'UNKNOWN')       AS category,
                 correct::int                         AS correct,
                 confidence
            FROM v1_archive.llm_predictions
           WHERE {where_sql}
           {limit_sql}
        )
        SELECT provider, model_id, league, category,
               count(*)                              AS n,
               sum(correct)                          AS n_correct,
               avg(correct::float)                   AS win_rate,
               -- Brier only over rows that supply a confidence.
               avg(CASE WHEN confidence IS NOT NULL
                        THEN (confidence - correct::float) *
                             (confidence - correct::float)
                        ELSE NULL END)               AS brier,
               avg(confidence)                       AS avg_confidence
          FROM src
         GROUP BY provider, model_id, league, category
         ORDER BY provider, model_id, league, category
    """

    try:
        rows = await db.fetch(sql, *params)
    except Exception as e:
        log.warning("v1_archive llm replay failed: %s", e)
        return empty

    buckets: list[ReplayBucket] = []
    total_n = 0
    total_correct = 0
    brier_weighted_sum = 0.0
    brier_weight = 0
    conf_weighted_sum = 0.0
    conf_weight = 0
    for r in rows:
        n = int(r["n"] or 0)
        n_correct = int(r["n_correct"] or 0)
        brier = float(r["brier"]) if r["brier"] is not None else None
        avg_conf = float(r["avg_confidence"]) if r["avg_confidence"] is not None else None
        buckets.append(ReplayBucket(
            provider=str(r["provider"]),
            model_id=str(r["model_id"]),
            league=str(r["league"]),
            category=str(r["category"]),
            n=n,
            n_correct=n_correct,
            win_rate=float(r["win_rate"] or 0.0),
            brier=brier,
            avg_confidence=avg_conf,
        ))
        total_n += n
        total_correct += n_correct
        if brier is not None:
            brier_weighted_sum += brier * n
            brier_weight += n
        if avg_conf is not None:
            conf_weighted_sum += avg_conf * n
            conf_weight += n

    overall_wr = (total_correct / total_n) if total_n else 0.0
    overall_brier = (brier_weighted_sum / brier_weight) if brier_weight else None
    overall_conf = (conf_weighted_sum / conf_weight) if conf_weight else None

    return ReplayResult(
        total_rows=total_n,
        buckets=tuple(buckets),
        overall_win_rate=overall_wr,
        overall_brier=overall_brier,
        overall_avg_confidence=overall_conf,
        filters=filters,
    )


__all__ = ["ReplayBucket", "ReplayResult", "replay"]
