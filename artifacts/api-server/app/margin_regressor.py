"""Per-league margin / total regressors for spread & total markets.

The canonical meta-model is a *binary* classifier — it emits a single
"YES" probability for any ticker (Winner, Spread, Total, …).  That works
fine for moneyline questions but throws away the structure of spread
and total markets, which are really questions about a continuous
quantity (margin of victory, combined points) compared against a line.

This module fills that gap with a small per-league regressor:

    NBA / NFL / MLB / NHL  →  predicts (expected_margin, expected_total)
                              from elo_diff + recent-form + HFA.

The regressor's output is converted to a binary probability via a
Gaussian CDF using the league's empirical residual standard deviation
(measured at fit time).  That probability is then *blended* with the
binary classifier's prediction so the meta-model gets to keep its edge
on signals it sees that the regressor doesn't (sharp money, time-zone
arb, etc.) while picking up the regressor's continuous-quantity
advantage on spread/total tickers.

Design notes
------------
* Stateless except for the in-process per-league fit cache.  Fits are
  cheap (handful of features × few thousand games per league per
  season), so we re-fit lazily on demand rather than persisting the
  coefficients.  A future iteration can pickle them next to the
  meta-model — the API here is designed to make that swap a one-liner.
* Pure numpy — no extra dependency on top of what predictor.py already
  pulls in.  Closed-form ridge regression keeps the math obvious.
* The blend weight is tuned *per league* by sweeping candidate weights
  through `backtest_spread()` and picking the one that minimises
  holdout Brier.  Different leagues have very different signal-to-noise
  ratios — NFL with 17 games per team behaves nothing like a fully-fit
  MLB regressor with 162 — so a flat 50/50 leaves accuracy on the table
  either way.  The tuned weight is refreshed alongside the periodic
  refit and stored in an in-process registry; callers fall through to
  per-league defaults when nothing has been tuned yet.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Iterable, Mapping, Sequence

from .elo import HFA_BY_LEAGUE


log = logging.getLogger(__name__)


# Leagues we ship a regressor for.  Other leagues fall through to the
# binary baseline unchanged so we never degrade markets we don't have
# enough historical data for.
SUPPORTED_LEAGUES: frozenset[str] = frozenset({"NBA", "NFL", "MLB", "NHL"})


# Reasonable residual std (points) used as a fallback when we don't yet
# have enough fitted history to measure it empirically.  These are the
# textbook season-long margin SDs used by sportsbooks for opening
# spread pricing and let `margin_to_spread_prob()` produce a sensible
# probability before the very first fit lands.
_DEFAULT_MARGIN_SD: dict[str, float] = {
    "NBA": 12.0,
    "NFL": 13.5,
    "MLB": 4.0,
    "NHL": 2.5,
}
_DEFAULT_TOTAL_SD: dict[str, float] = {
    "NBA": 18.0,
    "NFL": 13.0,
    "MLB": 4.2,
    "NHL": 2.6,
}


# Per-league cold-start blend weights used by `adjust_probability()` when
# nothing has been tuned yet.  Picked qualitatively from games-per-season
# / regressor-fit-quality:  MLB has 2,430 games a year so its regressor
# is the most trustworthy and gets the highest weight; NFL only 272 so
# we lean more on the binary baseline; NBA / NHL sit in between.  These
# are *defaults* — `tune_blend_weight()` overrides them after each refit.
_DEFAULT_BLEND_WEIGHT_BY_LEAGUE: dict[str, float] = {
    "NBA": 0.55,
    "NFL": 0.35,
    "MLB": 0.65,
    "NHL": 0.50,
}


# Candidate weights swept by `tune_blend_weight()`.  An 11-point grid
# from 0.0 → 1.0 is plenty given the residual noise on Brier and keeps
# the tune cheap (a backtest pass per weight, all on the same fitted
# regressor).  Including the endpoints lets the tuner say "ignore the
# regressor" or "drop the baseline" when the data demands it.
_BLEND_WEIGHT_GRID: tuple[float, ...] = tuple(round(0.1 * i, 2) for i in range(11))


def _norm_cdf(x: float) -> float:
    """Standard-normal CDF.  Avoids pulling in scipy for one call."""
    return 0.5 * (1.0 + math.erf(float(x) / math.sqrt(2.0)))


# ---------------------------------------------------------------------------
# Feature vector
# ---------------------------------------------------------------------------


# Names line up positionally with `extract_features()` below.  Stored
# on the fitted regressor so callers can introspect what the
# coefficients mean.
FEATURE_NAMES: tuple[str, ...] = (
    "intercept",
    "elo_diff_hfa",      # (home_elo + hfa) - away_elo
    "form_delta_diff",   # home recent-form Δ minus away recent-form Δ
    "rest_diff",         # home days-rest minus away days-rest (0 if missing)
)


def extract_features(
    candidate: Mapping[str, Any] | None,
    *,
    league: str | None = None,
    game_context: Mapping[str, Any] | None = None,
) -> list[float]:
    """Pull the regressor's input row from a candidate / game_context.

    Both sources are consulted so callers can pass either a flattened
    candidate dict (orchestrator path) or a raw historical game row
    (training / backtest path).  Missing signals default to 0 so a
    cold-start regressor still produces something.
    """
    sources: list[Mapping[str, Any]] = []
    if isinstance(candidate, Mapping):
        sources.append(candidate)
    if isinstance(game_context, Mapping):
        sources.append(game_context)

    def _get(key: str, default: float = 0.0) -> float:
        for src in sources:
            v = src.get(key)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
        return float(default)

    league_key = league or _get_str(sources, "league") or ""
    hfa = HFA_BY_LEAGUE.get(league_key.strip(), 0.0)

    home_elo = _get("home_elo")
    away_elo = _get("opponent_elo")
    elo_diff_signed = _get("elo_diff", default=(home_elo - away_elo))
    elo_diff_hfa = elo_diff_signed + hfa

    home_form = _get("recent_form_delta")
    away_form = _get("opponent_recent_form_delta")
    form_delta_diff = home_form - away_form

    rest_diff = _get("home_days_rest") - _get("away_days_rest")

    return [1.0, elo_diff_hfa, form_delta_diff, rest_diff]


def _get_str(sources: Sequence[Mapping[str, Any]], key: str) -> str | None:
    for src in sources:
        v = src.get(key)
        if isinstance(v, str) and v.strip():
            return v
    return None


# ---------------------------------------------------------------------------
# Regressor (closed-form ridge)
# ---------------------------------------------------------------------------


@dataclass
class LeagueScoreRegressor:
    """Predicts (expected_margin, expected_total) for a single league."""

    league: str
    margin_coef: list[float] = field(default_factory=list)
    total_coef: list[float] = field(default_factory=list)
    margin_sd: float = 12.0
    total_sd: float = 12.0
    n_train: int = 0
    feature_names: tuple[str, ...] = FEATURE_NAMES

    def is_fitted(self) -> bool:
        return bool(self.margin_coef) and bool(self.total_coef)

    def predict(self, features: Sequence[float]) -> tuple[float, float]:
        """Return (margin, total).  Falls back to (0, league_avg_total)
        when the regressor hasn't been fitted yet so callers can keep
        running on a cold cache."""
        if not self.is_fitted():
            return 0.0, _league_default_total(self.league)
        return _dot(self.margin_coef, features), _dot(self.total_coef, features)

    def margin_to_spread_prob(
        self,
        expected_margin: float,
        *,
        spread_line: float,
        side: str = "home",
    ) -> float:
        """Probability the chosen side covers `spread_line`.

        Convention: `spread_line` is the home team's spread on the
        ticker — negative when home is favored (e.g. -3.5).  Home
        covers iff (home_score - away_score) > -spread_line, so we
        compare the predicted margin against the cover threshold.
        """
        threshold = -float(spread_line)
        z = (float(expected_margin) - threshold) / max(self.margin_sd, 0.5)
        p_home_cover = 1.0 - _norm_cdf(-z)
        if side.lower() in {"away", "no"}:
            return 1.0 - p_home_cover
        return p_home_cover

    def total_to_over_prob(
        self,
        expected_total: float,
        *,
        total_line: float,
        side: str = "over",
    ) -> float:
        z = (float(expected_total) - float(total_line)) / max(self.total_sd, 0.5)
        p_over = 1.0 - _norm_cdf(-z)
        if side.lower() in {"under", "no"}:
            return 1.0 - p_over
        return p_over


def _dot(coef: Sequence[float], features: Sequence[float]) -> float:
    n = min(len(coef), len(features))
    return float(sum(coef[i] * features[i] for i in range(n)))


def _league_default_total(league: str) -> float:
    """Rough league-average totals — only used before the first fit."""
    return {
        "NBA": 225.0,
        "NFL": 44.0,
        "MLB": 8.5,
        "NHL": 6.0,
    }.get((league or "").strip(), 0.0)


# ---------------------------------------------------------------------------
# Closed-form ridge fit  (no sklearn dependency required)
# ---------------------------------------------------------------------------


def _ridge_fit(
    X: list[list[float]],
    y: list[float],
    *,
    alpha: float = 1.0,
) -> tuple[list[float], float]:
    """Solve (XᵀX + αI) β = Xᵀy.  Returns (coef, residual_sd).

    Implemented in numpy so we don't add a sklearn dep just for
    half a dozen features.  α regularises so a near-singular design
    (e.g. backtest with constant features) still yields a usable fit.
    """
    import numpy as np

    if not X or not y or len(X) != len(y):
        raise ValueError("X and y must be non-empty and equal length")

    Xa = np.asarray(X, dtype=float)
    ya = np.asarray(y, dtype=float)
    n, k = Xa.shape

    reg = float(alpha) * np.eye(k)
    # Don't penalise the intercept (column 0) — common convention.
    reg[0, 0] = 0.0
    A = Xa.T @ Xa + reg
    b = Xa.T @ ya
    coef = np.linalg.solve(A, b)
    resid = ya - Xa @ coef
    # ddof=k matches the unbiased estimator for OLS residual variance.
    if n > k:
        sd = float(np.sqrt(np.sum(resid ** 2) / (n - k)))
    else:
        sd = float(np.sqrt(np.mean(resid ** 2))) if n > 0 else 0.0
    return [float(c) for c in coef], sd


def fit_regressor(
    league: str,
    games: Iterable[Mapping[str, Any]],
    *,
    alpha: float = 1.0,
) -> LeagueScoreRegressor:
    """Fit a per-league regressor on iterable of historical games.

    Each game must carry at minimum `home_score`, `away_score` and the
    feature inputs (`elo_diff` or `home_elo`+`opponent_elo`, etc).
    Games with non-numeric scores are silently skipped.
    """
    X: list[list[float]] = []
    margins: list[float] = []
    totals: list[float] = []
    for g in games:
        try:
            home_score = float(g["home_score"])
            away_score = float(g["away_score"])
        except (KeyError, TypeError, ValueError):
            continue
        feats = extract_features(g, league=league)
        X.append(feats)
        margins.append(home_score - away_score)
        totals.append(home_score + away_score)
    if not X:
        # Nothing to fit on — return an unfitted regressor with sane SDs
        # so callers still get a reasonable spread/total probability.
        return LeagueScoreRegressor(
            league=league,
            margin_sd=_DEFAULT_MARGIN_SD.get(league, 12.0),
            total_sd=_DEFAULT_TOTAL_SD.get(league, 12.0),
        )

    margin_coef, margin_sd = _ridge_fit(X, margins, alpha=alpha)
    total_coef, total_sd = _ridge_fit(X, totals, alpha=alpha)
    # Floor the residual SDs at the textbook defaults so a tiny / noisy
    # fit can't produce overconfident probabilities.
    margin_sd = max(margin_sd, _DEFAULT_MARGIN_SD.get(league, 1.0) * 0.5)
    total_sd = max(total_sd, _DEFAULT_TOTAL_SD.get(league, 1.0) * 0.5)
    return LeagueScoreRegressor(
        league=league,
        margin_coef=margin_coef,
        total_coef=total_coef,
        margin_sd=margin_sd,
        total_sd=total_sd,
        n_train=len(X),
    )


# ---------------------------------------------------------------------------
# In-process registry  (lazy + thread-safe)
# ---------------------------------------------------------------------------


_REGISTRY: dict[str, LeagueScoreRegressor] = {}
_LAST_FIT_AT: dict[str, str] = {}  # ISO-8601 UTC timestamps, keyed by league
_BLEND_WEIGHTS: dict[str, float] = {}
_REGISTRY_LOCK = Lock()


def register_regressor(reg: LeagueScoreRegressor, *, fit_at: str | None = None) -> None:
    with _REGISTRY_LOCK:
        _REGISTRY[reg.league] = reg
        if fit_at:
            _LAST_FIT_AT[reg.league] = fit_at


def get_regressor(league: str | None) -> LeagueScoreRegressor | None:
    if not league or league not in SUPPORTED_LEAGUES:
        return None
    with _REGISTRY_LOCK:
        reg = _REGISTRY.get(league)
    if reg is None:
        # Cold cache — return a defaults-only regressor so callers can
        # still produce a reasonable probability before any training
        # data has been ingested.
        reg = LeagueScoreRegressor(
            league=league,
            margin_sd=_DEFAULT_MARGIN_SD.get(league, 12.0),
            total_sd=_DEFAULT_TOTAL_SD.get(league, 12.0),
        )
    return reg


def register_blend_weight(league: str, weight: float) -> None:
    """Pin the blend weight `adjust_probability()` should use for `league`.

    Clamped to [0, 1] so a misbehaving tuner can never produce an
    out-of-range probability downstream.
    """
    if not league:
        return
    w = max(0.0, min(1.0, float(weight)))
    with _REGISTRY_LOCK:
        _BLEND_WEIGHTS[league] = w


def get_blend_weight(league: str | None) -> float:
    """Return the configured blend weight for `league`.

    Resolution order:
      1. Tuned weight from the in-process registry (set by
         `tune_blend_weight()` / `refit_all_leagues()`).
      2. The per-league cold-start default.
      3. 0.5 as a final fallback for unknown leagues — same behaviour
         as the historical flat-50/50 blend.
    """
    if not league:
        return 0.5
    with _REGISTRY_LOCK:
        w = _BLEND_WEIGHTS.get(league)
    if w is None:
        w = _DEFAULT_BLEND_WEIGHT_BY_LEAGUE.get(league, 0.5)
    return max(0.0, min(1.0, float(w)))


def reset_registry() -> None:
    """Test hook — wipe in-process state between cases."""
    with _REGISTRY_LOCK:
        _REGISTRY.clear()
        _LAST_FIT_AT.clear()
        _BLEND_WEIGHTS.clear()


def status() -> dict[str, Any]:
    """Per-league fit status for the admin dashboard.

    Always returns one entry per supported league so a stale league
    (no fit yet) shows up as ``fitted=false`` rather than being
    omitted entirely.
    """
    out: dict[str, Any] = {}
    with _REGISTRY_LOCK:
        regs = dict(_REGISTRY)
        fits = dict(_LAST_FIT_AT)
        weights = dict(_BLEND_WEIGHTS)
    for league in sorted(SUPPORTED_LEAGUES):
        reg = regs.get(league)
        out[league] = {
            "league": league,
            "fitted": bool(reg and reg.is_fitted()),
            "n_train": int(reg.n_train) if reg else 0,
            "margin_sd": float(reg.margin_sd) if reg else _DEFAULT_MARGIN_SD.get(league, 12.0),
            "total_sd": float(reg.total_sd) if reg else _DEFAULT_TOTAL_SD.get(league, 12.0),
            "last_fit_at": fits.get(league),
            "blend_weight": float(
                weights.get(league, _DEFAULT_BLEND_WEIGHT_BY_LEAGUE.get(league, 0.5))
            ),
            "feature_names": list(reg.feature_names) if reg else list(FEATURE_NAMES),
        }
    return {"leagues": out, "supported": sorted(SUPPORTED_LEAGUES)}


# ---------------------------------------------------------------------------
# Live blending (called by the orchestrator)
# ---------------------------------------------------------------------------


def adjust_probability(
    baseline_prob: float,
    candidate: Mapping[str, Any],
    *,
    blend_weight: float | None = None,
) -> dict[str, Any] | None:
    """Blend a margin/total probability into the binary baseline.

    Returns a dict describing the adjustment, or None when no
    regressor applies (unsupported league, non-spread/total market,
    missing line, etc.).  Callers replace the baseline probability
    with the returned `blended` field.

    `blend_weight` is the weight on the regressor probability — 0 is
    "ignore the regressor", 1 is "trust only the regressor".  When
    omitted the per-league tuned weight (or its cold-start default)
    is looked up via `get_blend_weight()`, so different leagues no
    longer share a single flat 50/50 blend.
    """
    league = (candidate.get("league") or "").strip()
    market_type = (candidate.get("market_type") or "").strip()
    if league not in SUPPORTED_LEAGUES:
        return None
    if market_type not in {"Spread", "Total"}:
        return None
    # Soccer / cup competitions ship 3-way h/d/a moneyline markets
    # rather than continuous-margin Spread/Total questions.  The
    # Spread/Total filter above already excludes them in practice, but
    # we also short-circuit on the league-config flag so a future
    # Spread-style soccer market (e.g. Asian handicap) can't silently
    # pick up the NA-sports regressor without a 3-way-aware fit.
    try:
        from .sports.leagues import get_league_config as _glc
        _cfg = _glc(league)
    except Exception:
        _cfg = None
    if _cfg is not None and _cfg.three_way_market:
        return None

    ctx = candidate.get("game_context")
    if isinstance(ctx, str):
        try:
            import json
            ctx = json.loads(ctx)
        except Exception:
            ctx = None
    if not isinstance(ctx, Mapping):
        ctx = {}

    # Line + side resolution.  Look on the candidate first, then in
    # game_context — both are valid carriers depending on which feed
    # produced the row.
    line = _resolve_line(candidate, ctx, market_type)
    if line is None:
        return None
    side = _resolve_side(candidate)

    reg = get_regressor(league)
    if reg is None:
        return None
    feats = extract_features(candidate, league=league, game_context=ctx)
    margin, total = reg.predict(feats)
    if market_type == "Spread":
        regressor_prob = reg.margin_to_spread_prob(margin, spread_line=line, side=side)
        expected = margin
    else:
        regressor_prob = reg.total_to_over_prob(total, total_line=line, side=side)
        expected = total

    if blend_weight is None:
        w = get_blend_weight(league)
    else:
        w = max(0.0, min(1.0, float(blend_weight)))
    blended = (1.0 - w) * float(baseline_prob) + w * float(regressor_prob)
    blended = max(0.0, min(1.0, blended))
    return {
        "league": league,
        "market_type": market_type,
        "line": float(line),
        "side": side,
        "expected": float(expected),
        "regressor_prob": float(regressor_prob),
        "baseline_prob": float(baseline_prob),
        "blend_weight": w,
        "blended": float(blended),
        "n_train": int(reg.n_train),
        "fitted": reg.is_fitted(),
    }


def _resolve_line(
    candidate: Mapping[str, Any],
    ctx: Mapping[str, Any],
    market_type: str,
) -> float | None:
    """Find the spread / total line, looking in both common slots."""
    keys = (
        ("spread_line", "spread", "home_spread", "line")
        if market_type == "Spread"
        else ("total_line", "total", "over_under", "line")
    )
    for src in (candidate, ctx):
        for k in keys:
            v = src.get(k)
            if v is None:
                continue
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def _resolve_side(candidate: Mapping[str, Any]) -> str:
    """Map the candidate's side to the regressor's home/away or
    over/under convention."""
    explicit = (candidate.get("side") or "").strip().lower()
    if explicit in {"home", "away", "over", "under", "yes", "no"}:
        return explicit
    action = (candidate.get("recommended_action") or "").upper()
    if action == "BUY_NO":
        return "no"
    return "yes"


# ---------------------------------------------------------------------------
# Backtest harness
# ---------------------------------------------------------------------------


@dataclass
class BacktestResult:
    league: str
    n_train: int
    n_test: int
    baseline_brier: float
    blended_brier: float
    regressor_brier: float

    @property
    def improvement(self) -> float:
        return self.baseline_brier - self.blended_brier

    def as_dict(self) -> dict[str, Any]:
        return {
            "league": self.league,
            "n_train": self.n_train,
            "n_test": self.n_test,
            "baseline_brier": round(self.baseline_brier, 6),
            "blended_brier": round(self.blended_brier, 6),
            "regressor_brier": round(self.regressor_brier, 6),
            "improvement": round(self.improvement, 6),
        }


def backtest_spread(
    league: str,
    games: Sequence[Mapping[str, Any]],
    *,
    train_frac: float = 0.7,
    blend_weight: float = 0.5,
    baseline_fn: Any = None,
) -> BacktestResult:
    """Run a chronological train/test split and report Brier scores.

    Each game must carry `home_score`, `away_score`, `spread_line` and
    the regressor's input features.  `baseline_fn(game) -> prob` is the
    binary baseline being compared against; it defaults to a flat 0.5
    so the test always has *some* baseline to beat.
    """
    n = len(games)
    if n < 4:
        raise ValueError("need at least 4 games to backtest")
    n_train = max(2, int(round(n * float(train_frac))))
    train, test = games[:n_train], games[n_train:]
    if not test:
        raise ValueError("train_frac left no test rows")

    reg = fit_regressor(league, train)
    if baseline_fn is None:
        def baseline_fn(_g):  # type: ignore[no-redef]
            return 0.5

    base_sq = 0.0
    blend_sq = 0.0
    reg_sq = 0.0
    for g in test:
        home_score = float(g["home_score"])
        away_score = float(g["away_score"])
        spread_line = float(g["spread_line"])
        # Outcome convention: home covers iff margin > -spread_line.
        margin = home_score - away_score
        outcome = 1.0 if margin > -spread_line else 0.0

        feats = extract_features(g, league=league)
        m_hat, _ = reg.predict(feats)
        reg_prob = reg.margin_to_spread_prob(m_hat, spread_line=spread_line)
        base_prob = float(baseline_fn(g))
        w = max(0.0, min(1.0, float(blend_weight)))
        blended = (1.0 - w) * base_prob + w * reg_prob

        base_sq += (base_prob - outcome) ** 2
        blend_sq += (blended - outcome) ** 2
        reg_sq += (reg_prob - outcome) ** 2

    n_test = len(test)
    return BacktestResult(
        league=league,
        n_train=len(train),
        n_test=n_test,
        baseline_brier=base_sq / n_test,
        blended_brier=blend_sq / n_test,
        regressor_brier=reg_sq / n_test,
    )


# ---------------------------------------------------------------------------
# Blend-weight tuning  (per-league, picks the Brier-minimising weight)
# ---------------------------------------------------------------------------


@dataclass
class BlendWeightTuneResult:
    """Outcome of a per-league blend-weight sweep."""

    league: str
    weight: float
    blended_brier: float
    baseline_brier: float
    regressor_brier: float
    n_test: int
    grid: tuple[float, ...] = field(default_factory=tuple)
    grid_brier: tuple[float, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "league": self.league,
            "weight": round(self.weight, 4),
            "blended_brier": round(self.blended_brier, 6),
            "baseline_brier": round(self.baseline_brier, 6),
            "regressor_brier": round(self.regressor_brier, 6),
            "n_test": self.n_test,
            "grid": list(self.grid),
            "grid_brier": [round(b, 6) for b in self.grid_brier],
        }


def tune_blend_weight(
    league: str,
    games: Sequence[Mapping[str, Any]],
    *,
    train_frac: float = 0.7,
    grid: Sequence[float] = _BLEND_WEIGHT_GRID,
    baseline_fn: Any = None,
) -> BlendWeightTuneResult | None:
    """Pick the blend weight that minimises holdout Brier for `league`.

    Sweeps `grid` through `backtest_spread()` and returns the weight
    with the lowest blended Brier on the chronological holdout split.
    Returns None when there isn't enough data to backtest — callers
    should fall back to `_DEFAULT_BLEND_WEIGHT_BY_LEAGUE`.

    `baseline_fn` defaults to a flat 0.5 inside `backtest_spread()`,
    which is the same comparison `BacktestResult.baseline_brier`
    documents.  Tuning against the production binary baseline would
    require running the meta-model over every historical game; we
    keep the cheaper "beat 0.5" formulation here and rely on the
    fact that the optimal *blend* weight is largely determined by
    the regressor's own residual variance, not the baseline's mean.
    """
    # `load_training_games()` LEFT-JOINs spread/total lines, so it can
    # legitimately hand us rows with `spread_line=None` or non-numeric
    # scores.  `backtest_spread()` would raise on those — sanitize once
    # up front rather than aborting the entire league's tune on a single
    # bad row.
    clean: list[Mapping[str, Any]] = []
    for g in games:
        try:
            float(g["home_score"])
            float(g["away_score"])
            sl = g.get("spread_line")
            if sl is None:
                continue
            float(sl)
        except (KeyError, TypeError, ValueError):
            continue
        clean.append(g)
    if len(clean) < 4:
        return None
    candidate_weights = [
        max(0.0, min(1.0, float(w))) for w in grid
    ] or [0.5]

    best: BacktestResult | None = None
    best_w: float = candidate_weights[0]
    grid_brier: list[float] = []
    for w in candidate_weights:
        try:
            res = backtest_spread(
                league, clean,
                train_frac=train_frac,
                blend_weight=w,
                baseline_fn=baseline_fn,
            )
        except (ValueError, TypeError, KeyError) as e:
            log.debug("tune_blend_weight(%s) backtest failed at w=%s: %s",
                      league, w, e)
            return None
        grid_brier.append(res.blended_brier)
        if best is None or res.blended_brier < best.blended_brier:
            best = res
            best_w = w
    assert best is not None  # candidate_weights is non-empty
    return BlendWeightTuneResult(
        league=league,
        weight=best_w,
        blended_brier=best.blended_brier,
        baseline_brier=best.baseline_brier,
        regressor_brier=best.regressor_brier,
        n_test=best.n_test,
        grid=tuple(candidate_weights),
        grid_brier=tuple(grid_brier),
    )


# ---------------------------------------------------------------------------
# Production training:  pull historical games + closing lines from the DB,
# fit per-league regressors, and register them.  This is what makes the
# regressor non-trivial in prod — without this the orchestrator only ever
# sees the cold-start defaults.
# ---------------------------------------------------------------------------


# How many recent finished games to train on per league.  Generous on
# baseball (162-game season) and tighter on football (17-game season)
# so we keep ~2 seasons of signal everywhere without starving the fit.
_TRAIN_LIMIT_BY_LEAGUE: dict[str, int] = {
    "NBA": 2500,
    "NFL": 600,
    "MLB": 5000,
    "NHL": 2500,
}


# Sport-code → league code.  The `games` table is keyed by lowercase
# sport while every other table uses the upper-case league code, so we
# map between them once here.
_SPORT_TO_LEAGUE: dict[str, str] = {
    "basketball_nba": "NBA",
    "americanfootball_nfl": "NFL",
    "baseball_mlb": "MLB",
    "icehockey_nhl": "NHL",
    "nba": "NBA",
    "nfl": "NFL",
    "mlb": "MLB",
    "nhl": "NHL",
}
_LEAGUE_TO_SPORT_KEYS: dict[str, tuple[str, ...]] = {
    "NBA": ("basketball_nba", "nba"),
    "NFL": ("americanfootball_nfl", "nfl"),
    "MLB": ("baseball_mlb", "mlb"),
    "NHL": ("icehockey_nhl", "nhl"),
}


# `line_snapshots.market_type` carries odds-api naming.  Map each
# canonical market to the candidate strings we'll see on the wire.
_SPREAD_MARKET_KEYS = ("spreads", "spread", "spread_line")
_TOTAL_MARKET_KEYS = ("totals", "total", "total_line", "over_under")


async def load_training_games(
    league: str,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Pull finished games + their closing spread/total lines for a league.

    Joins `games` (canonical scores) with the most recent home-side
    `line_snapshots` row per (game_id, market_type) so each training
    row carries (home_score, away_score, spread_line, total_line)
    along with the elo features needed by `extract_features()`.

    Returns a list of plain dicts ready for `fit_regressor()` /
    `backtest_spread()`.  Returns [] (without raising) when the DB
    isn't reachable so callers can keep running on cold-start
    defaults.
    """
    from . import db, elo as elo_mod

    league = (league or "").strip().upper()
    if league not in SUPPORTED_LEAGUES:
        return []
    sport_keys = _LEAGUE_TO_SPORT_KEYS.get(league, ())
    if not sport_keys:
        return []
    cap = int(limit if limit is not None else _TRAIN_LIMIT_BY_LEAGUE.get(league, 2000))

    sql = """
    WITH finished AS (
        SELECT game_id, sport, league, home_team, away_team,
               scheduled_start, home_score, away_score
          FROM games
         WHERE (league = $1 OR sport = ANY($2::text[]))
           AND status IN ('closed', 'final', 'complete', 'completed')
           AND home_score IS NOT NULL
           AND away_score IS NOT NULL
         ORDER BY scheduled_start DESC
         LIMIT $3
    ),
    spread_lines AS (
        SELECT DISTINCT ON (ls.game_id)
               ls.game_id,
               ls.line_value AS spread_line,
               ls.timestamp  AS line_ts
          FROM line_snapshots ls
          JOIN finished f USING (game_id)
         WHERE ls.market_type = ANY($4::text[])
         ORDER BY ls.game_id, ls.timestamp DESC
    ),
    total_lines AS (
        SELECT DISTINCT ON (ls.game_id)
               ls.game_id,
               ls.line_value AS total_line
          FROM line_snapshots ls
          JOIN finished f USING (game_id)
         WHERE ls.market_type = ANY($5::text[])
         ORDER BY ls.game_id, ls.timestamp DESC
    )
    SELECT f.game_id, f.home_team, f.away_team, f.scheduled_start,
           f.home_score, f.away_score,
           sl.spread_line, tl.total_line
      FROM finished f
      LEFT JOIN spread_lines sl ON sl.game_id = f.game_id
      LEFT JOIN total_lines  tl ON tl.game_id = f.game_id
     ORDER BY f.scheduled_start ASC
    """
    try:
        rows = await db.fetch(
            sql, league, list(sport_keys), cap,
            list(_SPREAD_MARKET_KEYS), list(_TOTAL_MARKET_KEYS),
        )
    except Exception as e:
        log.warning("margin_regressor.load_training_games(%s) failed: %s", league, e)
        return []

    games: list[dict[str, Any]] = []
    for r in rows:
        try:
            home = r["home_team"]
            away = r["away_team"]
            home_rating = INITIAL_RATING_FALLBACK
            away_rating = INITIAL_RATING_FALLBACK
            if home and away:
                try:
                    h = await elo_mod.get_rating(league, str(home))
                    a = await elo_mod.get_rating(league, str(away))
                    home_rating = h.rating
                    away_rating = a.rating
                except Exception:
                    pass
            games.append({
                "league": league,
                "game_id": str(r["game_id"]),
                "home_team": home,
                "away_team": away,
                "home_score": float(r["home_score"]),
                "away_score": float(r["away_score"]),
                "spread_line": (
                    float(r["spread_line"]) if r["spread_line"] is not None else None
                ),
                "total_line": (
                    float(r["total_line"]) if r["total_line"] is not None else None
                ),
                "elo_diff": float(home_rating) - float(away_rating),
                "home_elo": float(home_rating),
                "opponent_elo": float(away_rating),
                "recent_form_delta": 0.0,
                "opponent_recent_form_delta": 0.0,
            })
        except Exception:
            continue
    return games


# Pulled out as a module constant so tests can monkeypatch it.
INITIAL_RATING_FALLBACK = 1500.0


@dataclass
class RefitSummary:
    fitted: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    blend_weights: dict[str, float] = field(default_factory=dict)
    started_at: str = ""
    completed_at: str = ""
    duration_seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "fitted": dict(self.fitted),
            "skipped": dict(self.skipped),
            "blend_weights": {
                k: round(v, 4) for k, v in self.blend_weights.items()
            },
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_seconds": round(self.duration_seconds, 3),
        }


async def refit_all_leagues(
    *, min_games: int = 50,
) -> RefitSummary:
    """Refresh the in-process regressors from the DB for every league.

    Skips a league when fewer than `min_games` finished rows are
    available — the cold-start defaults are safer than a tiny fit.
    Designed to be called periodically by the settlement scheduler.

    Each successful fit is persisted to the ``calibration_snapshots``
    table (sub_category ``margin_regressor:LEAGUE``) so a process
    restart picks up the fitted coefficients via ``load_all()`` rather
    than dropping back to the textbook fallback SDs.
    """
    import asyncio
    import time as _time
    from datetime import datetime, timezone

    summary = RefitSummary(started_at=datetime.now(timezone.utc).isoformat())
    t0 = _time.perf_counter()
    for league in sorted(SUPPORTED_LEAGUES):
        try:
            games = await load_training_games(league)
        except Exception as e:  # pragma: no cover — defensive
            summary.skipped[league] = f"load_failed: {type(e).__name__}: {e}"
            continue
        if len(games) < int(min_games):
            summary.skipped[league] = f"only {len(games)} games (need {min_games})"
            continue
        try:
            # ``fit_regressor`` is pure-numpy CPU work — offload to a
            # thread so the asyncio event loop keeps serving /api/healthz
            # (the production watchdog SIGKILLs the process after three
            # 30-second probe timeouts).
            reg = await asyncio.to_thread(fit_regressor, league, games)
        except Exception as e:
            summary.skipped[league] = f"fit_failed: {type(e).__name__}: {e}"
            continue
        fit_at = datetime.now(timezone.utc).isoformat()
        register_regressor(reg, fit_at=fit_at)
        # Best-effort persistence — a DB blip must not abort the refit
        # cycle; the in-process registry is already updated above so
        # the live cycle picks up the new coefficients regardless.
        try:
            await _persist_fit(reg, fit_at=fit_at)
        except Exception as e:  # pragma: no cover — defensive
            log.warning("margin_regressor persist failed for %s: %s", league, e)
        summary.fitted[league] = reg.n_train
        # Now that the regressor is fitted, sweep candidate blend
        # weights through the same training games and pin the one
        # that minimises holdout Brier.  Failures here only fall
        # back to the per-league default — never block the refit.
        # ``tune_blend_weight`` runs a multi-weight grid backtest in
        # numpy — easily multi-second per league.  Offload alongside
        # ``fit_regressor`` to keep the event loop responsive.
        try:
            tune = await asyncio.to_thread(tune_blend_weight, league, games)
        except Exception as e:  # pragma: no cover — defensive
            log.warning("blend-weight tune failed for %s: %s", league, e)
            tune = None
        if tune is not None:
            register_blend_weight(league, tune.weight)
            summary.blend_weights[league] = tune.weight
        else:
            # Tuning was a no-op (e.g. not enough valid rows after the
            # spread-line filter).  Explicitly register the cold-start
            # default so a stale tuned weight from a prior refit doesn't
            # silently keep driving live inference while the summary
            # reports the default.
            default_w = _DEFAULT_BLEND_WEIGHT_BY_LEAGUE.get(league, 0.5)
            register_blend_weight(league, default_w)
            summary.blend_weights[league] = default_w
    summary.completed_at = datetime.now(timezone.utc).isoformat()
    summary.duration_seconds = _time.perf_counter() - t0
    log.info(
        "margin_regressor refit: fitted=%s skipped=%s blend_weights=%s duration=%.2fs",
        summary.fitted, summary.skipped, summary.blend_weights,
        summary.duration_seconds,
    )
    return summary


# ---------------------------------------------------------------------------
# Persistence (calibration_snapshots)
# ---------------------------------------------------------------------------


# We piggy-back on the existing `calibration_snapshots` table — same
# pattern used by `app/calibrators.py` for per-league probability
# calibrators — so we don't have to ship a new migration just for
# four small JSON blobs.  The `sub_category` namespace keeps the
# margin-regressor rows from colliding with the per-league
# calibrator rows (`per_league:NBA` vs `margin_regressor:NBA`).
_SNAPSHOT_VERTICAL = "sports"
_SUB_CATEGORY_PREFIX = "margin_regressor:"


def _serialize(reg: LeagueScoreRegressor, *, fit_at: str) -> dict[str, Any]:
    return {
        "type": "margin_regressor",
        "fit_at": fit_at,
        "n_train": int(reg.n_train),
        "margin_coef": [float(c) for c in reg.margin_coef],
        "total_coef": [float(c) for c in reg.total_coef],
        "margin_sd": float(reg.margin_sd),
        "total_sd": float(reg.total_sd),
        "feature_names": list(reg.feature_names),
    }


def _deserialize(league: str, blob: Any) -> LeagueScoreRegressor | None:
    """Rehydrate a persisted regressor.  Returns None on malformed input
    so a corrupt snapshot can't take down the loader."""
    import json as _json

    if isinstance(blob, str):
        try:
            blob = _json.loads(blob)
        except Exception:
            return None
    if not isinstance(blob, Mapping):
        return None
    if str(blob.get("type") or "").lower() != "margin_regressor":
        return None
    try:
        margin_coef = [float(c) for c in (blob.get("margin_coef") or [])]
        total_coef = [float(c) for c in (blob.get("total_coef") or [])]
        margin_sd = float(blob.get("margin_sd") or _DEFAULT_MARGIN_SD.get(league, 12.0))
        total_sd = float(blob.get("total_sd") or _DEFAULT_TOTAL_SD.get(league, 12.0))
        n_train = int(blob.get("n_train") or 0)
    except (TypeError, ValueError):
        return None
    if not margin_coef or not total_coef:
        return None
    return LeagueScoreRegressor(
        league=league,
        margin_coef=margin_coef,
        total_coef=total_coef,
        margin_sd=margin_sd,
        total_sd=total_sd,
        n_train=n_train,
    )


async def _persist_fit(reg: LeagueScoreRegressor, *, fit_at: str) -> int | None:
    """Append one ``calibration_snapshots`` row carrying the fit blob."""
    from . import db
    import json as _json

    state = _serialize(reg, fit_at=fit_at)
    sub_cat = f"{_SUB_CATEGORY_PREFIX}{reg.league}"
    try:
        snap_id = await db.fetchval(
            """
            INSERT INTO calibration_snapshots
                (vertical, sub_category, ece, mce, brier_score, reliability,
                 resolution, sample_count, calibrator_state, drift_status, config)
            VALUES ($1, $2, NULL, NULL, NULL, NULL, NULL, $3,
                    $4::jsonb,
                    jsonb_build_object('status', 'fitted',
                                       'kind', 'margin_regressor'),
                    jsonb_build_object('source', 'margin_regressor_refit',
                                       'feature_names', $5::jsonb))
            RETURNING id
            """,
            _SNAPSHOT_VERTICAL,
            sub_cat,
            int(reg.n_train),
            _json.dumps(state),
            _json.dumps(list(reg.feature_names)),
        )
        return int(snap_id) if snap_id is not None else None
    except Exception as e:
        log.warning("margin_regressor snapshot insert failed for %s: %s",
                    reg.league, e)
        return None


async def load_all() -> dict[str, str]:
    """Load the most-recent persisted fit per league into the registry.

    Idempotent — callers may invoke this at startup and again after a
    fresh refit to pick up the new coefficients.  Failures are
    swallowed and logged so a flaky DB at boot can't block the API
    server from coming up; the registry simply stays empty and the
    cold-start defaults take over.

    Returns ``{league: fit_at}`` for every league that successfully
    hydrated, mostly so tests + admin tooling can assert what landed.
    """
    from . import db

    loaded: dict[str, str] = {}
    try:
        rows = await db.fetch(
            """
            SELECT DISTINCT ON (sub_category)
                   sub_category,
                   calibrator_state,
                   sample_count,
                   created_at
              FROM calibration_snapshots
             WHERE vertical = $1
               AND sub_category LIKE $2
               AND calibrator_state IS NOT NULL
               AND calibrator_state::text <> '{}'
               AND COALESCE(drift_status->>'status', '') <> 'rolled_back'
             ORDER BY sub_category, created_at DESC
            """,
            _SNAPSHOT_VERTICAL,
            f"{_SUB_CATEGORY_PREFIX}%",
        )
    except Exception as e:
        log.warning("margin_regressor load_all skipped: %s", e)
        return loaded

    for r in rows:
        sub = str(r["sub_category"] or "")
        if not sub.startswith(_SUB_CATEGORY_PREFIX):
            continue
        league = sub[len(_SUB_CATEGORY_PREFIX):].strip().upper()
        if league not in SUPPORTED_LEAGUES:
            continue
        reg = _deserialize(league, r["calibrator_state"])
        if reg is None:
            continue
        # Prefer the snapshot's serialized fit_at; fall back to
        # created_at so even legacy rows surface a usable timestamp.
        fit_at: str | None = None
        try:
            blob = r["calibrator_state"]
            if isinstance(blob, str):
                import json as _json
                blob = _json.loads(blob)
            if isinstance(blob, Mapping):
                v = blob.get("fit_at")
                if isinstance(v, str) and v.strip():
                    fit_at = v.strip()
        except Exception:
            fit_at = None
        if fit_at is None:
            created = r["created_at"]
            try:
                fit_at = (
                    created.isoformat() if hasattr(created, "isoformat")
                    else str(created)
                )
            except Exception:
                fit_at = None
        register_regressor(reg, fit_at=fit_at)
        if fit_at:
            loaded[league] = fit_at
    log.info(
        "margin_regressor load_all: loaded %d/%d leagues (%s)",
        len(loaded), len(SUPPORTED_LEAGUES),
        ",".join(sorted(loaded.keys())) or "(none)",
    )
    return loaded


# ---------------------------------------------------------------------------
# Live-cycle line enrichment:  attach the latest spread/total line from
# `line_snapshots` onto a candidate's `game_context` so adjust_probability
# has a line to work with.  Mirrors the `attach_elo_to_candidate` helper
# the orchestrator already runs.
# ---------------------------------------------------------------------------


async def attach_lines_to_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    """Best-effort: fill in `spread_line` / `total_line` on game_context.

    Looks up the most recent home-side line per market_type for the
    candidate's game_id.  Silently no-ops when game_id is missing,
    the line table is empty for the game, or the DB is unreachable —
    the orchestrator must never fail on a missing line.
    """
    from . import db

    league = (candidate.get("league") or "").strip().upper()
    if league not in SUPPORTED_LEAGUES:
        return candidate
    market_type = (candidate.get("market_type") or "").strip()
    if market_type not in {"Spread", "Total"}:
        return candidate

    ctx = candidate.get("game_context")
    if isinstance(ctx, str):
        try:
            import json as _json
            ctx = _json.loads(ctx)
        except Exception:
            ctx = None
    if not isinstance(ctx, dict):
        ctx = {}

    game_id = (
        candidate.get("game_id")
        or ctx.get("game_id")
        or ctx.get("sportradar_game_id")
    )
    if not game_id:
        return candidate

    # Don't refetch if the candidate / context already carries a line.
    if market_type == "Spread" and any(
        ctx.get(k) is not None or candidate.get(k) is not None
        for k in ("spread_line", "spread", "home_spread", "line")
    ):
        return candidate
    if market_type == "Total" and any(
        ctx.get(k) is not None or candidate.get(k) is not None
        for k in ("total_line", "total", "over_under", "line")
    ):
        return candidate

    market_keys = (
        list(_SPREAD_MARKET_KEYS) if market_type == "Spread" else list(_TOTAL_MARKET_KEYS)
    )
    try:
        row = await db.fetchrow(
            """
            SELECT line_value
              FROM line_snapshots
             WHERE game_id = $1
               AND market_type = ANY($2::text[])
               AND line_value IS NOT NULL
             ORDER BY timestamp DESC
             LIMIT 1
            """,
            str(game_id), market_keys,
        )
    except Exception as e:
        log.debug("attach_lines_to_candidate failed for %s: %s", game_id, e)
        return candidate
    if row is None or row["line_value"] is None:
        # Live ``line_snapshots`` is sparse for V1-era games; fall back
        # to the V1 archive so historical replays still get a line.
        from .v1_archive import features as _v1
        v1_row = await _v1.latest_line_for_game(str(game_id), market_keys)
        if v1_row is None:
            return candidate
        field_name = "spread_line" if market_type == "Spread" else "total_line"
        ctx[field_name] = v1_row["line_value"]
        ctx[f"{field_name}_source"] = "v1_archive"
        candidate["game_context"] = ctx
        return candidate

    field_name = "spread_line" if market_type == "Spread" else "total_line"
    ctx[field_name] = float(row["line_value"])
    candidate["game_context"] = ctx
    return candidate


__all__ = [
    "SUPPORTED_LEAGUES",
    "FEATURE_NAMES",
    "LeagueScoreRegressor",
    "BacktestResult",
    "BlendWeightTuneResult",
    "RefitSummary",
    "extract_features",
    "fit_regressor",
    "register_regressor",
    "get_regressor",
    "register_blend_weight",
    "get_blend_weight",
    "reset_registry",
    "status",
    "load_all",
    "adjust_probability",
    "backtest_spread",
    "tune_blend_weight",
    "load_training_games",
    "refit_all_leagues",
    "attach_lines_to_candidate",
]
