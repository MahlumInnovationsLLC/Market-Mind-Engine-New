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
from typing import Any, Iterable, Mapping

from .builders import decode_context, track_elo_inputs
from .registry import BuildContext, registry


# Names + count are derived from the live registry so a future column
# add/remove only needs to touch ``builders.py``. These reflect the
# *built-in* (non-shadow) production columns; promoted UW candidates
# extend the live vector at runtime — see :func:`set_promoted_names`.
FEATURE_NAMES: tuple[str, ...] = registry.names()
FEATURE_COUNT: int = len(FEATURE_NAMES)


# Process-wide promoted set. Loaded at boot from
# ``autoresearch_features WHERE status = 'production'`` so a candidate
# that an operator promoted is reflected in the live vector once a
# matching retrained model artifact has been published. Empty by
# default so test runs and cold installs see the original 32-column
# layout without any DB dependency.
_promoted_names: frozenset[str] = frozenset()


def set_promoted_names(names: Iterable[str]) -> None:
    """Replace the in-process promoted-feature set.

    Called by the API startup hook after reading the
    ``autoresearch_features`` table, and by the admin promote/demote
    endpoints so a flip takes effect on the next prediction without
    a process restart. Any name not registered as a shadow spec is
    silently ignored — the registry stays the source of truth.
    """
    global _promoted_names
    valid = {s.name for s in registry.shadow_specs()}
    _promoted_names = frozenset(n for n in names if n in valid)


def get_promoted_names() -> frozenset[str]:
    """Currently-active promoted set (read-only view)."""
    return _promoted_names


def production_names() -> tuple[str, ...]:
    """Live column order: built-ins + currently-promoted shadow specs."""
    return registry.production_names(_promoted_names)


def production_count() -> int:
    return len(production_names())


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
    promoted = _promoted_names
    # Walk every registered spec — production AND shadow — so AutoResearch
    # shadow candidates are computed each cycle (training-row collection).
    # Built-in specs always land in the vector; shadow specs only land
    # when promoted (status='production' in autoresearch_features).
    for spec in registry.all_specs():
        v = float(spec.compute(bc))
        bc.computed[spec.name] = v
        if (not spec.shadow) or (spec.name in promoted):
            values.append(v)

    # Pseudo-feature: pull Elo-style internal signals onto ``present``
    # without pushing them into the model vector (would break trained
    # column alignment).  The dashboard reads them straight off the
    # candidate via this map.
    track_elo_inputs(bc)

    names = registry.production_names(promoted)
    assert len(values) == len(names), (
        f"feature builder produced {len(values)} values, "
        f"expected {len(names)} (built-in {FEATURE_COUNT} + "
        f"promoted {len(promoted)})"
    )
    return FeatureVector(
        names=names, values=values, present=dict(bc.present),
    )
