"""Per-league Dixon-Coles rho fitter (task #900).

The live soccer predictor in :mod:`app.learning.soccer_live` applies a
Dixon-Coles low-score correlation correction (``tau``) to the
remaining-time goal grid.  The correlation parameter ``rho`` is a
per-league constant — originally a hand-set table sourced from public
DC fits, kept under ``_DC_RHO_BY_LEAGUE``.

This module re-estimates ``rho`` for each soccer league we trade
directly from our own historical full-time scorelines (collected in
``sportradar_live_state``) and persists the fitted vector in a
``runtime_settings`` row so the predictor can read it without a code
deploy.  The fit re-runs on a daily cadence from the settlement
scheduler so rho tracks any seasonal drift in low-score frequencies.

Method
------

For each league:

  * Pull all terminal-status ``sportradar_live_state`` rows
    (``status IN ('closed','ended','complete','finished')``) and take
    the latest row per ``game_id`` as the final scoreline.
  * Hold the per-league baseline scoring intensities
    (``_BASELINE_LAMBDAS_BY_LEAGUE``) fixed — this fitter is rho-only
    by design.  Re-fitting lambdas would couple this job to team
    strength estimation and isn't required to pin down the low-score
    correlation.
  * Build the truncated independent-Poisson joint goal grid (8x8 cap
    matches the predictor) and apply the DC ``tau`` correction to the
    four low-score cells parameterised by ``rho``.
  * Renormalise the grid so the corrected cells form a proper PMF
    (the original DC paper omits this since ``tau`` is meant as a
    small perturbation, but on small samples a non-trivial rho can
    push the four-cell mass off enough that the renormalised
    likelihood gives a more stable ML estimate).
  * Maximise the sum of log-PMFs over the observed scorelines via a
    coarse-then-fine 1-D scan on ``rho ∈ [-0.30, +0.05]``.  No SciPy
    dependency — the search is cheap (a few hundred grid evaluations
    per league) and avoids the gradient-singularity issues a generic
    optimiser would hit at the cell-positivity boundary.

Guard rails
-----------

  * Leagues with fewer than ``MIN_MATCHES_FOR_FIT`` terminal rows are
    skipped — the fitted value is left at whatever the persisted
    override (or the hand-set default) already says.
  * The fitted ``rho`` is clipped into the safe band
    ``[-0.20, 0.0]`` documented in the predictor.  A wider value
    risks negative corrected cells at the baseline lambdas; the
    predictor clips those at zero but the renormalisation would tilt
    the rest of the grid in a way the model wasn't validated for.
  * The full fitted vector is persisted as a single JSON string in
    ``runtime_settings`` under ``DC_RHO_OVERRIDES_KEY``.  The
    predictor reads it via :func:`soccer_live.refresh_dc_rho_overrides`
    (also called from the cycle below so a freshly-fitted vector takes
    effect immediately without waiting for the next refresh tick).
"""
from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .. import db
from . import soccer_live


log = logging.getLogger(__name__)


# ``runtime_settings`` key under which the fitted per-league rho
# vector is persisted (JSON object: {"EPL": -0.105, ...}).  Read by
# :func:`soccer_live.refresh_dc_rho_overrides`.
DC_RHO_OVERRIDES_KEY = "learning.dc_rho_by_league"

# Refit cadence.  The fit is cheap (a few thousand matches per league
# at worst) but the result only moves on seasonal time scales, so a
# daily cadence is plenty.
REFIT_INTERVAL_SECONDS = 24 * 3600.0

# Minimum sample size per league before we trust the MLE enough to
# override the hand-set default.  Below this, low-score cell counts
# are too noisy and the fitter can land on the band edge for the
# wrong reason.
MIN_MATCHES_FOR_FIT = 80

# Search band for rho.  Anchored to the documented safe band in
# ``soccer_live._DC_RHO_BY_LEAGUE`` plus a small positive tail so a
# league that empirically under-draws (rare for soccer but possible
# on a small window) doesn't get pinned at zero by the search.
RHO_SEARCH_MIN = -0.30
RHO_SEARCH_MAX = 0.05

# Final clipping band before persisting.  Tighter than the search
# band so the predictor never sees a rho that would push corrected
# cells negative at the baseline lambdas.
RHO_CLIP_MIN = -0.20
RHO_CLIP_MAX = 0.0

# Grid truncation cap — must match the predictor so the fitted
# likelihood mirrors what the predictor will evaluate at inference.
_GRID_MAX = soccer_live._DC_MAX_REMAINING_GOALS

# Terminal statuses that indicate the row carries a final scoreline.
# Mirrors the producer-side vocabulary in
# ``app.feeds.sportradar._extract_soccer_live_rows``.
_TERMINAL_STATUSES = ("closed", "ended", "complete", "finished")

# Monotonic timestamp of the last successful fit cycle.  Mirrors the
# pattern used by :mod:`app.calibrators` so the settlement loop can
# poll us cheaply on every tick and we only do real work once per
# ``REFIT_INTERVAL_SECONDS``.
_last_fit_at: float | None = None

# Most recent completed cycle result.  Cached so the operator
# dashboard can render per-league sample sizes, skipped reasons, and
# the last-fit timestamp without re-running the fit on each page
# load (task #904).
_last_cycle_result: "DcRhoFitCycleResult | None" = None


@dataclass
class LeagueFitResult:
    league: str
    matches: int
    rho_fitted: float | None
    rho_persisted: float | None
    log_likelihood: float | None
    skipped_reason: str | None = None


@dataclass
class DcRhoFitCycleResult:
    started_at: str
    completed_at: str | None = None
    duration_seconds: float = 0.0
    leagues_examined: int = 0
    leagues_fitted: int = 0
    leagues_skipped: int = 0
    per_league: list[LeagueFitResult] = field(default_factory=list)
    persisted: bool = False
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Likelihood
# ---------------------------------------------------------------------------


def _build_grid_independent(lam_h: float, lam_a: float) -> list[list[float]]:
    """Pre-compute the independent-Poisson joint grid once per league.

    The rho-only fit re-uses this grid for every candidate rho, only
    touching the four low-score cells via :func:`soccer_live._dc_tau`.
    """
    ph = [soccer_live._poisson_pmf(lam_h, k) for k in range(_GRID_MAX + 1)]
    pa = [soccer_live._poisson_pmf(lam_a, k) for k in range(_GRID_MAX + 1)]
    grid = [[ph[h] * pa[a] for a in range(_GRID_MAX + 1)] for h in range(_GRID_MAX + 1)]
    return grid


def _corrected_grid(
    base_grid: list[list[float]], lam_h: float, lam_a: float, rho: float,
) -> tuple[list[list[float]], float]:
    """Apply the DC tau correction to the four low-score cells.

    Returns ``(grid, total_mass)`` where ``total_mass`` is the sum of
    all cells *after* correction — the caller divides by this to get
    a proper PMF.
    """
    grid = [row[:] for row in base_grid]
    grid[0][0] *= soccer_live._dc_tau(0, 0, lam_h, lam_a, rho)
    grid[0][1] *= soccer_live._dc_tau(0, 1, lam_h, lam_a, rho)
    grid[1][0] *= soccer_live._dc_tau(1, 0, lam_h, lam_a, rho)
    grid[1][1] *= soccer_live._dc_tau(1, 1, lam_h, lam_a, rho)
    total = 0.0
    for row in grid:
        for v in row:
            total += v
    return grid, total


def _log_likelihood(
    matches: list[tuple[int, int]],
    base_grid: list[list[float]],
    lam_h: float,
    lam_a: float,
    rho: float,
) -> float:
    """Sum of log P(h, a) over the observed matches under the
    renormalised DC-corrected grid.

    Scores beyond the truncation cap fall back to the independent
    Poisson PMF (no DC correction beyond the low-score cells) so a
    rare 7-3 doesn't crash the fit; their contribution is rho-
    independent and so doesn't affect the argmax.
    """
    grid, total = _corrected_grid(base_grid, lam_h, lam_a, rho)
    if total <= 0.0:
        return float("-inf")
    ll = 0.0
    for h, a in matches:
        if 0 <= h <= _GRID_MAX and 0 <= a <= _GRID_MAX:
            cell = grid[h][a]
            if cell <= 0.0:
                return float("-inf")
            ll += math.log(cell / total)
        else:
            ph = soccer_live._poisson_pmf(lam_h, h)
            pa = soccer_live._poisson_pmf(lam_a, a)
            cell = ph * pa
            if cell <= 0.0:
                return float("-inf")
            ll += math.log(cell)
    return ll


def _fit_rho(
    matches: list[tuple[int, int]], lam_h: float, lam_a: float,
) -> tuple[float, float]:
    """Coarse-then-fine 1-D MLE for rho on the renormalised grid.

    Returns ``(rho_hat, log_likelihood_at_hat)``.  No SciPy: the
    1-D search is cheap and avoids gradient issues at the cell-
    positivity boundary.
    """
    base_grid = _build_grid_independent(lam_h, lam_a)
    # Coarse pass: 36 points across the search band.
    coarse_n = 36
    best_rho = 0.0
    best_ll = float("-inf")
    for i in range(coarse_n + 1):
        rho = RHO_SEARCH_MIN + (RHO_SEARCH_MAX - RHO_SEARCH_MIN) * (i / coarse_n)
        ll = _log_likelihood(matches, base_grid, lam_h, lam_a, rho)
        if ll > best_ll:
            best_ll = ll
            best_rho = rho
    # Fine pass: bracket the coarse winner with a ±2*step window and
    # resolve to 1e-4 precision.
    step = (RHO_SEARCH_MAX - RHO_SEARCH_MIN) / coarse_n
    lo = max(RHO_SEARCH_MIN, best_rho - 2.0 * step)
    hi = min(RHO_SEARCH_MAX, best_rho + 2.0 * step)
    fine_n = 200
    for i in range(fine_n + 1):
        rho = lo + (hi - lo) * (i / fine_n)
        ll = _log_likelihood(matches, base_grid, lam_h, lam_a, rho)
        if ll > best_ll:
            best_ll = ll
            best_rho = rho
    return best_rho, best_ll


def _clip_rho(rho: float) -> float:
    if rho < RHO_CLIP_MIN:
        return RHO_CLIP_MIN
    if rho > RHO_CLIP_MAX:
        return RHO_CLIP_MAX
    return rho


# ---------------------------------------------------------------------------
# Data fetch
# ---------------------------------------------------------------------------


async def _fetch_scorelines(league: str) -> list[tuple[int, int]]:
    """Pull final (home, away) scorelines for ``league`` from the
    live-state table.

    Takes the most-recently-fetched row per ``game_id`` whose status
    is terminal — that row is authoritative for the final score once
    the game has resolved.  Case-insensitive on both ``league`` and
    ``status`` since the producer normalises but historical rows from
    earlier ingest paths may differ.

    DB failures are *propagated* (not swallowed) so the cycle can
    label the league with the ``fetch_failed:<ExcType>`` skip reason
    the operator panel surfaces.  Returning ``[]`` on error would
    collapse fetch outages into the ``insufficient_samples`` bucket
    and hide a real infra problem from operators (task #904).
    """
    rows = await db.fetch(
        """
        SELECT DISTINCT ON (game_id) home_score, away_score
          FROM sportradar_live_state
         WHERE upper(league) = upper($1)
           AND lower(status) = ANY($2::text[])
           AND home_score IS NOT NULL
           AND away_score IS NOT NULL
         ORDER BY game_id, fetched_at DESC
        """,
        league, list(_TERMINAL_STATUSES),
    )
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


async def _persist_overrides(overrides: dict[str, float]) -> bool:
    """Upsert the fitted vector into ``runtime_settings``.

    Returns True on success.  Failures are logged and swallowed so a
    transient DB hiccup doesn't take down the settlement scheduler.
    """
    try:
        async with db.acquire() as c:
            await c.execute(
                """
                INSERT INTO runtime_settings (key, value, updated_at, updated_by)
                VALUES ($1, $2, now(), $3)
                ON CONFLICT (key) DO UPDATE SET
                    value      = EXCLUDED.value,
                    updated_at = now(),
                    updated_by = EXCLUDED.updated_by
                """,
                DC_RHO_OVERRIDES_KEY,
                json.dumps(overrides, sort_keys=True),
                "dc_rho_fitter",
            )
        return True
    except Exception as e:
        log.warning("[dc_rho_fitter] persist failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Cycle entry point
# ---------------------------------------------------------------------------


async def run_cycle() -> DcRhoFitCycleResult:
    """One pass: fit rho per soccer league, persist the vector, and
    refresh the predictor's in-memory override cache.

    Best-effort throughout — per-league failures are captured per-row
    and never abort the cycle.
    """
    started = time.perf_counter()
    started_at = datetime.now(timezone.utc).isoformat()
    cr = DcRhoFitCycleResult(started_at=started_at)

    # Start from whatever is already persisted so a league we skip on
    # this cycle (insufficient sample) keeps its previously-fitted
    # value instead of reverting silently to the hand-set default.
    existing_overrides: dict[str, float] = {}
    try:
        raw = await db.fetchval(
            "SELECT value FROM runtime_settings WHERE key = $1",
            DC_RHO_OVERRIDES_KEY,
        )
        if raw:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    try:
                        existing_overrides[str(k).upper()] = float(v)
                    except (TypeError, ValueError):
                        continue
    except Exception as e:
        log.debug("[dc_rho_fitter] existing override read failed: %s", e)

    next_overrides: dict[str, float] = dict(existing_overrides)

    leagues = sorted(soccer_live.SOCCER_LEAGUES)
    cr.leagues_examined = len(leagues)
    for league in leagues:
        try:
            matches = await _fetch_scorelines(league)
        except Exception as e:
            cr.errors.append(f"{league}: fetch {type(e).__name__}: {e}")
            cr.leagues_skipped += 1
            cr.per_league.append(
                LeagueFitResult(
                    league=league, matches=0, rho_fitted=None,
                    rho_persisted=next_overrides.get(league),
                    log_likelihood=None,
                    skipped_reason=f"fetch_failed:{type(e).__name__}",
                ),
            )
            continue
        if len(matches) < MIN_MATCHES_FOR_FIT:
            cr.leagues_skipped += 1
            cr.per_league.append(
                LeagueFitResult(
                    league=league, matches=len(matches), rho_fitted=None,
                    rho_persisted=next_overrides.get(league),
                    log_likelihood=None,
                    skipped_reason="insufficient_samples",
                ),
            )
            continue
        lam_h, lam_a = soccer_live._baseline_lambdas_for_league(league)
        try:
            rho_hat, ll = _fit_rho(matches, lam_h, lam_a)
        except Exception as e:
            cr.errors.append(f"{league}: fit {type(e).__name__}: {e}")
            cr.leagues_skipped += 1
            cr.per_league.append(
                LeagueFitResult(
                    league=league, matches=len(matches), rho_fitted=None,
                    rho_persisted=next_overrides.get(league),
                    log_likelihood=None,
                    skipped_reason=f"fit_failed:{type(e).__name__}",
                ),
            )
            continue
        rho_clipped = _clip_rho(rho_hat)
        next_overrides[league] = rho_clipped
        cr.leagues_fitted += 1
        cr.per_league.append(
            LeagueFitResult(
                league=league, matches=len(matches),
                rho_fitted=rho_hat, rho_persisted=rho_clipped,
                log_likelihood=ll,
            ),
        )

    # Only write & refresh if anything actually changed (saves an
    # audit-noisy write on cycles where every league was skipped).
    if next_overrides != existing_overrides:
        cr.persisted = await _persist_overrides(next_overrides)
        if cr.persisted:
            soccer_live.set_dc_rho_overrides(next_overrides)
    else:
        # Keep the in-memory cache warm even on no-op cycles so a
        # fresh process picks up the persisted vector promptly.
        soccer_live.set_dc_rho_overrides(next_overrides)

    cr.duration_seconds = round(time.perf_counter() - started, 3)
    cr.completed_at = datetime.now(timezone.utc).isoformat()
    log.info(
        "[dc_rho_fitter] cycle done examined=%d fitted=%d skipped=%d "
        "persisted=%s duration=%.2fs",
        cr.leagues_examined, cr.leagues_fitted, cr.leagues_skipped,
        cr.persisted, cr.duration_seconds,
    )
    global _last_cycle_result
    _last_cycle_result = cr
    return cr


def last_cycle_result() -> DcRhoFitCycleResult | None:
    """Return the most recent completed :func:`run_cycle` result, or
    ``None`` if the fitter hasn't run since process start.

    Operator dashboard reads this to render per-league sample sizes
    and skipped reasons without re-running the fit.
    """
    return _last_cycle_result


async def maybe_refit() -> DcRhoFitCycleResult | None:
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
    :func:`maybe_refit` repeatedly without waiting 24 hours."""
    global _last_fit_at, _last_cycle_result
    _last_fit_at = None
    _last_cycle_result = None
