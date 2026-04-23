"""Bridge between ``autoresearch_features`` rows and the feature registry.

AutoResearch mines candidate UW signals into a database table; the
feature registry only knows about specs that have been programmatically
``register``-ed with it. This module closes that loop so a name living
in ``autoresearch_features`` becomes a real ``FeatureSpec(shadow=True)``
the registry can return from ``shadow_specs()`` / ``production_specs()``.

The compute callable on the synthesised spec reads its value from the
candidate's ``uw_signals`` dict (a snapshot the cycle is responsible
for populating with the same aggregations the mining job ran offline).
When the snapshot is missing a name we return the spec's stored
``default`` so the live vector stays well-formed and the predictor's
input coercion path doesn't see NaNs.

Registering a shadow spec does **not** by itself opt the column into
the live vector — that still requires
``app.features.vector.set_promoted_names`` (driven by the row's
``status='production'`` flag). The two-step design keeps shadow
collection (always-on telemetry) cleanly separated from promotion
(operator-gated, retrain-required).
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping

from ..features.registry import BuildContext, FeatureSpec, registry


log = logging.getLogger("axiom.autoresearch.feature_bridge")


def _make_compute(name: str, default: float):
    """Build a registry-compatible compute that reads the candidate snapshot."""

    def _compute(bc: BuildContext) -> float:
        signals = bc.candidate.get("uw_signals") if bc.candidate else None
        if isinstance(signals, Mapping):
            v = signals.get(name)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return float(default)
        # Also accept a flattened ``uw_signals.<name>`` field (mirrors the
        # ``game_context`` flattening pattern used elsewhere).
        flat = bc.candidate.get(f"uw_signals.{name}") if bc.candidate else None
        if flat is not None:
            try:
                return float(flat)
            except (TypeError, ValueError):
                pass
        return float(default)

    return _compute


def ensure_shadow_spec(name: str, *, default: float = 0.0) -> FeatureSpec:
    """Idempotently register a shadow spec for an autoresearch candidate.

    Returns the existing spec if one is already registered under
    ``name`` (whether shadow or production); otherwise registers a new
    shadow spec backed by :func:`_make_compute`.
    """
    existing = registry.get(name)
    if existing is not None:
        return existing
    spec = FeatureSpec(
        name=name,
        source="autoresearch.uw_signals",
        transform="candidate.uw_signals[name] (default if missing)",
        dtype="raw",
        default=float(default),
        compute=_make_compute(name, float(default)),
        shadow=True,
    )
    registry.register(spec)
    return spec


def register_candidates(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    """Register a shadow spec for each row's ``name``.

    Accepts the dict shape returned by
    :func:`app.autoresearch.store.list_candidates`. Rows missing a
    ``name`` are skipped. Returns the list of names registered (or
    already present).
    """
    out: list[str] = []
    for row in rows:
        name = (row or {}).get("name")
        if not name:
            continue
        default = 0.0
        md = (row or {}).get("metadata") or {}
        if isinstance(md, Mapping):
            try:
                default = float(md.get("default", 0.0))
            except (TypeError, ValueError):
                default = 0.0
        try:
            ensure_shadow_spec(str(name), default=default)
            out.append(str(name))
        except Exception as e:  # registration failure shouldn't kill startup
            log.warning("feature_bridge: failed to register %s: %s", name, e)
    return out


def register_names(names: Iterable[str]) -> list[str]:
    """Register a shadow spec per bare name (no metadata available).

    Used by the offline retrain CLI which only has a list of promoted
    names, not the full row dicts.
    """
    out: list[str] = []
    for name in names:
        if not name:
            continue
        try:
            ensure_shadow_spec(str(name))
            out.append(str(name))
        except Exception as e:
            log.warning("feature_bridge: failed to register %s: %s", name, e)
    return out
