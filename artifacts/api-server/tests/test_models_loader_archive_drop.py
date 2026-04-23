"""Tests for dropping the reassembled tarballs after a successful extract.

`_ensure_one` should remove the `*.tar` once the extract dir is on disk
to halve the persistent cache footprint. The cleanup is opt-out via
`AXIOM_KEEP_MODEL_TARS`. A subsequent cold rebuild (extract dir gone)
must still re-download.
"""
from __future__ import annotations

import asyncio
import io
import tarfile
from pathlib import Path

import pytest

from app import models_loader


def _build_fake_tar(contents: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in contents.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def tar_cache(tmp_path, monkeypatch):
    """Reset loader state and patch storage.download_split_artifact to write
    a deterministic tar payload into the requested archive path."""
    payload = _build_fake_tar({"models/saved/meta_model.pkl": b"weights"})

    async def fake_download(prefix, bucket, dest):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(payload)
        return dest

    monkeypatch.setattr(
        models_loader.storage, "download_split_artifact", fake_download
    )
    models_loader._state = models_loader.ModelsState()
    yield tmp_path, payload
    models_loader._state = models_loader.ModelsState()


def _run_ensure_one(tmp_path: Path, name: str = "models"):
    archive = tmp_path / f"{name}.tar"
    extract = tmp_path / name
    info = asyncio.run(
        models_loader._ensure_one(name, f"{name}_part_", archive, extract)
    )
    return info, archive, extract


def test_archive_dropped_after_successful_extract(tar_cache, monkeypatch):
    tmp_path, _ = tar_cache
    monkeypatch.delenv("AXIOM_KEEP_MODEL_TARS", raising=False)

    info, archive, extract = _run_ensure_one(tmp_path)

    assert extract.exists()
    assert (extract / "models" / "saved" / "meta_model.pkl").read_bytes() == b"weights"
    assert not archive.exists(), "tar should be dropped after extract by default"
    # archive_bytes is still recorded from before the unlink for observability.
    assert info.archive_bytes > 0


def test_archive_kept_when_opt_out_env_set(tar_cache, monkeypatch):
    tmp_path, payload = tar_cache
    monkeypatch.setenv("AXIOM_KEEP_MODEL_TARS", "1")

    _, archive, extract = _run_ensure_one(tmp_path)

    assert extract.exists()
    assert archive.exists(), "tar should be kept when opt-out env is set"
    assert archive.read_bytes() == payload


@pytest.mark.parametrize("val", ["true", "YES", "On"])
def test_opt_out_accepts_truthy_variants(tar_cache, monkeypatch, val):
    tmp_path, _ = tar_cache
    monkeypatch.setenv("AXIOM_KEEP_MODEL_TARS", val)

    _, archive, _ = _run_ensure_one(tmp_path)
    assert archive.exists()


def test_warm_cache_skips_download_when_archive_dropped(tar_cache, monkeypatch):
    tmp_path, _ = tar_cache
    monkeypatch.delenv("AXIOM_KEEP_MODEL_TARS", raising=False)

    # First run: download + extract + drop tar.
    info1, archive, extract = _run_ensure_one(tmp_path)
    assert extract.exists()
    assert not archive.exists()

    # Now break the downloader so a second call must NOT attempt one.
    async def boom(*a, **kw):
        raise AssertionError("download_split_artifact should not be called on warm cache")

    monkeypatch.setattr(models_loader.storage, "download_split_artifact", boom)

    info2, archive2, extract2 = _run_ensure_one(tmp_path)
    assert extract2.exists()
    assert not archive2.exists()
    assert info2.cached is True
    assert info2.extract_seconds == 0.0  # extract skipped on warm cache


def test_cold_rebuild_redownloads_after_extract_dir_removed(tar_cache, monkeypatch):
    tmp_path, payload = tar_cache
    monkeypatch.delenv("AXIOM_KEEP_MODEL_TARS", raising=False)

    info1, archive, extract = _run_ensure_one(tmp_path)
    assert not archive.exists()

    # Simulate operator wiping the extract dir to force a cold rebuild.
    import shutil
    shutil.rmtree(extract)

    download_calls = {"n": 0}
    original = models_loader.storage.download_split_artifact

    async def counted(prefix, bucket, dest):
        download_calls["n"] += 1
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(payload)
        return dest

    monkeypatch.setattr(
        models_loader.storage, "download_split_artifact", counted
    )

    info2, archive2, extract2 = _run_ensure_one(tmp_path)
    assert download_calls["n"] == 1
    assert extract2.exists()
    assert not archive2.exists(), "tar should be dropped on the cold rebuild too"
    assert info2.cached is False
