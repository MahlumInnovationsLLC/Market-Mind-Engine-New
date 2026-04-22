"""VerticalSpec dataclass + module-level VerticalRegistry.

Pure stdlib so any caller (config.py, gates, dashboard) can import the
registry without dragging in heavy dependencies or risking circular
imports.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class VerticalSpec:
    """Self-contained description of a trading vertical.

    Fields are intentionally generic so future verticals (NFL, soccer,
    commodities, geopolitics, elections, entertainment, events/macro)
    can be added by registering a new spec without touching cycle.py /
    risk gates / dashboards.
    """

    name: str
    # League codes used for SQL `WHERE league = ANY($leagues)` filters
    # in the orchestrator. Empty tuple = no league restriction.
    leagues: tuple[str, ...] = ()
    # Kalshi/Polymarket ticker prefix patterns (informational for now,
    # consumed by upcoming per-vertical dashboards / market discovery).
    ticker_prefixes: tuple[str, ...] = ()
    # Persona profile name for the swarm runner. The runner currently
    # treats this as a tag carried on the debate; per-profile prompts
    # land in task #194.
    persona_profile: str = "default"
    # Multiplier applied on top of the operator-set Kelly fraction.
    kelly_multiplier: float = 1.0
    # Default per-vertical exposure cap as a fraction of bankroll;
    # operator overrides ('risk.per_vertical_cap_pct') still win.
    default_allocation_pct: float = 0.30
    # Free-form blackout windows ("daily 07:00-08:00 UTC" etc.) that
    # future schedulers can interpret. Currently informational.
    blackout_windows: tuple[str, ...] = ()
    # Optional predictor hook name. When set, cycle._predict_for_candidate
    # routes the candidate to the matching specialist predictor instead
    # of the generic meta-model. Currently only "crypto_binary" is wired.
    predictor_hook: str | None = None
    # Production duplicate-trade index doesn't apply to high-frequency
    # crypto markets; setting this to True exempts the vertical from
    # gate_duplicate_trade.
    duplicate_trade_exempt: bool = False
    # Whether to surface this vertical in the operator dashboard's
    # per-vertical strip. Defaults False so future internal verticals
    # don't bloat the strip until UI work lands.
    include_in_dashboard_strip: bool = False
    # Master enable/disable. Disabled verticals are ignored by the
    # scheduler even when listed in AXIOM_ORCHESTRATOR_VERTICALS.
    enabled: bool = True
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "leagues": list(self.leagues),
            "ticker_prefixes": list(self.ticker_prefixes),
            "persona_profile": self.persona_profile,
            "kelly_multiplier": self.kelly_multiplier,
            "default_allocation_pct": self.default_allocation_pct,
            "blackout_windows": list(self.blackout_windows),
            "predictor_hook": self.predictor_hook,
            "duplicate_trade_exempt": self.duplicate_trade_exempt,
            "include_in_dashboard_strip": self.include_in_dashboard_strip,
            "enabled": self.enabled,
            "description": self.description,
        }


class VerticalRegistry:
    """Insertion-ordered registry of `VerticalSpec` instances.

    Lookups are case-insensitive on the `name` field so callers don't
    have to normalize.  Registration order is preserved (Python 3.7+
    dict ordering) so dashboards that iterate the registry get a
    stable display order.
    """

    def __init__(self) -> None:
        self._specs: dict[str, VerticalSpec] = {}

    def register(self, spec: VerticalSpec, *, replace: bool = False) -> VerticalSpec:
        key = spec.name.lower()
        if not replace and key in self._specs:
            raise ValueError(f"vertical {spec.name!r} already registered")
        self._specs[key] = spec
        return spec

    def unregister(self, name: str) -> None:
        self._specs.pop((name or "").lower(), None)

    def get(self, name: str) -> VerticalSpec | None:
        return self._specs.get((name or "").lower())

    def require(self, name: str) -> VerticalSpec:
        spec = self.get(name)
        if spec is None:
            raise KeyError(
                f"unknown vertical {name!r}; registered: {sorted(self._specs)}"
            )
        return spec

    def has(self, name: str) -> bool:
        return (name or "").lower() in self._specs

    def names(self) -> list[str]:
        return [s.name for s in self._specs.values()]

    def all(self) -> list[VerticalSpec]:
        return list(self._specs.values())

    def league_filter(self, name: str) -> list[str]:
        spec = self.get(name)
        return list(spec.leagues) if spec else []

    def dashboard_strip(self) -> list[VerticalSpec]:
        return [s for s in self._specs.values() if s.include_in_dashboard_strip]


# Module-level singleton — `app.verticals.builtins` populates it on import.
REGISTRY = VerticalRegistry()


# Convenience wrappers so callers can import functions directly.
def get_spec(name: str) -> VerticalSpec | None:
    return REGISTRY.get(name)


def require_spec(name: str) -> VerticalSpec:
    return REGISTRY.require(name)


def league_filter(name: str) -> list[str]:
    return REGISTRY.league_filter(name)


def names() -> list[str]:
    return REGISTRY.names()


def all_specs() -> list[VerticalSpec]:
    return REGISTRY.all()


def has(name: str) -> bool:
    return REGISTRY.has(name)


def persona_profile_for(name: str) -> str:
    spec = REGISTRY.get(name)
    return spec.persona_profile if spec else "default"


def dashboard_strip_verticals() -> list[str]:
    return [s.name for s in REGISTRY.dashboard_strip()]
