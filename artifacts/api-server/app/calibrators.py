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


@dataclass
class _Calibrator:
    league: str
    type: str  # 'isotonic' | 'platt'
    state: dict[str, Any]
    fit_at: float  # epoch seconds
    sample_count: int


@dataclass
class CalibratorsState:
    loaded_at: float | None = None
    last_refit_at: float | None = None
    last_refit_summary: dict[str, Any] = field(default_factory=dict)
    calibrators: dict[str, _Calibrator] = field(default_factory=dict)

    def freshness(self) -> dict[str, Any]:
        now = time.time()
        per_league: dict[str, Any] = {}
        for lg in SUPPORTED_LEAGUES:
            c = self.calibrators.get(lg)
            if c is None:
                per_league[lg] = {
                    "loaded": False, "type": None,
                    "fit_at": None, "age_seconds": None,
                    "stale": True, "sample_count": 0,
                }
                continue
            age = now - c.fit_at
            per_league[lg] = {
                "loaded": True, "type": c.type,
                "fit_at": c.fit_at, "age_seconds": round(age, 1),
                "stale": age > FRESH_TTL_SECONDS,
                "sample_count": c.sample_count,
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


def apply(probability: float, league: str | None) -> tuple[float, dict[str, Any]]:
    """Return ``(calibrated_probability, info)``.

    Bypasses cleanly when ``league`` is unknown / not supported / has
    no fitted artifact: the caller still gets the raw probability and
    an ``info`` dict explaining the bypass so the caller can log it.
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
    cal = _state.calibrators.get(norm)
    if cal is None:
        info["reason"] = "no fitted calibrator"
        return p, info
    try:
        if cal.type == "isotonic":
            cp = _apply_isotonic(p, cal.state)
        elif cal.type == "platt":
            cp = _apply_platt(p, cal.state)
        else:
            info["reason"] = f"unknown calibrator type {cal.type!r}"
            return p, info
    except Exception as e:  # pragma: no cover — defensive
        log.warning("[calibrators] apply failed for %s: %s", norm, e)
        info["reason"] = f"apply error: {type(e).__name__}"
        return p, info
    cp = max(0.0, min(1.0, float(cp)))
    info.update({
        "applied": True,
        "type": cal.type,
        "raw_probability": p,
        "fit_at": cal.fit_at,
        "sample_count": cal.sample_count,
    })
    return cp, info


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

    Idempotent: callers can re-invoke after a refit to pick up the new
    artifacts.  Failures are swallowed and logged — a flaky DB at boot
    must NOT block the API server from serving requests; ``apply()``
    will simply bypass calibration until a successful load lands.
    """
    async with _lock:
        try:
            rows = await db.fetch(
                """
                SELECT DISTINCT ON (sub_category)
                       sub_category,
                       calibrator_state,
                       sample_count,
                       extract(epoch FROM created_at)::float AS fit_at
                  FROM calibration_snapshots
                 WHERE vertical = 'sports'
                   AND sub_category LIKE 'per_league:%'
                   AND calibrator_state IS NOT NULL
                   AND calibrator_state::text <> '{}'
                   AND COALESCE(drift_status->>'status', '') <> 'rolled_back'
                 ORDER BY sub_category, created_at DESC
                """
            )
        except Exception as e:
            log.warning("[calibrators] load skipped: %s", e)
            _state.loaded_at = time.time()
            return _state

        loaded: dict[str, _Calibrator] = {}
        for r in rows:
            sub = str(r["sub_category"] or "")
            if not sub.startswith("per_league:"):
                continue
            league = sub.split(":", 1)[1].strip().upper()
            if league not in SUPPORTED_LEAGUES:
                continue
            st = _parse_state_blob(r["calibrator_state"])
            if not st:
                continue
            kind = str(st.get("type") or "").lower()
            if kind not in ("isotonic", "platt"):
                continue
            loaded[league] = _Calibrator(
                league=league,
                type=kind,
                state=st,
                fit_at=float(r["fit_at"] or 0.0),
                sample_count=int(r["sample_count"] or 0),
            )
        _state.calibrators = loaded
        _state.loaded_at = time.time()
        log.info(
            "[calibrators] loaded %d/%d leagues: %s",
            len(loaded), len(SUPPORTED_LEAGUES),
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


async def _fetch_training_data() -> dict[str, dict[str, list]]:
    """Pull the last 90 days of settled trades+outcomes grouped by league.

    Returns ``{LEAGUE: {"probs":[...], "ys":[...]}}``.  Trades are
    aligned to the side they were placed on so the calibrator learns
    P(side wins), matching how ``apply()`` is invoked downstream.

    Two sources are UNION'd:

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

    The V1 union can be disabled at runtime via
    ``AXIOM_USE_V1_ARCHIVE_FOR_CALIBRATION=0`` for kill-switch safety.
    A separate preflight ``SELECT to_regclass(...)`` runs before SQL
    composition and gates the V1 CTE OUT entirely on environments
    that haven't run migration 0037 — a WHERE-clause guard would NOT
    suffice because Postgres resolves FROM-clause relations at parse
    time, before any predicate evaluates, and a missing table would
    raise ``UndefinedTable`` and silently drop live rows too.
    """
    import os

    use_v1 = (
        os.getenv("AXIOM_USE_V1_ARCHIVE_FOR_CALIBRATION", "1").lower()
        not in ("0", "false", "no", "off")
    )

    # PostgreSQL resolves FROM-clause relations at PARSE time, before any
    # WHERE predicate runs.  A `to_regclass(...) IS NOT NULL` clause in
    # WHERE does NOT protect the query from `UndefinedTable` when the
    # archive table is absent (fresh dev DBs that never ran migration
    # 0037).  So we MUST preflight separately and only compose the V1
    # CTE into the SQL when the table actually exists; otherwise we'd
    # raise into the broad except below and silently drop live rows
    # too.
    if use_v1:
        try:
            v1_present = await db.fetchval(
                "SELECT to_regclass('v1_archive.trade_analyses') IS NOT NULL"
            )
        except Exception as e:
            log.warning("[calibrators] v1_archive preflight failed: %s", e)
            v1_present = False
        use_v1 = bool(v1_present)

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
                 END                                          AS y_side
            FROM trades t
            JOIN trade_postmortems pm
              ON pm.trade_id = coalesce(t.trade_id, t.id::text)
           WHERE t.settled_at IS NOT NULL
             AND t.settled_at > now() - interval '{int(REFIT_WINDOW_DAYS)} days'
             AND t.model_prob IS NOT NULL
             AND pm.actual_outcome IS NOT NULL
             AND t.league IS NOT NULL
        )"""

    if use_v1:
        v1_cte = f""",
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
                 actual_outcome::int                          AS y_side
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
        )"""
        union_tail = """
            SELECT league, p_side, y_side FROM live_settled
            UNION ALL
            SELECT league, p_side, y_side FROM v1_settled"""
    else:
        v1_cte = ""
        union_tail = """
            SELECT league, p_side, y_side FROM live_settled"""

    sql = f"WITH {live_cte}{v1_cte}{union_tail}"

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
        bucket = grouped.setdefault(lg, {"probs": [], "ys": []})
        bucket["probs"].append(float(r["p_side"]))
        bucket["ys"].append(int(r["y_side"]))
    return grouped


async def _persist_calibrator(
    league: str, state: dict[str, Any], n: int,
    brier_before: float | None, brier_after: float | None,
) -> int | None:
    """Append a per-league calibrator snapshot.  Returns row id."""
    try:
        snap_id = await db.fetchval(
            """
            INSERT INTO calibration_snapshots
                (vertical, sub_category, ece, mce, brier_score, reliability,
                 resolution, sample_count, calibrator_state, drift_status, config)
            VALUES ('sports', $1, $2, $3, $4, NULL, NULL, $5,
                    $6::jsonb,
                    jsonb_build_object('status', 'fitted',
                                       'kind', $7::text,
                                       'window_days', $8::int),
                    jsonb_build_object('source', 'per_league_refit',
                                       'brier_before', $9::float,
                                       'brier_after', $10::float))
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
            float(brier_before) if brier_before is not None else None,
            float(brier_after) if brier_after is not None else None,
        )
        return int(snap_id) if snap_id is not None else None
    except Exception as e:
        log.warning("[calibrators] persist failed for %s: %s", league, e)
        return None


async def refit_all() -> dict[str, Any]:
    """Refit per-league calibrators from the last 90 days of trades.

    Always runs to completion: per-league failures are captured in the
    returned summary so a sick query for one league doesn't block the
    others.  The in-memory state is reloaded from the DB at the end so
    the next ``apply()`` picks up the new artifacts.
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
        bucket = grouped.get(league) or {"probs": [], "ys": []}
        probs = bucket["probs"]
        ys = bucket["ys"]
        n = len(probs)
        if n < MIN_REFIT_SAMPLES:
            results.append(LeagueRefitResult(
                league=league, sample_count=n,
                skipped_reason=f"below MIN_REFIT_SAMPLES ({MIN_REFIT_SAMPLES})",
            ))
            continue
        try:
            state = await asyncio.to_thread(_fit_sync, probs, ys)
            brier_before = _brier(probs, ys)
            calibrated = [_apply_one(state, p) for p in probs]
            brier_after = _brier(calibrated, ys)
        except Exception as e:
            log.exception("[calibrators] fit failed for %s", league)
            results.append(LeagueRefitResult(
                league=league, sample_count=n, error=f"{type(e).__name__}: {e}",
            ))
            continue
        snap_id = await _persist_calibrator(
            league, state, n, brier_before, brier_after,
        )
        results.append(LeagueRefitResult(
            league=league, sample_count=n, type=state.get("type"),
            brier_before=brier_before, brier_after=brier_after,
            snapshot_id=snap_id,
            error=None if snap_id is not None else "snapshot insert failed",
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
