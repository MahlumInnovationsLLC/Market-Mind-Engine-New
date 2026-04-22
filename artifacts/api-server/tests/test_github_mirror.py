"""Tests for the GitHub model-version mirror.

Exercises the configuration gate, the dedupe-by-version state file, the
not-ready guard, and the contents-API call sequence (idempotent skip when
the remote sha256 already matches).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app import github_mirror, models_loader


@pytest.fixture
def cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the loader cache + state file at a tmp dir for isolation."""
    monkeypatch.setattr(models_loader, "CACHE_ROOT", tmp_path, raising=True)
    monkeypatch.setattr(
        models_loader, "MANIFEST_LOCAL", tmp_path / "models_manifest.json",
        raising=True,
    )
    monkeypatch.setattr(
        models_loader, "MODELS_ARCHIVE", tmp_path / "models.tar", raising=True,
    )
    monkeypatch.setattr(
        models_loader, "SOURCE_ARCHIVE", tmp_path / "source.tar", raising=True,
    )
    return tmp_path


@pytest.fixture
def env_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_MIRROR_REPO", "axiom/recovery-mirror")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_dummy_test_token")
    monkeypatch.setenv("GITHUB_MIRROR_BRANCH", "main")
    monkeypatch.setenv("GITHUB_API_BASE", "https://api.github.test")


@pytest.fixture
def ready_state(monkeypatch: pytest.MonkeyPatch, cache_dir: Path) -> dict[str, Any]:
    state = models_loader.ModelsState()
    state.ready = True
    state.manifest = {"version": "2026.04.22", "generated_at": None, "predictor_count": 1}
    monkeypatch.setattr(models_loader, "get_state", lambda: state)
    # Drop a manifest + tiny "archive" file so _collect_artifact_files sees them.
    (cache_dir / "models_manifest.json").write_text('{"predictors":{}}')
    (cache_dir / "models.tar").write_bytes(b"models-tar-payload")
    (cache_dir / "source.tar").write_bytes(b"source-tar-payload")
    return state.manifest


@pytest.fixture(autouse=True)
def reset_module_state() -> None:
    github_mirror._last_result = None
    yield
    github_mirror._last_result = None


def _install_mock_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler,
) -> list[httpx.Request]:
    captured: list[httpx.Request] = []

    def _mock_handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    transport = httpx.MockTransport(_mock_handler)
    real_client = github_mirror._client

    def _client(cfg: dict[str, str]) -> httpx.AsyncClient:
        c = real_client(cfg)
        c._transport = transport  # type: ignore[attr-defined]
        return c

    monkeypatch.setattr(github_mirror, "_client", _client)
    return captured


def test_disabled_when_env_missing(cache_dir: Path) -> None:
    assert github_mirror.is_enabled() is False
    result = asyncio.run(github_mirror.mirror_active_model())
    assert result.skipped is True
    assert result.ok is False
    assert "not configured" in (result.reason or "")


def test_skipped_when_loader_not_ready(
    monkeypatch: pytest.MonkeyPatch, cache_dir: Path, env_configured: None
) -> None:
    state = models_loader.ModelsState()  # ready=False by default
    monkeypatch.setattr(models_loader, "get_state", lambda: state)
    result = asyncio.run(github_mirror.mirror_active_model())
    assert result.skipped is True
    assert "not ready" in (result.reason or "")


def test_pushes_then_dedupes_on_second_call(
    monkeypatch: pytest.MonkeyPatch,
    cache_dir: Path,
    env_configured: None,
    ready_state: dict[str, Any],
) -> None:
    # Mock GitHub: every GET returns 404 (file does not exist), every PUT
    # returns 201. After the first run the version is recorded in the state
    # file and the second run must short-circuit without any HTTP calls.
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(404, json={"message": "Not Found"})
        if req.method == "PUT":
            return httpx.Response(201, json={"content": {"sha": "deadbeef"}})
        return httpx.Response(500)

    captured = _install_mock_transport(monkeypatch, handler)

    result = asyncio.run(github_mirror.mirror_active_model())
    assert result.ok, result.reason
    assert result.skipped is False
    assert result.version == "2026.04.22"
    # 3 artifact files + 1 latest pointer = 4 PUTs (each preceded by a GET probe).
    puts = [r for r in captured if r.method == "PUT"]
    gets = [r for r in captured if r.method == "GET"]
    assert len(puts) == 4
    assert len(gets) == 4
    # All pushes hit the version-scoped tree:
    assert any("/contents/models/2026.04.22/models.tar" in str(r.url) for r in puts)
    assert any("/contents/models/2026.04.22/source.tar" in str(r.url) for r in puts)
    assert any("/contents/models/2026.04.22/models_manifest.json" in str(r.url) for r in puts)
    assert any("/contents/models/latest.json" in str(r.url) for r in puts)
    # Files actually pushed are tracked in the result.
    assert len(result.files_pushed) == 4
    # State file persisted.
    state_path = cache_dir / "github_mirror_state.json"
    assert state_path.exists()
    persisted = json.loads(state_path.read_text())
    assert persisted["last_version"] == "2026.04.22"

    # Second call: same version, no HTTP traffic.
    captured.clear()
    result2 = asyncio.run(github_mirror.mirror_active_model())
    assert result2.ok is True
    assert result2.skipped is True
    assert "already mirrored" in (result2.reason or "")
    assert captured == []


def test_force_repushes_only_changed_blobs(
    monkeypatch: pytest.MonkeyPatch,
    cache_dir: Path,
    env_configured: None,
    ready_state: dict[str, Any],
) -> None:
    # Pre-populate the persisted state for a different version so the
    # version dedupe doesn't trigger; then have GitHub report identical
    # sha256 for one file and missing for the others.
    models_tar_bytes = (cache_dir / "models.tar").read_bytes()
    same_sha = hashlib.sha256(models_tar_bytes).hexdigest()  # noqa: F841

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            url = str(req.url)
            if "models/2026.04.22/models.tar" in url:
                # Pretend the remote has identical content already.
                return httpx.Response(
                    200,
                    json={
                        "sha": "blobsha-models",
                        "encoding": "base64",
                        "content": base64.b64encode(models_tar_bytes).decode(),
                    },
                )
            return httpx.Response(404, json={"message": "Not Found"})
        if req.method == "PUT":
            return httpx.Response(201, json={"content": {"sha": "x"}})
        return httpx.Response(500)

    captured = _install_mock_transport(monkeypatch, handler)
    result = asyncio.run(github_mirror.mirror_active_model(force=True))
    assert result.ok, result.reason
    puts = [r for r in captured if r.method == "PUT"]
    # models.tar must NOT be re-uploaded (sha matched), but the other 2
    # artifact files + latest.json must be PUT — total 3.
    assert len(puts) == 3
    assert not any("models/2026.04.22/models.tar" in str(r.url) for r in puts)


def test_failure_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch,
    cache_dir: Path,
    env_configured: None,
    ready_state: dict[str, Any],
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "boom"})

    _install_mock_transport(monkeypatch, handler)
    result = asyncio.run(github_mirror.mirror_active_model())
    assert result.ok is False
    assert result.skipped is False
    assert "github_mirror" in (result.reason or "") or "500" in (result.reason or "")
    # Module-level state holds the latest result for the status endpoint.
    assert github_mirror.last_result() is not None
