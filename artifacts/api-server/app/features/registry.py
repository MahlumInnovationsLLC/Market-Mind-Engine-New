"""Feature registry — the canonical catalogue of model features.

Each :class:`FeatureSpec` carries enough metadata that any consumer
(predictor, AutoResearch, backtests, the drift report, the operator
console) can describe a feature without re-reading the live builder.
The ``compute`` callable is what actually turns a candidate dict into a
single ``float`` for that feature; it receives a :class:`BuildContext`
which exposes the candidate, decoded ``game_context``, the build
timestamp, caller-supplied overrides, and previously-computed feature
values (so derived features can read e.g. ``pred_prob`` and ``edge``
without re-deriving them).

The registry is intentionally small and dependency-free; the actual
feature definitions live in :mod:`app.features.builders` so this
module can be imported by drift / metadata code without dragging in
side-effects.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping


@dataclass
class BuildContext:
    """All the inputs a feature transform needs to produce its value."""

    candidate: Mapping[str, Any]
    ctx: Mapping[str, Any]
    now: datetime
    overrides: Mapping[str, Any]
    # Filled in by the registry as it walks the spec list.  Derived
    # features (e.g. ``prob_edge`` = ``pred_prob`` * ``edge``) read
    # earlier values from here instead of re-deriving them, so the
    # transforms stay tiny and the schema is order-stable.
    computed: dict[str, float] = field(default_factory=dict)
    # Track which named *raw inputs* were really present (vs defaulted).
    # Mirrors the existing ``FeatureVector.present`` contract so the
    # dashboard can still tell "real 0.5" from "missing 0.5".
    present: dict[str, float] = field(default_factory=dict)


Transform = Callable[[BuildContext], float]


@dataclass(frozen=True)
class FeatureSpec:
    """Describes one slot in the model's feature vector.

    Attributes
    ----------
    name:
        Stable identifier; matches the trained model's column header.
    source:
        Human-readable origin (``"candidate.consensus_probability"``,
        ``"derived"``, ``"time"``, ``"reserved"``, …) — informational.
    transform:
        Short description of the computation (``"x / 24"``,
        ``"abs(pred_prob - 0.5)"``).  Pure documentation; the actual
        logic lives in ``compute``.
    dtype:
        Logical dtype — one of ``"prob"``, ``"signed_unit"``,
        ``"bool01"``, ``"idx_norm"``, ``"unit"``, ``"raw"``.
    default:
        Value the trainer used when the upstream signal was missing.
        Surfaced so downstream tools (e.g. drift) can compare a live
        sample against a known-good baseline without re-discovering it.
    compute:
        Callable that produces the float for one ``BuildContext``.
    """

    name: str
    source: str
    transform: str
    dtype: str
    default: float
    compute: Transform


class FeatureRegistry:
    """Ordered catalogue of :class:`FeatureSpec` instances.

    Order matters: it is the column order the predictor consumes and
    the layout the trained meta-model was fit on.  Re-registering the
    same name is a programming error and raises ``ValueError`` rather
    than silently shadowing the previous spec.
    """

    def __init__(self) -> None:
        self._specs: list[FeatureSpec] = []
        self._by_name: dict[str, FeatureSpec] = {}

    def register(self, spec: FeatureSpec) -> FeatureSpec:
        if spec.name in self._by_name:
            raise ValueError(f"feature {spec.name!r} already registered")
        self._specs.append(spec)
        self._by_name[spec.name] = spec
        return spec

    def specs(self) -> tuple[FeatureSpec, ...]:
        return tuple(self._specs)

    def names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self._specs)

    def __len__(self) -> int:
        return len(self._specs)

    def get(self, name: str) -> FeatureSpec | None:
        return self._by_name.get(name)

    def metadata(self) -> list[dict[str, Any]]:
        """JSON-serialisable description of every registered feature.

        Used by ``GET /api/features/registry`` and AutoResearch when it
        needs to enumerate the available signals.
        """
        return [
            {
                "index": i,
                "name": s.name,
                "source": s.source,
                "transform": s.transform,
                "dtype": s.dtype,
                "default": s.default,
            }
            for i, s in enumerate(self._specs)
        ]

    def build(
        self,
        candidate: Mapping[str, Any],
        *,
        ctx: Mapping[str, Any],
        now: datetime,
        overrides: Mapping[str, Any],
    ) -> tuple[list[float], dict[str, float]]:
        """Run every registered transform in order, returning the vector.

        Returns ``(values, present)`` — ``values`` aligned positionally
        with :meth:`names`, ``present`` is the diagnostic map of raw
        inputs that were actually supplied (vs defaulted away).
        """
        bc = BuildContext(
            candidate=candidate,
            ctx=ctx,
            now=now,
            overrides=overrides,
        )
        values: list[float] = []
        for spec in self._specs:
            v = float(spec.compute(bc))
            bc.computed[spec.name] = v
            values.append(v)
        return values, dict(bc.present)


# Process-wide singleton.  Tests that need an isolated registry can
# instantiate :class:`FeatureRegistry` directly.
registry = FeatureRegistry()
