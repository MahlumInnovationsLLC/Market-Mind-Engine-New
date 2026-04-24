"""Model artifact loader.

Reassembles the multi-part archives from Supabase storage into a local
cache directory, extracts them, scans the resulting tree for serialized
predictors, and exposes the inventory to the rest of the API (notably
the health endpoint and the trading loop).

The loader is idempotent: with the cache already populated the second
call is a no-op. It is also safe to call concurrently — an internal
lock prevents duplicate downloads.

Layout under `CACHE_ROOT` (default: `<workspace>/.cache/axiom-models`,
override with `AXIOM_MODEL_CACHE_DIR`):
    models.tar           reassembled trained-model archive (dropped
                         after a successful extract; opt out with
                         AXIOM_KEEP_MODEL_TARS=1)
    source.tar           reassembled source archive (same lifecycle)
    models/              extracted models tree
    source/              extracted source tree

The cache lives on the persistent workspace volume rather than `/tmp`,
so warm restarts skip the ~30s reassembly + extract penalty.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import sys
import tarfile
import time
import warnings

import httpx
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import storage
from .config import (
    get_settings,
    resolve_model_cache_dir,
    resolve_model_cache_max_bytes,
)


log = logging.getLogger(__name__)

# Legacy ephemeral location; if we find a populated cache here at boot
# we'll move it into the persistent location to avoid re-downloading.
_LEGACY_CACHE_ROOT = Path("/tmp/axiom-cache")

CACHE_ROOT = resolve_model_cache_dir()
CACHE_MAX_BYTES = resolve_model_cache_max_bytes()
MODELS_DIR = CACHE_ROOT / "models"
SOURCE_DIR = CACHE_ROOT / "source"
MODELS_ARCHIVE = CACHE_ROOT / "models.tar"
SOURCE_ARCHIVE = CACHE_ROOT / "source.tar"

# Authoritative manifest pinning sha256 + version for each predictor. The
# loader downloads this on every boot and refuses to mark itself ready if
# required predictors are missing, extra files appear that aren't listed,
# or any sha256 fails to match. Path is relative to the storage bucket.
MANIFEST_REMOTE_PATH = "exports/models_manifest.json"
MANIFEST_LOCAL = CACHE_ROOT / "models_manifest.json"
# Detached ed25519 signature over the exact bytes of the manifest JSON.
# Verified against `Settings.manifest_public_key` -- a leaked storage
# key alone is not enough to swap the manifest because the attacker
# also needs the offline signing key.
MANIFEST_SIG_REMOTE_PATH = "exports/models_manifest.json.sig"
MANIFEST_SIG_LOCAL = CACHE_ROOT / "models_manifest.json.sig"

_PREDICTOR_EXTS = {
    ".pkl", ".joblib", ".pt", ".pth", ".onnx",
    ".h5", ".bin", ".safetensors", ".model",
}

# Canonical predictors the trading loop will reach for first. The loader
# attempts to deserialize each at boot as a smoke test and surfaces the
# per-predictor outcome via /api/health.models.
CANONICAL_PREDICTORS: tuple[str, ...] = (
    "models/saved/meta_model.pkl",
    "models/saved/calibrator_isotonic.joblib",
    "models/saved/temperature_gru.pkl",
    "models/saved/precip_gru.pkl",
    "models/archive/meta_model.pkl",
    "models/archive/calibrator.pkl",
)


@dataclass
class ArchiveInfo:
    name: str
    archive_path: str
    extracted_dir: str
    archive_bytes: int = 0
    extracted_bytes: int = 0
    file_count: int = 0
    reassembly_seconds: float = 0.0
    extract_seconds: float = 0.0
    cached: bool = False


@dataclass
class ModelsState:
    ready: bool = False
    in_progress: bool = False
    error: str | None = None
    started_at: float | None = None
    completed_at: float | None = None
    archives: dict[str, ArchiveInfo] = field(default_factory=dict)
    predictors: dict[str, dict[str, Any]] = field(default_factory=dict)
    loaded: dict[str, dict[str, Any]] = field(default_factory=dict)
    manifest: dict[str, Any] | None = None
    manifest_check: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        elapsed: float | None = None
        if self.started_at and self.completed_at:
            elapsed = round(self.completed_at - self.started_at, 2)
        return {
            "ready": self.ready,
            "in_progress": self.in_progress,
            "error": self.error,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "elapsed_seconds": elapsed,
            "predictor_count": len(self.predictors),
            "loaded_count": sum(1 for v in self.loaded.values() if v.get("ok")),
            "archives": {k: asdict(v) for k, v in self.archives.items()},
            "predictors": self.predictors,
            "loaded": self.loaded,
            "manifest": self.manifest,
            "manifest_check": self.manifest_check,
        }


_state = ModelsState()
_lock = asyncio.Lock()
# In-memory cache of deserialized predictor objects keyed by relative name.
_loaded_objects: dict[str, Any] = {}
_source_path_added = False
# Boot-cycle guard so a manifest-mismatch alert fires at most once per
# process. Reset by `ensure_artifacts(force=True)` so an operator-driven
# reload that fails again will re-page.
_integrity_alert_sent = False


def get_state() -> ModelsState:
    return _state


def _maybe_alert_integrity_failure(
    *,
    manifest_version: str | None,
    missing: list[str],
    mismatch: list[str],
    error: str,
) -> None:
    """Page the operator the first time manifest verification fails this
    boot cycle. Subsequent failures within the same process are
    suppressed so we don't spam Postmark on every retry. Reset by
    `ensure_artifacts(force=True)`.

    Failures inside the alert path are swallowed — the loader's job
    here is to surface the bad state, not to break further on a flaky
    notification channel.
    """
    global _integrity_alert_sent
    if _integrity_alert_sent:
        return
    try:
        from .notifications import alerts as _alerts
        _alerts.model_integrity_failure(
            manifest_version=manifest_version,
            missing=missing,
            mismatch=mismatch,
            error=error,
        )
        _integrity_alert_sent = True
        log.warning(
            "[models_loader] dispatched model-integrity alert "
            "(missing=%d mismatch=%d version=%s)",
            len(missing), len(mismatch), manifest_version,
        )
    except Exception as e:  # pragma: no cover — alerting must not break boot
        log.warning("[models_loader] integrity alert dispatch failed: %s", e)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _dir_size(path: Path) -> tuple[int, int]:
    total = 0
    n = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
                n += 1
            except OSError:
                pass
    return total, n


def _sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for blob in iter(lambda: f.read(chunk), b""):
            h.update(blob)
    return h.hexdigest()


def _safe_extract_tar(tar_path: Path, dest: Path) -> None:
    """Extract a tarball after rejecting path-traversal members."""
    dest.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest.resolve()
    with tarfile.open(tar_path, "r:*") as tf:
        for member in tf.getmembers():
            target = (dest / member.name).resolve()
            try:
                target.relative_to(dest_resolved)
            except ValueError as e:
                raise RuntimeError(
                    f"refusing to extract {member.name!r}: escapes target dir"
                ) from e
            if member.issym() or member.islnk():
                link_target = (target.parent / member.linkname).resolve()
                try:
                    link_target.relative_to(dest_resolved)
                except ValueError as e:
                    raise RuntimeError(
                        f"refusing to extract link {member.name!r}: escapes target dir"
                    ) from e
        # Use the data filter when available (Python 3.12+) for an extra
        # layer of safety; fall back silently on older runtimes.
        try:
            tf.extractall(dest, filter="data")  # type: ignore[arg-type]
        except TypeError:
            tf.extractall(dest)


def _should_keep_archive_after_extract() -> bool:
    """Opt-out switch for tar-after-extract cleanup.

    Default behaviour drops the reassembled `*.tar` once it has been
    safely extracted: the extracted tree is the source of truth for the
    rest of the loader, the tar is only needed on a cold rebuild (which
    re-downloads anyway), and keeping it around doubles the persistent
    cache footprint (~240 MiB for the two archives combined).

    Set `AXIOM_KEEP_MODEL_TARS=1` (or true/yes/on) to keep the tars on
    disk for debugging or out-of-band inspection.
    """
    val = (os.environ.get("AXIOM_KEEP_MODEL_TARS") or "").strip().lower()
    return val in {"1", "true", "yes", "on"}


async def _ensure_one(
    name: str, prefix: str, archive_dest: Path, extract_dest: Path
) -> ArchiveInfo:
    info = ArchiveInfo(
        name=name,
        archive_path=str(archive_dest),
        extracted_dir=str(extract_dest),
    )

    needs_extract = (not extract_dest.exists()) or not any(extract_dest.iterdir())
    # Only fetch the tar when we actually need to extract it. A warm
    # cache (extract dir present) skips the download even if the tar was
    # dropped after a previous successful extract.
    needs_download = needs_extract and not archive_dest.exists()

    t0 = time.monotonic()
    if needs_download:
        log.info("[models_loader] reassembling %s -> %s", name, archive_dest)
        await storage.download_split_artifact(prefix, "exports", archive_dest)
    elif archive_dest.exists():
        log.info("[models_loader] %s archive already cached at %s", name, archive_dest)
        info.cached = True
    else:
        log.info(
            "[models_loader] %s extract already cached, archive previously dropped",
            name,
        )
        info.cached = True
    info.reassembly_seconds = round(time.monotonic() - t0, 2)
    if archive_dest.exists():
        info.archive_bytes = archive_dest.stat().st_size
    log.info(
        "[models_loader] %s archive size=%.1f MiB reassembly=%.2fs (cached=%s)",
        name,
        info.archive_bytes / (1 << 20),
        info.reassembly_seconds,
        info.cached,
    )

    if needs_extract:
        if extract_dest.exists():
            shutil.rmtree(extract_dest)
        log.info("[models_loader] extracting %s -> %s", name, extract_dest)
        t1 = time.monotonic()
        await asyncio.to_thread(_safe_extract_tar, archive_dest, extract_dest)
        info.extract_seconds = round(time.monotonic() - t1, 2)
        # Drop the reassembled tar now that the extracted tree is on
        # disk — it's only re-read on a cold rebuild, which itself
        # re-downloads from object storage. Opt out via env for
        # debugging.
        if not _should_keep_archive_after_extract() and archive_dest.exists():
            try:
                dropped = archive_dest.stat().st_size
                archive_dest.unlink()
                log.info(
                    "[models_loader] dropped %s archive after extract (%.1f MiB freed)",
                    name,
                    dropped / (1 << 20),
                )
            except OSError as e:
                log.warning(
                    "[models_loader] failed to drop %s archive at %s: %s",
                    name, archive_dest, e,
                )
    else:
        log.info("[models_loader] %s already extracted, skipping untar", name)

    info.extracted_bytes, info.file_count = await asyncio.to_thread(
        _dir_size, extract_dest
    )
    log.info(
        "[models_loader] %s extracted=%.1f MiB files=%d extract=%.2fs",
        name,
        info.extracted_bytes / (1 << 20),
        info.file_count,
        info.extract_seconds,
    )
    return info


class ManifestSignatureError(RuntimeError):
    """Raised when the detached ed25519 signature on the manifest does
    not verify against the configured public key. Distinct from sha
    mismatches so `/api/health.models` can surface a "signature_invalid"
    reason and the operator can tell tampering apart from corruption."""


class ManifestUnavailable(RuntimeError):
    """Raised when the manifest object does not exist in storage (HTTP
    400/404). Distinct from network/auth errors so `ensure_artifacts`
    can surface this as a non-fatal "not configured yet" state — the
    deploy simply has no model artifacts published yet (T007 deferred)
    rather than a misconfigured/broken manifest."""


def _load_manifest_public_key():
    """Decode `Settings.manifest_public_key` into an Ed25519PublicKey, or
    return None if the operator has not configured one. Accepts the raw
    32-byte ed25519 public key encoded as base64 (the format printed by
    `scripts/build_models_manifest.py --gen-key`)."""
    try:
        cfg = get_settings()
    except Exception:
        return None
    raw_b64 = cfg.manifest_public_key
    if not raw_b64:
        return None
    import base64
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PublicKey,
    )
    try:
        raw = base64.b64decode(raw_b64, validate=True)
    except Exception as e:
        raise RuntimeError(
            f"AXIOM_MANIFEST_PUBLIC_KEY is not valid base64: {e}"
        ) from e
    if len(raw) != 32:
        raise RuntimeError(
            "AXIOM_MANIFEST_PUBLIC_KEY must decode to 32 raw ed25519 bytes "
            f"(got {len(raw)})"
        )
    return Ed25519PublicKey.from_public_bytes(raw)


async def _verify_manifest_signature(payload: bytes) -> str:
    """Download the detached signature for the manifest and verify it.

    Returns one of:
      * "ok"        -- signature verified against the configured pubkey
      * "skipped"   -- no public key configured (warning logged)

    Raises `ManifestSignatureError` on any failure to fetch, decode, or
    verify the signature when a public key IS configured. The exception
    message is what surfaces in `/api/health.models` so an operator can
    immediately tell signature failure apart from sha mismatch.
    """
    pubkey = _load_manifest_public_key()
    if pubkey is None:
        log.warning(
            "[models_loader] manifest signature verification SKIPPED — "
            "AXIOM_MANIFEST_PUBLIC_KEY is not set; a leaked storage key "
            "could substitute the manifest. Set the public key to enforce."
        )
        return "skipped"

    if MANIFEST_SIG_LOCAL.exists():
        try:
            MANIFEST_SIG_LOCAL.unlink()
        except OSError:
            pass
    try:
        await storage.download_to_path(
            MANIFEST_SIG_REMOTE_PATH, MANIFEST_SIG_LOCAL, force=True
        )
    except Exception as e:
        raise ManifestSignatureError(
            f"signature invalid: failed to download {MANIFEST_SIG_REMOTE_PATH}: {e}"
        ) from e

    import base64
    from cryptography.exceptions import InvalidSignature

    raw_sig_b64 = MANIFEST_SIG_LOCAL.read_bytes().strip()
    try:
        sig = base64.b64decode(raw_sig_b64, validate=True)
    except Exception as e:
        raise ManifestSignatureError(
            f"signature invalid: signature file is not base64: {e}"
        ) from e
    if len(sig) != 64:
        raise ManifestSignatureError(
            f"signature invalid: expected 64-byte ed25519 signature, got {len(sig)}"
        )
    try:
        pubkey.verify(sig, payload)
    except InvalidSignature as e:
        raise ManifestSignatureError(
            "signature invalid: ed25519 verification failed; manifest may "
            "have been tampered with via the storage bucket"
        ) from e
    log.info("[models_loader] manifest signature OK (ed25519)")
    return "ok"


async def _download_manifest() -> tuple[dict[str, Any], str]:
    """Fetch the authoritative manifest from the bucket, parse, validate,
    and verify the detached signature.

    The manifest must be a JSON object with a `predictors` mapping where
    each entry has at least an expected `sha256`. Optional per-entry
    fields: `version` (semantic tag) and `required` (bool, default True).

    Returns `(parsed_manifest, signature_status)` where signature_status
    is one of: "ok" | "skipped" (no public key configured). On signature
    failure raises `ManifestSignatureError` so the loader can surface a
    distinct reason vs. sha mismatch.
    """
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    if MANIFEST_LOCAL.exists():
        try:
            MANIFEST_LOCAL.unlink()
        except OSError:
            pass
    log.info("[models_loader] fetching manifest %s", MANIFEST_REMOTE_PATH)
    try:
        await storage.download_to_path(MANIFEST_REMOTE_PATH, MANIFEST_LOCAL, force=True)
    except httpx.HTTPStatusError as e:
        # Supabase storage returns 400 for "object not found" on the
        # signed-object endpoint and 404 for missing buckets. When the
        # operator has explicitly opted in via AXIOM_ALLOW_MISSING_MANIFEST
        # (T007 deferral), translate to a non-fatal sentinel so the caller
        # can surface "unconfigured" instead of "down". Otherwise treat as
        # a real error so a misconfigured bucket / wrong path / missing
        # permissions cannot silently masquerade as "no artifacts yet".
        if e.response is not None and e.response.status_code in (400, 404):
            allow_missing = False
            try:
                allow_missing = bool(get_settings().models_allow_missing_manifest)
            except Exception:
                allow_missing = False
            if allow_missing:
                raise ManifestUnavailable(
                    f"manifest not found at {MANIFEST_REMOTE_PATH} "
                    f"(HTTP {e.response.status_code}) — no model artifacts "
                    f"published (AXIOM_ALLOW_MISSING_MANIFEST=1)"
                ) from e
        raise
    payload = MANIFEST_LOCAL.read_bytes()

    # Verify the detached signature over the EXACT bytes pulled from
    # storage before we trust any of the parsed fields. Any tampering
    # -- even reformatting whitespace -- breaks the signature.
    sig_status = await _verify_manifest_signature(payload)

    try:
        data = json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"models manifest is not valid JSON: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("predictors"), dict):
        raise RuntimeError(
            "models manifest missing required top-level 'predictors' object"
        )
    for name, meta in data["predictors"].items():
        if not isinstance(meta, dict) or not isinstance(meta.get("sha256"), str):
            raise RuntimeError(
                f"manifest entry {name!r} missing required 'sha256' string"
            )
        sha = meta["sha256"].lower().strip()
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise RuntimeError(
                f"manifest entry {name!r} has malformed sha256 ({sha!r})"
            )
        meta["sha256"] = sha
    return data, sig_status


def _verify_against_manifest(
    predictors: dict[str, dict[str, Any]],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Compare scanned predictors against the manifest.

    Returns a structured report with three lists:
      * `missing`   — manifest entries marked required that aren't on disk
      * `mismatch`  — files present whose sha256 does not match the manifest
      * `unexpected` — files on disk not declared in the manifest
    Plus an `ok` flag (True when missing+mismatch are empty; unexpected
    files are surfaced but do not block readiness on their own — they
    indicate stale cache, not corruption of a required predictor).
    """
    expected = manifest.get("predictors", {}) or {}
    missing: list[dict[str, Any]] = []
    mismatch: list[dict[str, Any]] = []
    matched: list[str] = []

    for name, meta in expected.items():
        required = bool(meta.get("required", True))
        actual = predictors.get(name)
        if actual is None:
            if required:
                missing.append({
                    "name": name,
                    "expected_sha256": meta["sha256"],
                    "expected_version": meta.get("version"),
                })
            continue
        if actual["sha256"] != meta["sha256"]:
            mismatch.append({
                "name": name,
                "expected_sha256": meta["sha256"],
                "actual_sha256": actual["sha256"],
                "expected_version": meta.get("version"),
            })
        else:
            matched.append(name)

    unexpected = [n for n in predictors.keys() if n not in expected]

    return {
        "ok": not missing and not mismatch,
        "checked_at": time.time(),
        "manifest_predictor_count": len(expected),
        "matched_count": len(matched),
        "missing": missing,
        "mismatch": mismatch,
        "unexpected": unexpected,
    }


def _scan_predictors(models_dir: Path) -> dict[str, dict[str, Any]]:
    """Catalogue serialized model files under `models_dir`.

    For each predictor file we capture size, mtime, sha256, and a short
    "version" tag (the first 12 hex chars of the sha256). The trading
    loop can call `get_predictor_path(name)` to resolve a relative name
    back to an on-disk path.
    """
    out: dict[str, dict[str, Any]] = {}
    if not models_dir.exists():
        return out
    for p in sorted(models_dir.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix.lower() not in _PREDICTOR_EXTS:
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        rel = str(p.relative_to(models_dir))
        sha = _sha256_file(p)
        out[rel] = {
            "size": st.st_size,
            "mtime": st.st_mtime,
            "sha256": sha,
            "version": sha[:12],
        }
    return out


# ---------------------------------------------------------------------------
# public entrypoints
# ---------------------------------------------------------------------------


async def ensure_artifacts(*, force: bool = False) -> ModelsState:
    """Reassemble + extract both archives and load the predictor catalog.

    Safe to call repeatedly; concurrent callers will see a single
    download/extract pass thanks to the internal lock. Pass `force=True`
    to clear the in-memory state (and the cached predictor objects) and
    rerun the pipeline — used by `/api/models/reload`.
    """
    global _state, _integrity_alert_sent
    async with _lock:
        if force:
            _loaded_objects.clear()
            _state = ModelsState()
            _integrity_alert_sent = False
        if _state.ready:
            return _state
        _state.in_progress = True
        _state.error = None
        _state.started_at = time.time()
        try:
            CACHE_ROOT.mkdir(parents=True, exist_ok=True)
            log.info(
                "[models_loader] cache_root=%s persistent=%s",
                CACHE_ROOT,
                not str(CACHE_ROOT.resolve()).startswith("/tmp"),
            )
            await asyncio.to_thread(_migrate_legacy_cache)
            await asyncio.to_thread(_prune_cache)
            await asyncio.to_thread(_enforce_cache_budget)
            models_info = await _ensure_one(
                "models", "models_part_", MODELS_ARCHIVE, MODELS_DIR
            )
            _state.archives["models"] = models_info
            source_info = await _ensure_one(
                "source", "source_part_", SOURCE_ARCHIVE, SOURCE_DIR
            )
            _state.archives["source"] = source_info

            # Re-check the budget post-extract so first-boot overages
            # (download + untar pushed us over the limit) are surfaced
            # in the same boot rather than waiting for the next reload.
            await asyncio.to_thread(_enforce_cache_budget)

            predictors = await asyncio.to_thread(_scan_predictors, MODELS_DIR)
            _state.predictors = predictors

            # Verify scanned predictors against the published manifest
            # BEFORE marking ready. Any required-predictor mismatch or
            # missing file aborts readiness so we never trade against a
            # corrupt or partial set.
            try:
                manifest, sig_status = await _download_manifest()
            except ManifestUnavailable as e:
                # Deploy has not yet published a model manifest (T007
                # deferred). Leave _state.ready=False, _state.error=None
                # so /api/health.models surfaces "unconfigured" (handled
                # in app.health._probe_models) and the READY gate flips
                # without parking the orchestrator.
                log.info(
                    "[models_loader] manifest not configured: %s — "
                    "loader will idle until artifacts are published",
                    e,
                )
                _state.manifest_check = {
                    "ok": False,
                    "reason": "manifest_unavailable",
                    "checked_at": time.time(),
                    "signature": "skipped",
                    "error": str(e),
                    "missing": [],
                    "mismatch": [],
                    "unexpected": [],
                }
                # Drop the predictor scan results — without a manifest
                # we have nothing to verify them against, and the probe
                # uses (predictors==[] AND archives==[]) as the
                # "unconfigured" sentinel.
                _state.predictors = {}
                return _state
            except ManifestSignatureError as e:
                # Distinct surfacing in /api/health.models so an operator
                # can tell tampering ("signature invalid") apart from a
                # corrupt-cache sha mismatch.
                _state.manifest_check = {
                    "ok": False,
                    "reason": "signature_invalid",
                    "checked_at": time.time(),
                    "signature": "invalid",
                    "error": str(e),
                    "missing": [],
                    "mismatch": [],
                    "unexpected": [],
                }
                raise
            _state.manifest = {
                "version": manifest.get("version"),
                "generated_at": manifest.get("generated_at"),
                "predictor_count": len(manifest.get("predictors", {}) or {}),
                "signature": sig_status,
            }
            check = _verify_against_manifest(predictors, manifest)
            check["signature"] = sig_status
            _state.manifest_check = check
            if not check["ok"]:
                missing_names = [m["name"] for m in check["missing"]]
                mismatch_names = [m["name"] for m in check["mismatch"]]
                bad = missing_names + mismatch_names
                # Distinguish the two failure modes so /api/health.models
                # can surface a precise reason and the operator alert
                # carries actionable context.
                reason = "sha_mismatch" if mismatch_names else "missing_required"
                check["reason"] = reason
                detail = (
                    "sha mismatch" if reason == "sha_mismatch"
                    else "required predictor missing"
                )
                err = RuntimeError(
                    f"manifest verification failed ({detail}): "
                    f"missing={len(check['missing'])} "
                    f"mismatch={len(check['mismatch'])} "
                    f"files={bad}"
                )
                # Page the operator on the very first failure of this
                # boot cycle. Subsequent retries (e.g. periodic
                # `ensure_artifacts` callers) won't re-spam.
                _maybe_alert_integrity_failure(
                    manifest_version=(_state.manifest or {}).get("version"),
                    missing=missing_names,
                    mismatch=mismatch_names,
                    error=str(err),
                )
                raise err
            log.info(
                "[models_loader] manifest OK matched=%d/%d unexpected=%d signature=%s",
                check["matched_count"],
                check["manifest_predictor_count"],
                len(check["unexpected"]),
                sig_status,
            )

            # Mark catalog ready before smoke-loading so load_predictor
            # works inside the smoke step.
            _state.ready = True
            _state.loaded = await _smoke_load_canonical()
            ok_loaded = sum(1 for v in _state.loaded.values() if v.get("ok"))
            log.info(
                "[models_loader] READY predictors=%d loaded=%d/%d "
                "models=%.1f MiB source=%.1f MiB",
                len(predictors),
                ok_loaded,
                len(_state.loaded),
                models_info.extracted_bytes / (1 << 20),
                source_info.extracted_bytes / (1 << 20),
            )
        except Exception as e:
            _state.error = f"{type(e).__name__}: {e}"
            log.exception("[models_loader] failed to load model artifacts")
        finally:
            _state.in_progress = False
            _state.completed_at = time.time()
        return _state


def get_predictor_path(name: str) -> Path | None:
    """Resolve a predictor relative name to its extracted on-disk path."""
    if not _state.ready or name not in _state.predictors:
        return None
    return MODELS_DIR / name


def _ensure_source_on_path() -> None:
    """Add the extracted source archive to sys.path so pickled models that
    reference custom modules (e.g. `src.something`) can be unpickled."""
    global _source_path_added
    if _source_path_added:
        return
    if SOURCE_DIR.exists():
        sp = str(SOURCE_DIR.resolve())
        if sp not in sys.path:
            sys.path.insert(0, sp)
        _source_path_added = True


def _load_one(name: str) -> Any:
    """Synchronously deserialize a single predictor. Caller-managed cache."""
    import joblib  # local import keeps optional dep failures observable
    path = MODELS_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"predictor not found at {path}")
    _ensure_source_on_path()
    with warnings.catch_warnings():
        # sklearn version-mismatch warnings are noisy and not actionable here
        warnings.simplefilter("ignore")
        return joblib.load(path)


async def load_predictor(name: str) -> Any:
    """Public accessor used by the trading loop.

    Lazily deserializes the predictor with the given relative name (e.g.
    `models/saved/meta_model.pkl`) and caches the live object in process
    memory. Subsequent calls return the cached instance.

    Raises if the loader isn't ready or the predictor isn't in the
    catalog. Deserialization errors propagate.
    """
    if not _state.ready:
        raise RuntimeError("models_loader is not ready; ensure_artifacts() has not completed")
    if name not in _state.predictors:
        raise KeyError(f"unknown predictor {name!r}; not in catalog")
    if name in _loaded_objects:
        return _loaded_objects[name]
    obj = await asyncio.to_thread(_load_one, name)
    _loaded_objects[name] = obj
    return obj


def get_loaded_predictor(name: str) -> Any | None:
    """Return the cached predictor object if previously loaded, else None."""
    return _loaded_objects.get(name)


async def _smoke_load_canonical() -> dict[str, dict[str, Any]]:
    """Try to deserialize each canonical predictor and record the outcome."""
    results: dict[str, dict[str, Any]] = {}
    for name in CANONICAL_PREDICTORS:
        if name not in _state.predictors:
            results[name] = {"ok": False, "error": "not present in catalog"}
            continue
        t0 = time.monotonic()
        try:
            obj = await asyncio.to_thread(_load_one, name)
            _loaded_objects[name] = obj
            results[name] = {
                "ok": True,
                "type": type(obj).__name__,
                "module": type(obj).__module__,
                "load_seconds": round(time.monotonic() - t0, 3),
                "version": _state.predictors[name]["version"],
            }
            log.info(
                "[models_loader] loaded canonical predictor %s -> %s.%s in %.2fs",
                name, type(obj).__module__, type(obj).__name__,
                results[name]["load_seconds"],
            )
        except Exception as e:
            results[name] = {
                "ok": False,
                "error": f"{type(e).__name__}: {e}",
                "load_seconds": round(time.monotonic() - t0, 3),
            }
            log.warning("[models_loader] canonical load FAILED %s: %s", name, e)
    return results


def list_predictor_versions() -> dict[str, str]:
    """Compact `{name: version}` map for the smoke-test in /api/health."""
    return {name: meta["version"] for name, meta in _state.predictors.items()}


def _migrate_legacy_cache() -> None:
    """Move a populated `/tmp/axiom-cache` into the persistent root.

    Best-effort: if the legacy location is empty, missing, or the new
    root already has content, this is a no-op. Failures are logged and
    swallowed — we'll just re-download.
    """
    if CACHE_ROOT.resolve() == _LEGACY_CACHE_ROOT.resolve():
        return
    if not _LEGACY_CACHE_ROOT.exists():
        return
    moved = 0
    for name in ("models.tar", "source.tar", "models", "source"):
        src = _LEGACY_CACHE_ROOT / name
        dst = CACHE_ROOT / name
        if not src.exists() or dst.exists():
            continue
        try:
            shutil.move(str(src), str(dst))
            moved += 1
        except OSError as e:
            log.warning("[models_loader] legacy migrate %s failed: %s", name, e)
    if moved:
        log.info(
            "[models_loader] migrated %d entries from legacy cache %s -> %s",
            moved, _LEGACY_CACHE_ROOT, CACHE_ROOT,
        )


# Files we expect at the cache root.  Anything else (stray reassembly
# parts, half-written tempfiles, abandoned archive versions) is removed
# during the prune pass to keep disk usage bounded.
_EXPECTED_CACHE_ENTRIES = frozenset({
    "models", "source", "models.tar", "source.tar",
})


def _prune_cache() -> None:
    """Remove orphaned entries under CACHE_ROOT.

    Keeps disk usage bounded across reload cycles by deleting
    leftover `.partNNN` chunks, abandoned tempfiles, and any
    extracted-version directories that are no longer current.
    """
    if not CACHE_ROOT.exists():
        return
    removed = 0
    freed = 0
    for entry in CACHE_ROOT.iterdir():
        if entry.name in _EXPECTED_CACHE_ENTRIES:
            continue
        try:
            if entry.is_file() or entry.is_symlink():
                size = entry.stat().st_size if entry.is_file() else 0
                entry.unlink()
                freed += size
            else:
                size, _ = _dir_size(entry)
                shutil.rmtree(entry, ignore_errors=True)
                freed += size
            removed += 1
        except OSError as e:
            log.warning("[models_loader] prune %s failed: %s", entry, e)
    if removed:
        log.info(
            "[models_loader] pruned %d orphan cache entries (%.1f MiB freed)",
            removed, freed / (1 << 20),
        )


def _cache_total_bytes() -> int:
    """Sum on-disk size of every file under CACHE_ROOT."""
    if not CACHE_ROOT.exists():
        return 0
    total, _ = _dir_size(CACHE_ROOT)
    return total


def _enforce_cache_budget() -> dict[str, Any]:
    """Warn (and best-effort evict) when the cache exceeds its budget.

    Called from `ensure_artifacts` after `_prune_cache`, so any orphan
    chunks/tempfiles have already been removed. Anything still on disk
    is canonical, so we don't delete the four expected entries — that
    would just trigger a re-download loop. Instead we surface a loud
    warning and let the operator decide whether to raise
    AXIOM_MODEL_CACHE_MAX_BYTES or shrink the published archive.

    If a future schema sneaks an unexpected entry past `_prune_cache`
    (e.g. a new top-level file), we'll evict it oldest-first here to
    pull usage back under budget.
    """
    total = _cache_total_bytes()
    budget = CACHE_MAX_BYTES
    if total <= budget:
        return {"total_bytes": total, "budget_bytes": budget, "over_budget": False}

    log.warning(
        "[models_loader] cache size %.1f MiB exceeds budget %.1f MiB at %s "
        "(set AXIOM_MODEL_CACHE_MAX_BYTES to raise the limit)",
        total / (1 << 20),
        budget / (1 << 20),
        CACHE_ROOT,
    )

    # Oldest-first eviction of anything outside the canonical set.
    # Sorted by mtime so the staleest leftover goes first.
    candidates: list[tuple[float, Path]] = []
    for entry in CACHE_ROOT.iterdir():
        if entry.name in _EXPECTED_CACHE_ENTRIES:
            continue
        try:
            candidates.append((entry.stat().st_mtime, entry))
        except OSError:
            continue
    candidates.sort(key=lambda t: t[0])
    evicted = 0
    freed = 0
    for _mtime, entry in candidates:
        if total <= budget:
            break
        try:
            if entry.is_file() or entry.is_symlink():
                size = entry.stat().st_size if entry.is_file() else 0
                entry.unlink()
            else:
                size, _ = _dir_size(entry)
                shutil.rmtree(entry, ignore_errors=True)
            evicted += 1
            freed += size
            total -= size
        except OSError as e:
            log.warning("[models_loader] budget evict %s failed: %s", entry, e)
    if evicted:
        log.warning(
            "[models_loader] evicted %d non-canonical entries (%.1f MiB) to fit budget",
            evicted, freed / (1 << 20),
        )

    return {
        "total_bytes": _cache_total_bytes(),
        "budget_bytes": budget,
        "over_budget": _cache_total_bytes() > budget,
    }


def disk_usage_summary() -> dict[str, Any]:
    def file_size(p: Path) -> int:
        return p.stat().st_size if p.exists() and p.is_file() else 0

    def tree_size(p: Path) -> int:
        if not p.exists():
            return 0
        total, _ = _dir_size(p)
        return total

    models_archive_bytes = file_size(MODELS_ARCHIVE)
    source_archive_bytes = file_size(SOURCE_ARCHIVE)
    models_extracted_bytes = tree_size(MODELS_DIR)
    source_extracted_bytes = tree_size(SOURCE_DIR)
    total_bytes = (
        models_archive_bytes
        + source_archive_bytes
        + models_extracted_bytes
        + source_extracted_bytes
    )
    return {
        "cache_root": str(CACHE_ROOT),
        "persistent": CACHE_ROOT.resolve() != _LEGACY_CACHE_ROOT.resolve()
        and not str(CACHE_ROOT.resolve()).startswith("/tmp"),
        "models_archive_bytes": models_archive_bytes,
        "source_archive_bytes": source_archive_bytes,
        "models_extracted_bytes": models_extracted_bytes,
        "source_extracted_bytes": source_extracted_bytes,
        "total_bytes": total_bytes,
        "budget_bytes": CACHE_MAX_BYTES,
        "over_budget": total_bytes > CACHE_MAX_BYTES,
    }
