"""Health-page surface tests for the Sportradar feeds.

These tests run `_probe_feeds()` directly against an isolated FeedRegistry
so we can assert that:

  1. Every Sportradar feed registered in lifespan shows up as a
     `feed:<name>` card.
  2. Each feed card carries `last_pull` + `error_rate` (plus the raw
     counters) so the operator UI can render them as first-class metrics
     without a second round trip.
  3. The card detail string mentions the error rate when the feed has
     polled at least once.
"""
from __future__ import annotations

from app.feeds import sportradar
from app.feeds.odds_api import SPORTRADAR_SPORTS
from app.feeds.registry import FeedRegistry


def _isolated_registry():
    return FeedRegistry()


def test_probe_feeds_exposes_sportradar_with_metrics(monkeypatch):
    # Force every Sportradar feed to register as disabled (no keys).
    for s in SPORTRADAR_SPORTS:
        monkeypatch.delenv(f"SPORTRADAR_{s.upper()}_KEY", raising=False)
    monkeypatch.delenv("SPORTRADAR_API_KEY", raising=False)

    reg = _isolated_registry()
    monkeypatch.setattr(sportradar, "registry", reg)
    sportradar.register_sportradar_extra_feeds()

    # Use the same registry from the health module's POV.
    from app import health as health_mod
    monkeypatch.setattr(health_mod, "feed_registry", reg)

    cards = health_mod._probe_feeds()
    by_key = {c["key"]: c for c in cards}

    # Every Sportradar feed gets a card with the standard prefix.
    for s in SPORTRADAR_SPORTS:
        for kind in ("rosters", "injuries", "live"):
            key = f"feed:sportradar_{s}_{kind}"
            assert key in by_key, f"missing card {key}"
            extras = by_key[key].get("extras") or {}
            # First-class metrics for the UI to render.
            assert "last_pull" in extras
            assert "error_rate" in extras
            assert "poll_count" in extras
            assert "failure_count" in extras
            # Status defaults to unconfigured because no keys were set.
            assert by_key[key]["status"] == "unconfigured"


def test_probe_feeds_renders_error_rate_in_detail(monkeypatch):
    """When a feed has polled at least once, the detail string must
    surface the error rate so the row is self-explanatory even before
    the operator clicks into the extras panel."""
    reg = _isolated_registry()

    async def _ok():
        return 1

    from app.feeds.registry import Feed
    reg.register(Feed(name="probe_feed", cadence_seconds=60.0, poll=_ok, enabled=True))
    # Hand-mutate the status to simulate 9 successes / 1 failure.
    st = reg._status["probe_feed"]  # type: ignore[attr-defined]
    st.poll_count = 10
    st.success_count = 9
    st.failure_count = 1
    st.last_ok_at = 1_700_000_100.0
    st.last_finished_at = 1_700_000_100.0

    from app import health as health_mod
    monkeypatch.setattr(health_mod, "feed_registry", reg)
    cards = health_mod._probe_feeds()
    assert len(cards) == 1
    card = cards[0]
    assert card["status"] == "ok"
    assert "err 10.0%" in card["detail"]
    assert "(1/10)" in card["detail"]
    assert card["extras"]["error_rate"] == 0.1
