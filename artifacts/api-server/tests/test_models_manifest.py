"""Manifest verification tests for `app.models_loader`.

Cover the pure verification helper plus end-to-end behaviour of
`ensure_artifacts` (with storage + scan/smoke patched) for the four
relevant cases:
  * manifest matches the scanned predictors  -> ready=True
  * manifest mismatch on sha256              -> ready=False, error set
  * required predictor missing from disk     -> ready=False, error set
  * manifest JSON malformed                  -> ready=False, error set
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from app import models_loader


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def test_verify_against_manifest_ok():
    predictors = {
        "models/saved/meta_model.pkl": {"sha256": _sha("a"), "version": _sha("a")[:12]},
        "models/saved/calibrator.joblib": {"sha256": _sha("b"), "version": _sha("b")[:12]},
    }
    manifest = {
        "predictors": {
            "models/saved/meta_model.pkl": {"sha256": _sha("a"), "version": "1.0"},
            "models/saved/calibrator.joblib": {"sha256": _sha("b"), "version": "1.0"},
        }
    }
    report = models_loader._verify_against_manifest(predictors, manifest)
    assert report["ok"] is True
    assert report["matched_count"] == 2
    assert report["missing"] == []
    assert report["mismatch"] == []
    assert report["unexpected"] == []


def test_verify_against_manifest_mismatch_sha():
    predictors = {
        "models/saved/meta_model.pkl": {"sha256": _sha("BAD"), "version": "x"},
    }
    manifest = {
        "predictors": {
            "models/saved/meta_model.pkl": {"sha256": _sha("a"), "version": "1.0"},
        }
    }
    report = models_loader._verify_against_manifest(predictors, manifest)
    assert report["ok"] is False
    assert len(report["mismatch"]) == 1
    assert report["mismatch"][0]["name"] == "models/saved/meta_model.pkl"
    assert report["mismatch"][0]["expected_sha256"] == _sha("a")
    assert report["mismatch"][0]["actual_sha256"] == _sha("BAD")


def test_verify_against_manifest_required_missing():
    predictors: dict = {}
    manifest = {
        "predictors": {
            "models/saved/meta_model.pkl": {"sha256": _sha("a"), "required": True},
            "models/saved/optional.pkl": {"sha256": _sha("b"), "required": False},
        }
    }
    report = models_loader._verify_against_manifest(predictors, manifest)
    assert report["ok"] is False
    names = [m["name"] for m in report["missing"]]
    assert names == ["models/saved/meta_model.pkl"]


def test_verify_against_manifest_flags_unexpected_but_stays_ok():
    predictors = {
        "models/saved/meta_model.pkl": {"sha256": _sha("a"), "version": "x"},
        "models/saved/leftover.pkl": {"sha256": _sha("c"), "version": "x"},
    }
    manifest = {
        "predictors": {
            "models/saved/meta_model.pkl": {"sha256": _sha("a")},
        }
    }
    report = models_loader._verify_against_manifest(predictors, manifest)
    assert report["ok"] is True
    assert report["unexpected"] == ["models/saved/leftover.pkl"]


# --- _download_manifest validation -----------------------------------------


def _write_manifest(tmp_path: Path, body: str) -> Path:
    target = tmp_path / "models_manifest.json"
    target.write_text(body, encoding="utf-8")
    return target


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    """Redirect CACHE_ROOT/MANIFEST_LOCAL into a tmp dir and reset state."""
    monkeypatch.setattr(models_loader, "CACHE_ROOT", tmp_path)
    monkeypatch.setattr(models_loader, "MANIFEST_LOCAL", tmp_path / "models_manifest.json")
    models_loader._state = models_loader.ModelsState()
    yield tmp_path
    models_loader._state = models_loader.ModelsState()


def _patch_download(monkeypatch, body: str | bytes):
    async def fake_download(remote, dest, *, force=False):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, bytes):
            Path(dest).write_bytes(body)
        else:
            Path(dest).write_text(body, encoding="utf-8")
        return dest
    monkeypatch.setattr(models_loader.storage, "download_to_path", fake_download)


def test_download_manifest_rejects_bad_json(isolated_cache, monkeypatch):
    _patch_download(monkeypatch, "not json {")
    with pytest.raises(RuntimeError, match="not valid JSON"):
        asyncio.run(models_loader._download_manifest())


def test_download_manifest_rejects_missing_predictors(isolated_cache, monkeypatch):
    _patch_download(monkeypatch, json.dumps({"version": "1"}))
    with pytest.raises(RuntimeError, match="predictors"):
        asyncio.run(models_loader._download_manifest())


def test_download_manifest_rejects_bad_sha(isolated_cache, monkeypatch):
    _patch_download(monkeypatch, json.dumps({
        "predictors": {"x.pkl": {"sha256": "deadbeef"}}
    }))
    with pytest.raises(RuntimeError, match="malformed sha256"):
        asyncio.run(models_loader._download_manifest())


def test_download_manifest_normalizes_sha(isolated_cache, monkeypatch):
    sha = _sha("a")
    _patch_download(monkeypatch, json.dumps({
        "version": "1",
        "predictors": {"x.pkl": {"sha256": sha.upper(), "version": "1"}},
    }))
    data = asyncio.run(models_loader._download_manifest())
    assert data["predictors"]["x.pkl"]["sha256"] == sha


# --- ensure_artifacts integration ------------------------------------------


@pytest.fixture
def patched_pipeline(monkeypatch, isolated_cache):
    """Stub out archive download/extract + canonical smoke-load."""
    async def fake_ensure_one(name, prefix, archive_dest, extract_dest):
        Path(archive_dest).parent.mkdir(parents=True, exist_ok=True)
        Path(archive_dest).write_bytes(b"fake")
        Path(extract_dest).mkdir(parents=True, exist_ok=True)
        return models_loader.ArchiveInfo(
            name=name, archive_path=str(archive_dest),
            extracted_dir=str(extract_dest),
        )
    monkeypatch.setattr(models_loader, "_ensure_one", fake_ensure_one)

    async def fake_smoke():
        return {}
    monkeypatch.setattr(models_loader, "_smoke_load_canonical", fake_smoke)


def _set_scan(monkeypatch, predictors):
    monkeypatch.setattr(
        models_loader, "_scan_predictors", lambda _d: predictors
    )


def test_ensure_artifacts_ready_when_manifest_matches(monkeypatch, patched_pipeline):
    sha = _sha("meta")
    _set_scan(monkeypatch, {
        "models/saved/meta_model.pkl": {
            "size": 1, "mtime": 0.0, "sha256": sha, "version": sha[:12],
        },
    })
    _patch_download(monkeypatch, json.dumps({
        "version": "1",
        "predictors": {
            "models/saved/meta_model.pkl": {"sha256": sha, "version": "1.0"},
        },
    }))

    state = asyncio.run(models_loader.ensure_artifacts(force=True))
    assert state.ready is True
    assert state.error is None
    assert state.manifest_check["ok"] is True
    assert state.manifest_check["matched_count"] == 1


def test_ensure_artifacts_refuses_ready_on_mismatch(monkeypatch, patched_pipeline):
    sha_disk = _sha("CORRUPTED")
    sha_expected = _sha("meta")
    _set_scan(monkeypatch, {
        "models/saved/meta_model.pkl": {
            "size": 1, "mtime": 0.0, "sha256": sha_disk, "version": sha_disk[:12],
        },
    })
    _patch_download(monkeypatch, json.dumps({
        "version": "1",
        "predictors": {
            "models/saved/meta_model.pkl": {"sha256": sha_expected, "version": "1.0"},
        },
    }))

    state = asyncio.run(models_loader.ensure_artifacts(force=True))
    assert state.ready is False
    assert state.error is not None
    assert "manifest verification failed" in state.error
    assert state.manifest_check["ok"] is False
    assert any(
        m["name"] == "models/saved/meta_model.pkl"
        for m in state.manifest_check["mismatch"]
    )


def test_ensure_artifacts_refuses_ready_when_required_missing(
    monkeypatch, patched_pipeline
):
    _set_scan(monkeypatch, {})  # nothing on disk
    _patch_download(monkeypatch, json.dumps({
        "version": "1",
        "predictors": {
            "models/saved/meta_model.pkl": {"sha256": _sha("a"), "required": True},
        },
    }))

    state = asyncio.run(models_loader.ensure_artifacts(force=True))
    assert state.ready is False
    assert state.error is not None
    assert state.manifest_check["missing"]
    assert state.manifest_check["missing"][0]["name"] == "models/saved/meta_model.pkl"
