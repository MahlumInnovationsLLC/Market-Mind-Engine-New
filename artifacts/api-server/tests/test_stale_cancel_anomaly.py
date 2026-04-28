"""Tests for the rolling 24h stale-cancel baseline + anomaly evaluator
(task #711).

The pure-functional bits (`_is_volume_anomaly`, `_is_failure_spike`,
settings precedence) are tested directly; the DB-touching bits
(`record_observation`, `compute_baseline`) are tested with a stubbed
`db.fetch` / `db.execute`.

The dispatch path is tested by stubbing `dedupe_or_skip` and the alert
helpers so we can assert "the right alert was called with the right
telemetry" without sending real Postmark mail.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.orchestrator import stale_cancel_baseline as sc


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


# ---------------------------------------------------------------------------
# Settings precedence (env / global / per-vertical)
# ---------------------------------------------------------------------------


def test_resolve_int_default(monkeypatch):
    monkeypatch.delenv("AXIOM_STALE_CANCEL_VOLUME_FLOOR", raising=False)
    assert sc._resolve_int(
        "sports", {},
        "stale_cancel_volume_floor",
        "AXIOM_STALE_CANCEL_VOLUME_FLOOR",
        sc.DEFAULT_VOLUME_FLOOR,
    ) == sc.DEFAULT_VOLUME_FLOOR


def test_resolve_int_env_beats_default(monkeypatch):
    monkeypatch.setenv("AXIOM_STALE_CANCEL_VOLUME_FLOOR", "25")
    assert sc._resolve_int(
        "sports", {},
        "stale_cancel_volume_floor",
        "AXIOM_STALE_CANCEL_VOLUME_FLOOR",
        sc.DEFAULT_VOLUME_FLOOR,
    ) == 25


def test_resolve_int_global_beats_env(monkeypatch):
    monkeypatch.setenv("AXIOM_STALE_CANCEL_VOLUME_FLOOR", "25")
    overrides = {"stale_cancel_volume_floor": 50}
    assert sc._resolve_int(
        "sports", overrides,
        "stale_cancel_volume_floor",
        "AXIOM_STALE_CANCEL_VOLUME_FLOOR",
        sc.DEFAULT_VOLUME_FLOOR,
    ) == 50


def test_resolve_int_per_vertical_beats_global():
    overrides = {
        "stale_cancel_volume_floor": 50,
        "stale_cancel_volume_floor.sports": 100,
    }
    assert sc._resolve_int(
        "sports", overrides,
        "stale_cancel_volume_floor",
        "AXIOM_STALE_CANCEL_VOLUME_FLOOR",
        sc.DEFAULT_VOLUME_FLOOR,
    ) == 100
    # Other verticals still see global value.
    assert sc._resolve_int(
        "crypto", overrides,
        "stale_cancel_volume_floor",
        "AXIOM_STALE_CANCEL_VOLUME_FLOOR",
        sc.DEFAULT_VOLUME_FLOOR,
    ) == 50


def test_resolve_float_per_vertical(monkeypatch):
    monkeypatch.delenv("AXIOM_STALE_CANCEL_VOLUME_SIGMA", raising=False)
    overrides = {"stale_cancel_volume_sigma.sports": "2.5"}
    assert sc._resolve_float(
        "sports", overrides,
        "stale_cancel_volume_sigma",
        "AXIOM_STALE_CANCEL_VOLUME_SIGMA",
        sc.DEFAULT_VOLUME_SIGMA,
    ) == 2.5
    # Crypto falls back to default.
    assert sc._resolve_float(
        "crypto", overrides,
        "stale_cancel_volume_sigma",
        "AXIOM_STALE_CANCEL_VOLUME_SIGMA",
        sc.DEFAULT_VOLUME_SIGMA,
    ) == sc.DEFAULT_VOLUME_SIGMA


# ---------------------------------------------------------------------------
# Volume anomaly evaluator (pure)
# ---------------------------------------------------------------------------


def test_volume_anomaly_skips_when_window_too_small():
    baseline = {"count": 3, "mean": 1.0, "stdev": 0.5}
    is_anom, tel = sc._is_volume_anomaly(
        current_cancelled=100, baseline=baseline,
        sigma_threshold=3.0, floor=10,
    )
    assert is_anom is False
    assert tel["count"] == 3


def test_volume_anomaly_skips_when_stdev_zero():
    """A flat history (stdev=0) cannot produce a meaningful z-score —
    we'd alert on every tick."""
    baseline = {"count": 20, "mean": 5.0, "stdev": 0.0}
    is_anom, tel = sc._is_volume_anomaly(
        current_cancelled=100, baseline=baseline,
        sigma_threshold=3.0, floor=10,
    )
    assert is_anom is False
    assert tel["stdev"] == 0.0


def test_volume_anomaly_skips_below_floor():
    """Even at high z-score, sub-floor counts should not page."""
    baseline = {"count": 20, "mean": 0.5, "stdev": 0.2}
    is_anom, _ = sc._is_volume_anomaly(
        current_cancelled=5, baseline=baseline,
        sigma_threshold=3.0, floor=10,
    )
    assert is_anom is False


def test_volume_anomaly_fires_on_3sigma():
    baseline = {"count": 20, "mean": 2.0, "stdev": 1.0}
    # 2.0 + 3*1.0 = 5.0; 50 is way above.
    is_anom, tel = sc._is_volume_anomaly(
        current_cancelled=50, baseline=baseline,
        sigma_threshold=3.0, floor=10,
    )
    assert is_anom is True
    assert tel["z_score"] is not None and tel["z_score"] > 3.0


def test_volume_anomaly_does_not_fire_just_below_threshold():
    baseline = {"count": 20, "mean": 2.0, "stdev": 1.0}
    # 2.0 + 3*1.0 = 5.0; 4 is below.
    is_anom, tel = sc._is_volume_anomaly(
        current_cancelled=4, baseline=baseline,
        sigma_threshold=3.0, floor=4,
    )
    assert is_anom is False
    # Telemetry still records the z-score so operators can see how close it was.
    assert tel["z_score"] is not None


# ---------------------------------------------------------------------------
# Failure spike evaluator (pure)
# ---------------------------------------------------------------------------


def test_failure_spike_does_not_fire_on_single_bad_cycle():
    """One bad cycle in the tail isn't enough to satisfy the
    ``failure_window=2`` requirement — even if 24h history has many."""
    baseline = {
        "recent_failed_cycles": 1, "recent_failures_total": 5,
        "recent_failures": [0, 0, 5],  # only the last cycle failed
    }
    is_anom, tel = sc._is_failure_spike(
        baseline=baseline, failure_window=2, floor=3,
    )
    assert is_anom is False
    assert tel["tail"] == [0, 5]
    assert tel["tail_all_bad"] is False


def test_failure_spike_fires_on_two_consecutive_with_floor():
    baseline = {
        "recent_failed_cycles": 2, "recent_failures_total": 8,
        "recent_failures": [0, 0, 4, 4],  # last two cycles both bad
    }
    is_anom, tel = sc._is_failure_spike(
        baseline=baseline, failure_window=2, floor=3,
    )
    assert is_anom is True
    assert tel["failed_cycles"] == 2
    assert tel["tail"] == [4, 4]
    assert tel["tail_all_bad"] is True
    assert tel["tail_sum"] == 8


def test_failure_spike_does_not_fire_below_floor():
    """Two consecutive cycles with one failure each — that's a flake,
    not a spike. Wait for the failure_total to cross the floor."""
    baseline = {
        "recent_failed_cycles": 2, "recent_failures_total": 2,
        "recent_failures": [1, 1],
    }
    is_anom, _ = sc._is_failure_spike(
        baseline=baseline, failure_window=2, floor=3,
    )
    assert is_anom is False


def test_failure_spike_does_not_latch_after_recovery():
    """The big architect callout: the original 24h-aggregate version
    would *latch* a failure-spike condition for the rest of the day
    even after the venue recovered. The strict-tail rewrite must
    flip false the next cycle after the first clean one."""
    baseline = {
        # 24h aggregate still looks bad — 3 failed cycles, 9 total —
        # but the most recent cycle is clean.
        "recent_failed_cycles": 3, "recent_failures_total": 9,
        "recent_failures": [3, 3, 3, 0],
    }
    is_anom, tel = sc._is_failure_spike(
        baseline=baseline, failure_window=2, floor=3,
    )
    assert is_anom is False
    # Tail is [3, 0] — last entry clean, so tail_all_bad=False.
    assert tel["tail"] == [3, 0]
    assert tel["tail_all_bad"] is False


def test_failure_spike_handles_short_tail_gracefully():
    """A baseline with fewer recorded cycles than the failure_window
    must NOT page — we don't have enough data to claim a spike."""
    baseline = {
        "recent_failed_cycles": 1, "recent_failures_total": 5,
        "recent_failures": [5],  # only one cycle on file
    }
    is_anom, _ = sc._is_failure_spike(
        baseline=baseline, failure_window=2, floor=3,
    )
    assert is_anom is False


def test_failure_spike_handles_missing_tail_gracefully():
    """A baseline missing ``recent_failures`` (e.g. malformed cache)
    must not crash and must not page."""
    baseline = {
        "recent_failed_cycles": 5, "recent_failures_total": 10,
        # No 'recent_failures' key at all.
    }
    is_anom, tel = sc._is_failure_spike(
        baseline=baseline, failure_window=2, floor=3,
    )
    assert is_anom is False
    assert tel["tail"] == []


# ---------------------------------------------------------------------------
# compute_baseline / record_observation (DB-stubbed)
# ---------------------------------------------------------------------------


def test_compute_baseline_returns_zeros_on_empty_window(monkeypatch):
    async def _fake_fetch(query, *args):
        return []

    monkeypatch.setattr(sc.db, "fetch", _fake_fetch)
    out = _run(sc.compute_baseline("sports"))
    assert out["count"] == 0
    assert out["mean"] == 0.0
    assert out["stdev"] == 0.0
    assert out["recent_cancelled"] == []


def test_compute_baseline_computes_mean_and_stdev(monkeypatch):
    rows = [
        {"cancelled": 1, "failed": 0, "observed_at": None},
        {"cancelled": 3, "failed": 1, "observed_at": None},
        {"cancelled": 5, "failed": 0, "observed_at": None},
        {"cancelled": 7, "failed": 0, "observed_at": None},
    ]

    async def _fake_fetch(query, *args):
        return rows

    monkeypatch.setattr(sc.db, "fetch", _fake_fetch)
    out = _run(sc.compute_baseline("sports"))
    assert out["count"] == 4
    assert out["mean"] == 4.0  # (1+3+5+7)/4
    # Sample stdev of [1,3,5,7] = sqrt(20/3) ≈ 2.582
    assert 2.5 < out["stdev"] < 2.7
    assert out["max"] == 7
    assert out["recent_failed_cycles"] == 1
    assert out["recent_failures_total"] == 1
    assert out["recent_cancelled"] == [1, 3, 5, 7]
    assert out["recent_failures"] == [0, 1, 0, 0]


def test_compute_baseline_excludes_current_cycle_when_requested(monkeypatch):
    """``exclude_cycle_id`` filters out the named cycle so the
    evaluator can score a current-cycle spike against history that
    does not include itself. Without this guard the just-recorded
    spike dilutes the z-score it's being scored against."""
    captured: dict[str, Any] = {}

    async def _fake_fetch(query, *args):
        captured["query"] = query
        captured["args"] = args
        # Return only the rows that wouldn't have been the excluded one
        # — the test's job is to verify the query shape, not the DB.
        return [
            {"cancelled": 1, "failed": 0, "observed_at": None},
            {"cancelled": 3, "failed": 0, "observed_at": None},
        ]

    monkeypatch.setattr(sc.db, "fetch", _fake_fetch)
    out = _run(sc.compute_baseline("sports", exclude_cycle_id=999))
    # The query must include the cycle_id filter and pass cycle_id=999.
    assert "cycle_id" in captured["query"]
    assert captured["args"] == ("sports", 86400, 999)
    assert out["count"] == 2
    assert out["mean"] == 2.0


def test_compute_baseline_no_exclude_uses_simple_query(monkeypatch):
    """Without ``exclude_cycle_id`` the query has no cycle_id filter —
    keeps the common path (health snapshot) lean."""
    captured: dict[str, Any] = {}

    async def _fake_fetch(query, *args):
        captured["query"] = query
        captured["args"] = args
        return []

    monkeypatch.setattr(sc.db, "fetch", _fake_fetch)
    _run(sc.compute_baseline("sports"))
    assert "cycle_id" not in captured["query"]
    assert captured["args"] == ("sports", 86400)


def test_compute_baseline_fails_open_on_db_error(monkeypatch):
    async def _boom(query, *args):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(sc.db, "fetch", _boom)
    out = _run(sc.compute_baseline("sports"))
    # The cycle path must never get an exception out of compute_baseline.
    assert out["count"] == 0
    assert out["recent_cancelled"] == []


def test_record_observation_swallows_db_errors(monkeypatch):
    calls: list[Any] = []

    async def _boom(query, *args):
        calls.append((query, args))
        raise RuntimeError("write failed")

    monkeypatch.setattr(sc.db, "execute", _boom)
    # Must not raise — the cycle calls this in a fail-open path.
    _run(sc.record_observation(
        vertical="sports", cycle_id=42,
        scanned=2, cancelled=1, failed=0, max_age=30,
    ))
    assert len(calls) == 1


def test_record_observation_inserts_with_correct_args(monkeypatch):
    captured: dict[str, Any] = {}

    async def _ok(query, *args):
        captured["query"] = query
        captured["args"] = args
        return "INSERT 0 1"

    monkeypatch.setattr(sc.db, "execute", _ok)
    _run(sc.record_observation(
        vertical="sports", cycle_id=42,
        scanned=2, cancelled=1, failed=0, max_age=30,
    ))
    assert "stale_cancel_observations" in captured["query"]
    assert captured["args"] == ("sports", 42, 2, 1, 0, 30)


def test_record_observation_handles_null_cycle_id(monkeypatch):
    captured: dict[str, Any] = {}

    async def _ok(query, *args):
        captured["args"] = args
        return "INSERT 0 1"

    monkeypatch.setattr(sc.db, "execute", _ok)
    _run(sc.record_observation(
        vertical="sports", cycle_id=None,
        scanned=0, cancelled=0, failed=0, max_age=0,
    ))
    assert captured["args"][1] is None


# ---------------------------------------------------------------------------
# evaluate_and_alert — end-to-end orchestration
# ---------------------------------------------------------------------------


def _stub_baseline(monkeypatch, baseline_payload: dict):
    async def _fake_compute(
        vertical,
        *,
        window_secs=sc.DEFAULT_BASELINE_WINDOW_SECS,
        exclude_cycle_id=None,
    ):
        return {**baseline_payload, "vertical": vertical}
    monkeypatch.setattr(sc, "compute_baseline", _fake_compute)


def _stub_dedupe(monkeypatch, *, claim: bool = True):
    calls: list[tuple] = []

    async def _fake_dedupe(alert_type, dedupe_key, *, ttl_seconds):
        calls.append((alert_type, dedupe_key, ttl_seconds))
        return claim

    monkeypatch.setattr(sc.alerts_mod, "dedupe_or_skip", _fake_dedupe)
    return calls


def _stub_alert_helpers(monkeypatch):
    """Replace the two dispatch helpers with stubs that record their calls."""
    calls: dict[str, list[dict]] = {"volume": [], "failure": []}

    def _fake_vol(**kwargs):
        calls["volume"].append(kwargs)
        return {"ok": True}

    def _fake_fail(**kwargs):
        calls["failure"].append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(
        sc.alerts_mod, "stale_cancel_volume_anomaly", _fake_vol
    )
    monkeypatch.setattr(
        sc.alerts_mod, "stale_cancel_failure_spike", _fake_fail
    )
    return calls


def test_evaluate_and_alert_quiet_when_baseline_thin(monkeypatch):
    """Fewer than 5 samples = no alerts even with high cancel volume."""
    _stub_baseline(monkeypatch, {
        "count": 3, "mean": 1.0, "stdev": 0.5, "max": 1,
        "recent_failed_cycles": 0, "recent_failures_total": 0,
        "recent_cancelled": [1, 1, 1], "recent_failures": [0, 0, 0],
        "window_secs": 86400,
    })
    dedupe_calls = _stub_dedupe(monkeypatch)
    alert_calls = _stub_alert_helpers(monkeypatch)
    out = _run(sc.evaluate_and_alert(
        "sports", {}, {"cancelled": 100, "failed": 0, "scanned": 100, "max_age": 30},
    ))
    assert out["alerts"] == []
    assert dedupe_calls == []
    assert alert_calls["volume"] == []
    assert alert_calls["failure"] == []


def test_evaluate_and_alert_fires_volume_anomaly(monkeypatch):
    _stub_baseline(monkeypatch, {
        "count": 30, "mean": 2.0, "stdev": 1.0, "max": 5,
        "recent_failed_cycles": 0, "recent_failures_total": 0,
        "recent_cancelled": [2] * 30, "recent_failures": [0] * 30,
        "window_secs": 86400,
    })
    dedupe_calls = _stub_dedupe(monkeypatch, claim=True)
    alert_calls = _stub_alert_helpers(monkeypatch)
    out = _run(sc.evaluate_and_alert(
        "sports", {}, {"cancelled": 100, "failed": 0, "scanned": 100, "max_age": 30},
        cycle_id=999,
    ))
    assert "stale_cancel_volume_anomaly" in out["alerts"]
    assert out["volume"]["z_score"] is not None and out["volume"]["z_score"] > 3.0
    # Dedupe must be claimed *before* dispatch.
    assert dedupe_calls[0][0] == "stale_cancel_volume_anomaly"
    assert dedupe_calls[0][1] == "sports"
    assert len(alert_calls["volume"]) == 1
    assert alert_calls["volume"][0]["vertical"] == "sports"
    assert alert_calls["volume"][0]["current"] == 100
    assert alert_calls["volume"][0]["cycle_id"] == 999


def test_evaluate_and_alert_dedupe_suppresses(monkeypatch):
    """When dedupe_or_skip returns False, no alert is dispatched."""
    _stub_baseline(monkeypatch, {
        "count": 30, "mean": 2.0, "stdev": 1.0, "max": 5,
        "recent_failed_cycles": 0, "recent_failures_total": 0,
        "recent_cancelled": [2] * 30, "recent_failures": [0] * 30,
        "window_secs": 86400,
    })
    _stub_dedupe(monkeypatch, claim=False)
    alert_calls = _stub_alert_helpers(monkeypatch)
    out = _run(sc.evaluate_and_alert(
        "sports", {}, {"cancelled": 100, "failed": 0, "scanned": 100, "max_age": 30},
    ))
    # Anomaly still detected — alerts list is empty because dedupe suppressed dispatch.
    assert out["alerts"] == []
    assert alert_calls["volume"] == []


def test_evaluate_and_alert_fires_failure_spike(monkeypatch):
    _stub_baseline(monkeypatch, {
        "count": 30, "mean": 2.0, "stdev": 1.0, "max": 5,
        "recent_failed_cycles": 3, "recent_failures_total": 8,
        "recent_cancelled": [2] * 30,
        "recent_failures": [0] * 27 + [3, 3, 2],  # last 3 cycles all bad
        "window_secs": 86400,
    })
    _stub_dedupe(monkeypatch, claim=True)
    alert_calls = _stub_alert_helpers(monkeypatch)
    out = _run(sc.evaluate_and_alert(
        "sports", {}, {"cancelled": 1, "failed": 2, "scanned": 5, "max_age": 30},
        cycle_id=42,
    ))
    assert "stale_cancel_failure_spike" in out["alerts"]
    assert len(alert_calls["failure"]) == 1
    # Augmented aggregate: baseline (3 cycles / 8 failures) + the
    # current cycle (1 cycle / 2 failures) = 4 / 10. The evaluator
    # mirrors the current cycle into the alert telemetry so operators
    # see the count *including* the cycle that tripped the page.
    assert alert_calls["failure"][0]["failed_cycles"] == 4
    assert alert_calls["failure"][0]["failed_total"] == 10
    assert alert_calls["failure"][0]["cycle_id"] == 42


def test_default_failure_ttl_is_one_hour():
    """Pin the default failure TTL to 3600s so a future refactor can't
    silently shorten it back to the original 30-minute value (which
    paged operators every 30m during sustained venue outages).
    Volume TTL stays at 1800s — they're intentionally asymmetric."""
    assert sc.DEFAULT_FAILURE_TTL_SECS == 3600
    assert sc.DEFAULT_VOLUME_TTL_SECS == 1800
    assert sc.DEFAULT_FAILURE_TTL_SECS > sc.DEFAULT_VOLUME_TTL_SECS


def test_evaluate_and_alert_passes_default_ttl_to_failure_dedupe(monkeypatch):
    """End-to-end check that the failure dedupe call receives the
    1h TTL — guards against a future change that hard-codes a value
    or wires the wrong settings key."""
    _stub_baseline(monkeypatch, {
        "count": 30, "mean": 2.0, "stdev": 1.0, "max": 5,
        "recent_failed_cycles": 1, "recent_failures_total": 3,
        "recent_cancelled": [2] * 30,
        "recent_failures": [0] * 29 + [3],
        "window_secs": 86400,
    })
    dedupe_calls = _stub_dedupe(monkeypatch, claim=True)
    _stub_alert_helpers(monkeypatch)
    _run(sc.evaluate_and_alert(
        "sports", {},
        {"cancelled": 1, "failed": 2, "scanned": 5, "max_age": 30},
        cycle_id=999,
    ))
    fail_call = next(c for c in dedupe_calls if c[0] == "stale_cancel_failure_spike")
    assert fail_call[2] == sc.DEFAULT_FAILURE_TTL_SECS == 3600


def test_evaluate_and_alert_fires_immediately_on_2nd_consecutive_failure(monkeypatch):
    """The architect's #2 callout: detection must include the current
    cycle in the strict-tail check. With baseline tail = [3] (one
    prior failure) and current cycle failed=2, the augmented tail
    becomes [3, 2] — two consecutive bad cycles, fires THIS cycle
    rather than waiting for the next one to record."""
    _stub_baseline(monkeypatch, {
        "count": 30, "mean": 2.0, "stdev": 1.0, "max": 5,
        "recent_failed_cycles": 1, "recent_failures_total": 3,
        "recent_cancelled": [2] * 30,
        # Last entry only — the current cycle hasn't been recorded yet
        # (evaluator runs before record_observation).
        "recent_failures": [0] * 29 + [3],
        "window_secs": 86400,
    })
    _stub_dedupe(monkeypatch, claim=True)
    alert_calls = _stub_alert_helpers(monkeypatch)
    out = _run(sc.evaluate_and_alert(
        "sports", {},
        {"cancelled": 1, "failed": 2, "scanned": 5, "max_age": 30},
        cycle_id=200,
    ))
    assert "stale_cancel_failure_spike" in out["alerts"]
    assert len(alert_calls["failure"]) == 1
    # Aggregate counts in the alert telemetry must reflect the current
    # cycle (operator sees "2 cycles failed, 5 total" — which is N=1
    # prior + the current one).
    assert alert_calls["failure"][0]["failed_cycles"] == 2
    assert alert_calls["failure"][0]["failed_total"] == 5


def test_evaluate_and_alert_recovers_immediately_on_first_clean_cycle(monkeypatch):
    """The architect's #2 callout, recovery direction: a clean current
    cycle after a streak of failures must NOT page. Without the
    current-cycle injection the tail [3, 3] (prior history) would still
    look bad and we'd page once more."""
    _stub_baseline(monkeypatch, {
        "count": 30, "mean": 2.0, "stdev": 1.0, "max": 5,
        "recent_failed_cycles": 2, "recent_failures_total": 6,
        "recent_cancelled": [2] * 30,
        # Last two recorded cycles both bad.
        "recent_failures": [0] * 28 + [3, 3],
        "window_secs": 86400,
    })
    _stub_dedupe(monkeypatch, claim=True)
    alert_calls = _stub_alert_helpers(monkeypatch)
    out = _run(sc.evaluate_and_alert(
        "sports", {},
        # Current cycle is clean — venue recovered.
        {"cancelled": 1, "failed": 0, "scanned": 5, "max_age": 30},
        cycle_id=300,
    ))
    assert "stale_cancel_failure_spike" not in out["alerts"]
    assert alert_calls["failure"] == []
    # Telemetry should reflect the clean tail.
    assert out["failure"]["tail_all_bad"] is False
    # Augmented tail [3, 0] — last entry is the current cycle's clean run.
    assert out["failure"]["tail"] == [3, 0]


def test_evaluate_and_alert_fires_both_simultaneously(monkeypatch):
    _stub_baseline(monkeypatch, {
        "count": 30, "mean": 2.0, "stdev": 1.0, "max": 5,
        "recent_failed_cycles": 3, "recent_failures_total": 8,
        "recent_cancelled": [2] * 30,
        "recent_failures": [0] * 27 + [3, 3, 2],
        "window_secs": 86400,
    })
    _stub_dedupe(monkeypatch, claim=True)
    alert_calls = _stub_alert_helpers(monkeypatch)
    out = _run(sc.evaluate_and_alert(
        "sports", {}, {"cancelled": 100, "failed": 2, "scanned": 100, "max_age": 30},
    ))
    assert "stale_cancel_volume_anomaly" in out["alerts"]
    assert "stale_cancel_failure_spike" in out["alerts"]
    assert len(alert_calls["volume"]) == 1
    assert len(alert_calls["failure"]) == 1


def test_evaluate_and_alert_fails_open_on_baseline_error(monkeypatch):
    async def _boom(*a, **kw):
        raise RuntimeError("DB exploded")
    monkeypatch.setattr(sc, "compute_baseline", _boom)
    # Must not raise.
    out = _run(sc.evaluate_and_alert(
        "sports", {}, {"cancelled": 999, "failed": 99, "scanned": 999, "max_age": 30},
    ))
    assert out["alerts"] == []


def test_evaluate_and_alert_respects_per_vertical_floor(monkeypatch):
    """Per-vertical floor override raises the bar so a count that would
    have alerted with the default floor stays quiet."""
    _stub_baseline(monkeypatch, {
        "count": 30, "mean": 2.0, "stdev": 1.0, "max": 5,
        "recent_failed_cycles": 0, "recent_failures_total": 0,
        "recent_cancelled": [2] * 30, "recent_failures": [0] * 30,
        "window_secs": 86400,
    })
    dedupe_calls = _stub_dedupe(monkeypatch, claim=True)
    alert_calls = _stub_alert_helpers(monkeypatch)
    overrides = {"stale_cancel_volume_floor.sports": 200}
    out = _run(sc.evaluate_and_alert(
        "sports", overrides,
        {"cancelled": 100, "failed": 0, "scanned": 100, "max_age": 30},
    ))
    # 100 cancellations is high z-score but below 200 floor → no alert.
    assert "stale_cancel_volume_anomaly" not in out["alerts"]
    assert dedupe_calls == []
    assert alert_calls["volume"] == []


# ---------------------------------------------------------------------------
# Health snapshot integration (probe shape)
# ---------------------------------------------------------------------------


def test_health_probe_returns_per_vertical_cards(monkeypatch):
    from app import health as health_mod

    async def _fake_list(*, window_secs=86400):
        return [
            {
                "vertical": "sports",
                "window_secs": 86400,
                "count": 10, "mean": 2.0, "stdev": 1.0, "max": 5,
                "recent_failed_cycles": 0, "recent_failures_total": 0,
                "recent_cancelled": [1, 2, 3, 4, 5],
            },
            {
                "vertical": "crypto",
                "window_secs": 86400,
                "count": 5, "mean": 0.0, "stdev": 0.0, "max": 0,
                "recent_failed_cycles": 2, "recent_failures_total": 4,
                "recent_cancelled": [0, 0, 0, 0, 0],
            },
        ]

    monkeypatch.setattr(
        sc, "list_baselines", _fake_list
    )
    cards = _run(health_mod._probe_stale_cancel_baselines())
    assert len(cards) == 2
    by_v = {c["extras"]["vertical"]: c for c in cards}
    assert by_v["sports"]["status"] == "ok"
    assert by_v["sports"]["key"] == "stale_cancel_baseline:sports"
    assert by_v["sports"]["extras"]["recent_cancelled"] == [1, 2, 3, 4, 5]
    # Crypto has failures → degraded.
    assert by_v["crypto"]["status"] == "degraded"
    assert by_v["crypto"]["extras"]["recent_failed_cycles"] == 2


def test_health_probe_unconfigured_when_window_empty(monkeypatch):
    from app import health as health_mod

    async def _fake_list(*, window_secs=86400):
        return [{
            "vertical": "sports",
            "window_secs": 86400,
            "count": 0, "mean": 0.0, "stdev": 0.0, "max": 0,
            "recent_failed_cycles": 0, "recent_failures_total": 0,
            "recent_cancelled": [],
        }]

    monkeypatch.setattr(sc, "list_baselines", _fake_list)
    cards = _run(health_mod._probe_stale_cancel_baselines())
    assert len(cards) == 1
    assert cards[0]["status"] == "unconfigured"


def test_health_probe_fails_open_on_error(monkeypatch):
    from app import health as health_mod

    async def _boom(*, window_secs=86400):
        raise RuntimeError("DB exploded")

    monkeypatch.setattr(sc, "list_baselines", _boom)
    cards = _run(health_mod._probe_stale_cancel_baselines())
    # Snapshot endpoint stays alive — we just don't render baseline cards.
    assert cards == []


# ---------------------------------------------------------------------------
# Alert dispatch helpers (tag + body shape)
# ---------------------------------------------------------------------------


def test_volume_anomaly_alert_body_shape(monkeypatch):
    """Make sure the alert helper renders the telemetry the operator
    needs to triage — vertical, current count, baseline μ/σ, z-score."""
    captured: dict[str, Any] = {}

    def _fake_dispatch(subject, body, *, tag, dry_run=False):
        captured["subject"] = subject
        captured["body"] = body
        captured["tag"] = tag
        captured["dry_run"] = dry_run
        return {"ok": True}

    monkeypatch.setattr(sc.alerts_mod, "_dispatch", _fake_dispatch)
    sc.alerts_mod.stale_cancel_volume_anomaly(
        vertical="sports", cycle_id=42, current=100,
        mean=2.5, stdev=1.0, z_score=97.5,
        sigma_threshold=3.0, floor=10, sample_count=30,
        dry_run=True,
    )
    assert captured["tag"] == "stale-cancel-volume-anomaly"
    assert captured["dry_run"] is True
    assert "sports" in captured["subject"]
    assert "cancelled=100" in captured["subject"]
    assert "vertical" in captured["body"] and "sports" in captured["body"]
    assert "z_score" in captured["body"]
    assert "100" in captured["body"]


def test_failure_spike_alert_body_shape(monkeypatch):
    captured: dict[str, Any] = {}

    def _fake_dispatch(subject, body, *, tag, dry_run=False):
        captured["subject"] = subject
        captured["body"] = body
        captured["tag"] = tag
        return {"ok": True}

    monkeypatch.setattr(sc.alerts_mod, "_dispatch", _fake_dispatch)
    sc.alerts_mod.stale_cancel_failure_spike(
        vertical="crypto", cycle_id=99,
        failed_cycles=3, failed_total=10,
        failure_window=2, floor=3,
        dry_run=True,
    )
    assert captured["tag"] == "stale-cancel-failure-spike"
    assert "crypto" in captured["subject"]
    assert "failed_cycles=3" in captured["subject"]
    assert "failed_total=10" in captured["subject"]


# ---------------------------------------------------------------------------
# USER_VISIBLE_ALERTS + dedupe tag registration
# ---------------------------------------------------------------------------


def test_alert_types_registered_in_user_visible():
    """Both new alert types must be in USER_VISIBLE_ALERTS so settings
    page history surfaces them and so the dedupe key parsing works."""
    assert "stale_cancel_volume_anomaly" in sc.alerts_mod.USER_VISIBLE_ALERTS
    assert "stale_cancel_failure_spike" in sc.alerts_mod.USER_VISIBLE_ALERTS


def test_alert_tags_mapped_to_user_pref():
    """The dispatch tags must be wired into _TAG_TO_USER_PREF so opted-in
    users on the matching pref column get the broadcast."""
    mapping = sc.alerts_mod._TAG_TO_USER_PREF
    assert mapping.get("stale-cancel-volume-anomaly") == "email_drift"
    assert mapping.get("stale-cancel-failure-spike") == "email_halt"
