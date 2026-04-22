"""Live feature builder for the meta-model — compatibility shim.

The real definition of every feature now lives in
:mod:`app.features` (registry + builders + vector).  This module is
kept as a small surface so the cycle (and existing tests) can keep
importing the same symbols without churn:

    from app.orchestrator.features import (
        FEATURE_COUNT, FEATURE_NAMES, FeatureVector, build_features,
    )

Anything new should import from :mod:`app.features` directly so the
dependency arrow points at the registry.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from ..features import (  # re-exports from the central feature store
    FEATURE_COUNT,
    FEATURE_NAMES,
    FeatureVector,
    build_vector,
)


__all__ = ["FEATURE_COUNT", "FEATURE_NAMES", "FeatureVector", "build_features"]


def build_features(
    candidate: Mapping[str, Any],
    *,
    consensus: float | None = None,
    current_price: float | None = None,
    edge: float | None = None,
    kelly: float | None = None,
    now: datetime | None = None,
) -> FeatureVector:
    """Backwards-compatible alias for :func:`app.features.build_vector`.

    The cycle calls this on every candidate; we additionally tee the
    finished vector into the in-process drift collector so the operator
    console can show distribution drift without any caller changes.
    """
    fv = build_vector(
        candidate,
        consensus=consensus,
        current_price=current_price,
        edge=edge,
        kelly=kelly,
        now=now,
    )
    # Best-effort: a sick drift collector must NEVER take down the
    # trading loop — that's the whole point of decoupling it from the
    # builder.  Imported lazily to avoid an import cycle.
    try:
        from ..features.drift import record_observation

        record_observation(fv.values)
    except Exception:  # pragma: no cover - defensive
        pass
    return fv
