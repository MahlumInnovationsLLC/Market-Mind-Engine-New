"""Per-league probability calibrators.

Raw stack outputs are well-discriminated but uncalibrated — the
swarm/edge gates are only meaningful when the model probability lines
up with the true win frequency.  This module fits and applies a small
calibrator per supported league (NBA, NHL, MLB, NCAAB, NFL) on top of
whatever the active meta-model returns.

Two calibrator families are supported:

  * **Isotonic** (primary) — non-parametric monotone fit.  Used when
    we have at least ``ISOTONIC_MIN_SAMPLES`` settled trades for the
    league in the rolling 90-day window.
  * **Platt** (fallback) — single-feature logistic regression
    (``sigmoid(a * p + b)``).  Used for low-data leagues so we still
    get a real calibration curve from a handful of outcomes.

Persistence reuses the existing ``calibration_snapshots`` table with a
distinct ``sub_category`` namespace so the per-league artifacts never
collide with the per-vertical retrain rows the online-learning loop
already writes:

  ``vertical = 'sports'`` and ``sub_category = 'per_league:<LEAGUE>'``

The ``calibrator_state`` JSONB column carries the serialized fit:

  Isotonic: ``{"type":"isotonic","x":[...],"y":[...],"n":N,"fit_at":ISO}``
  Platt:    ``{"type":"platt","a":A,"b":B,"n":N,"fit_at":ISO}``

The loader pulls the most recent row per league at startup; the
nightly refit job pulls the last 90 days of settled trades + outcomes,
fits per league, and writes back.  ``apply()`` silently bypasses any
league with no fitted artifact so a cold install (or a new league)
trades against the raw model probability rather than crashing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import db


log = logging.getLogger(__name__)


# Leagues we maintain dedicated calibrators for.  Casing matches what
# the orchestrator carries on candidates (``cand['league']``) and what
# the trades / postmortems tables store.
SUPPORTED_LEAGUES: tuple[str, ...] = (
    "NBA", "NHL", "MLB", "NCAAB", "NFL",
    # Soccer (task #196).  3-way markets — `apply()` is shape-agnostic
    # because we calibrate the YES probability post-meta-model, but the
    # settlement pipeline branches on `LeagueConfig.three_way_market`
    # so a soccer draw outcome is scored as a YES miss on both
    # home-side and away-side legs rather than treated as a tie.
    "EPL", "LALIGA", "SERIEA", "BUNDES", "LIGUE1", "MLS", "UCL",
    # Combat & individual sports (task #197).  N-way outright fields —
    # the calibrator still operates on a per-leg YES probability so the
    # apply() shape doesn't change, but each leg of a UFC card / F1
    # race / ATP/WTA bracket / PGA tournament is calibrated against
    # its own league's settled-trade history.
    "UFC", "ATP", "WTA", "F1", "PGA",
    # Commodities (task #198).  Single league bucket — per-contract
    # calibration scoped via the calibrator's `(league, market_type)`
    # key already covers oil/gold/grains/etc separately when the
    # settlement pipeline records `market_type='commodity:WTI'` etc.
    "COMMODITIES",
    # Tech / AI (task #199).  Bayesian evidence model rather than ML
    # stack — calibrator still applies post-model so settled tech-AI
    # trades feed an isotonic / Platt curve scoped to TECH_AI just
    # like every other vertical.
    "TECH_AI",
    # Elections (task #201).  Poll-of-polls + state-bias-correction
    # model.  Calibrator still applies post-model so settled elections
    # trades feed an isotonic / Platt curve scoped to ELECTIONS just
    # like every other vertical.
    "ELECTIONS",
    # Events / macro economics (task #202).  Consensus-prior model with
    # rolling forecast-error variance.  Calibrator still applies
    # post-model so settled events-econ trades feed an isotonic / Platt
    # curve scoped to EVENTS_ECON.
    "EVENTS_ECON",
    # Entertainment (task #203).  Hybrid log-normal / Bayesian /
    # empirical model.  Calibrator still applies post-model so settled
    # entertainment trades feed an isotonic / Platt curve scoped to
    # ENTERTAINMENT just like every other vertical.
    "ENTERTAINMENT",
    # Crypto-binary (task #754).  V1 archive carries ~595k labelled
    # crypto-binary outcomes (BTC/ETH price-target contracts written
    # with ``vertical='crypto'`` and the V1 ``league='unknown'``
    # sentinel).  The forward predictor (``app/crypto/predictor.py``)
    # already calls ``apply(p, league='CRYPTO')`` so simply registering
    # the league here is enough to unblock both the bootstrap fit on
    # the V1 subset and the ongoing 6-h refit triggered by
    # ``app/crypto/backtest_scheduler.py``.  Single-league bucket for
    # v1; per-coin (BTC vs ETH vs SOL) calibration is intentionally
    # out-of-scope until signal warrants it.
    "CRYPTO",
    # Weather (task #767).  WEATHER markets ship through the GRU+EMOS
    # temperature stack and the precipitation predictor, both of which
    # already route their post-stack probability through `apply()` —
    # registering the league here is what stops `apply()` from
    # silently bypassing with `reason="unsupported league"`, which had
    # been shipping uncalibrated weather trades into Kelly sizing
    # (textbook EV leak).  The settlement retrain loop's per-league
    # snapshot scaffold picks WEATHER up automatically once it appears
    # in this tuple.  The new `gate_uncalibrated_vertical` risk gate
    # blocks weather candidates whose `(WEATHER)` snapshot is absent
    # until the calibrator has been fit; operators may opt into "ship
    # raw" via `risk.uncalibrated_raw_mode_weather` (audited + surfaced
    # as a red row in the daily honesty report).
    "WEATHER",
    # Player-prop verticals (task #829) — each league rolls up its own
    # per-stat models into a single calibrator bucket for v1.  Once
    # ≥4 weeks of settled props accrue per (league, stat), the
    # follow-up task carves the bucket into per-stat sub-keys
    # (NBA_PROPS_PTS, NHL_PROPS_SOG, …) so each stat gets its own
    # isotonic / Platt fit.  Ships uncalibrated — the new
    # ``gate_uncalibrated_vertical`` blocks every candidate routed
    # against these buckets until an operator opts into raw mode via
    # ``risk.uncalibrated_raw_mode_<vertical>``.
    "NBA_PROPS",
    "NHL_PROPS",
    "MLB_PROPS",
    "NFL_PROPS",
)

# Below this sample count we fall back to Platt; below MIN_REFIT we
# don't fit anything at all (nothing useful to learn from).
ISOTONIC_MIN_SAMPLES = 50
MIN_REFIT_SAMPLES = 10

# Rolling window for the refit job.
REFIT_WINDOW_DAYS = 90

# How long a fitted artifact is considered "fresh" before the health
# probe flips to degraded.  Slightly longer than the nightly cadence
# so a single missed refit doesn't trip the alert.
FRESH_TTL_SECONDS = 36 * 3600

# Nightly refit cadence — the settlement scheduler polls this and
# triggers a refit when the last one is older than the interval.
REFIT_INTERVAL_SECONDS = 24 * 3600


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


# Champion-challenger A/B framework (task #755).
#
# `role` is the column / dict key that distinguishes the production
# calibrator from the experimental one.  Predictions ALWAYS feed off
# the champion; the challenger is evaluated in parallel on the same
# windowed dataset and persisted alongside so the auto-promotion job
# can compare them on out-of-sample Brier.  When a challenger wins
# OOS Brier two consecutive snapshots in a row the prior champion is
# rewritten to `retired_champion` and the latest challenger row is
# promoted to `champion` in a single transaction.
ROLE_CHAMPION = "champion"
ROLE_CHALLENGER = "challenger"
ROLE_RETIRED_CHAMPION = "retired_champion"
_ACTIVE_ROLES: tuple[str, ...] = (ROLE_CHAMPION, ROLE_CHALLENGER)

# Fraction of the windowed training set held out as a temporal OOS
# slice.  10% is small enough that a low-data league still has a
# meaningful train half (MIN_REFIT_SAMPLES * 0.9) yet large enough
# that the OOS Brier moves outside noise on a single refit.
OOS_HOLDOUT_FRACTION = 0.10
OOS_HOLDOUT_MIN = 5

# Required margin of OOS-Brier improvement before a challenger snapshot
# counts as a "win".  A naked `<` would let floating-point noise on
# identical fits flip the role; a tiny epsilon keeps ties from
# promoting (matches the spec's "tied challenger does not promote").
PROMOTION_MIN_DELTA = 1e-9


@dataclass
class _Calibrator:
    league: str
    type: str  # 'isotonic' | 'platt'
    state: dict[str, Any]
    fit_at: float  # epoch seconds
    sample_count: int
    role: str = ROLE_CHAMPION


@dataclass
class CalibratorsState:
    loaded_at: float | None = None
    last_refit_at: float | None = None
    last_refit_summary: dict[str, Any] = field(default_factory=dict)
    # Two-level dict keyed by [league][role].  Indexed for the predictor
    # which always reads ROLE_CHAMPION; the challenger leg is read by
    # `apply_both()` and the dashboard A/B tile.  Replaces the
    # pre-#755 single-calibrator-per-league dict; freshness() and
    # apply() preserve their pre-#755 contract by reading role=champion.
    calibrators: dict[str, dict[str, _Calibrator]] = field(default_factory=dict)

    def champion(self, league: str) -> _Calibrator | None:
        return (self.calibrators.get(league) or {}).get(ROLE_CHAMPION)

    def challenger(self, league: str) -> _Calibrator | None:
        return (self.calibrators.get(league) or {}).get(ROLE_CHALLENGER)

    def freshness(self) -> dict[str, Any]:
        now = time.time()
        per_league: dict[str, Any] = {}
        for lg in SUPPORTED_LEAGUES:
            c = self.champion(lg)
            ch = self.challenger(lg)
            if c is None:
                per_league[lg] = {
                    "loaded": False, "type": None,
                    "fit_at": None, "age_seconds": None,
                    "stale": True, "sample_count": 0,
                    "challenger_loaded": ch is not None,
                }
                continue
            age = now - c.fit_at
            per_league[lg] = {
                "loaded": True, "type": c.type,
                "fit_at": c.fit_at, "age_seconds": round(age, 1),
                "stale": age > FRESH_TTL_SECONDS,
                "sample_count": c.sample_count,
                "challenger_loaded": ch is not None,
            }
        loaded_count = sum(1 for v in per_league.values() if v["loaded"])
        stale_count = sum(1 for v in per_league.values() if v["loaded"] and v["stale"])
        missing_count = len(SUPPORTED_LEAGUES) - loaded_count
        return {
            "supported": list(SUPPORTED_LEAGUES),
            "loaded_count": loaded_count,
            "missing_count": missing_count,
            "stale_count": stale_count,
            "per_league": per_league,
            "last_refit_at": self.last_refit_at,
            "last_refit_summary": self.last_refit_summary,
        }


_state = CalibratorsState()
_lock = asyncio.Lock()


def get_state() -> CalibratorsState:
    return _state


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def _apply_isotonic(p: float, st: dict[str, Any]) -> float:
    """Linear interpolation across the isotonic knots."""
    xs: list[float] = st.get("x") or []
    ys: list[float] = st.get("y") or []
    if not xs or not ys or len(xs) != len(ys):
        return p
    if p <= xs[0]:
        return float(ys[0])
    if p >= xs[-1]:
        return float(ys[-1])
    i = bisect_left(xs, p)
    # bisect_left returns insertion point; guard the edge.
    if i == 0:
        return float(ys[0])
    if i >= len(xs):
        return float(ys[-1])
    x0, x1 = xs[i - 1], xs[i]
    y0, y1 = ys[i - 1], ys[i]
    if x1 == x0:
        return float(y1)
    t = (p - x0) / (x1 - x0)
    return float(y0 + t * (y1 - y0))


def _apply_platt(p: float, st: dict[str, Any]) -> float:
    a = float(st.get("a", 0.0))
    b = float(st.get("b", 0.0))
    z = a * p + b
    # Numerically-stable sigmoid.
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    ez = math.exp(z)
    return ez / (1.0 + ez)


def _apply_with_calibrator(p: float, cal: _Calibrator) -> tuple[float, str | None]:
    """Run one calibrator on a probability; returns ``(p_out, error_reason)``."""
    try:
        if cal.type == "isotonic":
            cp = _apply_isotonic(p, cal.state)
        elif cal.type == "platt":
            cp = _apply_platt(p, cal.state)
        else:
            return p, f"unknown calibrator type {cal.type!r}"
    except Exception as e:  # pragma: no cover — defensive
        log.warning("[calibrators] apply failed for %s/%s: %s", cal.league, cal.role, e)
        return p, f"apply error: {type(e).__name__}"
    return max(0.0, min(1.0, float(cp))), None


def apply(probability: float, league: str | None) -> tuple[float, dict[str, Any]]:
    """Return ``(calibrated_probability, info)``.

    Bypasses cleanly when ``league`` is unknown / not supported / has
    no fitted artifact: the caller still gets the raw probability and
    an ``info`` dict explaining the bypass so the caller can log it.

    Champion-only — the challenger never feeds trade decisions per
    task #755; predictors that want to surface the A/B leg should call
    :func:`apply_both` instead.
    """
    info: dict[str, Any] = {"applied": False, "league": league}
    if not isinstance(probability, (int, float)):
        info["reason"] = "non-numeric input"
        return float(probability or 0.0), info
    p = max(0.0, min(1.0, float(probability)))
    if not league:
        info["reason"] = "no league supplied"
        return p, info
    norm = str(league).upper().strip()
    info["league"] = norm
    if norm not in SUPPORTED_LEAGUES:
        info["reason"] = "unsupported league"
        return p, info
    cal = _state.champion(norm)
    if cal is None:
        info["reason"] = "no fitted calibrator"
        return p, info
    cp, err = _apply_with_calibrator(p, cal)
    if err is not None:
        info["reason"] = err
        return p, info
    info.update({
        "applied": True,
        "type": cal.type,
        "raw_probability": p,
        "fit_at": cal.fit_at,
        "sample_count": cal.sample_count,
        "role": cal.role,
    })
    return cp, info


def apply_both(
    probability: float, league: str | None
) -> tuple[float, dict[str, Any]]:
    """Apply champion + challenger calibrators in parallel.

    Returns ``(p_champion_or_raw, info)`` where ``info`` carries the
    full A/B context the predictor stitches into ``prediction.raw``:

      * ``p_champion`` — calibrated probability under the champion
        (or the raw probability when no champion is loaded — same
        bypass semantics as :func:`apply`).
      * ``p_challenger`` — calibrated probability under the
        challenger when one is loaded; ``None`` otherwise.
      * ``applied`` / ``reason`` / ``type`` / ``fit_at`` /
        ``sample_count`` mirror :func:`apply` so call-sites that
        currently log ``cal_info`` keep the same shape.

    The downstream trade decision continues to feed off the champion
    (the first element of the return tuple) — the challenger is
    persisted to the prediction record purely for the dashboard A/B
    tile and the auto-promotion job's audit trail.
    """
    p_out, info = apply(probability, league)
    # ``p_champion`` always reflects the value the predictor actually used
    # for trading (champion-calibrated when a champion is loaded; raw input
    # on bypass).  This matches the function's contract and lets dashboards
    # render the champion column even on cold-start leagues.
    info["p_champion"] = p_out
    norm = info.get("league")
    challenger = (
        _state.challenger(norm)
        if isinstance(norm, str) and norm in SUPPORTED_LEAGUES
        else None
    )
    if challenger is None:
        info["p_challenger"] = None
        info["challenger_applied"] = False
        return p_out, info
    p_in = max(0.0, min(1.0, float(probability))) if isinstance(
        probability, (int, float)
    ) else 0.0
    p_ch, err = _apply_with_calibrator(p_in, challenger)
    if err is not None:
        info["p_challenger"] = None
        info["challenger_applied"] = False
        info["challenger_reason"] = err
        return p_out, info
    info["p_challenger"] = float(p_ch)
    info["challenger_applied"] = True
    info["challenger_type"] = challenger.type
    info["challenger_fit_at"] = challenger.fit_at
    info["challenger_sample_count"] = challenger.sample_count
    return p_out, info


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _parse_state_blob(blob: Any) -> dict[str, Any] | None:
    if blob is None:
        return None
    if isinstance(blob, dict):
        return blob
    if isinstance(blob, (str, bytes, bytearray)):
        try:
            return json.loads(blob)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
    return None


async def load_all() -> CalibratorsState:
    """Pull the most recent per-league calibrator from the DB into RAM.

    Loads BOTH roles per league (champion + challenger) when the
    snapshot history carries them — the predictor's :func:`apply`
    only ever reads the champion leg, but :func:`apply_both` and the
    dashboard A/B tile both depend on having the challenger hydrated
    in the same dict.  ``DISTINCT ON (sub_category, role)`` returns
    the most recent row of each role per league; rows whose role is
    ``retired_champion`` are silently skipped (they're history kept
    for auditing the auto-promotion swap, not for live evaluation).

    Idempotent: callers can re-invoke after a refit to pick up the new
    artifacts.  Failures are swallowed and logged — a flaky DB at boot
    must NOT block the API server from serving requests; ``apply()``
    will simply bypass calibration until a successful load lands.

    Backwards-compatible with pre-#755 schemas that have no ``role``
    column — the SELECT defaults missing values to ``'champion'`` via
    ``COALESCE(role, 'champion')`` so a fresh dev DB whose migration
    0044 hasn't run yet still hydrates exactly like before.
    """
    async with _lock:
        try:
            rows = await db.fetch(
                """
                SELECT DISTINCT ON (sub_category, role)
                       sub_category,
                       role,
                       calibrator_state,
                       sample_count,
                       extract(epoch FROM created_at)::float AS fit_at
                  FROM (
                    SELECT sub_category,
                           COALESCE(role, 'champion')           AS role,
                           calibrator_state,
                           sample_count,
                           created_at,
                           drift_status
                      FROM calibration_snapshots
                     WHERE vertical = 'sports'
                       AND sub_category LIKE 'per_league:%'
                       AND calibrator_state IS NOT NULL
                       AND calibrator_state::text <> '{}'
                       AND COALESCE(drift_status->>'status', '') <> 'rolled_back'
                  ) src
                 WHERE role IN ('champion', 'challenger')
                 ORDER BY sub_category, role, created_at DESC
                """
            )
        except Exception as e:
            log.warning("[calibrators] load skipped: %s", e)
            _state.loaded_at = time.time()
            return _state

        loaded: dict[str, dict[str, _Calibrator]] = {}
        for r in rows:
            sub = str(r["sub_category"] or "")
            if not sub.startswith("per_league:"):
                continue
            league = sub.split(":", 1)[1].strip().upper()
            if league not in SUPPORTED_LEAGUES:
                continue
            try:
                role = str(r["role"] or ROLE_CHAMPION).strip().lower()
            except (KeyError, TypeError):
                role = ROLE_CHAMPION
            if role not in _ACTIVE_ROLES:
                continue
            st = _parse_state_blob(r["calibrator_state"])
            if not st:
                continue
            kind = str(st.get("type") or "").lower()
            if kind not in ("isotonic", "platt"):
                continue
            loaded.setdefault(league, {})[role] = _Calibrator(
                league=league,
                type=kind,
                state=st,
                fit_at=float(r["fit_at"] or 0.0),
                sample_count=int(r["sample_count"] or 0),
                role=role,
            )
        _state.calibrators = loaded
        _state.loaded_at = time.time()
        ch_count = sum(1 for v in loaded.values() if ROLE_CHAMPION in v)
        challenger_count = sum(1 for v in loaded.values() if ROLE_CHALLENGER in v)
        log.info(
            "[calibrators] loaded champion=%d/%d challenger=%d leagues: %s",
            ch_count, len(SUPPORTED_LEAGUES), challenger_count,
            ",".join(sorted(loaded.keys())) or "(none)",
        )
        return _state


def invalidate() -> None:
    """Drop the in-memory calibrators (used by tests + /reload paths)."""
    _state.calibrators = {}
    _state.loaded_at = None
    _state.last_refit_at = None
    _state.last_refit_summary = {}


# ---------------------------------------------------------------------------
# Fit
# ---------------------------------------------------------------------------


def _fit_isotonic(probs: list[float], outcomes: list[int]) -> dict[str, Any]:
    """Fit a sklearn isotonic regressor and return its serializable knots."""
    from sklearn.isotonic import IsotonicRegression  # local: optional dep

    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(probs, outcomes)
    xs = [float(v) for v in getattr(iso, "X_thresholds_", [])]
    ys = [float(v) for v in getattr(iso, "y_thresholds_", [])]
    return {
        "type": "isotonic",
        "x": xs,
        "y": ys,
        "n": len(probs),
        "fit_at": datetime.now(timezone.utc).isoformat(),
    }


def _fit_platt(probs: list[float], outcomes: list[int]) -> dict[str, Any]:
    """Fit Platt scaling: P(y=1) = sigmoid(a * p + b).

    Uses sklearn's LogisticRegression with a single feature so we get
    a numerically robust MLE without re-implementing IRLS by hand.
    """
    from sklearn.linear_model import LogisticRegression  # local: optional dep

    X = [[float(p)] for p in probs]
    y = [int(v) for v in outcomes]
    # Edge case: all outcomes identical — LogisticRegression refuses to
    # fit.  Fall back to a degenerate calibrator that returns the mean
    # outcome regardless of input, which is the right answer here.
    if len(set(y)) < 2:
        mean = sum(y) / len(y) if y else 0.5
        # Encode as Platt with a=0, b=logit(mean) so apply() still works.
        m = max(1e-6, min(1.0 - 1e-6, mean))
        b = math.log(m / (1.0 - m))
        return {
            "type": "platt", "a": 0.0, "b": float(b),
            "n": len(probs), "fit_at": datetime.now(timezone.utc).isoformat(),
            "degenerate": True,
        }
    lr = LogisticRegression(C=1.0, solver="lbfgs", max_iter=200)
    lr.fit(X, y)
    a = float(lr.coef_[0][0])
    b = float(lr.intercept_[0])
    return {
        "type": "platt", "a": a, "b": b,
        "n": len(probs),
        "fit_at": datetime.now(timezone.utc).isoformat(),
    }


def _brier(probs: list[float], outcomes: list[int]) -> float:
    if not probs:
        return 0.0
    return sum((p - y) * (p - y) for p, y in zip(probs, outcomes)) / len(probs)


def _apply_one(state: dict[str, Any], p: float) -> float:
    if state.get("type") == "isotonic":
        return _apply_isotonic(p, state)
    return _apply_platt(p, state)


@dataclass
class LeagueRefitResult:
    league: str
    sample_count: int = 0
    type: str | None = None
    brier_before: float | None = None
    brier_after: float | None = None
    snapshot_id: int | None = None
    skipped_reason: str | None = None
    error: str | None = None
    # Champion-challenger A/B (task #755).  ``snapshot_id`` is the
    # newly-written CHAMPION row's id (the canonical "primary" snap
    # for the cycle so existing dashboards keep working); the
    # challenger row id is exposed separately so the auto-promotion
    # job's audit trail can be reconstructed.
    challenger_snapshot_id: int | None = None
    in_sample_size: int = 0
    out_of_sample_size: int = 0
    champion_brier_in_sample: float | None = None
    champion_brier_out_of_sample: float | None = None
    challenger_brier_in_sample: float | None = None
    challenger_brier_out_of_sample: float | None = None
    promoted: bool = False
    promotion_reason: str | None = None


async def _fetch_training_data() -> dict[str, dict[str, list]]:
    """Pull the last 90 days of settled trades+outcomes grouped by league.

    Returns ``{LEAGUE: {"probs":[...], "ys":[...], "sources":[...]}}``.
    Trades are aligned to the side they were placed on so the
    calibrator learns P(side wins), matching how ``apply()`` is
    invoked downstream.  ``sources`` is a parallel list of source
    tags (``'live'`` / ``'v1'`` / ``'shadow'``) which the persistence
    helper uses to thread a per-source breakdown into the
    ``calibration_snapshots.config`` JSONB so an operator can audit
    which fraction of a refit's training came from each source.

    Three sources are UNION'd:

    * **Live** — current ``trades`` x ``trade_postmortems`` join.  YES-
      aligned ``actual_outcome`` is flipped to side-aligned
      ``y_side`` per the side label.
    * **V1 archive** — ``v1_archive.trade_analyses``, the historical
      data set imported by ``scripts/import_v1_history.py``.  V1's
      ``actual_outcome`` is **already side-aligned** (1 = bet won), so
      no flip on ``y``; the **prob** is YES-aligned in V1's stack
      output, so it's flipped per side identically to the live path.
      Only sides whose semantics are unambiguous in V1 are kept
      (``yes/buy/live_yes/buy yes/no/sell/live_no/buy no``); rows for
      ``pass``, sport-specific bet types, and crypto buy/sell over
      ``league='Unknown'`` are filtered out.
    * **Shadow** — ``shadow_learning_buffer`` (task #753).  Snapshots
      of the model's *calibrated* YES probability for every active
      Kalshi market the orchestrator considered, irrespective of
      whether we traded it.  The shadow predictor never picks a
      side, so ``predicted_prob`` and ``actual_outcome`` are both
      YES-aligned by construction — no flip needed.  This is the
      central trick of the shadow loop: the calibrator trains on
      the unbiased universe of Kalshi markets, not just the
      sub-population our edge gates surfaced for trading.

    Either union can be disabled at runtime via
    ``AXIOM_USE_V1_ARCHIVE_FOR_CALIBRATION=0`` /
    ``AXIOM_USE_SHADOW_BUFFER_FOR_CALIBRATION=0`` for kill-switch
    safety.  A separate preflight ``SELECT to_regclass(...)`` runs
    before SQL composition and gates the corresponding CTE OUT
    entirely on environments that haven't run the relevant
    migration — a WHERE-clause guard would NOT suffice because
    Postgres resolves FROM-clause relations at parse time, before
    any predicate evaluates, and a missing table would raise
    ``UndefinedTable`` and silently drop live rows too.
    """
    import os

    use_v1 = (
        os.getenv("AXIOM_USE_V1_ARCHIVE_FOR_CALIBRATION", "1").lower()
        not in ("0", "false", "no", "off")
    )
    use_shadow = (
        os.getenv("AXIOM_USE_SHADOW_BUFFER_FOR_CALIBRATION", "1").lower()
        not in ("0", "false", "no", "off")
    )

    # PostgreSQL resolves FROM-clause relations at PARSE time, before any
    # WHERE predicate runs.  A `to_regclass(...) IS NOT NULL` clause in
    # WHERE does NOT protect the query from `UndefinedTable` when the
    # archive table is absent (fresh dev DBs that never ran migration
    # 0037 / 0043).  So we MUST preflight separately and only compose
    # the corresponding CTE into the SQL when the table actually
    # exists; otherwise we'd raise into the broad except below and
    # silently drop live rows too.
    if use_v1:
        try:
            v1_present = await db.fetchval(
                "SELECT to_regclass('v1_archive.trade_analyses') IS NOT NULL"
            )
        except Exception as e:
            log.warning("[calibrators] v1_archive preflight failed: %s", e)
            v1_present = False
        use_v1 = bool(v1_present)

    if use_shadow:
        try:
            shadow_present = await db.fetchval(
                "SELECT to_regclass('public.shadow_learning_buffer') IS NOT NULL"
            )
        except Exception as e:
            log.warning(
                "[calibrators] shadow_learning_buffer preflight failed: %s", e
            )
            shadow_present = False
        use_shadow = bool(shadow_present)

    live_cte = f"""
        live_settled AS (
          SELECT upper(t.league)                              AS league,
                 CASE
                   WHEN lower(coalesce(t.side, '')) IN ('no','n','sell')
                     THEN 1.0 - t.model_prob::float
                   ELSE t.model_prob::float
                 END                                          AS p_side,
                 CASE
                   WHEN lower(coalesce(t.side, '')) IN ('no','n','sell')
                     THEN 1 - pm.actual_outcome::int
                   ELSE pm.actual_outcome::int
                 END                                          AS y_side,
                 'live'::text                                 AS source,
                 extract(epoch FROM t.settled_at)::float      AS ts
            FROM trades t
            JOIN trade_postmortems pm
              ON pm.trade_id = coalesce(t.trade_id, t.id::text)
           WHERE t.settled_at IS NOT NULL
             AND t.settled_at > now() - interval '{int(REFIT_WINDOW_DAYS)} days'
             AND t.model_prob IS NOT NULL
             AND pm.actual_outcome IS NOT NULL
             AND t.league IS NOT NULL
        )"""

    extra_ctes: list[str] = []
    union_parts: list[str] = [
        "SELECT league, p_side, y_side, source, ts FROM live_settled"
    ]

    if use_v1:
        # Two row populations are folded into ``v1_settled``:
        #
        # * League-aligned trades — the original path.  Requires a
        #   resolved ``league`` AND a known YES/NO side label so we
        #   can flip ``predicted_prob`` to side-aligned.
        # * Crypto-binary placeholders (task #754) — V1 wrote these
        #   with ``vertical='crypto'`` and ``league='unknown'``
        #   (no league inference rule covers them).  The contract is
        #   binary YES/NO on a price target and V1's
        #   ``predicted_prob`` is YES-aligned by construction, so
        #   no side flip is needed and the synthetic
        #   ``league='CRYPTO'`` matches the forward predictor's
        #   ``apply(p, league='CRYPTO')`` call site exactly.
        extra_ctes.append(f""",
        v1_settled AS (
          SELECT upper(league)                                AS league,
                 CASE
                   WHEN lower(coalesce(analysis_json->>'side', '')) IN
                        ('no','sell','live_no','buy no')
                     THEN greatest(0.0, least(1.0,
                            1.0 - predicted_prob::float))
                   ELSE greatest(0.0, least(1.0,
                            predicted_prob::float))
                 END                                          AS p_side,
                 actual_outcome::int                          AS y_side,
                 'v1'::text                                   AS source,
                 extract(epoch FROM created_at)::float        AS ts
            FROM v1_archive.trade_analyses
           WHERE to_regclass('v1_archive.trade_analyses') IS NOT NULL
             AND created_at IS NOT NULL
             AND created_at > now() - interval '{int(REFIT_WINDOW_DAYS)} days'
             AND predicted_prob IS NOT NULL
             AND actual_outcome IS NOT NULL
             AND league IS NOT NULL
             AND lower(coalesce(analysis_json->>'side', '')) IN
                 ('yes','buy','live_yes','buy yes',
                  'no','sell','live_no','buy no')
             -- Mutual-exclusion guard with the CRYPTO branch below.
             -- V1 crypto rows live in vertical='crypto' (typically
             -- league='unknown', no explicit side); excluding them
             -- here makes the two UNION ALL branches partitioned by
             -- vertical so no row can be counted twice or split-bucket
             -- into both its inferred league AND CRYPTO.
             AND lower(coalesce(vertical, '')) <> 'crypto'
          UNION ALL
          SELECT 'CRYPTO'::text                               AS league,
                 greatest(0.0, least(1.0,
                          predicted_prob::float))             AS p_side,
                 actual_outcome::int                          AS y_side,
                 'v1'::text                                   AS source,
                 extract(epoch FROM created_at)::float        AS ts
            FROM v1_archive.trade_analyses
           WHERE to_regclass('v1_archive.trade_analyses') IS NOT NULL
             AND created_at IS NOT NULL
             AND created_at > now() - interval '{int(REFIT_WINDOW_DAYS)} days'
             AND predicted_prob IS NOT NULL
             AND actual_outcome IS NOT NULL
             AND lower(coalesce(vertical, '')) = 'crypto'
             AND actual_outcome IN (0, 1)
        )""")
        union_parts.append(
            "SELECT league, p_side, y_side, source, ts FROM v1_settled"
        )

    if use_shadow:
        # Shadow predictor never picks a side — predicted_prob is
        # YES-aligned by construction, and actual_outcome is the YES
        # resolution.  No side flip needed.  We clamp to [0, 1]
        # defensively so a calibrator with a slightly out-of-range
        # output (rare numeric edge case) can't poison the fit.
        extra_ctes.append(f""",
        shadow_settled AS (
          SELECT upper(league)                                AS league,
                 greatest(0.0, least(1.0,
                          predicted_prob::float))             AS p_side,
                 actual_outcome::int                          AS y_side,
                 'shadow'::text                               AS source,
                 extract(epoch FROM settled_at)::float        AS ts
            FROM shadow_learning_buffer
           WHERE to_regclass('public.shadow_learning_buffer') IS NOT NULL
             AND settled_at IS NOT NULL
             AND settled_at > now() - interval '{int(REFIT_WINDOW_DAYS)} days'
             AND predicted_prob IS NOT NULL
             AND actual_outcome IS NOT NULL
             AND league IS NOT NULL
        )""")
        union_parts.append(
            "SELECT league, p_side, y_side, source, ts FROM shadow_settled"
        )

    union_tail = "\n            UNION ALL\n            ".join(union_parts)
    # Order by ts ASC so per-league buckets accumulate in chronological
    # order — the temporal OOS-holdout split in `refit_all` slices the
    # trailing N rows directly off the end of each bucket without an
    # in-Python re-sort.  Rows with NULL ts (defensive — every UNION
    # branch currently emits a non-null timestamp) are placed first so
    # they're in the in-sample slice rather than the holdout.
    sql = (
        f"WITH {live_cte}{''.join(extra_ctes)}\n            "
        f"{union_tail}\n            ORDER BY ts NULLS FIRST"
    )

    try:
        rows = await db.fetch(sql)
    except Exception as e:
        log.warning("[calibrators] training fetch failed: %s", e)
        return {}

    grouped: dict[str, dict[str, list]] = {}
    for r in rows:
        lg = str(r["league"] or "").upper()
        if lg not in SUPPORTED_LEAGUES:
            continue
        bucket = grouped.setdefault(
            lg, {"probs": [], "ys": [], "sources": [], "ts": []}
        )
        bucket["probs"].append(float(r["p_side"]))
        bucket["ys"].append(int(r["y_side"]))
        # 'source' is present in every UNION branch, but defensively
        # default to 'live' when the column is missing so a hand-rolled
        # query in tests still parses.
        try:
            src = r["source"]
        except (KeyError, TypeError):
            src = "live"
        bucket["sources"].append(str(src or "live"))
        # 'ts' powers the temporal OOS-holdout split in refit_all().
        # Defensively default to 0.0 when absent so a hand-rolled test
        # query without a ts column still parses (the in-Python sort
        # below pins NULL/zero rows to the front of the bucket which
        # is the in-sample slice — matches the SQL `NULLS FIRST`).
        try:
            ts = r["ts"]
        except (KeyError, TypeError):
            ts = None
        bucket["ts"].append(float(ts) if ts is not None else 0.0)
    return grouped


async def _persist_calibrator(
    league: str, state: dict[str, Any], n: int,
    brier_before: float | None, brier_after: float | None,
    *,
    source_breakdown: dict[str, int] | None = None,
    role: str = ROLE_CHAMPION,
    brier_in_sample: float | None = None,
    brier_out_of_sample: float | None = None,
) -> int | None:
    """Append a per-league calibrator snapshot.  Returns row id.

    ``source_breakdown`` is a ``{source: count}`` dict (e.g.
    ``{'live': 120, 'shadow': 480, 'v1': 30}``) that gets threaded
    into ``calibration_snapshots.config.source_breakdown`` so the
    operator dashboard can show "this calibrator was 80% trained on
    shadow rows" without re-running the SQL.

    ``role`` / ``brier_in_sample`` / ``brier_out_of_sample`` (task
    #755) populate the dedicated columns added by migration 0044 so
    the auto-promotion job can compare champion vs challenger on the
    held-out slice without re-walking the snapshot history.  The same
    metrics are also mirrored into ``config`` so dashboards / log
    consumers that haven't picked up the new columns still see them.
    """
    config_json = json.dumps({
        "source": "per_league_refit",
        "role": str(role),
        "brier_before": (
            float(brier_before) if brier_before is not None else None
        ),
        "brier_after": (
            float(brier_after) if brier_after is not None else None
        ),
        "brier_in_sample": (
            float(brier_in_sample) if brier_in_sample is not None else None
        ),
        "brier_out_of_sample": (
            float(brier_out_of_sample) if brier_out_of_sample is not None else None
        ),
        "source_breakdown": dict(source_breakdown or {}),
    })
    try:
        snap_id = await db.fetchval(
            """
            INSERT INTO calibration_snapshots
                (vertical, sub_category, ece, mce, brier_score, reliability,
                 resolution, sample_count, calibrator_state, drift_status, config,
                 role, brier_in_sample, brier_out_of_sample)
            VALUES ('sports', $1, $2, $3, $4, NULL, NULL, $5,
                    $6::jsonb,
                    jsonb_build_object('status', 'fitted',
                                       'kind', $7::text,
                                       'window_days', $8::int,
                                       'role', $10::text),
                    $9::jsonb,
                    $10, $11, $12)
            RETURNING id
            """,
            f"per_league:{league}",
            float(brier_after if brier_after is not None else 0.0),
            float(brier_after if brier_after is not None else 0.0),
            float(brier_after if brier_after is not None else 0.0),
            int(n),
            json.dumps(state),
            state.get("type") or "unknown",
            int(REFIT_WINDOW_DAYS),
            config_json,
            str(role),
            float(brier_in_sample) if brier_in_sample is not None else None,
            float(brier_out_of_sample) if brier_out_of_sample is not None else None,
        )
        return int(snap_id) if snap_id is not None else None
    except Exception as e:
        log.warning("[calibrators] persist failed for %s: %s", league, e)
        return None


def _temporal_split(
    probs: list[float], ys: list[int], sources: list[str], ts: list[float],
) -> tuple[
    list[float], list[int], list[str],
    list[float], list[int], list[str],
]:
    """Split a chronologically-ordered bucket into ``(in_sample, oos)``.

    The OOS slice is the trailing ``OOS_HOLDOUT_FRACTION`` of the
    bucket, clamped to at least ``OOS_HOLDOUT_MIN`` rows; the
    in-sample slice is the rest.  The input is assumed already sorted
    by ``ts`` ascending — :func:`_fetch_training_data` guarantees that
    via the ``ORDER BY ts`` tail of the SQL.  We re-sort defensively
    here too because hand-rolled test fixtures may insert rows in any
    order, and a mis-ordered bucket would silently leak future rows
    into the in-sample slice (defeating the temporal-holdout point).
    """
    n = len(probs)
    if n == 0:
        return [], [], [], [], [], []
    order = sorted(range(n), key=lambda i: ts[i] if i < len(ts) else 0.0)
    p_sorted = [probs[i] for i in order]
    y_sorted = [ys[i] for i in order]
    s_sorted = [sources[i] if i < len(sources) else "live" for i in order]
    oos_n = max(OOS_HOLDOUT_MIN, int(round(n * OOS_HOLDOUT_FRACTION)))
    oos_n = min(oos_n, n - 1) if n > 1 else 0
    oos_n = max(0, oos_n)
    if oos_n <= 0:
        return p_sorted, y_sorted, s_sorted, [], [], []
    cut = n - oos_n
    return (
        p_sorted[:cut], y_sorted[:cut], s_sorted[:cut],
        p_sorted[cut:], y_sorted[cut:], s_sorted[cut:],
    )


async def _maybe_promote_challenger(league: str) -> tuple[bool, str | None]:
    """Run the auto-promotion check for ``league`` after a refit cycle.

    Reads the most recent two ACTIVE champion + challenger snapshot
    pairs (same ``(sub_category)`` and active roles).  If the
    challenger's OOS Brier is strictly less than the paired champion's
    OOS Brier (margin >= ``PROMOTION_MIN_DELTA``) in BOTH consecutive
    pairs the role swap fires in a single transaction:

      1. Every ``role='champion'`` row for this league is rewritten to
         ``role='retired_champion'``.
      2. The latest challenger snapshot row is rewritten to
         ``role='champion'``.

    Returns ``(promoted, reason)``.  ``reason`` is a short human
    string for the audit log explaining why we did or didn't swap.
    """
    sub = f"per_league:{league}"
    try:
        rows = await db.fetch(
            """
            SELECT id, role, brier_out_of_sample, created_at
              FROM calibration_snapshots
             WHERE vertical = 'sports'
               AND sub_category = $1
               AND COALESCE(role, 'champion') IN ('champion', 'challenger')
               AND brier_out_of_sample IS NOT NULL
             ORDER BY created_at DESC
             LIMIT 8
            """,
            sub,
        )
    except Exception as e:  # pragma: no cover — defensive
        log.warning("[calibrators] promotion read failed for %s: %s", league, e)
        return False, f"promotion read error: {type(e).__name__}"

    champs: list[tuple[Any, float, Any]] = []
    challs: list[tuple[Any, float, Any]] = []
    for r in rows:
        try:
            role = str(r["role"] or ROLE_CHAMPION).strip().lower()
            oos = r["brier_out_of_sample"]
            if oos is None:
                continue
            tup = (r["id"], float(oos), r["created_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if role == ROLE_CHAMPION and len(champs) < 2:
            champs.append(tup)
        elif role == ROLE_CHALLENGER and len(challs) < 2:
            challs.append(tup)
        if len(champs) >= 2 and len(challs) >= 2:
            break

    if len(champs) < 2 or len(challs) < 2:
        return False, "insufficient history (need 2 champion + 2 challenger snapshots)"

    pairs = [(champs[0][1], challs[0][1]), (champs[1][1], challs[1][1])]
    for ch_brier, cl_brier in pairs:
        if not (cl_brier < ch_brier - PROMOTION_MIN_DELTA):
            return (
                False,
                f"challenger oos {cl_brier:.6f} not better than champion {ch_brier:.6f} in last 2 cycles",
            )

    latest_challenger_id = challs[0][0]
    try:
        # Atomic swap: any in-flight reader either sees the OLD champion
        # OR the NEW champion, never two simultaneously.  Run-around
        # within a single statement using a CTE so even tests that mock
        # `db.execute` without transaction support do the right thing.
        await db.execute(
            """
            WITH retired AS (
              UPDATE calibration_snapshots
                 SET role = 'retired_champion'
               WHERE vertical = 'sports'
                 AND sub_category = $1
                 AND COALESCE(role, 'champion') = 'champion'
              RETURNING id
            )
            UPDATE calibration_snapshots
               SET role = 'champion'
             WHERE id = $2
            """,
            sub, latest_challenger_id,
        )
    except Exception as e:
        log.warning("[calibrators] promotion swap failed for %s: %s", league, e)
        return False, f"promotion swap error: {type(e).__name__}"

    log.info(
        "[calibrators] PROMOTED challenger -> champion for %s "
        "(oos brier pairs: %.6f<%.6f, %.6f<%.6f)",
        league, pairs[0][1], pairs[0][0], pairs[1][1], pairs[1][0],
    )
    return True, "challenger won 2 consecutive OOS Brier comparisons"


async def refit_all() -> dict[str, Any]:
    """Refit per-league calibrators from the last 90 days of trades.

    Each cycle walks the per-league bucket, slices a temporal OOS
    holdout off the tail, and produces up to TWO snapshots:

      * ``role='challenger'`` — a fresh calibrator fit on the
        in-sample slice and evaluated on both slices.  This is the
        "what would today's data say" candidate.
      * ``role='champion'``   — the **currently-loaded** champion's
        state re-scored on the same in-sample + OOS slices so the
        promotion job has matched-window Brier on both calibrators.
        On a cold start (no champion in memory) the freshly-fit
        calibrator is written as the champion instead and the
        challenger row is skipped — there's nothing yet to A/B
        against, and we want the very first refit to behave exactly
        like the pre-#755 single-calibrator path.

    After persistence the auto-promotion check fires; if the
    challenger has won OOS Brier in both of the most recent two
    cycles the role columns are swapped atomically and the next
    ``load_all()`` (called at the end of this function) hydrates the
    new champion straight into ``apply()``.

    Always runs to completion: per-league failures are captured in
    the returned summary so a sick query for one league doesn't block
    the others.
    """
    started = time.perf_counter()
    started_at = datetime.now(timezone.utc).isoformat()
    grouped = await _fetch_training_data()
    results: list[LeagueRefitResult] = []

    def _fit_sync(probs: list[float], ys: list[int]) -> dict[str, Any]:
        if len(probs) >= ISOTONIC_MIN_SAMPLES:
            return _fit_isotonic(probs, ys)
        return _fit_platt(probs, ys)

    for league in SUPPORTED_LEAGUES:
        bucket = grouped.get(league) or {
            "probs": [], "ys": [], "sources": [], "ts": [],
        }
        probs = bucket["probs"]
        ys = bucket["ys"]
        sources = bucket.get("sources") or ["live"] * len(probs)
        ts = bucket.get("ts") or [0.0] * len(probs)
        n = len(probs)
        if n < MIN_REFIT_SAMPLES:
            results.append(LeagueRefitResult(
                league=league, sample_count=n,
                skipped_reason=f"below MIN_REFIT_SAMPLES ({MIN_REFIT_SAMPLES})",
            ))
            continue

        # Temporal split — chronological tail becomes the OOS holdout
        # so the challenger Brier is evaluated on rows the fit never
        # saw (the whole point of the A/B framework).
        is_p, is_y, is_s, oos_p, oos_y, _oos_s = _temporal_split(
            probs, ys, sources, ts,
        )
        oos_size = len(oos_p)
        in_size = len(is_p)
        existing_champion = _state.champion(league)
        cold_start = existing_champion is None

        try:
            if cold_start:
                # No prior champion — fit the full window and write it
                # as the champion (no challenger row this cycle).  This
                # matches the pre-#755 single-calibrator behaviour for
                # the very first refit per league.
                champion_state = await asyncio.to_thread(_fit_sync, probs, ys)
                challenger_state = None
            else:
                # Warm path — challenger is fit on the in-sample slice
                # only so the OOS Brier is genuinely out-of-sample.
                # The champion's stored state is reused as-is (we are
                # NOT refitting the champion — the whole point is to
                # measure the production calibrator's degradation
                # against a fresh fit).
                challenger_state = await asyncio.to_thread(
                    _fit_sync, is_p, is_y,
                )
                champion_state = dict(existing_champion.state)
        except Exception as e:
            log.exception("[calibrators] fit failed for %s", league)
            results.append(LeagueRefitResult(
                league=league, sample_count=n, error=f"{type(e).__name__}: {e}",
            ))
            continue

        # Pre-/post-Brier on the FULL window — preserves the legacy
        # operator-facing comparison ("did calibration help vs raw").
        brier_before = _brier(probs, ys)
        try:
            full_calibrated = [_apply_one(champion_state, p) for p in probs]
            brier_after = _brier(full_calibrated, ys)
        except Exception:
            brier_after = brier_before

        def _safe_brier(state: dict[str, Any], xs: list[float], ws: list[int]) -> float | None:
            if not xs:
                return None
            try:
                return _brier([_apply_one(state, p) for p in xs], ws)
            except Exception:  # pragma: no cover — defensive
                return None

        champion_in = _safe_brier(champion_state, is_p, is_y)
        champion_oos = _safe_brier(champion_state, oos_p, oos_y)
        challenger_in = (
            _safe_brier(challenger_state, is_p, is_y)
            if challenger_state is not None else None
        )
        challenger_oos = (
            _safe_brier(challenger_state, oos_p, oos_y)
            if challenger_state is not None else None
        )

        # Per-source counts for the snapshot config — operator-facing
        # audit trail so we can tell at a glance how shadow-heavy a
        # given calibrator's training was.
        breakdown: dict[str, int] = {}
        for s in sources:
            key = str(s or "live")
            breakdown[key] = breakdown.get(key, 0) + 1

        champ_id = await _persist_calibrator(
            league, champion_state, n, brier_before, brier_after,
            source_breakdown=breakdown,
            role=ROLE_CHAMPION,
            brier_in_sample=champion_in,
            brier_out_of_sample=champion_oos,
        )
        chall_id: int | None = None
        # Only persist the challenger and run promotion if the champion
        # row landed cleanly — a partial-write cycle (champion failed +
        # challenger written) would mis-pair the most-recent two pairs
        # the promotion gate inspects, and could promote a challenger
        # whose paired champion was never recorded.
        if champ_id is not None and challenger_state is not None:
            chall_id = await _persist_calibrator(
                league, challenger_state, in_size,
                brier_before, brier_after,
                source_breakdown=breakdown,
                role=ROLE_CHALLENGER,
                brier_in_sample=challenger_in,
                brier_out_of_sample=challenger_oos,
            )

        promoted = False
        promo_reason: str | None = None
        if (
            not cold_start
            and champ_id is not None
            and chall_id is not None
        ):
            promoted, promo_reason = await _maybe_promote_challenger(league)

        results.append(LeagueRefitResult(
            league=league, sample_count=n,
            type=(challenger_state or champion_state).get("type"),
            brier_before=brier_before, brier_after=brier_after,
            snapshot_id=champ_id,
            challenger_snapshot_id=chall_id,
            in_sample_size=in_size,
            out_of_sample_size=oos_size,
            champion_brier_in_sample=champion_in,
            champion_brier_out_of_sample=champion_oos,
            challenger_brier_in_sample=challenger_in,
            challenger_brier_out_of_sample=challenger_oos,
            promoted=promoted,
            promotion_reason=promo_reason,
            error=None if champ_id is not None else "snapshot insert failed",
        ))

    # Reload so apply() sees the fresh artifacts immediately.
    try:
        await load_all()
    except Exception as e:  # pragma: no cover — defensive
        log.warning("[calibrators] reload after refit failed: %s", e)

    summary = {
        "started_at": started_at,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.perf_counter() - started, 3),
        "leagues": [r.__dict__ for r in results],
        "fitted": sum(1 for r in results if r.snapshot_id is not None),
        "skipped": sum(1 for r in results if r.skipped_reason is not None),
        "errors": sum(1 for r in results if r.error is not None),
    }
    _state.last_refit_at = time.time()
    _state.last_refit_summary = summary
    log.info(
        "[calibrators] refit done fitted=%d skipped=%d errors=%d duration=%.2fs",
        summary["fitted"], summary["skipped"], summary["errors"],
        summary["duration_seconds"],
    )
    return summary


# ---------------------------------------------------------------------------
# Nightly trigger — polled by the settlement scheduler
# ---------------------------------------------------------------------------


async def maybe_refit() -> dict[str, Any] | None:
    """Run ``refit_all()`` if the last refit is older than the cadence.

    Designed to be called from a long-running loop: returns ``None``
    when nothing happened so the caller can record a no-op in its
    status payload, or the refit summary when a fit ran.
    """
    last = _state.last_refit_at
    if last is not None and (time.time() - last) < REFIT_INTERVAL_SECONDS:
        return None
    return await refit_all()
