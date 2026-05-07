"""Tests for the tail-risk burst-rate alert (task #746).

The two tail-risk gates (`tail_price_size_cap`, `jumbo_quantity_cap`)
already log every veto into `risk_gate_trips` and dispatch a per-veto
info-severity alert, but neither catches "the model just started
recommending deep-OTM / jumbo bets in burst" — the per-veto alert is
deduped per `(gate_name, market_id)`. The
``notifications.tail_risk_burst.check_tail_risk_burst`` helper closes
that gap by counting trips per gate over a rolling 60-minute window
and paging when the count crosses an operator-tunable threshold.

These tests cover:

  1. Threshold-trip behavior — under-threshold counts stay quiet,
     over-threshold counts dispatch one alert per gate with a per-
     vertical breakdown and a deep-link to the dashboard tile.
  2. Dedupe-window behavior — the same gate burst pages once per TTL,
     and an expired dedupe row re-pages so a long-lived incident
     doesn't fall off the on-call's radar.
  3. Operator opt-out via ``threshold=0`` short-circuits the helper
     before any DB read.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app import db as _db
from app.notifications import alerts as _alerts
from app.notifications import tail_risk_burst as _burst


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeDedupe:
    """In-memory stand-in for ``notification_dedupe`` claims."""

    def __init__(self) -> None:
        self.rows: dict[str, datetime] = {}

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        assert "notification_dedupe" in query
        key, ttl = args[0], int(args[1])
        now = datetime.now(timezone.utc)
        if key not in self.rows or self.rows[key] <= now:
            self.rows[key] = now + timedelta(seconds=ttl)
            return {"ok": 1}
        return None


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    counts: dict[str, list[tuple[str, int]]],
    last_trips: dict[str, dict[str, Any]] | None = None,
) -> tuple[_FakeDedupe, list[dict[str, Any]]]:
    """Wire up an in-memory dedupe table, capture every dispatched
    alert, and pin ``risk_gate_trips`` reads to a known per-gate
    breakdown.

    ``counts`` maps ``gate_name -> [(vertical, count), ...]`` and
    drives the ``GROUP BY vertical`` count query.  ``last_trips`` (if
    given) pins the ``ORDER BY created_at DESC LIMIT 1`` lookup so
    the test can assert the ``last_market_id`` / ``last_at`` fields
    landed in the alert body.
    """
    fake = _FakeDedupe()
    monkeypatch.setattr(_db, "fetchrow", fake.fetchrow)

    last_trips = last_trips or {}

    async def _fetch(query: str, *args: Any) -> list[dict[str, Any]]:
        # The burst helper's count query selects vertical + n grouped
        # by vertical for the gate passed as the first arg.
        assert "risk_gate_trips" in query
        gate_name = args[0]
        rows = counts.get(gate_name, [])
        return [{"vertical": v, "n": n} for v, n in rows]

    async def _fetchrow_dispatch(query: str, *args: Any) -> dict[str, Any] | None:
        # Two distinct queries reach `db.fetchrow`:
        #   - the dedupe claim (handled by `_FakeDedupe.fetchrow`),
        #   - the "last trip" lookup against `risk_gate_trips`.
        if "notification_dedupe" in query:
            return await fake.fetchrow(query, *args)
        assert "risk_gate_trips" in query
        gate_name = args[0]
        return last_trips.get(gate_name)

    monkeypatch.setattr(_db, "fetchrow", _fetchrow_dispatch)
    monkeypatch.setattr(_db, "fetch", _fetch)

    dispatched: list[dict[str, Any]] = []

    def _fake_dispatch(
        subject: str,
        text_body: str,
        *,
        tag: str,
        dry_run: bool,
        html_body: str | None = None,
    ) -> dict[str, Any]:
        dispatched.append({
            "subject": subject,
            "body": text_body,
            "tag": tag,
        })
        return {"tag": tag, "dispatched": True}

    monkeypatch.setattr(_alerts, "_dispatch", _fake_dispatch)

    # Stub the Telegram fan-out so the test never imports the real
    # broadcaster (which would attempt a network call on first import).
    import sys
    import types
    tg_pkg_name = "app.telegram"
    if tg_pkg_name not in sys.modules:
        sys.modules[tg_pkg_name] = types.ModuleType(tg_pkg_name)
    notify_mod = types.ModuleType(f"{tg_pkg_name}.notify")
    notify_mod.broadcast_in_background = lambda _msg: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, f"{tg_pkg_name}.notify", notify_mod)

    return fake, dispatched


# ---------------------------------------------------------------------------
# Threshold-trip behavior
# ---------------------------------------------------------------------------


def test_below_threshold_is_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both gates well under the burst threshold → zero pages."""
    _, dispatched = _install_fakes(
        monkeypatch,
        counts={
            "tail_price_size_cap": [("sports", 4), ("crypto", 2)],
            "jumbo_quantity_cap": [("sports", 1)],
        },
    )

    res = asyncio.run(_burst.check_tail_risk_burst(threshold=10))

    assert res["checked"] is True
    assert res["disabled"] is False
    assert res["over_threshold"] == []
    assert res["fired"] == []
    assert res["suppressed"] == []
    assert dispatched == []
    # per-gate summary still reflects the read so the operator console
    # can show "we checked, nothing crossed".
    assert res["per_gate"]["tail_price_size_cap"]["count"] == 6
    assert res["per_gate"]["jumbo_quantity_cap"]["count"] == 1


def test_burst_alert_fires_for_each_overshooting_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each tail-risk gate that crossed the threshold pages once with
    its own subject + per-vertical breakdown + dashboard deep-link."""
    last_at = "2026-05-05T10:11:12Z"
    _, dispatched = _install_fakes(
        monkeypatch,
        counts={
            "tail_price_size_cap": [("sports", 9), ("crypto", 5)],
            "jumbo_quantity_cap": [("sports", 12)],
        },
        last_trips={
            "tail_price_size_cap": {
                "market_id": "KX-DEEP-OTM-1",
                "last_at": last_at,
            },
            "jumbo_quantity_cap": {
                "market_id": "KX-JUMBO-9",
                "last_at": last_at,
            },
        },
    )

    res = asyncio.run(_burst.check_tail_risk_burst(threshold=10))

    assert sorted(res["fired"]) == sorted(
        ["tail_price_size_cap", "jumbo_quantity_cap"]
    )
    assert sorted(res["over_threshold"]) == sorted(res["fired"])
    assert res["suppressed"] == []
    assert res["window_minutes"] == 60

    tags = [d["tag"] for d in dispatched]
    assert tags == ["tail-risk-burst"] * 2

    by_gate = {}
    for d in dispatched:
        for gate in ("tail_price_size_cap", "jumbo_quantity_cap"):
            if gate in d["subject"]:
                by_gate[gate] = d
    assert set(by_gate.keys()) == {"tail_price_size_cap", "jumbo_quantity_cap"}

    tp = by_gate["tail_price_size_cap"]
    assert "[TAIL-RISK BURST]" in tp["subject"]
    assert "14" in tp["subject"]  # 9 + 5
    assert "60m" in tp["subject"]
    body = tp["body"]
    assert "sports=9" in body
    assert "crypto=5" in body
    assert "KX-DEEP-OTM-1" in body
    assert last_at in body
    assert "/system#tail-risk" in body  # dashboard deep-link

    jb = by_gate["jumbo_quantity_cap"]
    assert "12" in jb["subject"]
    assert "sports=12" in jb["body"]
    assert "KX-JUMBO-9" in jb["body"]


def test_only_overshooting_gate_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """One gate over, one gate under → exactly one page, and the
    quiet gate's breakdown is still captured for the cycle log."""
    _, dispatched = _install_fakes(
        monkeypatch,
        counts={
            "tail_price_size_cap": [("sports", 25)],
            "jumbo_quantity_cap": [("sports", 3)],
        },
        last_trips={
            "tail_price_size_cap": {
                "market_id": "KX-A",
                "last_at": "2026-05-05T11:00:00Z",
            },
        },
    )

    res = asyncio.run(_burst.check_tail_risk_burst(threshold=10))

    assert res["fired"] == ["tail_price_size_cap"]
    assert res["suppressed"] == []
    assert len(dispatched) == 1
    assert "tail_price_size_cap" in dispatched[0]["subject"]
    assert res["per_gate"]["jumbo_quantity_cap"]["count"] == 3


# ---------------------------------------------------------------------------
# Dedupe-window behavior
# ---------------------------------------------------------------------------


def test_same_gate_dedupes_within_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gate that stays in burst for many cycles must page exactly
    once inside the dedupe TTL."""
    _, dispatched = _install_fakes(
        monkeypatch,
        counts={
            "tail_price_size_cap": [("sports", 25)],
            "jumbo_quantity_cap": [],
        },
        last_trips={
            "tail_price_size_cap": {
                "market_id": "KX-A",
                "last_at": "2026-05-05T11:00:00Z",
            },
        },
    )

    first = asyncio.run(_burst.check_tail_risk_burst(threshold=10))
    assert first["fired"] == ["tail_price_size_cap"]
    assert first["suppressed"] == []
    assert len(dispatched) == 1

    second = asyncio.run(_burst.check_tail_risk_burst(threshold=10))
    assert second["fired"] == []
    assert second["suppressed"] == ["tail_price_size_cap"]
    assert second["over_threshold"] == ["tail_price_size_cap"]
    assert len(dispatched) == 1  # still only the original alert


def test_independent_gates_dedupe_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second gate crossing the threshold *after* the first one
    already paged still gets its own alert — the dedupe key is the
    gate name, not "any tail-risk burst"."""
    fake, dispatched = _install_fakes(
        monkeypatch,
        counts={
            "tail_price_size_cap": [("sports", 25)],
            "jumbo_quantity_cap": [("sports", 3)],
        },
        last_trips={
            "tail_price_size_cap": {
                "market_id": "KX-A",
                "last_at": "2026-05-05T11:00:00Z",
            },
            "jumbo_quantity_cap": {
                "market_id": "KX-B",
                "last_at": "2026-05-05T11:30:00Z",
            },
        },
    )

    first = asyncio.run(_burst.check_tail_risk_burst(threshold=10))
    assert first["fired"] == ["tail_price_size_cap"]
    assert len(dispatched) == 1

    # Mutate the per-gate counts so the jumbo gate now also bursts;
    # the tail_price gate is still over the line and inside its
    # dedupe TTL, so it must stay quiet.
    second_counts = {
        "tail_price_size_cap": [("sports", 25)],
        "jumbo_quantity_cap": [("sports", 30)],
    }

    async def _fetch(query: str, *args: Any) -> list[dict[str, Any]]:
        assert "risk_gate_trips" in query
        rows = second_counts.get(args[0], [])
        return [{"vertical": v, "n": n} for v, n in rows]

    monkeypatch.setattr(_db, "fetch", _fetch)

    res = asyncio.run(_burst.check_tail_risk_burst(threshold=10))

    assert res["fired"] == ["jumbo_quantity_cap"]
    assert res["suppressed"] == ["tail_price_size_cap"]
    assert len(dispatched) == 2


def test_dedupe_expires_and_realerts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the dedupe row expires, a still-bursting gate re-pages so
    a long-lived incident doesn't silently fall off the on-call's
    radar."""
    fake, dispatched = _install_fakes(
        monkeypatch,
        counts={
            "tail_price_size_cap": [("sports", 25)],
            "jumbo_quantity_cap": [],
        },
        last_trips={
            "tail_price_size_cap": {
                "market_id": "KX-A",
                "last_at": "2026-05-05T11:00:00Z",
            },
        },
    )

    asyncio.run(
        _burst.check_tail_risk_burst(threshold=10, dedupe_ttl_seconds=60)
    )
    assert len(dispatched) == 1

    # Force-expire every dedupe row.
    for k in list(fake.rows.keys()):
        fake.rows[k] = datetime.now(timezone.utc) - timedelta(seconds=1)

    res = asyncio.run(
        _burst.check_tail_risk_burst(threshold=10, dedupe_ttl_seconds=60)
    )
    assert res["fired"] == ["tail_price_size_cap"]
    assert len(dispatched) == 2


# ---------------------------------------------------------------------------
# Operator opt-out
# ---------------------------------------------------------------------------


def test_threshold_zero_disables_burst_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``threshold=0`` short-circuits the helper before any DB read,
    so an operator can silence the burst page without disabling the
    underlying gate or the per-veto trip alert."""
    # Counts are rich but irrelevant — the helper must not look.
    _, dispatched = _install_fakes(
        monkeypatch,
        counts={
            "tail_price_size_cap": [("sports", 9999)],
            "jumbo_quantity_cap": [("sports", 9999)],
        },
    )

    res = asyncio.run(_burst.check_tail_risk_burst(threshold=0))

    assert res["disabled"] is True
    assert res["fired"] == []
    assert res["suppressed"] == []
    assert dispatched == []


# ---------------------------------------------------------------------------
# Alert helper body
# ---------------------------------------------------------------------------


def test_alert_helper_body_includes_burst_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The body must surface the per-vertical breakdown, the burst
    threshold, the rolling-window length, and the dashboard deep-link
    so the on-call can triage without hitting the dashboard."""
    out = _alerts.tail_risk_burst(
        gate_name="tail_price_size_cap",
        count=14,
        threshold=10,
        window_minutes=60,
        by_vertical=[("sports", 9), ("crypto", 5)],
        last_market_id="KX-DEEP-OTM-1",
        last_at="2026-05-05T10:11:12Z",
        dashboard_url="https://example.com/system#tail-risk",
        dry_run=True,
    )
    assert out["dry_run"] is True
    assert out["tag"] == "tail-risk-burst"
    assert "[TAIL-RISK BURST]" in out["subject"]
    assert "tail_price_size_cap" in out["subject"]
    assert "14" in out["subject"]
    assert "60m" in out["subject"]
    body = out["body_preview"]
    assert "sports=9" in body
    assert "crypto=5" in body
    assert "KX-DEEP-OTM-1" in body
    assert "2026-05-05T10:11:12Z" in body
    assert "https://example.com/system#tail-risk" in body
    assert "burst threshold" in body.lower()
