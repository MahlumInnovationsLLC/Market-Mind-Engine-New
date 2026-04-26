"""Tests for the Kalshi cluster-mismatch operator alert (task #520).

The boot-time / periodic Kalshi cluster probe surfaces a red dashboard
card when the configured ``KALSHI_ENV`` does not match the cluster the
API key actually authenticates against. This test suite exercises the
follow-on Postmark alert that fans out the same verdict so the on-call
gets paged immediately, instead of waiting for someone to look at the
operator dashboard.

Three things matter for this signal to be useful:

  1. A wrong-cluster probe result triggers exactly one alert (with a
     subject that names the env mismatch).
  2. Repeated probes within the dedupe TTL — the boot probe on a
     restart, every periodic re-probe — do not re-page.
  3. Probe results that are NOT the wrong-cluster case (probe ok,
     credentials missing, both clusters reject, transient 5xx) never
     fire this alert.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app import db as _db
from app.kalshi import client as kalshi_client
from app.notifications import alerts as _alerts


class _FakeDedupe:
    def __init__(self) -> None:
        self.rows: dict[str, datetime] = {}
        self.dispatched: list[dict[str, Any]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        assert "notification_dedupe" in query
        key, ttl = args[0], int(args[1])
        now = datetime.now(timezone.utc)
        if key not in self.rows or self.rows[key] <= now:
            self.rows[key] = now + timedelta(seconds=ttl)
            return {"ok": 1}
        return None


@pytest.fixture
def fake_pipe(monkeypatch: pytest.MonkeyPatch) -> _FakeDedupe:
    fake = _FakeDedupe()
    monkeypatch.setattr(_db, "fetchrow", fake.fetchrow)

    def _fake_dispatch(
        subject: str,
        text_body: str,
        *,
        tag: str,
        dry_run: bool,
        html_body: str | None = None,
    ) -> dict[str, Any]:
        fake.dispatched.append(
            {"subject": subject, "body": text_body, "tag": tag}
        )
        return {"tag": tag, "dispatched": True}

    monkeypatch.setattr(_alerts, "_dispatch", _fake_dispatch)
    kalshi_client._cluster_probe_state.clear()
    kalshi_client._cluster_probe_state["checked"] = False
    return fake


def _alerts_only(fake: _FakeDedupe) -> list[dict[str, Any]]:
    return [d for d in fake.dispatched if d["tag"] == "kalshi-cluster-mismatch"]


def _record_and_drain(result: dict[str, Any]) -> dict[str, Any]:
    """Record a probe result via the production code path and drain
    any tasks the dispatch hook scheduled on the running loop."""

    async def _go() -> dict[str, Any]:
        stamped = kalshi_client._record_cluster_probe_result(result)
        # Let the fire-and-forget dispatch task run to completion.
        for _ in range(5):
            pending = [
                t
                for t in asyncio.all_tasks()
                if t is not asyncio.current_task() and not t.done()
            ]
            if not pending:
                break
            await asyncio.gather(*pending, return_exceptions=True)
        return stamped

    return asyncio.run(_go())


# ---------------------------------------------------------------------------
# Wrong-cluster verdict triggers exactly one alert with a useful subject
# ---------------------------------------------------------------------------


def test_wrong_cluster_result_dispatches_alert(fake_pipe: _FakeDedupe) -> None:
    _record_and_drain({
        "ok": False,
        "configured_env": "prod",
        "key_belongs_to": "demo",
        "status": 401,
        "error": "Kalshi credentials look valid but were rejected by cluster 'prod'",
    })
    msgs = _alerts_only(fake_pipe)
    assert len(msgs) == 1
    subject = msgs[0]["subject"]
    # Subject names the env mismatch in the format the task spec calls out.
    assert "demo" in subject
    assert "prod" in subject
    assert "authenticates against demo, not prod" in subject
    body = msgs[0]["body"]
    assert "KALSHI_ENV=demo" in body
    assert "configured_env" in body and "key_belongs_to" in body


# ---------------------------------------------------------------------------
# Dedupe — restarts and periodic re-probes do not re-page
# ---------------------------------------------------------------------------


def test_repeated_wrong_cluster_results_dedupe(fake_pipe: _FakeDedupe) -> None:
    bad = {
        "ok": False,
        "configured_env": "prod",
        "key_belongs_to": "demo",
        "status": 401,
        "error": "wrong cluster",
    }
    _record_and_drain(bad)
    _record_and_drain(bad)  # simulate periodic re-probe
    _record_and_drain(bad)  # simulate restart
    assert len(_alerts_only(fake_pipe)) == 1


def test_dedupe_uses_configured_to_detected_pair(fake_pipe: _FakeDedupe) -> None:
    """Different (configured, detected) pairs are different incidents
    and are paged separately."""
    _record_and_drain({
        "ok": False,
        "configured_env": "prod",
        "key_belongs_to": "demo",
        "status": 401,
        "error": "wrong cluster",
    })
    _record_and_drain({
        "ok": False,
        "configured_env": "demo",
        "key_belongs_to": "prod",
        "status": 401,
        "error": "wrong cluster",
    })
    msgs = _alerts_only(fake_pipe)
    assert len(msgs) == 2
    subjects = {m["subject"] for m in msgs}
    assert any("demo, not prod" in s for s in subjects)
    assert any("prod, not demo" in s for s in subjects)


# ---------------------------------------------------------------------------
# Non-wrong-cluster verdicts never fire this alert
# ---------------------------------------------------------------------------


def test_ok_probe_result_does_not_alert(fake_pipe: _FakeDedupe) -> None:
    _record_and_drain({
        "ok": True,
        "configured_env": "prod",
        "key_belongs_to": "prod",
        "status": 200,
        "detail": "authenticated against cluster 'prod'",
    })
    assert _alerts_only(fake_pipe) == []


def test_missing_credentials_does_not_alert(fake_pipe: _FakeDedupe) -> None:
    """Missing creds are a config error, not a wrong-cluster signal —
    don't page on this with the wrong-cluster wording."""
    _record_and_drain({
        "ok": False,
        "configured_env": "prod",
        "key_belongs_to": None,
        "error": "missing KALSHI_API_KEY_ID or KALSHI_API_PRIVATE_KEY",
    })
    assert _alerts_only(fake_pipe) == []


def test_both_clusters_reject_does_not_alert(fake_pipe: _FakeDedupe) -> None:
    """Both clusters returning 401 means the key is bad in general, not
    pointed at the wrong env. The probe leaves ``key_belongs_to=None``
    in this case so the wrong-cluster alert must not fire."""
    _record_and_drain({
        "ok": False,
        "configured_env": "prod",
        "key_belongs_to": None,
        "status": 401,
        "error": "Kalshi credentials rejected by cluster 'prod' (http 401); "
                 "the other cluster 'demo' also returned http 401.",
    })
    assert _alerts_only(fake_pipe) == []


def test_transient_5xx_does_not_alert(fake_pipe: _FakeDedupe) -> None:
    """A 5xx from the configured cluster is not a wrong-env signal —
    the probe records ``key_belongs_to=None`` so this alert stays
    silent."""
    _record_and_drain({
        "ok": False,
        "configured_env": "prod",
        "key_belongs_to": None,
        "status": 503,
        "error": "http 503 from cluster 'prod': upstream down",
    })
    assert _alerts_only(fake_pipe) == []


# ---------------------------------------------------------------------------
# Alert helper body
# ---------------------------------------------------------------------------


def test_alert_helper_body_includes_diagnostics() -> None:
    out = _alerts.kalshi_cluster_mismatch(
        configured_env="prod",
        key_belongs_to="demo",
        status=401,
        detail="Kalshi credentials look valid but were rejected by cluster 'prod'",
        dry_run=True,
    )
    assert out["dry_run"] is True
    assert out["tag"] == "kalshi-cluster-mismatch"
    assert "demo, not prod" in out["subject"]
    body = out["body_preview"]
    assert "configured_env" in body and "prod" in body
    assert "key_belongs_to" in body and "demo" in body
    assert "KALSHI_ENV=demo" in body
    assert "401" in body
