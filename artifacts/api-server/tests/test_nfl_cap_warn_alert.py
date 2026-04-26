"""Tests for the NFL cap-bucket warn alert (task #438).

The dashboard panel from task #208 surfaces a visual warning when
per-league / per-game / Sunday-window utilisation crosses 80%, but
operators only see it if they happen to be looking at the page.  The
``notifications.cap_warnings.check_nfl_cap_warnings`` helper is the
background fan-out that pages the on-call when a bucket first crosses
the warn threshold.

Three things matter for this signal to be useful:

  1. Every bucket whose utilisation is at or above the warn threshold
     dispatches exactly one operator alert per incident — and buckets
     under the threshold (or with no utilisation, e.g. cap not
     configured) stay quiet.
  2. The dedupe table suppresses follow-up alerts on the same bucket
     within the TTL window so the on-call hears about each incident
     once, not once per cycle.
  3. The threshold is configurable (env var override + explicit
     argument) and clamps non-numeric / nonsense values back to the
     0.80 default the panel uses.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app import db as _db
from app.notifications import alerts as _alerts
from app.notifications import cap_warnings as _cap


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeDedupe:
    """In-memory stand-in for the `notification_dedupe` table.

    Mirrors the SQL semantics exercised by `alerts.dedupe_or_skip`:
    insert-or-claim with a TTL, return non-None when the caller is the
    winner (i.e. should send), None when an earlier claim is still
    live.
    """

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
    snapshot: dict[str, Any],
) -> tuple[_FakeDedupe, list[dict[str, Any]]]:
    """Wire up a fresh dedupe table, capture every dispatched alert, and
    pin `dashboard.get_nfl_exposure` to a known snapshot."""
    fake = _FakeDedupe()
    monkeypatch.setattr(_db, "fetchrow", fake.fetchrow)

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

    from app import dashboard as _dash

    async def _fake_exposure() -> dict[str, Any]:
        return snapshot

    monkeypatch.setattr(_dash, "get_nfl_exposure", _fake_exposure)
    return fake, dispatched


def _bucket(
    *,
    open_usd: float,
    cap_pct: float | None,
    cap_usd: float | None,
    utilization: float | None,
) -> dict[str, Any]:
    return {
        "open_usd": open_usd,
        "cap_pct": cap_pct,
        "cap_usd": cap_usd,
        "headroom_usd": (
            max(0.0, cap_usd - open_usd)
            if (cap_usd is not None) else None
        ),
        "utilization": utilization,
        "warn": (utilization is not None and utilization >= 0.80),
    }


def _snapshot(
    *,
    per_league_util: float | None,
    sunday_util: float | None,
    matchups: list[tuple[str, float | None]],
    bankroll: float = 10_000.0,
) -> dict[str, Any]:
    """Build a `get_nfl_exposure()`-shaped snapshot.

    Each utilisation argument controls one bucket's `utilization` value;
    pass ``None`` to render the bucket without a configured cap (which
    the helper must skip even if it's the only bucket).
    """
    cap_l = bankroll * 0.05
    cap_s = bankroll * 0.03
    cap_g = bankroll * 0.015

    per_matchup = []
    for matchup, util in matchups:
        per_matchup.append({
            **_bucket(
                open_usd=(util or 0.0) * cap_g,
                cap_pct=0.015,
                cap_usd=cap_g,
                utilization=util,
            ),
            "matchup": matchup,
        })

    return {
        "league": "NFL",
        "bankroll_usd": bankroll,
        "warn_threshold": 0.80,
        "per_league": _bucket(
            open_usd=(per_league_util or 0.0) * cap_l,
            cap_pct=0.05,
            cap_usd=cap_l,
            utilization=per_league_util,
        ),
        "sunday_window": _bucket(
            open_usd=(sunday_util or 0.0) * cap_s,
            cap_pct=0.03,
            cap_usd=cap_s,
            utilization=sunday_util,
        ),
        "per_matchup": per_matchup,
        "raw": {},
    }


# ---------------------------------------------------------------------------
# Detection: which buckets count as "over threshold"
# ---------------------------------------------------------------------------


def test_below_threshold_is_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    """Three under-threshold buckets must produce zero alerts — the
    operator panel still glows quietly but the on-call is not paged."""
    snap = _snapshot(
        per_league_util=0.50,
        sunday_util=0.40,
        matchups=[("BUF@MIA", 0.79)],
    )
    _, dispatched = _install_fakes(monkeypatch, snapshot=snap)

    res = asyncio.run(_cap.check_nfl_cap_warnings())

    assert res["checked"] is True
    assert res["over_threshold"] == []
    assert res["fired"] == []
    assert res["suppressed"] == []
    assert dispatched == []


def test_alert_fires_for_each_overshooting_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When per-league, Sunday-window and a matchup all cross 80%, each
    must produce its own page so the on-call can tell which bucket is
    about to start blocking bets."""
    snap = _snapshot(
        per_league_util=0.85,
        sunday_util=0.90,
        matchups=[
            ("BUF@MIA", 0.95),
            ("KC@DEN", 0.50),  # under — must stay quiet
        ],
    )
    _, dispatched = _install_fakes(monkeypatch, snapshot=snap)

    res = asyncio.run(_cap.check_nfl_cap_warnings())

    keys = sorted(res["fired"])
    assert keys == sorted([
        "NFL:per_league",
        "NFL:sunday_window",
        "NFL:per_matchup:BUF@MIA",
    ])
    assert sorted(res["over_threshold"]) == keys
    assert res["suppressed"] == []

    tags = [d["tag"] for d in dispatched]
    assert tags == ["cap-bucket-warn"] * 3

    subjects = " ".join(d["subject"] for d in dispatched)
    assert "per-league cap" in subjects
    assert "Sunday-window cap" in subjects
    assert "BUF@MIA" in subjects


def test_buckets_with_no_cap_or_no_utilization_are_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bucket with `utilization=None` (cap not configured, or bankroll
    unknown) must never page — the dashboard renders it as `warn=False`
    and so does the alert helper."""
    snap = _snapshot(
        per_league_util=None,    # bankroll unknown / no cap
        sunday_util=None,
        matchups=[("BUF@MIA", None)],
    )
    _, dispatched = _install_fakes(monkeypatch, snapshot=snap)

    res = asyncio.run(_cap.check_nfl_cap_warnings())

    assert res["fired"] == []
    assert res["over_threshold"] == []
    assert dispatched == []


def test_threshold_boundary_is_inclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Matches the panel's `>= 0.80` rule — a bucket exactly at 80% must
    page, not just buckets above 80%."""
    snap = _snapshot(
        per_league_util=0.80,
        sunday_util=0.7999,  # just under
        matchups=[],
    )
    _, dispatched = _install_fakes(monkeypatch, snapshot=snap)

    res = asyncio.run(_cap.check_nfl_cap_warnings())

    assert res["fired"] == ["NFL:per_league"]
    assert len(dispatched) == 1


# ---------------------------------------------------------------------------
# Dedupe: same bucket must not page every cycle
# ---------------------------------------------------------------------------


def test_same_bucket_dedupes_within_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bucket that stays over 80% for many cycles must page exactly
    once until the dedupe TTL expires."""
    snap = _snapshot(
        per_league_util=0.95,
        sunday_util=0.40,
        matchups=[],
    )
    _, dispatched = _install_fakes(monkeypatch, snapshot=snap)

    first = asyncio.run(_cap.check_nfl_cap_warnings())
    assert first["fired"] == ["NFL:per_league"]
    assert first["suppressed"] == []
    assert len(dispatched) == 1

    second = asyncio.run(_cap.check_nfl_cap_warnings())
    assert second["fired"] == []
    assert second["suppressed"] == ["NFL:per_league"]
    assert len(dispatched) == 1  # no extra alert dispatched


def test_independent_buckets_dedupe_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second bucket crossing the threshold after the first one
    already paged must still fire its own alert — the dedupe key is
    per-bucket, not per-incident."""
    snap_one = _snapshot(
        per_league_util=0.85,
        sunday_util=0.40,
        matchups=[],
    )
    fake, dispatched = _install_fakes(monkeypatch, snapshot=snap_one)

    asyncio.run(_cap.check_nfl_cap_warnings())
    assert [d["subject"] for d in dispatched if "per-league" in d["subject"]]

    # Sunday window now also crosses; per-league still over, but its
    # dedupe row is live so it must not re-page.
    snap_two = _snapshot(
        per_league_util=0.85,
        sunday_util=0.90,
        matchups=[],
    )

    from app import dashboard as _dash

    async def _new_exposure() -> dict[str, Any]:
        return snap_two

    monkeypatch.setattr(_dash, "get_nfl_exposure", _new_exposure)

    res = asyncio.run(_cap.check_nfl_cap_warnings())

    assert res["fired"] == ["NFL:sunday_window"]
    assert res["suppressed"] == ["NFL:per_league"]


def test_dedupe_expires_and_realerts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Once the dedupe row expires, a still-overshooting bucket must
    page again so a long-lived incident does not silently fall off the
    on-call's radar."""
    snap = _snapshot(
        per_league_util=0.95,
        sunday_util=0.40,
        matchups=[],
    )
    fake, dispatched = _install_fakes(monkeypatch, snapshot=snap)

    asyncio.run(_cap.check_nfl_cap_warnings(ttl_seconds=60))
    assert len(dispatched) == 1

    # Force-expire the dedupe row.
    for k in list(fake.rows.keys()):
        fake.rows[k] = datetime.now(timezone.utc) - timedelta(seconds=1)

    res = asyncio.run(_cap.check_nfl_cap_warnings(ttl_seconds=60))
    assert res["fired"] == ["NFL:per_league"]
    assert len(dispatched) == 2


# ---------------------------------------------------------------------------
# Threshold configuration
# ---------------------------------------------------------------------------


def test_threshold_argument_overrides_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit `threshold=` argument wins over the env var so the
    operator can probe the helper at a custom level (e.g. dry-run a
    50% test threshold) without reconfiguring the process."""
    snap = _snapshot(
        per_league_util=0.55,
        sunday_util=0.40,
        matchups=[],
    )
    _, dispatched = _install_fakes(monkeypatch, snapshot=snap)

    res = asyncio.run(_cap.check_nfl_cap_warnings(threshold=0.50))

    assert res["threshold"] == 0.50
    assert res["fired"] == ["NFL:per_league"]
    assert dispatched and "warn >= 50%" in dispatched[0]["subject"]


def test_env_var_threshold_is_picked_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AXIOM_NFL_CAP_WARN_THRESHOLD", "0.65")
    snap = _snapshot(
        per_league_util=0.66,
        sunday_util=0.40,
        matchups=[],
    )
    _, dispatched = _install_fakes(monkeypatch, snapshot=snap)

    res = asyncio.run(_cap.check_nfl_cap_warnings())

    assert res["threshold"] == pytest.approx(0.65)
    assert res["fired"] == ["NFL:per_league"]
    assert len(dispatched) == 1


def test_invalid_env_var_threshold_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Garbage / out-of-range threshold env vars must not page on every
    bucket on every cycle — the helper falls back to 0.80."""
    for bad in ["nope", "0", "-0.1", "1.5"]:
        monkeypatch.setenv("AXIOM_NFL_CAP_WARN_THRESHOLD", bad)
        assert _cap._read_threshold() == 0.80


# ---------------------------------------------------------------------------
# Alert helper body
# ---------------------------------------------------------------------------


def test_alert_helper_body_includes_cap_diagnostics() -> None:
    """The body must carry the open / cap / utilisation numbers the
    on-call needs to triage without hitting the dashboard."""
    out = _alerts.cap_bucket_warn(
        league="NFL",
        bucket="per_matchup",
        label="per-game cap (BUF@MIA)",
        matchup="BUF@MIA",
        open_usd=300.0,
        cap_usd=150.0,
        cap_pct=0.015,
        utilization=2.0,
        warn_threshold=0.80,
        bankroll_usd=10_000.0,
        dry_run=True,
    )
    assert out["dry_run"] is True
    assert out["tag"] == "cap-bucket-warn"
    assert "BUF@MIA" in out["subject"]
    assert "200" in out["subject"]  # 200% utilisation
    body = out["body_preview"]
    assert "BUF@MIA" in body
    assert "300.00" in body  # open_usd
    assert "150.00" in body  # cap_usd
    assert "200.00%" in body  # utilization_pct
    assert "80.00%" in body  # warn_threshold_pct
    assert "10,000.00" in body  # bankroll_usd
