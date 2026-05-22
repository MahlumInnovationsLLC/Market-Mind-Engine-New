"""Per-league soccer baseline lambda refresher (task #896).

The live soccer predictor in :mod:`app.learning.soccer_live` reads
per-league home/away goal baselines from ``_BASELINE_LAMBDAS_BY_LEAGUE``.
Those values were hand-set from 2022/23 → 2024/25 public goal data and
will drift as new seasons roll over and we have no per-team strength
model on top.  Rather than recompiling the file every summer, this
module recomputes the per-league baselines from our own settled
historical scorelines (``sportradar_live_state`` terminal rows) on a
daily cadence and persists the result in ``runtime_settings`` so the
predictor can read it without a code deploy.

Method
------

For each soccer league we trade:

  * Pull terminal-status ``sportradar_live_state`` rows from the last
    ``LOOKBACK_DAYS`` (~2-3 completed seasons of EPL-style calendars)
    and take the latest row per ``game_id`` as the final scoreline.
  * Compute the simple per-league mean of ``home_score`` and
    ``away_score`` — that is the maximum-likelihood estimate of the
    full-match Poisson intensity under the same independent-Poisson
    assumption the predictor uses to build its goal grid.  No team
    strength terms: this is the LEAGUE baseline, identical for every
    fixture, and the Dixon-Coles tau correction (fitted separately in
    :mod:`app.learning.dc_rho_fitter`) carries the only correlation
    structure the live predictor needs.
  * Clip the resulting (home, away) pair into a documented safe band
    so a degenerate sample (e.g. one outlier scoreline pushing the
    mean above 4 goals per side) can never produce a lambda the goal
    grid wasn't validated on.

Guard rails
-----------

  * Leagues with fewer than ``MIN_MATCHES_FOR_REFRESH`` terminal rows
    are skipped — the persisted value is left at whatever the previous
    cycle wrote (or the hand-set default if nothing has been persisted
    yet).  This matches the dc_rho_fitter's behaviour: we never
    silently revert a real estimate to the hand-set default just
    because the rolling window briefly dipped under the bar.
  * The full vector is persisted as a single JSON blob under
    ``BASELINE_LAMBDAS_KEY`` with a ``refreshed_at`` timestamp so
    Section 9 of the daily honesty report can show when the
    baselines were last recomputed.  The predictor reads it via
    :func:`soccer_live.refresh_baseline_lambda_overrides` (also
    called from the cycle below so a freshly-refreshed vector takes
    effect immediately without waiting for the next refresh tick).
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .. import db
from . import soccer_live


log = logging.getLogger(__name__)


# ``runtime_settings`` key under which the refreshed per-league
# baseline vector is persisted.  Read by
# :func:`soccer_live.refresh_baseline_lambda_overrides`.
BASELINE_LAMBDAS_KEY = "learning.baseline_lambdas_by_league"

# Refresh cadence.  Baselines move on seasonal timescales so a daily
# cadence is plenty; the actual work is one aggregation query per
# league.
REFRESH_INTERVAL_SECONDS = 24 * 3600.0

# Rolling history window — ~2-3 completed seasons (an EPL season runs
# Aug to May, so 800 days covers two full seasons plus the current
# in-progress one).  Sized so each league has at least a few hundred
# matches once the table has been ingesting for that long.
LOOKBACK_DAYS = 800

# Minimum sample size per league before we trust the rolling mean
# enough to override the hand-set default.  Below this the per-league
# mean is too noisy and we keep the previous estimate.
MIN_MATCHES_FOR_REFRESH = 80

# Safe band for the per-league baselines.  The hand-set values range
# from 1.05 (LIGUE1 away) to 1.70 (BUNDES home); this band is wide
# enough to track real seasonal drift but tight enough that a
# degenerate sample can't push the goal grid into the regime where
# the truncation cap or the Dixon-Coles tau correction stop being
# valid.
LAMBDA_CLIP_MIN = 0.6
LAMBDA_CLIP_MAX = 3.0

# Terminal statuses that indicate the row carries a final scoreline.
# Matches dc_rho_fitter and the producer-side vocabulary in
# ``app.feeds.sportradar._extract_soccer_live_rows``.
_TERMINAL_STATUSES = ("closed", "ended", "complete", "finished")

# Monotonic timestamp of the last successful refresh cycle.  Mirrors
# the pattern used by :mod:`app.learning.dc_rho_fitter` so the
# settlement loop can poll cheaply on every tick and we only do real
# work once per ``REFRESH_INTERVAL_SECONDS``.
_last_refresh_at: float | None = None


@dataclass
class LeagueRefreshResult:
    league: str
    matches: int
    lambda_home_raw: float | None
    lambda_away_raw: float | None
    lambda_home_persisted: float | None
    lambda_away_persisted: float | None
    skipped_reason: str | None = None


@dataclass
class BaselineRefreshCycleResult:
    started_at: str
    completed_at: str | None = None
    duration_seconds: float = 0.0
    leagues_examined: int = 0
    leagues_refreshed: int = 0
    leagues_skipped: int = 0
    per_league: list[LeagueRefreshResult] = field(default_factory=list)
    persisted: bool = False
    refreshed_at: str | None = None
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Data fetch
# ---------------------------------------------------------------------------


async def _fetch_scorelines(league: str) -> list[tuple[int, int]]:
    """Pull final (home, away) scorelines for ``league`` from the
    live-state table, restricted to the rolling lookback window.

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
               AND fetched_at > (now() - ($3::int || ' days')::interval)
             ORDER BY game_id, fetched_at DESC
            """,
            league, list(_TERMINAL_STATUSES), int(LOOKBACK_DAYS),
        )
    except Exception as e:
        log.warning("[soccer_baseline_fitter] scoreline fetch failed for %s: %s", league, e)
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


def _clip_lambda(value: float) -> float:
    if value < LAMBDA_CLIP_MIN:
        return LAMBDA_CLIP_MIN
    if value > LAMBDA_CLIP_MAX:
        return LAMBDA_CLIP_MAX
    return value


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


async def _persist_overrides(
    overrides: dict[str, tuple[float, float]], refreshed_at: str,
) -> bool:
    """Upsert the refreshed vector into ``runtime_settings``.

    Returns True on success.  Failures are logged and swallowed so a
    transient DB hiccup doesn't take down the settlement scheduler.
    """
    payload = {
        "refreshed_at": refreshed_at,
        "lambdas": {k: [round(v[0], 4), round(v[1], 4)] for k, v in overrides.items()},
    }
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
                BASELINE_LAMBDAS_KEY,
                json.dumps(payload, sort_keys=True),
                "soccer_baseline_fitter",
            )
        return True
    except Exception as e:
        log.warning("[soccer_baseline_fitter] persist failed: %s", e)
        return False


def _load_existing_overrides_from_json(
    raw: str | None,
) -> tuple[dict[str, tuple[float, float]], str | None]:
    """Parse a persisted JSON blob into ``(overrides, refreshed_at)``.

    Tolerates both the new ``{"refreshed_at": ..., "lambdas": {...}}``
    shape and a bare ``{LEAGUE: [home, away]}`` shape (for forward
    compatibility / manual edits)."""
    if not raw:
        return {}, None
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}, None
    if not isinstance(parsed, dict):
        return {}, None
    refreshed_at: str | None = None
    lambdas_obj: Any
    if "lambdas" in parsed and isinstance(parsed.get("lambdas"), dict):
        lambdas_obj = parsed["lambdas"]
        ra = parsed.get("refreshed_at")
        if isinstance(ra, str):
            refreshed_at = ra
    else:
        lambdas_obj = parsed
    out: dict[str, tuple[float, float]] = {}
    for k, v in lambdas_obj.items():
        if not k:
            continue
        try:
            if isinstance(v, dict):
                lam_h = float(v.get("home"))
                lam_a = float(v.get("away"))
            else:
                seq = list(v)
                if len(seq) < 2:
                    continue
                lam_h = float(seq[0])
                lam_a = float(seq[1])
        except (TypeError, ValueError):
            continue
        if lam_h <= 0.0 or lam_a <= 0.0:
            continue
        out[str(k).strip().upper()] = (lam_h, lam_a)
    return out, refreshed_at


# ---------------------------------------------------------------------------
# Cycle entry point
# ---------------------------------------------------------------------------


async def run_cycle() -> BaselineRefreshCycleResult:
    """One pass: aggregate scorelines per soccer league, persist the
    vector, and refresh the predictor's in-memory override cache.

    Best-effort throughout — per-league failures are captured per-row
    and never abort the cycle.
    """
    started = time.perf_counter()
    started_at = datetime.now(timezone.utc).isoformat()
    cr = BaselineRefreshCycleResult(started_at=started_at)

    # Start from whatever is already persisted so a league we skip on
    # this cycle (insufficient samples) keeps its previously-refreshed
    # value instead of reverting silently to the hand-set default.
    existing_overrides: dict[str, tuple[float, float]] = {}
    try:
        raw = await db.fetchval(
            "SELECT value FROM runtime_settings WHERE key = $1",
            BASELINE_LAMBDAS_KEY,
        )
        existing_overrides, _existing_at = _load_existing_overrides_from_json(raw)
    except Exception as e:
        log.debug("[soccer_baseline_fitter] existing override read failed: %s", e)

    next_overrides: dict[str, tuple[float, float]] = dict(existing_overrides)
    any_refresh = False

    leagues = sorted(soccer_live.SOCCER_LEAGUES)
    cr.leagues_examined = len(leagues)
    for league in leagues:
        try:
            matches = await _fetch_scorelines(league)
        except Exception as e:
            cr.errors.append(f"{league}: fetch {type(e).__name__}: {e}")
            cr.leagues_skipped += 1
            prev = next_overrides.get(league)
            cr.per_league.append(
                LeagueRefreshResult(
                    league=league, matches=0,
                    lambda_home_raw=None, lambda_away_raw=None,
                    lambda_home_persisted=prev[0] if prev else None,
                    lambda_away_persisted=prev[1] if prev else None,
                    skipped_reason=f"fetch_failed:{type(e).__name__}",
                ),
            )
            continue
        if len(matches) < MIN_MATCHES_FOR_REFRESH:
            cr.leagues_skipped += 1
            prev = next_overrides.get(league)
            cr.per_league.append(
                LeagueRefreshResult(
                    league=league, matches=len(matches),
                    lambda_home_raw=None, lambda_away_raw=None,
                    lambda_home_persisted=prev[0] if prev else None,
                    lambda_away_persisted=prev[1] if prev else None,
                    skipped_reason="insufficient_samples",
                ),
            )
            continue
        n = len(matches)
        raw_h = sum(h for h, _ in matches) / n
        raw_a = sum(a for _, a in matches) / n
        clipped_h = _clip_lambda(raw_h)
        clipped_a = _clip_lambda(raw_a)
        next_overrides[league] = (clipped_h, clipped_a)
        any_refresh = True
        cr.leagues_refreshed += 1
        cr.per_league.append(
            LeagueRefreshResult(
                league=league, matches=n,
                lambda_home_raw=raw_h, lambda_away_raw=raw_a,
                lambda_home_persisted=clipped_h,
                lambda_away_persisted=clipped_a,
            ),
        )

    refreshed_at = datetime.now(timezone.utc).isoformat()
    # Only write & refresh if anything actually changed (saves an
    # audit-noisy write on cycles where every league was skipped).
    if next_overrides != existing_overrides:
        cr.persisted = await _persist_overrides(next_overrides, refreshed_at)
        if cr.persisted:
            soccer_live.set_baseline_lambda_overrides(
                next_overrides, refreshed_at=refreshed_at,
            )
            cr.refreshed_at = refreshed_at
    else:
        # Keep the in-memory cache warm even on no-op cycles so a
        # fresh process picks up the persisted vector promptly.
        # Use the stored refresh timestamp (or "now" if none) so the
        # daily report still shows something sensible.
        soccer_live.set_baseline_lambda_overrides(
            next_overrides,
            refreshed_at=soccer_live.get_baseline_lambdas_refreshed_at() or refreshed_at,
        )

    cr.duration_seconds = round(time.perf_counter() - started, 3)
    cr.completed_at = datetime.now(timezone.utc).isoformat()
    log.info(
        "[soccer_baseline_fitter] cycle done examined=%d refreshed=%d skipped=%d "
        "persisted=%s any_refresh=%s duration=%.2fs",
        cr.leagues_examined, cr.leagues_refreshed, cr.leagues_skipped,
        cr.persisted, any_refresh, cr.duration_seconds,
    )
    return cr


async def maybe_refit() -> BaselineRefreshCycleResult | None:
    """Run :func:`run_cycle` if the last successful refresh is older
    than :data:`REFRESH_INTERVAL_SECONDS`.  Returns ``None`` on a
    no-op so the caller can record "nothing happened" without
    ambiguity."""
    global _last_refresh_at
    if (
        _last_refresh_at is not None
        and (time.time() - _last_refresh_at) < REFRESH_INTERVAL_SECONDS
    ):
        return None
    cr = await run_cycle()
    _last_refresh_at = time.time()
    return cr


def _reset_state_for_tests() -> None:
    """Test hook — clear the cadence gate so a unit test can drive
    :func:`maybe_refit` repeatedly without waiting 24 hours."""
    global _last_refresh_at
    _last_refresh_at = None
