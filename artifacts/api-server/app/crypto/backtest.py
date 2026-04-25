"""Expanding-window backtest scaffold for the crypto-binary predictor.

The trained models will be retrained continuously via AutoResearch +
drift detection.  This scaffold gives the operator (and the test
suite) a way to evaluate either the in-process baseline or any future
trained artifact against historical OHLCV without leaving a Python
shell:

    from app.crypto.backtest import run_backtest
    summary = await run_backtest(symbol="BTC", timeframe="1h", days=14)
    print(summary)

Output is a `BacktestResult` dataclass with `n_evaluated`, `accuracy`,
`brier_score`, `log_loss`, and a per-bin reliability table.  We score
"BTC closes above K at the next bar" using a strike-grid sampled
relative to the price at evaluation time so the scaffold doesn't need a
real Kalshi market record.

The implementation is deliberately simple — at every bar in the test
window we:

  1. take all bars up to and including the current one as the feature
     window (an *expanding* window — at index `i` the model sees
     `bars[: i + 1]`).  We require at least `lookback` bars before the
     first evaluation so the rolling RSI/vol windows are warmed up;
  2. for each strike offset, build a synthetic ticker and ask the
     predictor for a probability;
  3. settle against the *next* close (was it above K? yes/no);
  4. accumulate the standard scoring stats.

The expanding-window choice keeps the scaffold cheap while letting the
features see all available history.  A future fixed-window variant
(`bars[i - lookback : i + 1]`) is a one-line change if the trainer
ever needs a stricter "online" backtest — but the current models are
stateless and don't benefit from the truncation.

This is enough to compare two candidate models over the same window
and to surface "drift" via a moving Brier score in the operator
dashboard.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from .. import db
from .features import build_features_from_bars
from .models import BaselineLGBXGBStack
from .predictor import _proba_yes
from .tickers import CryptoTicker


log = logging.getLogger(__name__)


# Strike offsets (as fraction of spot at eval time) we test.  +0.005 means
# "K = spot * 1.005" — i.e. needs a +50bp move at the horizon.  The
# symmetric grid keeps the Brier reasonably balanced between YES and NO.
DEFAULT_STRIKE_OFFSETS: tuple[float, ...] = (-0.01, -0.005, 0.0, 0.005, 0.01)


@dataclass
class BacktestResult:
    symbol: str
    timeframe: str
    n_evaluated: int = 0
    accuracy: float = 0.0
    brier_score: float = 0.0
    log_loss: float = 0.0
    # Reliability bins: [(bin_lower, bin_upper, mean_predicted, mean_actual, count), ...]
    reliability: list[tuple[float, float, float, float, int]] = field(default_factory=list)
    started_at: str = ""
    completed_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "n_evaluated": self.n_evaluated,
            "accuracy": round(self.accuracy, 6),
            "brier_score": round(self.brier_score, 6),
            "log_loss": round(self.log_loss, 6),
            "reliability": [
                {
                    "bin_lower": round(lo, 3),
                    "bin_upper": round(hi, 3),
                    "mean_predicted": round(p, 6),
                    "mean_actual": round(a, 6),
                    "count": n,
                }
                for (lo, hi, p, a, n) in self.reliability
            ],
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


def _reliability_table(
    predictions: Sequence[float], outcomes: Sequence[int], bins: int = 10
) -> list[tuple[float, float, float, float, int]]:
    edges = [i / bins for i in range(bins + 1)]
    out: list[tuple[float, float, float, float, int]] = []
    for i in range(bins):
        lo, hi = edges[i], edges[i + 1]
        ps: list[float] = []
        ys: list[int] = []
        for p, y in zip(predictions, outcomes):
            if (i == bins - 1 and lo <= p <= hi) or (lo <= p < hi):
                ps.append(p)
                ys.append(y)
        if not ps:
            continue
        out.append((lo, hi, sum(ps) / len(ps), sum(ys) / len(ys), len(ps)))
    return out


def evaluate_against_bars(
    bars: Sequence[dict[str, Any]],
    *,
    symbol: str,
    timeframe: str,
    model: Any | None = None,
    lookback: int = 50,
    strike_offsets: Sequence[float] = DEFAULT_STRIKE_OFFSETS,
) -> BacktestResult:
    """Pure-function rolling-window evaluator.

    `bars` is an oldest-first list of dicts (`ts`, `o`, `h`, `l`, `c`,
    `v`).  Steps through every index ≥ ``lookback`` and ≤ ``len(bars) - 2``
    (we need a "next bar" to settle), generating one prediction per
    strike offset.
    """
    if model is None:
        model = BaselineLGBXGBStack(timeframe=timeframe)

    started = datetime.now(timezone.utc).isoformat()
    predictions: list[float] = []
    outcomes: list[int] = []
    n_correct = 0
    sum_brier = 0.0
    sum_log_loss = 0.0

    for i in range(lookback, len(bars) - 1):
        window = bars[: i + 1]
        spot = float(window[-1]["c"])
        next_close = float(bars[i + 1]["c"])
        if spot <= 0:
            continue
        for offset in strike_offsets:
            strike = spot * (1.0 + offset)
            ticker = CryptoTicker(
                raw=f"BACKTEST-{symbol}-{timeframe}-{strike:.2f}",
                symbol=symbol,
                timeframe=timeframe,
                threshold=strike,
                direction="above",
            )
            try:
                fv = build_features_from_bars(window, ticker)
            except Exception:  # pragma: no cover — defensive
                continue
            p, _, _ = _proba_yes(model, fv.values)
            outcome = 1 if next_close > strike else 0
            predictions.append(p)
            outcomes.append(outcome)
            sum_brier += (p - outcome) ** 2
            # Clamp for log-loss stability — predict_proba can hit
            # 0/1 exactly when the sigmoid saturates.
            p_clamped = min(1 - 1e-9, max(1e-9, p))
            sum_log_loss += -(
                outcome * math.log(p_clamped)
                + (1 - outcome) * math.log(1 - p_clamped)
            )
            if (p >= 0.5) == bool(outcome):
                n_correct += 1

    n = len(predictions)
    result = BacktestResult(
        symbol=symbol,
        timeframe=timeframe,
        n_evaluated=n,
        accuracy=(n_correct / n) if n else 0.0,
        brier_score=(sum_brier / n) if n else 0.0,
        log_loss=(sum_log_loss / n) if n else 0.0,
        reliability=_reliability_table(predictions, outcomes) if n else [],
        started_at=started,
        completed_at=datetime.now(timezone.utc).isoformat(),
    )
    return result


async def record_run(
    result: BacktestResult, *, days: int, lookback: int
) -> None:
    """Persist a completed backtest into ``crypto_backtest_runs``.

    Best-effort: callers run the backtest synchronously and report the
    summary to the operator regardless, so a write failure here is
    logged and swallowed rather than failing the surrounding HTTP call.
    Empty runs (``n_evaluated == 0`` — no bars matched the requested
    window) are skipped so the trend chart isn't polluted with
    meaningless zero rows.
    """
    if result.n_evaluated <= 0:
        return
    started: datetime | None = None
    if result.started_at:
        try:
            started = datetime.fromisoformat(result.started_at)
        except ValueError:
            started = None
    completed: datetime | None = None
    if result.completed_at:
        try:
            completed = datetime.fromisoformat(result.completed_at)
        except ValueError:
            completed = None
    reliability_json = json.dumps(
        [
            {"lo": lo, "hi": hi, "p_mean": p, "actual": a, "n": n}
            for (lo, hi, p, a, n) in result.reliability
        ]
    )
    try:
        await db.execute(
            """
            INSERT INTO crypto_backtest_runs (
                symbol, timeframe, days, lookback, n_evaluated,
                accuracy, brier_score, log_loss, reliability,
                started_at, completed_at
            )
            VALUES (
                $1, $2, $3, $4, $5,
                $6, $7, $8, $9::jsonb,
                COALESCE($10, now()), COALESCE($11, now())
            )
            """,
            result.symbol,
            result.timeframe,
            int(days),
            int(lookback),
            int(result.n_evaluated),
            float(result.accuracy),
            float(result.brier_score),
            float(result.log_loss),
            reliability_json,
            started,
            completed,
        )
    except Exception:  # pragma: no cover — defensive: never fail the caller
        log.exception(
            "[crypto.backtest] failed to persist backtest result for %s/%s",
            result.symbol,
            result.timeframe,
        )


async def list_history(
    *, symbol: str, timeframe: str, limit: int = 50
) -> list[dict[str, Any]]:
    """Return the most recent ``limit`` persisted runs for the pair.

    Sorted oldest-first so the operator dashboard can plot the
    returned series straight onto a left-to-right time axis without
    re-sorting in JS.
    """
    capped = max(1, min(int(limit), 500))
    try:
        rows = await db.fetch(
            """
            SELECT id, symbol, timeframe, days, lookback,
                   n_evaluated, accuracy, brier_score, log_loss,
                   started_at, completed_at
              FROM crypto_backtest_runs
             WHERE symbol = $1 AND timeframe = $2
             ORDER BY completed_at DESC
             LIMIT $3
            """,
            symbol,
            timeframe,
            capped,
        )
    except Exception:
        log.exception(
            "[crypto.backtest] failed to list history for %s/%s",
            symbol,
            timeframe,
        )
        return []
    items = [
        {
            "id": int(r["id"]),
            "symbol": r["symbol"],
            "timeframe": r["timeframe"],
            "days": int(r["days"]),
            "lookback": int(r["lookback"]),
            "n_evaluated": int(r["n_evaluated"]),
            "accuracy": float(r["accuracy"]),
            "brier_score": float(r["brier_score"]),
            "log_loss": float(r["log_loss"]),
            "started_at": r["started_at"].isoformat() if r["started_at"] else None,
            "completed_at": (
                r["completed_at"].isoformat() if r["completed_at"] else None
            ),
        }
        for r in rows
    ]
    # Reverse to oldest-first so the chart reads left-to-right.
    items.reverse()
    return items


async def run_backtest(
    *,
    symbol: str = "BTC",
    timeframe: str = "1h",
    days: int = 14,
    lookback: int = 50,
    model: Any | None = None,
) -> BacktestResult:
    """DB-backed wrapper: pull bars from `crypto_ohlcv` then evaluate."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    rows = await db.fetch(
        """
        SELECT ts, o, h, l, c, v
          FROM crypto_ohlcv
         WHERE symbol = $1 AND granularity = $2
           AND ts >= $3 AND ts <= $4
         ORDER BY ts ASC
        """,
        symbol,
        timeframe,
        start,
        end,
    )
    bars = [dict(r) for r in rows]
    log.info(
        "[crypto.backtest] %s/%s window=%dd bars=%d", symbol, timeframe, days, len(bars)
    )
    return evaluate_against_bars(
        bars,
        symbol=symbol,
        timeframe=timeframe,
        model=model,
        lookback=lookback,
    )
