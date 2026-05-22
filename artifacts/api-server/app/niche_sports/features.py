"""Per-sport feature builders for the niche individual-sport verticals
(task #831).

Every builder takes a free-form candidate dict and returns a tuple of
``(names, values)`` where ``len(names) == len(values)`` and every value
is a float in a finite range.  Null-safety is the headline contract:
a builder MUST NOT raise on a missing field, MUST NOT emit ``nan`` or
``inf``, and MUST emit a consistent column order so the feature
pipeline can append the vector to the meta-model row without per-call
schema drift.

Categorical inputs (court surface, circuit category, weight class,
course green type) are **one-hot encoded** — never used as raw
strings — per the architectural constraint in the task brief.

Each builder is intentionally a stub: the value semantics line up
with the per-sport edge sources the task brief describes, but the
defaults are deterministic placeholders (``0.0`` for differences,
``0.5`` for normalised rates) so the calibrator scaffold can persist
identity-Platt snapshots before the real backfill lands.  Once
:mod:`app.niche_sports.calibrator_scaffold` is re-fit against the
populated ``niche_sport_outcomes`` table, the same builders feed the
real model — the column order is the trained-model contract.
"""
from __future__ import annotations

from typing import Any


_TENNIS_SURFACES: tuple[str, ...] = ("hard", "clay", "grass", "carpet")
_F1_CIRCUIT_KINDS: tuple[str, ...] = ("street", "permanent", "hybrid")
_PGA_GREEN_TYPES: tuple[str, ...] = ("bent", "bermuda", "poa", "other")
_UFC_WEIGHT_CLASSES: tuple[str, ...] = (
    "strawweight",
    "flyweight",
    "bantamweight",
    "featherweight",
    "lightweight",
    "welterweight",
    "middleweight",
    "light_heavyweight",
    "heavyweight",
)


def _as_float(value: Any, default: float = 0.0) -> float:
    """Coerce ``value`` to a finite float, falling back on default.

    Catches ``None``, non-numeric strings, ``NaN`` and ``inf`` so the
    pipeline never propagates a bad feature into the meta-model.
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return float(default)
    if f != f or f in (float("inf"), float("-inf")):
        return float(default)
    return f


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _one_hot(value: Any, categories: tuple[str, ...]) -> list[float]:
    """Return a one-hot vector for ``value`` against ``categories``.

    Unknown / missing values produce an all-zero vector — the
    calibrator treats "unspecified surface" as its own implicit
    bucket rather than guessing.
    """
    norm = str(value or "").strip().lower()
    return [1.0 if norm == c else 0.0 for c in categories]


def build_tennis(cand: dict[str, Any]) -> tuple[list[str], list[float]]:
    """Tennis (ATP/WTA) features.

    Inputs (all optional — defaults are deterministic):
      * ``surface``                 — hard/clay/grass/carpet.
      * ``surface_elo_diff``        — entrant Elo − opponent Elo on
                                      this surface over the last 12mo.
                                      Already normalised to ~[-1, 1]
                                      (divide by 400 if you have raw
                                      Elo points).
      * ``h2h_win_rate``            — fraction of prior meetings the
                                      entrant won (default 0.5).
      * ``recent_round_depth``      — average tournament round reached
                                      in the last 8 events, normalised
                                      to [0, 1] (R128=0.0, final=1.0).
      * ``retirement_rate_3mo``     — fraction of prior 3-month matches
                                      ended in mid-match retirement.
    """
    surface = _one_hot(cand.get("surface"), _TENNIS_SURFACES)
    elo = _clamp(_as_float(cand.get("surface_elo_diff"), 0.0), -2.0, 2.0)
    h2h = _clamp(_as_float(cand.get("h2h_win_rate"), 0.5), 0.0, 1.0)
    depth = _clamp(_as_float(cand.get("recent_round_depth"), 0.5), 0.0, 1.0)
    ret = _clamp(_as_float(cand.get("retirement_rate_3mo"), 0.0), 0.0, 1.0)
    names = [
        "tennis_surface_hard",
        "tennis_surface_clay",
        "tennis_surface_grass",
        "tennis_surface_carpet",
        "tennis_surface_elo_diff",
        "tennis_h2h_win_rate",
        "tennis_recent_round_depth",
        "tennis_retirement_rate_3mo",
    ]
    values = surface + [elo, h2h, depth, ret]
    return names, values


def build_f1(cand: dict[str, Any]) -> tuple[list[str], list[float]]:
    """Formula 1 features.

    Inputs:
      * ``circuit_kind``         — street/permanent/hybrid.
      * ``grid_vs_quali_delta``  — grid − qualifying position (positive
                                   = penalty); clamped to ±20.
      * ``fp3_long_run_pace``    — z-scored long-run lap delta from
                                   FP3 (negative = faster than field
                                   average); clamped to ±3.
      * ``wet_race_win_rate``    — fraction of wet races the driver
                                   has finished P1-P3 (default 0.0).
      * ``high_drs_circuit``     — 1.0 if the circuit has a top-quartile
                                   DRS-zone count, 0.0 otherwise.
    """
    circuit = _one_hot(cand.get("circuit_kind"), _F1_CIRCUIT_KINDS)
    grid = _clamp(_as_float(cand.get("grid_vs_quali_delta"), 0.0), -20.0, 20.0)
    fp3 = _clamp(_as_float(cand.get("fp3_long_run_pace"), 0.0), -3.0, 3.0)
    wet = _clamp(_as_float(cand.get("wet_race_win_rate"), 0.0), 0.0, 1.0)
    drs = _clamp(_as_float(cand.get("high_drs_circuit"), 0.0), 0.0, 1.0)
    names = [
        "f1_circuit_street",
        "f1_circuit_permanent",
        "f1_circuit_hybrid",
        "f1_grid_vs_quali_delta",
        "f1_fp3_long_run_pace",
        "f1_wet_race_win_rate",
        "f1_high_drs_circuit",
    ]
    values = circuit + [grid, fp3, wet, drs]
    return names, values


def build_pga(cand: dict[str, Any]) -> tuple[list[str], list[float]]:
    """PGA features.

    Inputs:
      * ``green_type``              — bent/bermuda/poa/other.
      * ``course_made_cuts_5y``     — fraction of cuts made at this
                                      venue over the last 5 years.
      * ``course_top10_5y``         — fraction of top-10 finishes at
                                      this venue over the last 5 years.
      * ``sg_total_8event``         — strokes-gained total per round
                                      over the last 8 events; clamped
                                      to ±5.
      * ``sg_putting_8event``       — strokes-gained putting per round
                                      over the last 8 events; clamped
                                      to ±3.
      * ``sg_app_8event``           — strokes-gained approach per round
                                      over the last 8 events; clamped
                                      to ±3.
      * ``course_length_yds_norm``  — (course_length_yds − 7000) / 700
                                      so 7000y → 0, ±1 ≈ ±700y.
    """
    green = _one_hot(cand.get("green_type"), _PGA_GREEN_TYPES)
    cuts = _clamp(_as_float(cand.get("course_made_cuts_5y"), 0.5), 0.0, 1.0)
    top10 = _clamp(_as_float(cand.get("course_top10_5y"), 0.1), 0.0, 1.0)
    sg_tot = _clamp(_as_float(cand.get("sg_total_8event"), 0.0), -5.0, 5.0)
    sg_putt = _clamp(_as_float(cand.get("sg_putting_8event"), 0.0), -3.0, 3.0)
    sg_app = _clamp(_as_float(cand.get("sg_app_8event"), 0.0), -3.0, 3.0)
    length = _clamp(_as_float(cand.get("course_length_yds_norm"), 0.0), -2.0, 2.0)
    names = [
        "pga_green_bent",
        "pga_green_bermuda",
        "pga_green_poa",
        "pga_green_other",
        "pga_course_made_cuts_5y",
        "pga_course_top10_5y",
        "pga_sg_total_8event",
        "pga_sg_putting_8event",
        "pga_sg_app_8event",
        "pga_course_length_yds_norm",
    ]
    values = green + [cuts, top10, sg_tot, sg_putt, sg_app, length]
    return names, values


def build_ufc(cand: dict[str, Any]) -> tuple[list[str], list[float]]:
    """UFC features.

    Inputs:
      * ``weight_class``               — strawweight … heavyweight.
      * ``reach_diff_inches``          — fighter reach − opponent
                                         reach (clamped ±12).
      * ``td_defense_rate``            — share of opponent takedown
                                         attempts the fighter has
                                         defended (default 0.5).
      * ``td_attempt_rate_per_15min``  — own takedown attempts per 15
                                         minutes of cage time
                                         (clamped 0..20).
      * ``last_fight_win``             — 1.0 if last fight was a win,
                                         0.0 otherwise.
      * ``days_since_last_fight``      — capped at 730 (2 years)
                                         then normalised to [0, 1].
    """
    weight = _one_hot(cand.get("weight_class"), _UFC_WEIGHT_CLASSES)
    reach = _clamp(_as_float(cand.get("reach_diff_inches"), 0.0), -12.0, 12.0)
    td_def = _clamp(_as_float(cand.get("td_defense_rate"), 0.5), 0.0, 1.0)
    td_att = _clamp(
        _as_float(cand.get("td_attempt_rate_per_15min"), 0.0), 0.0, 20.0
    )
    last_win = _clamp(_as_float(cand.get("last_fight_win"), 0.0), 0.0, 1.0)
    days = _clamp(_as_float(cand.get("days_since_last_fight"), 0.0), 0.0, 730.0)
    days_norm = days / 730.0
    names = [
        f"ufc_weight_{wc}" for wc in _UFC_WEIGHT_CLASSES
    ] + [
        "ufc_reach_diff_inches",
        "ufc_td_defense_rate",
        "ufc_td_attempt_rate_per_15min",
        "ufc_last_fight_win",
        "ufc_days_since_last_fight_norm",
    ]
    values = weight + [reach, td_def, td_att, last_win, days_norm]
    return names, values


# Map league → builder.  ATP and WTA share the tennis builder because
# the per-tour calibrator is what handles the gender split — the
# features themselves are identical.
SPORT_BUILDERS: dict[str, Any] = {
    "ATP": build_tennis,
    "WTA": build_tennis,
    "F1": build_f1,
    "PGA": build_pga,
    "UFC": build_ufc,
}


def build_for_league(
    league: str, cand: dict[str, Any]
) -> tuple[list[str], list[float]]:
    """Dispatch helper — returns an empty vector for unsupported leagues."""
    key = (league or "").strip().upper()
    builder = SPORT_BUILDERS.get(key)
    if builder is None:
        return [], []
    return builder(cand)
