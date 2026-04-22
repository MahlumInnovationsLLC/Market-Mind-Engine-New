"""Register every production feature against :data:`registry`.

This file is the only place feature columns should be added.  Each
``register(...)`` call appends one slot to the meta-model's vector — keep
their order aligned with the trained model, and update the matching
``models/saved/meta_model.pkl`` schema before changing any name.

The transforms here mirror, line-for-line, the original inline builder
that lived in ``app/orchestrator/features.py`` so flipping the predictor
to read through the registry is an O(0) behaviour change.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from .registry import BuildContext, FeatureSpec, registry


# ---------------------------------------------------------------------------
# Categorical lookup tables — KEEP IN SYNC with the trainer's
# ``predict_with_meta_model`` so league/market_type indices match the
# column the model was fit on.
# ---------------------------------------------------------------------------

_LEAGUE_MAP: dict[str, int] = {
    "NBA": 0, "NFL": 1, "NHL": 2, "MLB": 3,
    "NCAAB": 4, "PGA": 5, "MLS": 6, "Soccer": 7,
}
_TYPE_MAP: dict[str, int] = {
    "Winner": 0, "Total": 1, "Spread": 2, "Points": 3,
    "Assists": 4, "Rebounds": 5, "3-Pointers": 6, "Market": 7,
}


# ---------------------------------------------------------------------------
# Coercion helpers — careful about ``None`` vs ``0.0``: the trainer
# treats a legitimate zero (e.g. a market that just printed at 0¢) as a
# real value, NOT as a missing signal.
# ---------------------------------------------------------------------------


def _coerce_float(v: Any, default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _coerce_bool(v: Any) -> float:
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return 1.0 if v else 0.0
    if isinstance(v, str):
        return 1.0 if v.strip().lower() in {"1", "true", "yes", "y"} else 0.0
    return 0.0


def decode_context(blob: Any) -> dict[str, Any]:
    """``game_context`` may arrive as JSON (DB read) or dict (live).

    Returns ``{}`` for any unparseable payload so callers can ``.get()``
    safely without try/except sprinkled at every site.
    """
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
    matches the feature vector's ``side_val``."""
    explicit = candidate.get("side")
    if isinstance(explicit, str) and explicit.strip():
        return "yes" if explicit.strip().upper() != "NO" else "no"
    action = (candidate.get("recommended_action") or "").upper()
    return "no" if action == "BUY_NO" else "yes"


def _resolve_market_vs_model(
    pred_prob: float, market_prob: float, explicit: Any,
) -> float:
    """Trainer codes 'market_higher' → +1, 'market_lower' → -1, else 0.
    When the upstream feed didn't supply the label, infer it from the
    prob gap so the live vector stays aligned with how labelled training
    rows were built.  ±2pp threshold matches the trainer."""
    if isinstance(explicit, str):
        e = explicit.strip().lower()
        if e == "market_higher":
            return 1.0
        if e == "market_lower":
            return -1.0
        if e in {"aligned", "neutral", ""}:
            return 0.0
    diff = market_prob - pred_prob
    if diff > 0.02:
        return 1.0
    if diff < -0.02:
        return -1.0
    return 0.0


def _mark_present(bc: BuildContext, key: str, raw_value: Any) -> None:
    """Track that ``key`` was supplied by the upstream feed.

    Stored as a float so the operator console can render it inline; for
    non-numeric categoricals we store ``1.0`` to mark "present, opaque".
    """
    if raw_value is None:
        return
    try:
        bc.present[key] = float(raw_value)
    except (TypeError, ValueError):
        bc.present[key] = 1.0


# ---------------------------------------------------------------------------
# Per-feature transforms
#
# Each ``_*`` function returns a single float for one BuildContext.  We
# keep them top-level (rather than lambdas) so tracebacks point at a
# named transform when something goes wrong.
# ---------------------------------------------------------------------------


def _t_pred_prob(bc: BuildContext) -> float:
    if bc.overrides.get("consensus") is not None:
        src: Any = bc.overrides["consensus"]
    else:
        src = _lookup(bc.candidate, bc.ctx, key="consensus_probability")
        if src is None:
            src = _lookup(bc.candidate, bc.ctx, key="home_win_prob")
        if src is None:
            src = _lookup(bc.candidate, bc.ctx, key="pred_prob")
    _mark_present(bc, "consensus_probability",
                  _lookup(bc.candidate, bc.ctx, key="consensus_probability"))
    _mark_present(bc, "home_win_prob",
                  _lookup(bc.candidate, bc.ctx, key="home_win_prob"))
    return _coerce_float(src, default=0.5)


def _t_market_prob(bc: BuildContext) -> float:
    if bc.overrides.get("current_price") is not None:
        src: Any = bc.overrides["current_price"]
    else:
        src = _lookup(bc.candidate, bc.ctx, key="kalshi_price")
        if src is None:
            src = _lookup(bc.candidate, bc.ctx, key="market_prob")
    _mark_present(bc, "kalshi_price",
                  _lookup(bc.candidate, bc.ctx, key="kalshi_price"))
    return _coerce_float(src, default=0.5)


def _t_edge(bc: BuildContext) -> float:
    pred = bc.computed["pred_prob"]
    mkt = bc.computed["market_prob"]
    if bc.overrides.get("edge") is not None:
        return _coerce_float(bc.overrides["edge"], default=pred - mkt)
    src = _lookup(bc.candidate, bc.ctx, key="edge")
    _mark_present(bc, "edge", src)
    return _coerce_float(src, default=pred - mkt)


def _t_signal_strength(bc: BuildContext) -> float:
    src = _lookup(bc.candidate, bc.ctx, key="signal_strength")
    if src is None:
        src = _lookup(bc.candidate, bc.ctx, key="agreement_rate")
    _mark_present(bc, "agreement_rate",
                  _lookup(bc.candidate, bc.ctx, key="agreement_rate"))
    return _coerce_float(src, default=0.0)


def _t_kelly(bc: BuildContext) -> float:
    k = bc.overrides.get("kelly")
    if k is None:
        return 0.25
    return _coerce_float(k, default=0.25)


def _t_hour_norm(bc: BuildContext) -> float:
    return bc.now.hour / 24.0


def _t_side_val(bc: BuildContext) -> float:
    return 1.0 if _resolve_side(bc.candidate) == "yes" else 0.0


def _t_league_idx_norm(bc: BuildContext) -> float:
    league_key = (bc.candidate.get("league") or bc.ctx.get("league") or "")
    _mark_present(bc, "league", bc.candidate.get("league") or bc.ctx.get("league"))
    return _LEAGUE_MAP.get(str(league_key), 7) / 7.0


def _t_market_type_idx_norm(bc: BuildContext) -> float:
    type_key = (bc.candidate.get("market_type") or bc.ctx.get("market_type") or "")
    _mark_present(bc, "market_type",
                  bc.candidate.get("market_type") or bc.ctx.get("market_type"))
    return _TYPE_MAP.get(str(type_key), 7) / 7.0


def _t_sharp_detected(bc: BuildContext) -> float:
    return _coerce_bool(_lookup(bc.candidate, bc.ctx, key="sharp_money_detected"))


def _t_sharp_direction(bc: BuildContext) -> float:
    return _coerce_bool(_lookup(bc.candidate, bc.ctx, key="sharp_money_direction"))


def _t_sharp_confidence(bc: BuildContext) -> float:
    return _coerce_float(
        _lookup(bc.candidate, bc.ctx, key="sharp_money_confidence"),
        default=0.0,
    )


def _t_tipster_div(bc: BuildContext) -> float:
    return _coerce_float(
        _lookup(bc.candidate, bc.ctx, key="tipster_divergence_pct"),
        default=0.0,
    ) / 100.0


def _t_market_vs_model(bc: BuildContext) -> float:
    return _resolve_market_vs_model(
        bc.computed["pred_prob"],
        bc.computed["market_prob"],
        _lookup(bc.candidate, bc.ctx, key="market_vs_model_direction"),
    )


def _t_reserved(_bc: BuildContext) -> float:
    # Two zero-padded slots the trainer left blank; preserved so the
    # downstream column offsets stay aligned.
    return 0.0


def _t_tz_detected(bc: BuildContext) -> float:
    return _coerce_bool(_lookup(bc.candidate, bc.ctx, key="tz_arb_detected"))


def _t_tz_div(bc: BuildContext) -> float:
    return _coerce_float(
        _lookup(bc.candidate, bc.ctx, key="tz_arb_divergence_pct"),
        default=0.0,
    ) / 100.0


def _t_tz_conf(bc: BuildContext) -> float:
    return _coerce_float(
        _lookup(bc.candidate, bc.ctx, key="tz_arb_confidence"),
        default=0.0,
    )


def _t_tz_prime(bc: BuildContext) -> float:
    return _coerce_bool(_lookup(bc.candidate, bc.ctx, key="tz_arb_in_prime_window"))


def _t_ev_agreement(bc: BuildContext) -> float:
    return _coerce_float(
        _lookup(bc.candidate, bc.ctx, key="ev_source_agreement"),
        default=0.0,
    )


def _t_ev_spread(bc: BuildContext) -> float:
    return _coerce_float(
        _lookup(bc.candidate, bc.ctx, key="ev_source_spread"),
        default=0.0,
    )


def _t_ev_int_vs_mean(bc: BuildContext) -> float:
    return _coerce_float(
        _lookup(bc.candidate, bc.ctx, key="ev_source_internal_vs_mean"),
        default=0.0,
    )


def _t_ev_count(bc: BuildContext) -> float:
    return min(
        1.0,
        _coerce_float(
            _lookup(bc.candidate, bc.ctx, key="ev_source_count"), default=0.0,
        ) / 5.0,
    )


def _t_prob_edge(bc: BuildContext) -> float:
    return bc.computed["pred_prob"] * bc.computed["edge"]


def _t_prob_signal(bc: BuildContext) -> float:
    return bc.computed["pred_prob"] * bc.computed["signal_strength"]


def _t_edge_abs(bc: BuildContext) -> float:
    return abs(bc.computed["edge"])


def _t_prob_dev(bc: BuildContext) -> float:
    return abs(bc.computed["pred_prob"] - 0.5)


def _t_market_dev(bc: BuildContext) -> float:
    return abs(bc.computed["market_prob"] - 0.5)


def _t_prob_market_gap(bc: BuildContext) -> float:
    return bc.computed["pred_prob"] - bc.computed["market_prob"]


def _t_is_weekend(bc: BuildContext) -> float:
    return 1.0 if bc.now.weekday() >= 5 else 0.0


def _t_is_prime(bc: BuildContext) -> float:
    return 1.0 if 14 <= bc.now.hour <= 23 else 0.0


# Internal Elo signals — not part of the meta-model's fixed feature
# vector, but we still want them on the ``present`` map so sports-
# specific predictors and the dashboard can read them.  Done by the
# pseudo-feature ``_t_track_elo`` after the regular slots; it returns
# 0.0 and is dropped before vector emission (see :func:`build_vector`).
_ELO_KEYS = (
    "team_elo", "opponent_elo", "elo_diff",
    "recent_form_delta", "opponent_recent_form_delta",
)


def track_elo_inputs(bc: BuildContext) -> None:
    """Populate ``bc.present`` with any Elo-style signals the upstream
    feed attached to the candidate."""
    for k in _ELO_KEYS:
        _mark_present(bc, k, _lookup(bc.candidate, bc.ctx, key=k))


# ---------------------------------------------------------------------------
# Registration — the order here defines the model's column order.
# ---------------------------------------------------------------------------


_SPECS: tuple[FeatureSpec, ...] = (
    FeatureSpec("pred_prob",
                source="candidate.consensus_probability|home_win_prob|pred_prob",
                transform="float, default 0.5",
                dtype="prob", default=0.5, compute=_t_pred_prob),
    FeatureSpec("market_prob",
                source="candidate.kalshi_price|market_prob",
                transform="float, default 0.5",
                dtype="prob", default=0.5, compute=_t_market_prob),
    FeatureSpec("edge",
                source="candidate.edge or pred_prob - market_prob",
                transform="float, default = pred_prob - market_prob",
                dtype="signed_unit", default=0.0, compute=_t_edge),
    FeatureSpec("signal_strength",
                source="candidate.signal_strength|agreement_rate",
                transform="float, default 0.0",
                dtype="unit", default=0.0, compute=_t_signal_strength),
    FeatureSpec("kelly",
                source="override.kelly",
                transform="float, default 0.25",
                dtype="unit", default=0.25, compute=_t_kelly),
    FeatureSpec("hour_norm",
                source="time.utc.hour",
                transform="hour / 24",
                dtype="unit", default=0.0, compute=_t_hour_norm),
    FeatureSpec("side_val",
                source="candidate.side|recommended_action",
                transform="yes -> 1.0, no -> 0.0",
                dtype="bool01", default=1.0, compute=_t_side_val),
    FeatureSpec("league_idx_norm",
                source="candidate.league|ctx.league",
                transform="LEAGUE_MAP[league] / 7 (default 7/7)",
                dtype="idx_norm", default=1.0, compute=_t_league_idx_norm),
    FeatureSpec("market_type_idx_norm",
                source="candidate.market_type|ctx.market_type",
                transform="TYPE_MAP[market_type] / 7 (default 7/7)",
                dtype="idx_norm", default=1.0, compute=_t_market_type_idx_norm),
    FeatureSpec("sharp_money_detected",
                source="candidate.sharp_money_detected",
                transform="truthy -> 1.0",
                dtype="bool01", default=0.0, compute=_t_sharp_detected),
    FeatureSpec("sharp_money_direction",
                source="candidate.sharp_money_direction",
                transform="truthy -> 1.0",
                dtype="bool01", default=0.0, compute=_t_sharp_direction),
    FeatureSpec("sharp_money_confidence",
                source="candidate.sharp_money_confidence",
                transform="float, default 0.0",
                dtype="unit", default=0.0, compute=_t_sharp_confidence),
    FeatureSpec("tipster_divergence_frac",
                source="candidate.tipster_divergence_pct",
                transform="value / 100",
                dtype="unit", default=0.0, compute=_t_tipster_div),
    FeatureSpec("market_vs_model_val",
                source="candidate.market_vs_model_direction or inferred",
                transform="market_higher -> +1, market_lower -> -1, else 0; ±2pp threshold",
                dtype="signed_unit", default=0.0, compute=_t_market_vs_model),
    FeatureSpec("reserved_0",
                source="reserved", transform="constant 0.0",
                dtype="raw", default=0.0, compute=_t_reserved),
    FeatureSpec("reserved_1",
                source="reserved", transform="constant 0.0",
                dtype="raw", default=0.0, compute=_t_reserved),
    FeatureSpec("tz_arb_detected",
                source="candidate.tz_arb_detected",
                transform="truthy -> 1.0",
                dtype="bool01", default=0.0, compute=_t_tz_detected),
    FeatureSpec("tz_arb_divergence_frac",
                source="candidate.tz_arb_divergence_pct",
                transform="value / 100",
                dtype="unit", default=0.0, compute=_t_tz_div),
    FeatureSpec("tz_arb_confidence",
                source="candidate.tz_arb_confidence",
                transform="float, default 0.0",
                dtype="unit", default=0.0, compute=_t_tz_conf),
    FeatureSpec("tz_arb_in_prime_window",
                source="candidate.tz_arb_in_prime_window",
                transform="truthy -> 1.0",
                dtype="bool01", default=0.0, compute=_t_tz_prime),
    FeatureSpec("ev_source_agreement",
                source="candidate.ev_source_agreement",
                transform="float, default 0.0",
                dtype="unit", default=0.0, compute=_t_ev_agreement),
    FeatureSpec("ev_source_spread",
                source="candidate.ev_source_spread",
                transform="float, default 0.0",
                dtype="unit", default=0.0, compute=_t_ev_spread),
    FeatureSpec("ev_source_internal_vs_mean",
                source="candidate.ev_source_internal_vs_mean",
                transform="float, default 0.0",
                dtype="signed_unit", default=0.0, compute=_t_ev_int_vs_mean),
    FeatureSpec("ev_source_count_norm",
                source="candidate.ev_source_count",
                transform="min(1, count / 5)",
                dtype="unit", default=0.0, compute=_t_ev_count),
    FeatureSpec("prob_edge",
                source="derived(pred_prob, edge)",
                transform="pred_prob * edge",
                dtype="signed_unit", default=0.0, compute=_t_prob_edge),
    FeatureSpec("prob_signal",
                source="derived(pred_prob, signal_strength)",
                transform="pred_prob * signal_strength",
                dtype="unit", default=0.0, compute=_t_prob_signal),
    FeatureSpec("edge_abs",
                source="derived(edge)",
                transform="abs(edge)",
                dtype="unit", default=0.0, compute=_t_edge_abs),
    FeatureSpec("prob_dev",
                source="derived(pred_prob)",
                transform="abs(pred_prob - 0.5)",
                dtype="unit", default=0.0, compute=_t_prob_dev),
    FeatureSpec("market_dev",
                source="derived(market_prob)",
                transform="abs(market_prob - 0.5)",
                dtype="unit", default=0.0, compute=_t_market_dev),
    FeatureSpec("prob_market_gap",
                source="derived(pred_prob, market_prob)",
                transform="pred_prob - market_prob",
                dtype="signed_unit", default=0.0, compute=_t_prob_market_gap),
    FeatureSpec("is_weekend",
                source="time.utc.weekday",
                transform="weekday >= 5 -> 1.0",
                dtype="bool01", default=0.0, compute=_t_is_weekend),
    FeatureSpec("is_prime",
                source="time.utc.hour",
                transform="14 <= hour <= 23 -> 1.0",
                dtype="bool01", default=0.0, compute=_t_is_prime),
)


for _spec in _SPECS:
    registry.register(_spec)
