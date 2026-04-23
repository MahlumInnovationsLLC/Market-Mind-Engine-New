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
from typing import Any, Callable, Iterable, Mapping


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
    # When True the spec is computed every cycle (so AutoResearch can
    # collect telemetry / training rows for it) but is **not** part of
    # the production feature vector emitted by :func:`build`. Promotion
    # to production happens out-of-band via the admin console; flipping
    # this flag at runtime would shift the trained model's column
    # alignment, so we keep promotion gated on an offline retrain.
    shadow: bool = False


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
        """Production specs in declared order (excludes shadow features).

        This is the predictor's view: column order and count match the
        trained meta-model. Shadow specs registered by AutoResearch are
        excluded so they cannot accidentally drift the schema.
        """
        return tuple(s for s in self._specs if not s.shadow)

    def all_specs(self) -> tuple[FeatureSpec, ...]:
        """Every registered spec, production *and* shadow, in declared order."""
        return tuple(self._specs)

    def shadow_specs(self) -> tuple[FeatureSpec, ...]:
        return tuple(s for s in self._specs if s.shadow)

    def production_specs(
        self, promoted: Iterable[str] | None = None
    ) -> tuple[FeatureSpec, ...]:
        """Specs that should appear in the live feature vector.

        Built-in (non-shadow) specs are always included; shadow specs
        whose ``name`` is in ``promoted`` are opted back in. ``promoted``
        is normally :func:`app.autoresearch.store.list_promoted_names`
        — the set of ``autoresearch_features`` rows whose ``status`` is
        ``'production'`` after an operator promotion. Order is the
        registry's declared order (so a retrain run with the same
        promoted set produces a column layout the live builder will
        match positionally).
        """
        promoted_set = set(promoted or ())
        return tuple(
            s for s in self._specs
            if (not s.shadow) or (s.name in promoted_set)
        )

    def production_names(
        self, promoted: Iterable[str] | None = None
    ) -> tuple[str, ...]:
        """Names of :meth:`production_specs` in column order."""
        return tuple(s.name for s in self.production_specs(promoted))

    def names(self) -> tuple[str, ...]:
        """Production-only names (column order of the live feature vector)."""
        return tuple(s.name for s in self._specs if not s.shadow)

    def __len__(self) -> int:
        """Production-only count (matches :meth:`names`)."""
        return sum(1 for s in self._specs if not s.shadow)

    def get(self, name: str) -> FeatureSpec | None:
        return self._by_name.get(name)

    def metadata(self, *, include_shadow: bool = False) -> list[dict[str, Any]]:
        """JSON-serialisable description of every registered feature.

        Used by ``GET /api/features/registry`` and AutoResearch when it
        needs to enumerate the available signals. By default only
        production specs are returned; pass ``include_shadow=True`` to
        also surface shadow candidates (e.g. for the AutoResearch admin
        view).
        """
        out: list[dict[str, Any]] = []
        idx = 0
        for s in self._specs:
            if s.shadow and not include_shadow:
                continue
            out.append({
                "index": idx,
                "name": s.name,
                "source": s.source,
                "transform": s.transform,
                "dtype": s.dtype,
                "default": s.default,
                "shadow": s.shadow,
            })
            idx += 1
        return out

    def build(
        self,
        candidate: Mapping[str, Any],
        *,
        ctx: Mapping[str, Any],
        now: datetime,
        overrides: Mapping[str, Any],
        promoted: Iterable[str] | None = None,
    ) -> tuple[list[float], dict[str, float]]:
        """Run every registered transform in order, returning the vector.

        Returns ``(values, present)`` — ``values`` aligned positionally
        with :meth:`production_names` (which collapses to :meth:`names`
        when ``promoted`` is empty). ``present`` is the diagnostic map
        of raw inputs that were actually supplied (vs defaulted away).
        """
        promoted_set = set(promoted or ())
        bc = BuildContext(
            candidate=candidate,
            ctx=ctx,
            now=now,
            overrides=overrides,
        )
        values: list[float] = []
        # Walk every registered spec — production AND shadow — so derived
        # production features can read upstream shadow values from
        # ``bc.computed`` if needed, and so AutoResearch shadow rows are
        # collected each cycle. Shadow specs only contribute to the
        # output vector when promoted by an operator (status='production'
        # in autoresearch_features); otherwise the predictor's column
        # count stays stable.
        for spec in self._specs:
            v = float(spec.compute(bc))
            bc.computed[spec.name] = v
            if (not spec.shadow) or (spec.name in promoted_set):
                values.append(v)
        return values, dict(bc.present)


# Process-wide singleton.  Tests that need an isolated registry can
# instantiate :class:`FeatureRegistry` directly.
registry = FeatureRegistry()
