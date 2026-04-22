"""Live feature builder for the meta-model.

Mirrors the training-time feature construction in
`scripts/.../model_trainer.py::predict_with_meta_model` so the
canonical predictor (`models/saved/meta_model.pkl`) sees the *same*
32-element vector during live trading that it saw during training.

The cycle hands us the candidate dict it built from `_cached_debates`
or `_candidate_predictions` (plus the swarm's current consensus).  We
extract the raw signals, fall back through `game_context` for fields
that hitched a ride with the debate row, and emit:

    FeatureVector(
        names = (..32 names..),
        values = [..32 floats..],
    )

The vector is aligned positionally with the training schema.  If any
candidate is missing a signal we substitute the same default the
training-time code used (typically 0.0 / 0.5) so the model never sees
NaN.  Categorical fields (`league`, `market_type`, `side`) are mapped
through the same lookup tables the trainer used.

The module is intentionally pure / sync — the cycle already hops to a
worker thread for the actual `predict_proba` call, so all this needs to
do is build a vector quickly and deterministically.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping


# Training-time enum tables.  KEEP IN SYNC with
# `predict_with_meta_model()` in src/ml_training/model_trainer.py — the
# meta-model hard-codes these indices in its decision boundaries.
_LEAGUE_MAP: dict[str, int] = {
    "NBA": 0, "NFL": 1, "NHL": 2, "MLB": 3,
    "NCAAB": 4, "PGA": 5, "MLS": 6, "Soccer": 7,
}
_TYPE_MAP: dict[str, int] = {
    "Winner": 0, "Total": 1, "Spread": 2, "Points": 3,
    "Assists": 4, "Rebounds": 5, "3-Pointers": 6, "Market": 7,
}

# Names (in order) of every slot in the meta-model feature vector.  The
# pair `(0.0, 0.0)` after `mvs_val` keeps two reserved training slots
# that the trainer left zero — we preserve their position so the model
# offsets stay correct.
FEATURE_NAMES: tuple[str, ...] = (
    "pred_prob", "market_prob", "edge", "signal_strength", "kelly",
    "hour_norm", "side_val",
    "league_idx_norm", "market_type_idx_norm",
    "sharp_money_detected", "sharp_money_direction", "sharp_money_confidence",
    "tipster_divergence_frac", "market_vs_model_val",
    "reserved_0", "reserved_1",
    "tz_arb_detected", "tz_arb_divergence_frac", "tz_arb_confidence",
    "tz_arb_in_prime_window",
    "ev_source_agreement", "ev_source_spread", "ev_source_internal_vs_mean",
    "ev_source_count_norm",
    "prob_edge", "prob_signal", "edge_abs",
    "prob_dev", "market_dev", "prob_market_gap",
    "is_weekend", "is_prime",
)
FEATURE_COUNT = len(FEATURE_NAMES)


@dataclass(frozen=True)
class FeatureVector:
    """Result of `build_features()` — names + values aligned positionally."""
    names: tuple[str, ...]
    values: list[float]
    # Subset of named inputs that were actually present on the candidate
    # (vs. defaulted).  Useful for telemetry — lets the dashboard show
    # which signals drove a given live probability.
    present: dict[str, float]

    def as_dict(self) -> dict[str, float]:
        return dict(zip(self.names, self.values))


def _coerce_float(v: Any, default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _coerce_bool(v: Any) -> float:
    """Truthy → 1.0, falsy → 0.0.  Strings 'yes'/'true'/'1' are truthy."""
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return 1.0 if v else 0.0
    if isinstance(v, str):
        return 1.0 if v.strip().lower() in {"1", "true", "yes", "y"} else 0.0
    return 0.0


def _decode_context(blob: Any) -> dict[str, Any]:
    """`game_context` may arrive as a JSON string from a DB read or as
    a live dict.  Always return a dict so callers can `.get()` safely."""
    if isinstance(blob, dict):
        return blob
    if isinstance(blob, str):
        try:
            parsed = json.loads(blob)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _lookup(*sources: Mapping[str, Any], key: str) -> Any:
    """First non-None hit across the supplied dicts wins."""
    for src in sources:
        if not isinstance(src, Mapping):
            continue
        v = src.get(key)
        if v is not None:
            return v
    return None


def _resolve_side(candidate: Mapping[str, Any]) -> str:
    """Match cycle.py's side derivation so the gate's view of side
    matches the feature vector's `side_val`."""
    explicit = candidate.get("side")
    if isinstance(explicit, str) and explicit.strip():
        return "yes" if explicit.strip().upper() != "NO" else "no"
    action = (candidate.get("recommended_action") or "").upper()
    return "no" if action == "BUY_NO" else "yes"


def _resolve_market_vs_model(
    pred_prob: float,
    market_prob: float,
    explicit: Any,
) -> float:
    """Trainer codes 'market_higher' → +1, 'market_lower' → -1, else 0.
    When the feed didn't supply the label we infer it from the prob gap
    so the live vector stays aligned with how trainer rows were built
    when the upstream feed had labelled the divergence."""
    if isinstance(explicit, str):
        e = explicit.strip().lower()
        if e == "market_higher":
            return 1.0
        if e == "market_lower":
            return -1.0
        if e in {"aligned", "neutral", ""}:
            return 0.0
    diff = market_prob - pred_prob
    # Match the trainer's "labelled" thresholding: anything within ±2pp
    # of the model's prob is treated as aligned.
    if diff > 0.02:
        return 1.0
    if diff < -0.02:
        return -1.0
    return 0.0


def build_features(
    candidate: Mapping[str, Any],
    *,
    consensus: float | None = None,
    current_price: float | None = None,
    edge: float | None = None,
    kelly: float | None = None,
    now: datetime | None = None,
) -> FeatureVector:
    """Turn a candidate dict into the meta-model's 32-feature vector.

    The candidate may carry a flattened `game_context` JSON sub-dict
    (the cached debate path) — we look there as a second source for any
    field that isn't on the top level.  Caller-supplied `consensus`,
    `current_price`, `edge`, `kelly` win over whatever's on the dict so
    the cycle can pass the *effective* values it already computed.
    """
    ctx = _decode_context(candidate.get("game_context"))
    now = now or datetime.now(timezone.utc)

    # Pricing + alpha block ----------------------------------------------------
    # Use explicit `is not None` chains rather than Python `or` so a
    # legitimate 0.0 (e.g. a market that just printed at zero) is NOT
    # treated as "missing" and defaulted away.
    if consensus is not None:
        pred_src: Any = consensus
    else:
        pred_src = _lookup(
            candidate, ctx, key="consensus_probability"
        )
        if pred_src is None:
            pred_src = _lookup(candidate, ctx, key="home_win_prob")
        if pred_src is None:
            pred_src = _lookup(candidate, ctx, key="pred_prob")
    pred_prob = _coerce_float(pred_src, default=0.5)

    if current_price is not None:
        market_src: Any = current_price
    else:
        market_src = _lookup(candidate, ctx, key="kalshi_price")
        if market_src is None:
            market_src = _lookup(candidate, ctx, key="market_prob")
    market_prob = _coerce_float(market_src, default=0.5)

    if edge is not None:
        edge_src: Any = edge
    else:
        edge_src = _lookup(candidate, ctx, key="edge")
    edge_v = _coerce_float(edge_src, default=pred_prob - market_prob)

    signal_src = _lookup(candidate, ctx, key="signal_strength")
    if signal_src is None:
        signal_src = _lookup(candidate, ctx, key="agreement_rate")
    signal = _coerce_float(signal_src, default=0.0)
    kelly_v = _coerce_float(kelly, default=0.25) if kelly is not None else 0.25

    # Time + categorical block -------------------------------------------------
    hour = now.hour
    side = _resolve_side(candidate)
    side_val = 1.0 if side == "yes" else 0.0
    league_key = (candidate.get("league") or ctx.get("league") or "")
    league_idx = _LEAGUE_MAP.get(str(league_key), 7)
    type_key = (candidate.get("market_type") or ctx.get("market_type") or "")
    type_idx = _TYPE_MAP.get(str(type_key), 7)

    # Sharp-money block (defaults to "no signal" if upstream feed silent) ------
    sharp_detected = _coerce_bool(_lookup(candidate, ctx, key="sharp_money_detected"))
    sharp_direction = _coerce_bool(_lookup(candidate, ctx, key="sharp_money_direction"))
    sharp_confidence = _coerce_float(
        _lookup(candidate, ctx, key="sharp_money_confidence"), default=0.0
    )

    tipster_div = _coerce_float(
        _lookup(candidate, ctx, key="tipster_divergence_pct"), default=0.0
    ) / 100.0
    mvs_val = _resolve_market_vs_model(
        pred_prob, market_prob,
        _lookup(candidate, ctx, key="market_vs_model_direction"),
    )

    # Time-zone arbitrage block ------------------------------------------------
    tz_detected = _coerce_bool(_lookup(candidate, ctx, key="tz_arb_detected"))
    tz_div = _coerce_float(
        _lookup(candidate, ctx, key="tz_arb_divergence_pct"), default=0.0
    ) / 100.0
    tz_conf = _coerce_float(
        _lookup(candidate, ctx, key="tz_arb_confidence"), default=0.0
    )
    tz_prime = _coerce_bool(_lookup(candidate, ctx, key="tz_arb_in_prime_window"))

    # EV-source-agreement block ------------------------------------------------
    ev_agreement = _coerce_float(
        _lookup(candidate, ctx, key="ev_source_agreement"), default=0.0
    )
    ev_spread = _coerce_float(
        _lookup(candidate, ctx, key="ev_source_spread"), default=0.0
    )
    ev_int_vs_mean = _coerce_float(
        _lookup(candidate, ctx, key="ev_source_internal_vs_mean"), default=0.0
    )
    ev_count = min(
        1.0,
        _coerce_float(_lookup(candidate, ctx, key="ev_source_count"), default=0.0)
        / 5.0,
    )

    # Derived block (must match training expressions exactly) ------------------
    prob_edge = pred_prob * edge_v
    prob_signal = pred_prob * signal
    edge_abs = abs(edge_v)
    prob_dev = abs(pred_prob - 0.5)
    market_dev = abs(market_prob - 0.5)
    prob_market_gap = pred_prob - market_prob
    is_weekend = 1.0 if now.weekday() >= 5 else 0.0
    is_prime = 1.0 if 14 <= hour <= 23 else 0.0

    values: list[float] = [
        pred_prob, market_prob, edge_v, signal, kelly_v,
        hour / 24.0, side_val,
        league_idx / 7.0, type_idx / 7.0,
        sharp_detected, sharp_direction, sharp_confidence,
        tipster_div, mvs_val,
        0.0, 0.0,
        tz_detected, tz_div, tz_conf, tz_prime,
        ev_agreement, ev_spread, ev_int_vs_mean, ev_count,
        prob_edge, prob_signal, edge_abs,
        prob_dev, market_dev, prob_market_gap,
        is_weekend, is_prime,
    ]
    assert len(values) == FEATURE_COUNT, (
        f"feature builder produced {len(values)} values, expected {FEATURE_COUNT}"
    )

    # Track which named inputs were *really* present so callers (and
    # tests) can tell pred_prob=0.5 (default) from pred_prob=0.5 (real).
    present: dict[str, float] = {}
    for key, raw_value in (
        ("consensus_probability", _lookup(candidate, ctx, key="consensus_probability")),
        ("home_win_prob", _lookup(candidate, ctx, key="home_win_prob")),
        ("kalshi_price", _lookup(candidate, ctx, key="kalshi_price")),
        ("edge", _lookup(candidate, ctx, key="edge")),
        ("agreement_rate", _lookup(candidate, ctx, key="agreement_rate")),
        ("league", candidate.get("league") or ctx.get("league")),
        ("market_type", candidate.get("market_type") or ctx.get("market_type")),
        # Internal Elo signals — not part of the meta-model's fixed
        # 32-feature vector (which would break trained-model alignment),
        # but exposed via `present` so sports-specific predictors and
        # the dashboard can read them off the candidate payload.
        ("team_elo", _lookup(candidate, ctx, key="team_elo")),
        ("opponent_elo", _lookup(candidate, ctx, key="opponent_elo")),
        ("elo_diff", _lookup(candidate, ctx, key="elo_diff")),
        ("recent_form_delta", _lookup(candidate, ctx, key="recent_form_delta")),
        ("opponent_recent_form_delta",
         _lookup(candidate, ctx, key="opponent_recent_form_delta")),
    ):
        if raw_value is not None:
            try:
                present[key] = float(raw_value)
            except (TypeError, ValueError):
                # Categoricals — store as 1.0 to mark "present, non-numeric".
                present[key] = 1.0

    return FeatureVector(
        names=FEATURE_NAMES,
        values=values,
        present=present,
    )
