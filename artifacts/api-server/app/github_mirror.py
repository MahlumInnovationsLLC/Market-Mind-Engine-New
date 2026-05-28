"""Mirror promoted model artifacts to a private GitHub repository.

Spec: every "make active" model promotion must push the artifact + manifest
to a private GitHub repo so we can recover from a Supabase outage. This
module is invoked from `/api/models/reload` after `ensure_artifacts(force=True)`
completes successfully.

Configuration (env):
    GITHUB_MIRROR_REPO     "owner/repo"      (required to enable mirroring)
    GITHUB_MIRROR_BRANCH   default "main"
    GITHUB_TOKEN           PAT with `repo` scope (required)
    GITHUB_API_BASE        default "https://api.github.com"

Layout in the mirror repo:
    models/<version>/models_manifest.json
    models/<version>/models_part_aa, models_part_ab, ...
    models/<version>/source_part_aa, source_part_ab, ...
    models/latest.json   { "version": "<v>", "promoted_at": "<iso>", "sha256": "..." }

The mirror is idempotent: if the manifest blob at the version path already
matches the local sha256, the push is skipped. A small JSON state file under
the model cache tracks the last successfully mirrored version so repeated
`/api/models/reload` calls don't re-push the same artifacts.

The push is rate-limited (single in-flight via a module-level lock) and runs
under a generous timeout. Failures are logged but never raised — the mirror
is best-effort and must not break model promotion.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from . import models_loader


log = logging.getLogger(__name__)


@dataclass
class MirrorResult:
    ok: bool
    skipped: bool = False
    reason: str | None = None
    version: str | None = None
    files_pushed: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    started_at: float | None = None
    completed_at: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "skipped": self.skipped,
            "reason": self.reason,
            "version": self.version,
            "files_pushed": list(self.files_pushed),
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


# Single in-flight push at a time so concurrent /api/models/reload calls
# don't double-push the same blobs and exhaust the GitHub rate limit.
_lock = asyncio.Lock()
_last_result: MirrorResult | None = None


def _state_file() -> Path:
    return models_loader.CACHE_ROOT / "github_mirror_state.json"


def _load_state() -> dict[str, Any]:
    p = _state_file()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict[str, Any]) -> None:
    p = _state_file()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    except OSError as e:
        log.warning("[github_mirror] failed to persist state: %s", e)


def _config() -> dict[str, str] | None:
    repo = (os.environ.get("GITHUB_MIRROR_REPO") or "").strip()
    token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    if not repo or "/" not in repo:
        return None
    if not token:
        return None
    branch = (os.environ.get("GITHUB_MIRROR_BRANCH") or "main").strip() or "main"
    base = (os.environ.get("GITHUB_API_BASE") or "https://api.github.com").rstrip("/")
    return {"repo": repo, "token": token, "branch": branch, "base": base}


def is_enabled() -> bool:
    return _config() is not None


def last_result() -> dict[str, Any] | None:
    return _last_result.as_dict() if _last_result is not None else None


def last_persisted_state() -> dict[str, Any]:
    """Return the on-disk state file (last successfully mirrored version,
    timestamp, repo, branch). Survives process restarts so the dashboard
    can show "last mirrored version" even when the in-memory `last_result`
    has been cleared. Returns ``{}`` when no mirror push has succeeded yet."""
    return _load_state()


def config_summary() -> dict[str, Any]:
    """Return the effective mirror configuration for display purposes.

    Never exposes the GitHub token. Returns ``{}`` when the mirror is
    disabled (so the UI can render an "unconfigured" state without
    leaking which env vars are missing)."""
    cfg = _config()
    if cfg is None:
        return {}
    return {"repo": cfg["repo"], "branch": cfg["branch"]}


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _client(cfg: dict[str, str]) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=cfg["base"],
        headers={
            "Authorization": f"Bearer {cfg['token']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "axiom-predict-mirror/1.0",
        },
        # 120s read budget is high because we push gzipped JSONL
        # archives, but connect/pool must stay tight so a stuck DNS
        # or exhausted httpx pool fails fast (task #922).
        timeout=httpx.Timeout(connect=5.0, read=120.0, write=120.0, pool=5.0),
    )


async def _get_existing_sha(
    c: httpx.AsyncClient, cfg: dict[str, str], path: str
) -> tuple[str | None, str | None]:
    """Return (blob_sha, content_sha256) for the file at `path` on the mirror
    branch, or (None, None) if absent.

    `blob_sha` is GitHub's git blob SHA (used for the `sha` field on update).
    `content_sha256` is the sha256 of the raw decoded content (used to skip
    unchanged uploads).
    """
    r = await c.get(
        f"/repos/{cfg['repo']}/contents/{path}",
        params={"ref": cfg["branch"]},
    )
    if r.status_code == 404:
        return None, None
    r.raise_for_status()
    body = r.json()
    blob_sha = body.get("sha")
    enc = body.get("encoding")
    content_b64 = body.get("content") or ""
    content_sha: str | None = None
    if enc == "base64" and content_b64:
        try:
            raw = base64.b64decode(content_b64)
            content_sha = _sha256_bytes(raw)
        except Exception:
            content_sha = None
    return blob_sha, content_sha


async def _put_file(
    c: httpx.AsyncClient,
    cfg: dict[str, str],
    path: str,
    data: bytes,
    message: str,
) -> bool:
    """Create or update `path` on the mirror branch. Returns True if a write
    happened, False if the existing content already matched."""
    blob_sha, existing_sha = await _get_existing_sha(c, cfg, path)
    if existing_sha is not None and existing_sha == _sha256_bytes(data):
        return False
    payload: dict[str, Any] = {
        "message": message,
        "branch": cfg["branch"],
        "content": base64.b64encode(data).decode("ascii"),
    }
    if blob_sha is not None:
        payload["sha"] = blob_sha
    r = await c.put(f"/repos/{cfg['repo']}/contents/{path}", json=payload)
    if r.status_code >= 400:
        raise RuntimeError(
            f"github_mirror PUT {path} failed: {r.status_code} {r.text[:300]}"
        )
    return True


def _collect_artifact_files() -> list[Path]:
    """Return the set of local files to mirror.

    We mirror the reassembled archives plus the manifest (everything needed
    to bootstrap the predictor catalog from scratch on a Supabase outage).
    The split parts in storage are an implementation detail of how the
    artifacts were uploaded — once reassembled locally, a single tarball
    per archive is the cleanest unit to recover from.
    """
    candidates = [
        models_loader.MANIFEST_LOCAL,
        models_loader.MODELS_ARCHIVE,
        models_loader.SOURCE_ARCHIVE,
    ]
    return [p for p in candidates if p.exists() and p.is_file()]


async def mirror_active_model(*, force: bool = False) -> MirrorResult:
    """Mirror the currently-loaded model artifacts + manifest to GitHub.

    Best-effort: any failure is logged and returned as ok=False; never raises.
    Requires `models_loader.ensure_artifacts()` to have completed successfully.
    Pass `force=True` to push even if the version matches the last-mirrored
    state file (useful for manual re-syncs).
    """
    global _last_result
    started = time.time()
    result = MirrorResult(ok=False, started_at=started)

    cfg = _config()
    if cfg is None:
        result.skipped = True
        result.reason = "GITHUB_MIRROR_REPO and/or GITHUB_TOKEN not configured"
        result.completed_at = time.time()
        result.elapsed_seconds = result.completed_at - started
        _last_result = result
        return result

    state = models_loader.get_state()
    if not state.ready:
        result.skipped = True
        result.reason = "models_loader not ready; cannot mirror"
        result.completed_at = time.time()
        result.elapsed_seconds = result.completed_at - started
        _last_result = result
        return result

    version = (state.manifest or {}).get("version") if state.manifest else None
    if not version:
        result.skipped = True
        result.reason = "manifest has no version field; cannot mirror"
        result.completed_at = time.time()
        result.elapsed_seconds = result.completed_at - started
        _last_result = result
        return result
    result.version = str(version)

    if _lock.locked():
        result.skipped = True
        result.reason = "another mirror push is already in flight"
        result.completed_at = time.time()
        result.elapsed_seconds = result.completed_at - started
        _last_result = result
        return result

    async with _lock:
        try:
            persisted = _load_state()
            if (
                not force
                and persisted.get("last_version") == result.version
                and persisted.get("repo") == cfg["repo"]
                and persisted.get("branch") == cfg["branch"]
            ):
                result.ok = True
                result.skipped = True
                result.reason = (
                    f"version {result.version} already mirrored to "
                    f"{cfg['repo']}@{cfg['branch']}"
                )
                result.completed_at = time.time()
                result.elapsed_seconds = result.completed_at - started
                _last_result = result
                return result

            files = _collect_artifact_files()
            if not files:
                result.skipped = True
                result.reason = "no local artifact files found to mirror"
                result.completed_at = time.time()
                result.elapsed_seconds = result.completed_at - started
                _last_result = result
                return result

            log.info(
                "[github_mirror] pushing version=%s files=%d -> %s@%s",
                result.version,
                len(files),
                cfg["repo"],
                cfg["branch"],
            )

            async with _client(cfg) as c:
                pushed: list[str] = []
                for src in files:
                    rel_name = src.name
                    remote_path = f"models/{result.version}/{rel_name}"
                    data = src.read_bytes()
                    wrote = await _put_file(
                        c,
                        cfg,
                        remote_path,
                        data,
                        message=(
                            f"mirror: model {result.version} {rel_name} "
                            f"({len(data)} bytes)"
                        ),
                    )
                    if wrote:
                        pushed.append(remote_path)

                # Update the latest pointer so recovery can find the most
                # recent promoted version without enumerating the tree.
                latest_payload = {
                    "version": result.version,
                    "promoted_at": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)
                    ),
                    "files": [src.name for src in files],
                }
                latest_bytes = json.dumps(
                    latest_payload, indent=2, sort_keys=True
                ).encode("utf-8")
                wrote_latest = await _put_file(
                    c,
                    cfg,
                    "models/latest.json",
                    latest_bytes,
                    message=f"mirror: latest -> {result.version}",
                )
                if wrote_latest:
                    pushed.append("models/latest.json")

            result.ok = True
            result.files_pushed = pushed
            persisted.update(
                {
                    "last_version": result.version,
                    "last_mirrored_at": time.time(),
                    "repo": cfg["repo"],
                    "branch": cfg["branch"],
                    "files_pushed": pushed,
                }
            )
            _save_state(persisted)
            log.info(
                "[github_mirror] OK version=%s pushed=%d (skipped %d unchanged)",
                result.version,
                len(pushed),
                len(files) + 1 - len(pushed),
            )
        except Exception as e:
            result.ok = False
            result.reason = f"{type(e).__name__}: {e}"
            log.exception("[github_mirror] push failed")
        finally:
            result.completed_at = time.time()
            result.elapsed_seconds = result.completed_at - started
            _last_result = result

    return result
