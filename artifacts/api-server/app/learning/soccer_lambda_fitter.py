"""Per-league soccer baseline scoring-rate fitter (task #903).

The live soccer predictor in :mod:`app.learning.soccer_live` uses a
per-league ``(lambda_home, lambda_away)`` pair as the prior scoring
intensity for the remaining-time goal grid.  The hand-set table in
``_BASELINE_LAMBDAS_BY_LEAGUE`` is seeded from public 2022-2024 goal
aggregates — fine for a cold start but it drifts as competitions evolve
(rule changes, expansion sides, set-piece training trends, etc.).

This module re-estimates ``(lambda_home, lambda_away)`` for each soccer
league we trade from our own historical full-time scorelines (collected
in ``sportradar_live_state`` by the same path the Dixon-Coles rho
fitter uses, task #900) and persists the fitted vector in the
``soccer_baseline_lambdas`` table created by migration 0059.  The
predictor reads the table on startup (and after each successful refit)
into an in-memory override map; the hand-set defaults remain the
fallback whenever the override row is missing or the fit was skipped
for insufficient samples.

Method
------

For each league:

  * Pull all terminal-status ``sportradar_live_state`` rows
    (``status IN ('closed','ended','complete','finished')``) for games
    whose ``fetched_at`` falls in the trailing-N-months window and take
    the latest row per ``game_id`` as the final scoreline.
  * The MLE for the independent-Poisson home / away goal counts is just
    the sample mean of each marginal — no optimiser needed.  Holding
    the Dixon-Coles low-score correction out of the fit keeps this
    estimator on the same support the predictor uses ``(lam, 0)`` /
    ``(0, lam)`` integrals against, and the rho-only fitter remains
    responsible for the low-score correction.
  * Compute the independent-Poisson log-likelihood at the MLE for the
    operator-panel readout.

Guard rails
-----------

  * Leagues with fewer than ``MIN_MATCHES_FOR_FIT`` terminal rows are
    skipped — the previously-persisted override (or the hand-set
    default) survives the cycle.  Below this, the sample mean is too
    noisy to trust over the calibrated baseline.
  * Fitted intensities are clipped to ``[LAMBDA_CLIP_MIN,
    LAMBDA_CLIP_MAX]`` so a corrupt feed (e.g. a missing-data run that
    looked like a flood of 0-0s) can't push the predictor's grid into
    a degenerate regime.
  * The full fitted vector is written one row per league into the
    ``soccer_baseline_lambdas`` table.  The predictor reads it via
    :func:`soccer_live.refresh_baseline_lambda_overrides` (also called
    from the cycle below so a freshly-fitted vector takes effect
    immediately without waiting for the next refresh tick).
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .. import db
from . import soccer_live


log = logging.getLogger(__name__)


# Refit cadence.  Per the task brief the fit re-runs on a weekly
# schedule — scoring rates only move on seasonal time scales so a
# daily refit is overkill and would add noise from short windows.
REFIT_INTERVAL_SECONDS = 7 * 24 * 3600.0

# Trailing window for the sample.  Twelve months covers a full season
# in every league we trade (including the summer-tournament tail for
# UCL) without dragging in pre-rule-change history.
LOOKBACK_MONTHS = 12

# Minimum sample size per league before we trust the MLE enough to
# override the hand-set default.  Below this, the sample-mean
# estimator's standard error blows past the gap between league
# defaults and the model wouldn't benefit from the swap.
MIN_MATCHES_FOR_FIT = 80

# Safe band on the fitted intensities.  The hand-set defaults sit in
# roughly ``[1.0, 1.8]``; this band gives plenty of room for real
# drift while protecting against pathological data.
LAMBDA_CLIP_MIN = 0.5
LAMBDA_CLIP_MAX = 3.5

# Terminal statuses that indicate the row carries a final scoreline.
# Mirrors the dc_rho_fitter so a future schema tweak only needs one
# follow-up.
_TERMINAL_STATUSES = ("closed", "ended", "complete", "finished")

# Monotonic timestamp of the last successful refit cycle.  Mirrors the
# cadence-gate pattern used by :mod:`app.learning.dc_rho_fitter` so
# the settlement loop can poll us cheaply on every tick.
_last_fit_at: float | None = None


@dataclass
class LeagueLambdaFitResult:
    league: str
    matches: int
    lambda_home: float | None
    lambda_away: float | None
    log_likelihood: float | None
    skipped_reason: str | None = None


@dataclass
class LambdaFitCycleResult:
    started_at: str
    completed_at: str | None = None
    duration_seconds: float = 0.0
    leagues_examined: int = 0
    leagues_fitted: int = 0
    leagues_skipped: int = 0
    per_league: list[LeagueLambdaFitResult] = field(default_factory=list)
    persisted: bool = False
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Likelihood + MLE
# ---------------------------------------------------------------------------


def _fit_lambdas(
    matches: list[tuple[int, int]],
) -> tuple[float, float, float]:
    """Independent-Poisson MLE for the home and away goal marginals.

    Returns ``(lambda_home, lambda_away, log_likelihood)``.  The MLE
    for an independent-Poisson sample is just the sample mean of each
    marginal, so no iterative search is needed.  Empty input returns
    ``(0.0, 0.0, -inf)`` — the cycle guards against this with the
    ``MIN_MATCHES_FOR_FIT`` gate before it ever gets here.
    """
    if not matches:
        return 0.0, 0.0, float("-inf")
    n = len(matches)
    sh = sum(h for h, _ in matches)
    sa = sum(a for _, a in matches)
    lam_h = sh / n
    lam_a = sa / n
    # Independent-Poisson log-likelihood at the MLE.  Kept finite even
    # when one marginal collapses to zero (every match nil-nil on that
    # side) so the operator panel always gets a number to read.
    ll = 0.0
    for h, a in matches:
        if lam_h > 0.0:
            ll += -lam_h + h * math.log(lam_h) - math.lgamma(h + 1)
        elif h != 0:
            return lam_h, lam_a, float("-inf")
        if lam_a > 0.0:
            ll += -lam_a + a * math.log(lam_a) - math.lgamma(a + 1)
        elif a != 0:
            return lam_h, lam_a, float("-inf")
    return lam_h, lam_a, ll


def _clip_lambda(lam: float) -> float:
    if lam < LAMBDA_CLIP_MIN:
        return LAMBDA_CLIP_MIN
    if lam > LAMBDA_CLIP_MAX:
        return LAMBDA_CLIP_MAX
    return lam


# ---------------------------------------------------------------------------
# Data fetch
# ---------------------------------------------------------------------------


async def _fetch_scorelines(league: str) -> list[tuple[int, int]]:
    """Pull final (home, away) scorelines for ``league`` from the
    live-state table, restricted to the trailing
    :data:`LOOKBACK_MONTHS` window.

    Takes the most-recently-fetched row per ``game_id`` whose status
    is terminal — that row is authoritative for the final score once
    the game has resolved.  Case-insensitive on both ``league`` and
    ``status`` since the producer normalises but historical rows from
    earlier ingest paths may differ.
    """
    try:
        rows = await db.fetch(
            """
            SELECT DISTINCT ON (game_id) home_score, away_score
              FROM sportradar_live_state
             WHERE upper(league) = upper($1)
               AND lower(status) = ANY($2::text[])
               AND home_score IS NOT NULL
               AND away_score IS NOT NULL
               AND fetched_at >= now() - ($3 || ' months')::interval
             ORDER BY game_id, fetched_at DESC
            """,
            league, list(_TERMINAL_STATUSES), str(LOOKBACK_MONTHS),
        )
    except Exception as e:
        log.warning("[soccer_lambda_fitter] scoreline fetch failed for %s: %s", league, e)
        return []
    out: list[tuple[int, int]] = []
    for r in rows:
        try:
            h = int(r["home_score"])
            a = int(r["away_score"])
        except (TypeError, ValueError):
            continue
        if h < 0 or a < 0:
            continue
        out.append((h, a))
    return out


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


async def _persist_row(
    league: str,
    lambda_home: float,
    lambda_away: float,
    matches: int,
    log_likelihood: float | None,
) -> bool:
    """Upsert one league's fitted row into ``soccer_baseline_lambdas``.

    Returns True on success.  Failures are logged and swallowed so a
    transient DB hiccup on one league doesn't abort the cycle.
    """
    try:
        async with db.acquire() as c:
            await c.execute(
                """
                INSERT INTO soccer_baseline_lambdas (
                    league, lambda_home, lambda_away, matches,
                    log_likelihood, fitted_at
                ) VALUES ($1, $2, $3, $4, $5, now())
                ON CONFLICT (league) DO UPDATE SET
                    lambda_home    = EXCLUDED.lambda_home,
                    lambda_away    = EXCLUDED.lambda_away,
                    matches        = EXCLUDED.matches,
                    log_likelihood = EXCLUDED.log_likelihood,
                    fitted_at      = EXCLUDED.fitted_at
                """,
                league, lambda_home, lambda_away, matches, log_likelihood,
            )
        return True
    except Exception as e:
        log.warning("[soccer_lambda_fitter] persist failed for %s: %s", league, e)
        return False


# ---------------------------------------------------------------------------
# Cycle entry point
# ---------------------------------------------------------------------------


async def run_cycle() -> LambdaFitCycleResult:
    """One pass: fit ``(lambda_home, lambda_away)`` per soccer league,
    persist the rows, and refresh the predictor's in-memory override
    cache.

    Best-effort throughout — per-league failures are captured per-row
    and never abort the cycle.
    """
    started = time.perf_counter()
    started_at = datetime.now(timezone.utc).isoformat()
    cr = LambdaFitCycleResult(started_at=started_at)

    persisted_any = False

    leagues = sorted(soccer_live.SOCCER_LEAGUES)
    cr.leagues_examined = len(leagues)
    for league in leagues:
        try:
            matches = await _fetch_scorelines(league)
        except Exception as e:
            cr.errors.append(f"{league}: fetch {type(e).__name__}: {e}")
            cr.leagues_skipped += 1
            cr.per_league.append(
                LeagueLambdaFitResult(
                    league=league, matches=0,
                    lambda_home=None, lambda_away=None,
                    log_likelihood=None,
                    skipped_reason=f"fetch_failed:{type(e).__name__}",
                ),
            )
            continue
        if len(matches) < MIN_MATCHES_FOR_FIT:
            cr.leagues_skipped += 1
            cr.per_league.append(
                LeagueLambdaFitResult(
                    league=league, matches=len(matches),
                    lambda_home=None, lambda_away=None,
                    log_likelihood=None,
                    skipped_reason="insufficient_samples",
                ),
            )
            continue
        try:
            lam_h, lam_a, ll = _fit_lambdas(matches)
        except Exception as e:
            cr.errors.append(f"{league}: fit {type(e).__name__}: {e}")
            cr.leagues_skipped += 1
            cr.per_league.append(
                LeagueLambdaFitResult(
                    league=league, matches=len(matches),
                    lambda_home=None, lambda_away=None,
                    log_likelihood=None,
                    skipped_reason=f"fit_failed:{type(e).__name__}",
                ),
            )
            continue
        lam_h_clipped = _clip_lambda(lam_h)
        lam_a_clipped = _clip_lambda(lam_a)
        ll_for_panel = ll if math.isfinite(ll) else None
        ok = await _persist_row(
            league, lam_h_clipped, lam_a_clipped, len(matches), ll_for_panel,
        )
        if ok:
            persisted_any = True
        cr.leagues_fitted += 1
        cr.per_league.append(
            LeagueLambdaFitResult(
                league=league, matches=len(matches),
                lambda_home=lam_h_clipped, lambda_away=lam_a_clipped,
                log_likelihood=ll_for_panel,
                skipped_reason=None if ok else "persist_failed",
            ),
        )

    cr.persisted = persisted_any
    # Refresh the predictor's in-memory cache from the table so the
    # freshly-persisted values take effect without waiting for the
    # next startup.  Best-effort — a refresh failure leaves the
    # previous cache in place.
    try:
        await soccer_live.refresh_baseline_lambda_overrides()
    except Exception as e:
        cr.errors.append(f"refresh: {type(e).__name__}: {e}")

    cr.duration_seconds = round(time.perf_counter() - started, 3)
    cr.completed_at = datetime.now(timezone.utc).isoformat()
    log.info(
        "[soccer_lambda_fitter] cycle done examined=%d fitted=%d skipped=%d "
        "persisted=%s duration=%.2fs",
        cr.leagues_examined, cr.leagues_fitted, cr.leagues_skipped,
        cr.persisted, cr.duration_seconds,
    )
    return cr


async def maybe_refit() -> LambdaFitCycleResult | None:
    """Run :func:`run_cycle` if the last successful fit is older than
    :data:`REFIT_INTERVAL_SECONDS`.  Returns ``None`` on a no-op so
    the caller can record "nothing happened" without ambiguity."""
    global _last_fit_at
    if _last_fit_at is not None and (time.time() - _last_fit_at) < REFIT_INTERVAL_SECONDS:
        return None
    cr = await run_cycle()
    _last_fit_at = time.time()
    return cr


def _reset_state_for_tests() -> None:
    """Test hook — clear the cadence gate so a unit test can drive
    :func:`maybe_refit` repeatedly without waiting a week."""
    global _last_fit_at
    _last_fit_at = None
