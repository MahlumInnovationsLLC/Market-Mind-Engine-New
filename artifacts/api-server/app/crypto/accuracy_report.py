"""Rolling-window accuracy report for settled crypto predictions (task #580).

Once a Kalshi crypto market settles, the discovery + settler feed in
:mod:`app.feeds.kalshi_crypto_markets` stamps the resolved
``outcome`` / ``settle_price`` / ``settled_at`` columns onto the
originating ``predictions`` row (migration ``0022_predictions_settlement``).
The values were unused by reporting until now — there was no place that
joined a settled prediction back to the model probability the
orchestrator scored it with.

This module closes that loop: for a configurable rolling window
(``24h`` / ``7d`` / ``30d`` by default) it joins each settled crypto
prediction to the most recent ``swarm_debates.consensus_probability``
recorded for the same market and reports realised accuracy, Brier and
log-loss — sliced by ``(symbol, timeframe)`` so the operator can see
which crypto contracts the live model handles well versus poorly.

The report intentionally pulls the model probability from
``swarm_debates`` (not ``predictions.home_win_prob``, which is unused
by the crypto producer) because that is where the orchestrator
persists the YES probability it actually traded against — written by
:func:`app.swarm.runner.run_debate` and joined back via
``swarm_debates.game_id == predictions.game_id`` (the Kalshi ticker
for crypto markets) or via
``swarm_debates.game_context->>'kalshi_market_id'``.

Public surface:
    DEFAULT_WINDOW_HOURS — windows surfaced by the admin endpoint
    SliceMetrics, WindowReport — return shapes
    evaluate_window(window_hours) — single-window report
    evaluate_report(window_hours_list=...) — multi-window admin payload
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

from .. import db
from .tickers import (
    InvalidCryptoTicker,
    SUPPORTED_SYMBOLS,
    SUPPORTED_TIMEFRAMES,
    parse_crypto_ticker,
)


log = logging.getLogger(__name__)


# Default rolling windows surfaced by the admin endpoint.  Hours so a
# single integer column can express both intra-day and multi-day
# rolling reports without a unit-tag string in the schema.
DEFAULT_WINDOW_HOURS: tuple[int, ...] = (24, 24 * 7, 24 * 30)

# Numerical guard for log-loss: probabilities exactly at 0 or 1 would
# blow up `log(p)` / `log(1 - p)`, so clamp the same way the in-process
# crypto backtest scaffold does (``app.crypto.backtest``).
_LOG_LOSS_EPS: float = 1e-9

# League tag used by the Kalshi crypto producer when it upserts
# candidate rows.  Keep in sync with
# :mod:`app.feeds.kalshi_crypto_markets`.
_LEAGUE: str = "CRYPTO"


# Joined SELECT: each settled crypto prediction in the rolling window
# alongside the most recent debate's consensus probability for the
# same market.  The lateral join walks `swarm_debates` via either the
# game_id heuristic (crypto's game_id == kalshi ticker) or the more
# explicit `game_context->>'kalshi_market_id'` linkage so legacy
# debates persisted before the linkage convention still resolve.
_WINDOW_SQL = """
SELECT
    p.id                                   AS prediction_id,
    p.kalshi_market_id                     AS market_id,
    p.outcome                              AS outcome,
    p.settle_price                         AS settle_price,
    p.settled_at                           AS settled_at,
    coalesce(p.game_context->>'symbol', '')    AS symbol,
    coalesce(p.game_context->>'timeframe', '') AS timeframe,
    sd.consensus_probability               AS p_yes
  FROM predictions p
  LEFT JOIN LATERAL (
      SELECT consensus_probability
        FROM swarm_debates
       WHERE (game_context->>'kalshi_market_id') = p.kalshi_market_id
          OR game_id = p.game_id
       ORDER BY created_at DESC
       LIMIT 1
  ) sd ON true
 WHERE p.league = $1
   AND p.settled_at IS NOT NULL
   AND p.settled_at >= now() - make_interval(hours => $2)
"""


@dataclass(frozen=True)
class SliceMetrics:
    """Realised accuracy / Brier / log-loss for one ``(symbol, timeframe)``
    bucket inside a rolling window.

    ``symbol`` / ``timeframe`` are ``None`` for the overall summary row
    (no slicing).  ``yes_count`` / ``no_count`` let the operator spot
    label-imbalance issues that would otherwise make a low Brier
    deceptive — e.g. a window of 100 predictions that all settled
    ``no``.

    Metrics are ``None`` (not ``0``) when the bucket has zero scored
    rows so the dashboard can render "no data" instead of a misleading
    perfect-zero accuracy.
    """

    symbol: str | None
    timeframe: str | None
    n: int
    accuracy: float | None
    brier: float | None
    log_loss: float | None
    yes_count: int
    no_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "n": self.n,
            "accuracy": _round(self.accuracy),
            "brier": _round(self.brier),
            "log_loss": _round(self.log_loss),
            "yes_count": self.yes_count,
            "no_count": self.no_count,
        }


@dataclass(frozen=True)
class WindowReport:
    """All slices for one rolling window plus an overall summary.

    ``n_void`` is reported separately because Kalshi voids refund every
    contract — they can't contribute to a Brier and including them in
    the denominator would unfairly drag accuracy.  ``n_unscored``
    counts settled rows we couldn't score because no model probability
    exists yet (e.g. the swarm never debated the market before it
    settled), which is also useful to surface so the operator knows
    coverage.
    """

    window_hours: int
    n_settled: int
    n_void: int
    n_unscored: int
    overall: SliceMetrics
    slices: list[SliceMetrics] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "window_hours": self.window_hours,
            "n_settled": self.n_settled,
            "n_void": self.n_void,
            "n_unscored": self.n_unscored,
            "overall": self.overall.as_dict(),
            "slices": [s.as_dict() for s in self.slices],
        }


def _round(v: float | None) -> float | None:
    if v is None:
        return None
    return round(float(v), 6)


def _coerce_prob(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    if not (0.0 <= v <= 1.0):
        return None
    return v


def _slot_from_row(row: dict[str, Any]) -> tuple[str | None, str | None]:
    """Resolve ``(symbol, timeframe)`` for a row.

    Prefers the values the discovery feed wrote into
    ``game_context`` — that's the canonical source.  Falls back to
    re-parsing the Kalshi ticker so legacy rows (or rows whose
    game_context predates the symbol/timeframe tags) still get
    grouped correctly instead of collapsing into an "unknown" bucket.
    """
    symbol = (row.get("symbol") or "").strip().upper() or None
    timeframe = (row.get("timeframe") or "").strip().lower() or None
    if symbol and timeframe:
        return symbol, timeframe
    market_id = row.get("market_id")
    if isinstance(market_id, str) and market_id:
        try:
            parsed = parse_crypto_ticker(market_id)
        except InvalidCryptoTicker:
            return symbol, timeframe
        if symbol is None and parsed.symbol in SUPPORTED_SYMBOLS:
            symbol = parsed.symbol
        if timeframe is None and parsed.timeframe in SUPPORTED_TIMEFRAMES:
            timeframe = parsed.timeframe
    return symbol, timeframe


def _summarize(
    symbol: str | None,
    timeframe: str | None,
    samples: list[tuple[float, int]],
) -> SliceMetrics:
    """Pure-function: turn a list of ``(p_yes, y)`` pairs into a
    :class:`SliceMetrics` row.

    ``samples`` is empty → metrics are ``None`` and counters zero so
    the dashboard renders a clear "no data" state.
    """
    n = len(samples)
    if n == 0:
        return SliceMetrics(
            symbol=symbol, timeframe=timeframe, n=0,
            accuracy=None, brier=None, log_loss=None,
            yes_count=0, no_count=0,
        )
    correct = 0
    sum_brier = 0.0
    sum_log_loss = 0.0
    yes_count = 0
    for p, y in samples:
        if (p >= 0.5) == bool(y):
            correct += 1
        sum_brier += (p - y) ** 2
        p_clamped = min(1.0 - _LOG_LOSS_EPS, max(_LOG_LOSS_EPS, p))
        sum_log_loss += -(
            y * math.log(p_clamped) + (1 - y) * math.log(1.0 - p_clamped)
        )
        if y == 1:
            yes_count += 1
    return SliceMetrics(
        symbol=symbol,
        timeframe=timeframe,
        n=n,
        accuracy=correct / n,
        brier=sum_brier / n,
        log_loss=sum_log_loss / n,
        yes_count=yes_count,
        no_count=n - yes_count,
    )


def compute_from_rows(
    window_hours: int, rows: list[dict[str, Any]]
) -> WindowReport:
    """Pure compute step factored out so the unit tests don't need a DB.

    Counts every settled row that landed in the window, then partitions
    them into:

      * ``void`` — refunded, dropped from the metrics (counted in
        ``n_void``);
      * scored — has a usable YES probability and a binary outcome;
      * unscored — binary outcome but no model probability we can
        join to (also counted so coverage gaps surface in the report).
    """
    n_void = 0
    n_unscored = 0
    overall_buf: list[tuple[float, int]] = []
    sliced: dict[tuple[str | None, str | None], list[tuple[float, int]]] = {}
    for row in rows:
        outcome = (row.get("outcome") or "").strip().lower()
        if outcome == "void":
            n_void += 1
            continue
        if outcome not in ("yes", "no"):
            # Defensive: an unrecognised label means the settler stamped
            # something we don't model — treat it like "unscored" so the
            # counter still climbs and the operator can investigate.
            n_unscored += 1
            continue
        prob = _coerce_prob(row.get("p_yes"))
        if prob is None:
            n_unscored += 1
            continue
        y = 1 if outcome == "yes" else 0
        symbol, timeframe = _slot_from_row(row)
        sample = (prob, y)
        overall_buf.append(sample)
        sliced.setdefault((symbol, timeframe), []).append(sample)

    overall = _summarize(None, None, overall_buf)
    # Stable, predictable ordering: alphabetical by symbol, then by the
    # canonical timeframe ordering (5m < 15m < 1h).  Unknown buckets
    # ("~") sort to the end so the dashboard's primary rows stay first.
    timeframe_rank = {tf: i for i, tf in enumerate(SUPPORTED_TIMEFRAMES)}
    slice_rows = sorted(
        (
            _summarize(sym, tf, samples)
            for (sym, tf), samples in sliced.items()
        ),
        key=lambda m: (
            m.symbol or "~",
            timeframe_rank.get(m.timeframe or "", len(SUPPORTED_TIMEFRAMES)),
            m.timeframe or "~",
        ),
    )
    n_settled = len(overall_buf) + n_unscored + n_void
    return WindowReport(
        window_hours=window_hours,
        n_settled=n_settled,
        n_void=n_void,
        n_unscored=n_unscored,
        overall=overall,
        slices=slice_rows,
    )


async def _fetch_rows(window_hours: int) -> list[dict[str, Any]]:
    """Read every settled crypto prediction in the rolling window.

    Soft-fails to an empty list so a transient DB blip degrades the
    report to "no data" instead of taking the admin endpoint down —
    matches the convention used elsewhere in this package
    (``app.tech_ai.promotion``, ``app.crypto.backtest_scheduler``).
    """
    try:
        rows = await db.fetch(_WINDOW_SQL, _LEAGUE, int(window_hours))
    except Exception as e:  # noqa: BLE001 — defensive
        log.debug(
            "crypto accuracy report fetch failed (window=%sh): %s",
            window_hours, e,
        )
        return []
    return [dict(r) for r in rows]


async def evaluate_window(window_hours: int) -> WindowReport:
    """Pull joined rows for the window and compute the report."""
    if window_hours <= 0:
        raise ValueError(
            f"window_hours must be positive, got {window_hours!r}"
        )
    rows = await _fetch_rows(window_hours)
    return compute_from_rows(window_hours, rows)


async def evaluate_report(
    window_hours_list: tuple[int, ...] = DEFAULT_WINDOW_HOURS,
) -> dict[str, Any]:
    """Build the multi-window payload the admin endpoint surfaces.

    Windows are evaluated in the order supplied; duplicates and
    non-positive values are dropped so an operator override can't
    accidentally produce a noisy payload.
    """
    seen: set[int] = set()
    windows: list[dict[str, Any]] = []
    for raw in window_hours_list:
        try:
            w = int(raw)
        except (TypeError, ValueError):
            continue
        if w <= 0 or w in seen:
            continue
        seen.add(w)
        report = await evaluate_window(w)
        windows.append(report.as_dict())
    return {
        "league": _LEAGUE,
        "windows": windows,
    }
