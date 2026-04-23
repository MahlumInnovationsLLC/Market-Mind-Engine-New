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
    data, sig_status = asyncio.run(models_loader._download_manifest())
    assert data["predictors"]["x.pkl"]["sha256"] == sha
    assert sig_status == "skipped"


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


# --- operator alert on manifest-mismatch -----------------------------------


def _patch_alert(monkeypatch):
    """Stub the integrity alert helper and capture every call."""
    calls: list[dict] = []

    def fake_alert(**kwargs):
        calls.append(kwargs)
        return {"queued": True}

    from app.notifications import alerts as _alerts
    monkeypatch.setattr(_alerts, "model_integrity_failure", fake_alert)
    return calls


def test_ensure_artifacts_pages_operator_on_manifest_mismatch(
    monkeypatch, patched_pipeline
):
    sha_disk = _sha("CORRUPTED")
    sha_expected = _sha("meta")
    _set_scan(monkeypatch, {
        "models/saved/meta_model.pkl": {
            "size": 1, "mtime": 0.0, "sha256": sha_disk, "version": sha_disk[:12],
        },
    })
    _patch_download(monkeypatch, json.dumps({
        "version": "v42",
        "predictors": {
            "models/saved/meta_model.pkl": {"sha256": sha_expected, "version": "1.0"},
            "models/saved/missing_required.pkl": {"sha256": _sha("b")},
        },
    }))
    calls = _patch_alert(monkeypatch)
    models_loader._integrity_alert_sent = False

    state = asyncio.run(models_loader.ensure_artifacts(force=True))
    assert state.ready is False
    assert len(calls) == 1
    payload = calls[0]
    assert payload["manifest_version"] == "v42"
    assert "models/saved/meta_model.pkl" in payload["mismatch"]
    assert "models/saved/missing_required.pkl" in payload["missing"]
    assert "manifest verification failed" in payload["error"]


def test_integrity_alert_suppressed_within_same_boot_cycle(
    monkeypatch, patched_pipeline
):
    sha_disk = _sha("CORRUPTED")
    sha_expected = _sha("meta")
    _set_scan(monkeypatch, {
        "models/saved/meta_model.pkl": {
            "size": 1, "mtime": 0.0, "sha256": sha_disk, "version": sha_disk[:12],
        },
    })
    _patch_download(monkeypatch, json.dumps({
        "version": "v1",
        "predictors": {
            "models/saved/meta_model.pkl": {"sha256": sha_expected, "version": "1.0"},
        },
    }))
    calls = _patch_alert(monkeypatch)
    models_loader._integrity_alert_sent = False

    # First failed boot -> page once.
    asyncio.run(models_loader.ensure_artifacts(force=True))
    assert len(calls) == 1

    # Subsequent ensure_artifacts call within the same process must NOT
    # re-page (boot-cycle dedupe). force=False so the in-memory state
    # carries over.
    asyncio.run(models_loader.ensure_artifacts(force=False))
    assert len(calls) == 1

    # An operator-driven reload (force=True) clears the guard, so a
    # repeat failure pages again — operators expect feedback on reload.
    asyncio.run(models_loader.ensure_artifacts(force=True))
    assert len(calls) == 2


# --- signature verification ------------------------------------------------


import base64 as _base64

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def _make_keypair() -> tuple[Ed25519PrivateKey, str]:
    """Return (private_key, base64_public_key) suitable for the loader."""
    from cryptography.hazmat.primitives import serialization
    key = Ed25519PrivateKey.generate()
    pub_raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return key, _base64.b64encode(pub_raw).decode()


def _patch_manifest_and_sig(monkeypatch, manifest_bytes: bytes, sig_b64: bytes | None):
    """Patch storage.download_to_path so the manifest fetch returns
    `manifest_bytes` and the signature fetch returns `sig_b64` (or
    raises FileNotFoundError when sig_b64 is None)."""
    async def fake_download(remote, dest, *, force=False):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        if str(remote).endswith(".sig"):
            if sig_b64 is None:
                raise FileNotFoundError(remote)
            Path(dest).write_bytes(sig_b64 + b"\n")
        else:
            Path(dest).write_bytes(manifest_bytes)
        return dest
    monkeypatch.setattr(models_loader.storage, "download_to_path", fake_download)


def _patch_pubkey(monkeypatch, pub_b64: str | None):
    """Make `_load_manifest_public_key` return a key derived from pub_b64
    (or None) without depending on real Settings."""
    if pub_b64 is None:
        monkeypatch.setattr(models_loader, "_load_manifest_public_key", lambda: None)
        return
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    pub = Ed25519PublicKey.from_public_bytes(_base64.b64decode(pub_b64))
    monkeypatch.setattr(models_loader, "_load_manifest_public_key", lambda: pub)


def test_download_manifest_verifies_good_signature(isolated_cache, monkeypatch):
    key, pub_b64 = _make_keypair()
    payload = json.dumps({
        "version": "1",
        "predictors": {"x.pkl": {"sha256": _sha("x"), "version": "1"}},
    }).encode()
    sig = _base64.b64encode(key.sign(payload))
    _patch_manifest_and_sig(monkeypatch, payload, sig)
    _patch_pubkey(monkeypatch, pub_b64)

    data, status = asyncio.run(models_loader._download_manifest())
    assert status == "ok"
    assert data["predictors"]["x.pkl"]["sha256"] == _sha("x")


def test_download_manifest_rejects_bad_signature(isolated_cache, monkeypatch):
    key, pub_b64 = _make_keypair()
    payload = json.dumps({
        "version": "1",
        "predictors": {"x.pkl": {"sha256": _sha("x"), "version": "1"}},
    }).encode()
    # Sign DIFFERENT bytes than what gets served -> verification must fail.
    sig = _base64.b64encode(key.sign(b"tampered payload"))
    _patch_manifest_and_sig(monkeypatch, payload, sig)
    _patch_pubkey(monkeypatch, pub_b64)

    with pytest.raises(models_loader.ManifestSignatureError, match="signature invalid"):
        asyncio.run(models_loader._download_manifest())


def test_download_manifest_rejects_signature_by_wrong_key(isolated_cache, monkeypatch):
    legit_key, _ = _make_keypair()
    _, attacker_pub_b64 = _make_keypair()
    payload = json.dumps({
        "version": "1",
        "predictors": {"x.pkl": {"sha256": _sha("x")}},
    }).encode()
    # Signed by `legit_key` but the loader is configured to trust a
    # different key — mirrors the leaked-storage-key threat model.
    sig = _base64.b64encode(legit_key.sign(payload))
    _patch_manifest_and_sig(monkeypatch, payload, sig)
    _patch_pubkey(monkeypatch, attacker_pub_b64)

    with pytest.raises(models_loader.ManifestSignatureError):
        asyncio.run(models_loader._download_manifest())


def test_download_manifest_rejects_missing_signature_when_key_configured(
    isolated_cache, monkeypatch,
):
    _, pub_b64 = _make_keypair()
    payload = json.dumps({
        "version": "1",
        "predictors": {"x.pkl": {"sha256": _sha("x")}},
    }).encode()
    _patch_manifest_and_sig(monkeypatch, payload, None)  # no .sig in storage
    _patch_pubkey(monkeypatch, pub_b64)

    with pytest.raises(models_loader.ManifestSignatureError, match="failed to download"):
        asyncio.run(models_loader._download_manifest())


def test_ensure_artifacts_surfaces_signature_invalid_distinctly(
    monkeypatch, patched_pipeline
):
    """Signature failure must show up as reason='signature_invalid' in
    `manifest_check`, distinct from the sha-mismatch path. This is what
    `/api/health.models` will surface to the operator."""
    sha = _sha("meta")
    _set_scan(monkeypatch, {
        "models/saved/meta_model.pkl": {
            "size": 1, "mtime": 0.0, "sha256": sha, "version": sha[:12],
        },
    })
    key, pub_b64 = _make_keypair()
    payload = json.dumps({
        "version": "1",
        "predictors": {
            "models/saved/meta_model.pkl": {"sha256": sha, "version": "1.0"},
        },
    }).encode()
    bad_sig = _base64.b64encode(key.sign(b"different bytes"))
    _patch_manifest_and_sig(monkeypatch, payload, bad_sig)
    _patch_pubkey(monkeypatch, pub_b64)

    state = asyncio.run(models_loader.ensure_artifacts(force=True))
    assert state.ready is False
    assert state.manifest_check["reason"] == "signature_invalid"
    assert state.manifest_check["signature"] == "invalid"
    assert "signature invalid" in state.error


def test_ensure_artifacts_surfaces_sha_mismatch_distinctly(
    monkeypatch, patched_pipeline
):
    """sha mismatch must NOT be reported as signature failure — the two
    failure modes have different remediations and need different reasons."""
    sha_disk = _sha("CORRUPTED")
    sha_expected = _sha("meta")
    _set_scan(monkeypatch, {
        "models/saved/meta_model.pkl": {
            "size": 1, "mtime": 0.0, "sha256": sha_disk, "version": sha_disk[:12],
        },
    })
    key, pub_b64 = _make_keypair()
    payload = json.dumps({
        "version": "1",
        "predictors": {
            "models/saved/meta_model.pkl": {"sha256": sha_expected, "version": "1.0"},
        },
    }).encode()
    sig = _base64.b64encode(key.sign(payload))  # valid signature
    _patch_manifest_and_sig(monkeypatch, payload, sig)
    _patch_pubkey(monkeypatch, pub_b64)

    state = asyncio.run(models_loader.ensure_artifacts(force=True))
    assert state.ready is False
    assert state.manifest_check["reason"] == "sha_mismatch"
    assert state.manifest_check["signature"] == "ok"
    assert "sha mismatch" in state.error


# --- build_models_manifest --sign-key end-to-end ---------------------------


def test_build_script_round_trip_signing(tmp_path):
    """Run the build script with a generated key and verify the loader's
    signature check accepts the produced .sig file."""
    import importlib.util
    import subprocess
    import sys as _sys

    # Generate a keypair via the script's --gen-key.
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts" / "build_models_manifest.py"
    )
    key_path = tmp_path / "signing.pem"
    out = subprocess.run(
        [_sys.executable, str(script), "--gen-key", str(key_path)],
        check=True, capture_output=True, text=True,
    )
    # Last non-empty line is the public key.
    pub_b64 = [l for l in out.stdout.splitlines() if l.strip()][-1]
    assert key_path.exists()

    # Build a one-file fake models tree and run the script to produce
    # both the manifest and the .sig.
    models_dir = tmp_path / "models"
    (models_dir / "saved").mkdir(parents=True)
    (models_dir / "saved" / "meta_model.pkl").write_bytes(b"fake-model-bytes")
    out_json = tmp_path / "manifest.json"
    subprocess.run(
        [
            _sys.executable, str(script),
            "--models-dir", str(models_dir),
            "--version", "test-1.0",
            "--out", str(out_json),
            "--sign-key", str(key_path),
        ],
        check=True, capture_output=True,
    )
    sig_path = out_json.with_suffix(out_json.suffix + ".sig")
    assert sig_path.exists()

    # Verify the produced signature with the printed public key — exactly
    # what the loader does at boot.
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    pub = Ed25519PublicKey.from_public_bytes(_base64.b64decode(pub_b64))
    sig = _base64.b64decode(sig_path.read_bytes().strip())
    pub.verify(sig, out_json.read_bytes())  # raises if invalid
