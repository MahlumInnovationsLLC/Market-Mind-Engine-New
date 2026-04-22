"""High-level vector builder backed by the registry.

This is the public entry point both the live cycle and the drift report
go through.  The lower-level :mod:`app.features.registry` knows how to
walk one ``FeatureSpec`` at a time; this module wires the registry up
to the legacy ``FeatureVector`` contract so existing callers keep
working unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from .builders import decode_context, track_elo_inputs
from .registry import BuildContext, registry


# Names + count are derived from the live registry so a future column
# add/remove only needs to touch ``builders.py``.
FEATURE_NAMES: tuple[str, ...] = registry.names()
FEATURE_COUNT: int = len(FEATURE_NAMES)


@dataclass(frozen=True)
class FeatureVector:
    """Result of :func:`build_vector` — names + values aligned positionally."""

    names: tuple[str, ...]
    values: list[float]
    # Subset of named inputs that were *really* present on the
    # candidate (vs defaulted away).  Useful for telemetry — lets the
    # dashboard show which signals drove a given live probability.
    present: dict[str, float]

    def as_dict(self) -> dict[str, float]:
        return dict(zip(self.names, self.values))


def build_vector(
    candidate: Mapping[str, Any],
    *,
    consensus: float | None = None,
    current_price: float | None = None,
    edge: float | None = None,
    kelly: float | None = None,
    now: datetime | None = None,
) -> FeatureVector:
    """Run every registered feature transform on ``candidate``.

    The candidate may carry a flattened ``game_context`` JSON sub-dict
    (the cached debate path); we look there as a second source for any
    field that isn't on the top level.  Caller-supplied ``consensus``,
    ``current_price``, ``edge``, ``kelly`` win over whatever's on the
    dict so the cycle can pass the *effective* values it already
    computed instead of letting the registry re-derive them.
    """
    ctx = decode_context(candidate.get("game_context"))
    now = now or datetime.now(timezone.utc)
    overrides: dict[str, Any] = {
        "consensus": consensus,
        "current_price": current_price,
        "edge": edge,
        "kelly": kelly,
    }

    # Walk the registry in declared order; each compute reads earlier
    # values from ``BuildContext.computed`` and may push raw-input
    # provenance into ``BuildContext.present``.
    bc = BuildContext(
        candidate=candidate, ctx=ctx, now=now, overrides=overrides,
    )
    values: list[float] = []
    for spec in registry.specs():
        v = float(spec.compute(bc))
        bc.computed[spec.name] = v
        values.append(v)

    # Pseudo-feature: pull Elo-style internal signals onto ``present``
    # without pushing them into the model vector (would break trained
    # column alignment).  The dashboard reads them straight off the
    # candidate via this map.
    track_elo_inputs(bc)

    assert len(values) == FEATURE_COUNT, (
        f"feature builder produced {len(values)} values, expected {FEATURE_COUNT}"
    )
    return FeatureVector(
        names=FEATURE_NAMES, values=values, present=dict(bc.present),
    )
