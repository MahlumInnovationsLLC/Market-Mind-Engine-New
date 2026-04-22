"""Pluggable vertical registry.

Each trading vertical (sports, weather, crypto, future verticals like
NFL / commodities / geopolitics) is a self-contained `VerticalSpec`
registered in the module-level `REGISTRY`.  The orchestrator, risk
gates, dashboard, and swarm runner read everything they need to scope
behaviour from this registry rather than hardcoded conditionals.

Importing this package registers the built-in verticals — that's the
only side-effect of import time, so `from app import verticals` is
enough to populate the registry.
"""
from __future__ import annotations

from .registry import (
    REGISTRY,
    VerticalRegistry,
    VerticalSpec,
    all_specs,
    dashboard_strip_verticals,
    get_spec,
    has,
    league_filter,
    names,
    persona_profile_for,
    require_spec,
)
from . import builtins as _builtins  # noqa: F401 — registers built-in specs

__all__ = [
    "REGISTRY",
    "VerticalRegistry",
    "VerticalSpec",
    "all_specs",
    "dashboard_strip_verticals",
    "get_spec",
    "has",
    "league_filter",
    "names",
    "persona_profile_for",
    "require_spec",
]
