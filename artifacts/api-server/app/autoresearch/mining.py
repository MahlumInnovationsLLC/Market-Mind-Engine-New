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
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

from .. import db
from . import stats, store


# Verticals whose Kalshi tickers carry a meaningful underlying symbol
# (BTC, ETH, AAPL, OIL, ...) we can join against UW's per-ticker rows.
# For these we aggregate UW signals per (vertical, underlying-ticker)
# instead of globally per vertical so each (signal × aggregator ×
# ticker) gets its own row in ``autoresearch_features`` — letting the
# admin promote e.g. "AAPL flow_alert count_24h predicts AAPL trades"
# without it being washed out by every other equity.
TICKER_LINKED_VERTICALS: frozenset[str] = frozenset({
    "crypto",
    "crypto-binary",
    "commodities",
    "tech-ai",
    "elections",
    "events-econ",
    "entertainment",
    "equities",
    "equity",
    "macro",
})


_TICKER_SPLIT_RE = re.compile(r"[-_.]")


def is_ticker_linked(vertical: str) -> bool:
    """Whether ``vertical`` should aggregate UW signals per-ticker."""
    return (vertical or "").strip().lower() in TICKER_LINKED_VERTICALS


def extract_underlying_ticker(market_id: str | None) -> str | None:
    """Best-effort underlying-symbol extraction from a Kalshi market id.

    Kalshi tickers look like ``KXBTC-25APR2310-T100000`` or
    ``KXAAPL_2025_Q2``; UW rows are keyed by the bare underlying
    (``BTC``, ``AAPL``).  We grab the first ``-``/``_``/``.`` separated
    segment and strip the ``KX`` prefix.  Returns ``None`` when the
    input is empty / unrecognisable so the caller can fall back to the
    vertical-wide aggregation rather than silently aggregating the
    wrong rows.
    """
    if not market_id:
        return None
    head = _TICKER_SPLIT_RE.split(str(market_id).strip(), 1)[0].upper()
    if not head:
        return None
    if head.startswith("KX") and len(head) > 2:
        head = head[2:]
    return head or None


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
                  t.executed_at                    AS created_at,
                  lower(coalesce(t.side, ''))      AS side_lc,
                  t.model_prob                     AS model_prob,
                  t.market_id                      AS market_id,
                  pm.actual_outcome                AS actual_outcome
                  FROM trades t
                  JOIN trade_postmortems pm
                    ON pm.trade_id = coalesce(t.trade_id, t.id::text)
                 WHERE t.vertical = $1
                   AND t.settled_at IS NOT NULL
                   AND t.settled_at > now() - ($2::int || ' days')::interval
                   AND t.model_prob IS NOT NULL
                   AND pm.actual_outcome IS NOT NULL
                   AND t.executed_at IS NOT NULL
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
            "market_id": r["market_id"],
        })
    return out


def _group_by_ticker(
    trades: Sequence[dict[str, Any]],
) -> dict[str | None, list[dict[str, Any]]]:
    """Bucket settled trades by their extracted underlying ticker.

    Trades whose ``market_id`` is missing or unparseable are collected
    under the ``None`` key so the caller can still evaluate them via
    the global (ticker-less) UW aggregation rather than silently
    dropping the rows. Buckets with no parseable trades simply omit
    the ``None`` key.
    """
    buckets: dict[str | None, list[dict[str, Any]]] = {}
    for t in trades:
        tk = extract_underlying_ticker(t.get("market_id"))
        buckets.setdefault(tk, []).append(t)
    return buckets


async def _aggregate_signal(
    *,
    signal_type: str,
    agg: _Aggregator,
    end_at: datetime,
    ticker: str | None = None,
) -> float:
    """Roll the UW stream up to ``end_at`` into a single scalar.

    When ``ticker`` is ``None`` we aggregate globally — appropriate for
    sports verticals where Kalshi markets have no UW counterpart.  When
    a ticker is provided (used for verticals listed in
    :data:`TICKER_LINKED_VERTICALS`) we additionally restrict to UW
    rows whose ``ticker`` column matches the supplied symbol so each
    underlying gets its own (signal × aggregator) candidate.
    """
    if end_at.tzinfo is None:
        end_at = end_at.replace(tzinfo=timezone.utc)
    start_at = end_at.timestamp() - agg.window_seconds
    ticker_norm = ticker.strip().upper() if ticker else None
    try:
        async with db.acquire() as c:
            if agg.op == "count":
                if ticker_norm is not None:
                    row = await c.fetchrow(
                        """
                        SELECT count(*)::int AS v
                          FROM uw_signals
                         WHERE signal_type = $1
                           AND occurred_at >= to_timestamp($2)
                           AND occurred_at <  $3
                           AND upper(ticker) = $4
                        """,
                        signal_type, start_at, end_at, ticker_norm,
                    )
                else:
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
                if ticker_norm is not None:
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
                           AND upper(ticker) = $4
                        """,
                        signal_type, start_at, end_at, ticker_norm,
                    )
                else:
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
    ticker: str | None = None,
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
            ticker=ticker,
        )
        values.append(v)
        residuals.append(float(t["y_side"]) - float(t["p_side"]))
    summary = stats.evaluate(values, residuals)
    summary["vertical"] = vertical
    summary["signal_type"] = signal_type
    summary["aggregator"] = aggregator.name
    if ticker is not None:
        summary["ticker"] = ticker
    return summary


async def _persist_candidate(
    *,
    vertical: str,
    signal_type: str,
    aggregator: _Aggregator,
    summary: dict[str, Any],
    ticker: str | None,
) -> bool:
    """Upsert one candidate row.  Returns True if persisted."""
    name = store.feature_name(vertical, signal_type, aggregator.name, ticker=ticker)
    metadata: dict[str, Any] = {
        "window_seconds": aggregator.window_seconds,
        "op": aggregator.op,
    }
    if ticker is not None:
        metadata["ticker"] = ticker
    row = await store.upsert_metrics(
        name=name,
        vertical=vertical,
        signal_type=signal_type,
        aggregator=aggregator.name,
        ic=float(summary["ic"]),
        sharpe=float(summary["sharpe"]),
        monotonicity=float(summary["monotonicity"]),
        n_samples=int(summary["n"]),
        metadata=metadata,
    )
    return row is not None


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

        # Verticals whose Kalshi tickers carry a real underlying symbol
        # (crypto, equities, macro, ...) get a per-(vertical, ticker)
        # pass so each underlying surfaces its own signal × aggregator
        # candidate.  Trades without a parseable market_id land in the
        # ``None`` bucket and fall through to the global aggregation so
        # we never silently lose them.  Other verticals stay on the
        # original global-only path.
        buckets: dict[str | None, list[dict[str, Any]]]
        if is_ticker_linked(vertical):
            buckets = _group_by_ticker(trades)
        else:
            buckets = {None: list(trades)}

        for ticker_key, bucket_trades in buckets.items():
            if len(bucket_trades) < _min_samples():
                res.insufficient_samples += 1
                continue
            for sig in signal_types:
                for agg in aggregators:
                    res.candidates_evaluated += 1
                    try:
                        summary = await _evaluate_candidate(
                            vertical=vertical,
                            signal_type=sig,
                            aggregator=agg,
                            trades=bucket_trades,
                            ticker=ticker_key,
                        )
                    except Exception as e:
                        suffix = f"/{ticker_key}" if ticker_key else ""
                        res.errors.append(
                            f"{vertical}{suffix}/{sig}/{agg.name}: "
                            f"{type(e).__name__}: {e}"
                        )
                        continue
                    if summary is None:
                        res.insufficient_samples += 1
                        continue
                    persisted = await _persist_candidate(
                        vertical=vertical,
                        signal_type=sig,
                        aggregator=agg,
                        summary=summary,
                        ticker=ticker_key,
                    )
                    if persisted:
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
