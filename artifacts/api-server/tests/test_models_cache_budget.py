"""Tests for the model cache size budget (task #66).

Cover the env resolver, the boot-time enforcement helper (warn + evict
non-canonical entries oldest-first), and the surfaced fields on
`disk_usage_summary`.
"""
from __future__ import annotations

import importlib
import logging
import os
from pathlib import Path

import pytest


@pytest.fixture
def fresh_loader(tmp_path, monkeypatch):
    """Reload `app.models_loader` against a tmp cache dir + budget."""
    monkeypatch.setenv("AXIOM_MODEL_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("AXIOM_MODEL_CACHE_MAX_BYTES", str(1024))
    from app import models_loader as _ml
    yield importlib.reload(_ml)
    monkeypatch.delenv("AXIOM_MODEL_CACHE_DIR", raising=False)
    monkeypatch.delenv("AXIOM_MODEL_CACHE_MAX_BYTES", raising=False)
    importlib.reload(_ml)


def test_resolve_budget_default(monkeypatch):
    monkeypatch.delenv("AXIOM_MODEL_CACHE_MAX_BYTES", raising=False)
    from app.config import resolve_model_cache_max_bytes, DEFAULT_MODEL_CACHE_MAX_BYTES
    assert resolve_model_cache_max_bytes() == DEFAULT_MODEL_CACHE_MAX_BYTES
    assert DEFAULT_MODEL_CACHE_MAX_BYTES == 2 * (1 << 30)


def test_resolve_budget_override(monkeypatch):
    monkeypatch.setenv("AXIOM_MODEL_CACHE_MAX_BYTES", "12345")
    from app.config import resolve_model_cache_max_bytes
    assert resolve_model_cache_max_bytes() == 12345


def test_resolve_budget_rejects_non_integer(monkeypatch):
    monkeypatch.setenv("AXIOM_MODEL_CACHE_MAX_BYTES", "two-gig")
    from app.config import resolve_model_cache_max_bytes
    with pytest.raises(RuntimeError, match="must be an integer"):
        resolve_model_cache_max_bytes()


def test_resolve_budget_rejects_non_positive(monkeypatch):
    monkeypatch.setenv("AXIOM_MODEL_CACHE_MAX_BYTES", "0")
    from app.config import resolve_model_cache_max_bytes
    with pytest.raises(RuntimeError, match="must be > 0"):
        resolve_model_cache_max_bytes()


def test_disk_usage_summary_exposes_budget(fresh_loader, tmp_path):
    summary = fresh_loader.disk_usage_summary()
    assert summary["budget_bytes"] == 1024
    assert summary["over_budget"] is False
    assert summary["total_bytes"] == 0


def test_enforce_budget_under_budget_is_quiet(fresh_loader, caplog):
    (fresh_loader.CACHE_ROOT).mkdir(parents=True, exist_ok=True)
    (fresh_loader.CACHE_ROOT / "models.tar").write_bytes(b"x" * 100)
    with caplog.at_level(logging.WARNING, logger="app.models_loader"):
        report = fresh_loader._enforce_cache_budget()
    assert report["over_budget"] is False
    assert report["total_bytes"] == 100
    assert not [r for r in caplog.records if "exceeds budget" in r.getMessage()]


def test_enforce_budget_warns_when_over(fresh_loader, caplog):
    fresh_loader.CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    # Two canonical archives, both well over the 1024-byte budget.
    (fresh_loader.CACHE_ROOT / "models.tar").write_bytes(b"a" * 800)
    (fresh_loader.CACHE_ROOT / "source.tar").write_bytes(b"b" * 800)
    with caplog.at_level(logging.WARNING, logger="app.models_loader"):
        report = fresh_loader._enforce_cache_budget()
    assert report["over_budget"] is True
    assert report["total_bytes"] == 1600
    assert any("exceeds budget" in r.getMessage() for r in caplog.records)
    # Canonical entries are NOT evicted (would cause a re-download loop).
    assert (fresh_loader.CACHE_ROOT / "models.tar").exists()
    assert (fresh_loader.CACHE_ROOT / "source.tar").exists()


def test_ensure_artifacts_warns_when_extract_pushes_over_budget(
    fresh_loader, monkeypatch, caplog
):
    """First-boot regression: cache may start empty (under budget) and end
    over budget once archives are downloaded + extracted. The post-load
    enforcement call inside ensure_artifacts must surface that.
    """
    import asyncio

    async def fake_ensure_one(name, prefix, archive_dest, extract_dest):
        # Simulate a download + extract that lands well over the 1024-byte
        # budget configured by the fixture.
        archive_dest.parent.mkdir(parents=True, exist_ok=True)
        archive_dest.write_bytes(b"x" * 900)
        extract_dest.mkdir(parents=True, exist_ok=True)
        (extract_dest / "blob.bin").write_bytes(b"y" * 900)
        return fresh_loader.ArchiveInfo(
            name=name,
            archive_path=str(archive_dest),
            extracted_dir=str(extract_dest),
            archive_bytes=900,
            extracted_bytes=900,
            file_count=1,
        )

    async def fake_download_manifest():
        return {"predictors": {}, "version": "test"}

    monkeypatch.setattr(fresh_loader, "_ensure_one", fake_ensure_one)
    monkeypatch.setattr(fresh_loader, "_download_manifest", fake_download_manifest)
    monkeypatch.setattr(fresh_loader, "_scan_predictors", lambda _d: {})

    async def noop_smoke():
        return {}

    monkeypatch.setattr(fresh_loader, "_smoke_load_canonical", noop_smoke)

    with caplog.at_level(logging.WARNING, logger="app.models_loader"):
        asyncio.run(fresh_loader.ensure_artifacts(force=True))

    assert any("exceeds budget" in r.getMessage() for r in caplog.records)
    summary = fresh_loader.disk_usage_summary()
    assert summary["over_budget"] is True
    assert summary["budget_bytes"] == 1024


def test_enforce_budget_evicts_non_canonical_oldest_first(fresh_loader):
    root = fresh_loader.CACHE_ROOT
    root.mkdir(parents=True, exist_ok=True)
    # Three unexpected entries with controlled mtimes; canonical archive
    # below budget but the unexpected ones push us over.
    (root / "models.tar").write_bytes(b"c" * 200)
    older = root / "stale_v1.tar"
    newer = root / "stale_v2.tar"
    older.write_bytes(b"o" * 600)
    newer.write_bytes(b"n" * 600)
    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))

    report = fresh_loader._enforce_cache_budget()
    # Oldest should be evicted first; newer kept until we're under budget.
    assert not older.exists(), "oldest non-canonical entry should be evicted"
    assert (root / "models.tar").exists()
    # Total now: 200 (canonical) + 600 (newer) = 800 <= 1024 budget.
    assert report["over_budget"] is False
    assert report["total_bytes"] <= 1024
