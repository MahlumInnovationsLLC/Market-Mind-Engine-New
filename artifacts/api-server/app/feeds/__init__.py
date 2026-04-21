"""External data feed pollers for Axiom-Predict.

The trading orchestrator needs fresh signal: live odds from Sportradar +
The Odds API; live + forecast weather from Open-Meteo, NWS, AccuWeather
minutecast, and the HRRR mesoscale model.  This package wires each of
those sources behind a uniform `Feed` abstraction so they can run on
independent cadences, fault-isolated from each other, with a single
observability surface (`FeedRegistry`).

Public interface:

    from .feeds import registry, scheduler

    await scheduler.start()        # boot all configured pollers
    snap = registry.snapshot()     # live status of every feed

The trading orchestrator (follow-up task) reads from the same DB tables
the pollers write to.  The canonical odds-tick table on Supabase is
`odds_changes` (verified via information_schema; the legacy spec name
`odds_history` does not exist on the production schema).  Other targets:
`line_snapshots`, `weather_minutecast`, `weather_extended`,
`weather_observations`, `weather_hrrr_forecasts`.  This task only owns
ingestion.
"""
from __future__ import annotations

from .registry import Feed, FeedStatus, FeedRegistry, registry
from .scheduler import scheduler

__all__ = ["Feed", "FeedStatus", "FeedRegistry", "registry", "scheduler"]
