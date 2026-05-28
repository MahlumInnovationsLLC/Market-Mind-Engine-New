"""Outbound retry-hygiene tests (task #929).

Verifies that the previously-raw outbound calls now go through a retry
policy:

  * **Sportradar** reads use the shared ``retry_request`` wrapper.
  * **Stripe** SDK calls inherit ``stripe.max_network_retries``.
  * **Clerk** proxy retries idempotent verbs (GET/HEAD) but not writes.
  * **OpenAI / Anthropic** clients are built with an explicit
    ``max_retries``.

Each provider gets a happy path + a transient-failure path (where
applicable).  Everything is hermetic — no network, no DB.
"""
from __future__ import annotations

import asyncio
import types

import httpx
import pytest

from app import http_retry


@pytest.fixture(autouse=True)
def _reset_counters():
    http_retry.reset_counters()
    yield
    http_retry.reset_counters()


async def _no_sleep(_):
    return None


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def _resp(status: int, *, content: bytes = b"{}", headers: dict | None = None):
    return httpx.Response(status, headers=headers or {}, content=content)


# ---------------------------------------------------------------------------
# Sportradar — shared retry_request wrapper
# ---------------------------------------------------------------------------


class _FakeSportradarClient:
    """Stands in for ``httpx.AsyncClient`` used in sportradar._get_json."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None):  # noqa: ARG002
        self.calls += 1
        item = self._responses[min(self.calls - 1, len(self._responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


def _patch_sportradar_client(monkeypatch, fake):
    from app.feeds import sportradar

    monkeypatch.setattr(
        sportradar.httpx, "AsyncClient", lambda *a, **k: fake
    )
    # Keep backoff instant so the retry test doesn't actually sleep.
    monkeypatch.setattr(http_retry.asyncio, "sleep", _no_sleep)
    return sportradar


def test_sportradar_happy_path(monkeypatch):
    fake = _FakeSportradarClient([_resp(200, content=b'{"ok": true}')])
    sportradar = _patch_sportradar_client(monkeypatch, fake)

    out = _run(sportradar._get_json("nba", "https://x/y.json", "key"))
    assert out == {"ok": True}
    assert fake.calls == 1


def test_sportradar_retries_transient_5xx(monkeypatch):
    fake = _FakeSportradarClient(
        [_resp(503), _resp(200, content=b'{"ok": true}')]
    )
    sportradar = _patch_sportradar_client(monkeypatch, fake)

    out = _run(sportradar._get_json("nba", "https://x/y.json", "key"))
    assert out == {"ok": True}
    assert fake.calls == 2  # retried once after the 503
    snap = http_retry.snapshot_counters()
    assert snap["sportradar:nba"]["total_retries"] == 1


# ---------------------------------------------------------------------------
# Stripe — explicit max_network_retries
# ---------------------------------------------------------------------------


def test_stripe_client_sets_max_network_retries(monkeypatch):
    from app import billing

    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_dummy")
    # Start from a clean slate so we prove _stripe_client sets it.
    billing.stripe.max_network_retries = 0

    client = billing._stripe_client()
    assert client.max_network_retries == billing._STRIPE_MAX_NETWORK_RETRIES
    assert client.max_network_retries >= 1


def test_stripe_client_requires_key(monkeypatch):
    from fastapi import HTTPException

    from app import billing

    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    with pytest.raises(HTTPException) as ei:
        billing._stripe_client()
    assert ei.value.status_code == 503


# ---------------------------------------------------------------------------
# OpenAI / Anthropic — explicit max_retries on the SDK clients
# ---------------------------------------------------------------------------


def _patch_settings(monkeypatch, **kw):
    from app.swarm import runner

    cfg = types.SimpleNamespace(
        openai_api_key=kw.get("openai_api_key", "sk-openai"),
        anthropic_api_key=kw.get("anthropic_api_key", "sk-anthropic"),
    )
    monkeypatch.setattr(runner, "get_settings", lambda: cfg)
    return runner


def test_openai_client_built_with_max_retries(monkeypatch):
    import openai

    runner = _patch_settings(monkeypatch)
    runner._openai_client = None

    captured: dict = {}

    class _FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(openai, "AsyncOpenAI", _FakeOpenAI)

    runner._get_openai()
    assert captured["max_retries"] == runner._LLM_MAX_RETRIES
    assert captured["max_retries"] >= 1
    runner._openai_client = None


def test_anthropic_client_built_with_max_retries(monkeypatch):
    import anthropic

    runner = _patch_settings(monkeypatch)
    runner._anthropic_client = None

    captured: dict = {}

    class _FakeAnthropic:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _FakeAnthropic)

    runner._get_anthropic()
    assert captured["max_retries"] == runner._LLM_MAX_RETRIES
    assert captured["max_retries"] >= 1
    runner._anthropic_client = None


# ---------------------------------------------------------------------------
# Clerk proxy — retry idempotent verbs only
# ---------------------------------------------------------------------------


class _FakeClerkClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def request(self, method, path, content=None, headers=None):  # noqa: ARG002
        self.calls += 1
        item = self._responses[min(self.calls - 1, len(self._responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


def _build_clerk_app(monkeypatch, fake):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app import clerk_proxy

    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_clerk")
    monkeypatch.setattr(
        clerk_proxy.httpx, "AsyncClient", lambda *a, **k: fake
    )
    monkeypatch.setattr(http_retry.asyncio, "sleep", _no_sleep)
    app = FastAPI()
    assert clerk_proxy.register_clerk_proxy(app) is True
    return TestClient(app)


def test_clerk_proxy_get_retries_transient_5xx(monkeypatch):
    fake = _FakeClerkClient([_resp(503), _resp(200, content=b"ok")])
    client = _build_clerk_app(monkeypatch, fake)

    r = client.get("/api/__clerk/v1/client")
    assert r.status_code == 200
    assert fake.calls == 2  # retried once


def test_clerk_proxy_post_is_single_shot(monkeypatch):
    # A POST that fails transiently must NOT be retried (avoid double-apply).
    fake = _FakeClerkClient([_resp(503, content=b"boom")])
    client = _build_clerk_app(monkeypatch, fake)

    r = client.post("/api/__clerk/v1/client/sessions")
    assert r.status_code == 503
    assert fake.calls == 1  # no retry on a write verb
