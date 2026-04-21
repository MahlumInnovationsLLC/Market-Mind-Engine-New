"""Supabase Storage REST client with multi-part artifact reassembly.

The production export uses `<name>.partNNN` chunks with a sibling
`<name>.manifest.json` describing chunk order and total size. Reassembly
streams chunks to disk so we never hold a 2 GB file in memory.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import httpx

from .config import get_settings


log = logging.getLogger(__name__)
CACHE_ROOT = Path("/tmp/axiom-cache")


def _client() -> httpx.AsyncClient:
    s = get_settings()
    return httpx.AsyncClient(
        base_url=s.supabase_storage_url,
        headers={
            "Authorization": f"Bearer {s.supabase_service_key}",
            "apikey": s.supabase_service_key,
        },
        timeout=60.0,
    )


@dataclass
class StorageEntry:
    name: str
    is_folder: bool
    size: int | None
    updated_at: str | None


async def list_path(prefix: str = "", limit: int = 100, offset: int = 0) -> list[StorageEntry]:
    s = get_settings()
    payload: dict[str, Any] = {"limit": limit, "offset": offset, "prefix": prefix}
    async with _client() as c:
        r = await c.post(f"/object/list/{s.supabase_bucket}", json=payload)
        r.raise_for_status()
        rows = r.json()
    out: list[StorageEntry] = []
    for row in rows:
        meta = row.get("metadata") or {}
        out.append(
            StorageEntry(
                name=row["name"],
                is_folder=row.get("id") is None and not meta,
                size=meta.get("size"),
                updated_at=row.get("updated_at"),
            )
        )
    return out


async def head_object(path: str) -> dict[str, Any] | None:
    """Return `{size, etag, content_type}` or None if not found."""
    s = get_settings()
    async with _client() as c:
        r = await c.head(f"/object/{s.supabase_bucket}/{path}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return {
            "size": int(r.headers.get("content-length", 0) or 0),
            "etag": r.headers.get("etag"),
            "content_type": r.headers.get("content-type"),
        }


async def download_to_path(remote: str, dest: Path, *, force: bool = False) -> Path:
    """Stream a single object to disk."""
    s = get_settings()
    if dest.exists() and not force:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    async with _client() as c:
        async with c.stream("GET", f"/object/{s.supabase_bucket}/{remote}") as r:
            r.raise_for_status()
            with tmp.open("wb") as f:
                async for chunk in r.aiter_bytes(1 << 20):
                    f.write(chunk)
    os.replace(tmp, dest)
    return dest


async def download_split_artifact(
    prefix: str, parent_path: str, dest: Path, *, force: bool = False
) -> Path:
    """Reassemble a multi-part artifact produced by GNU `split`.

    The production export uses names like `exports/models_part_aa`,
    `exports/models_part_ab`, etc. Strict checks:
      * each match must be `<prefix><two lowercase a-z letters>`,
      * the suffixes must form a contiguous sequence starting from `aa`,
      * the result is paged so files with > 1000 parts raise loudly.
    """
    import re

    if dest.exists() and not force:
        return dest

    entries = await list_path(parent_path, limit=1000)
    if len(entries) >= 1000:
        raise RuntimeError(
            f"reassembly aborted: {parent_path!r} returned >=1000 entries (paging not implemented)"
        )

    pat = re.compile(rf"^{re.escape(prefix)}([a-z]{{2}})$")
    matches = [(m.group(1), e.name) for e in entries if not e.is_folder for m in [pat.match(e.name)] if m]
    if not matches:
        raise RuntimeError(f"no parts found under {parent_path!r} matching prefix {prefix!r}")
    matches.sort(key=lambda t: t[0])

    def expected_suffix(i: int) -> str:
        return chr(ord("a") + i // 26) + chr(ord("a") + i % 26)

    for i, (suffix, name) in enumerate(matches):
        if suffix != expected_suffix(i):
            raise RuntimeError(
                f"reassembly aborted: parts not contiguous, expected {expected_suffix(i)} "
                f"got {suffix} (file {name!r}). Existing parts: {[m[1] for m in matches]}"
            )
    parts = [name for _, name in matches]

    s = get_settings()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    log.info("Reassembling %s from %d parts -> %s", prefix, len(parts), dest)
    with tmp.open("wb") as out:
        async with _client() as c:
            for part_name in parts:
                remote = f"{parent_path.rstrip('/')}/{part_name}" if parent_path else part_name
                async with c.stream("GET", f"/object/{s.supabase_bucket}/{remote}") as r:
                    r.raise_for_status()
                    async for chunk in r.aiter_bytes(1 << 20):
                        out.write(chunk)
    os.replace(tmp, dest)
    return dest
