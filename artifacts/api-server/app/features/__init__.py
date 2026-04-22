"""Centralised feature store (v2).

A single source of truth for every feature the live trading path consumes.
Each feature is described by a :class:`FeatureSpec` (name, source, transform,
dtype, default) and registered in :data:`registry`.  The same registry powers:

* ``orchestrator.features.build_features`` — the live meta-model vector.
* ``features.drift`` — drift report comparing live distributions to a
  training-time baseline.
* AutoResearch + backtests (downstream tasks) — they read the registry
  metadata so they all see the same feature set as production.

Importing this package eagerly imports :mod:`app.features.builders`, which
performs the registration as a side-effect.  Downstream code should never
register features twice; the registry raises if a duplicate name appears.
"""
from __future__ import annotations

from .registry import (
    BuildContext,
    FeatureRegistry,
    FeatureSpec,
    Transform,
    registry,
)
# Importing builders triggers registration of every production feature.
from . import builders as _builders  # noqa: F401  (side-effect import)
from .vector import FEATURE_COUNT, FEATURE_NAMES, FeatureVector, build_vector

__all__ = [
    "BuildContext",
    "FeatureRegistry",
    "FeatureSpec",
    "Transform",
    "FEATURE_COUNT",
    "FEATURE_NAMES",
    "FeatureVector",
    "build_vector",
    "registry",
]
