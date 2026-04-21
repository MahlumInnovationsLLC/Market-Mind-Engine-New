"""Build `exports/models_manifest.json` from a local extracted models tree.

The API loader compares each predictor it scans on disk against this
manifest and refuses to mark itself ready if any required entry is
missing or its sha256 doesn't match. Run this script after producing a
new export, then upload the resulting JSON to `<bucket>/exports/models_manifest.json`
alongside the `models_part_*` chunks.

Example:
    python scripts/build_models_manifest.py \\
        --models-dir /tmp/axiom-cache/models \\
        --version 2026.04.21 \\
        --out exports/models_manifest.json
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import sys
from pathlib import Path

_PREDICTOR_EXTS = {
    ".pkl", ".joblib", ".pt", ".pth", ".onnx",
    ".h5", ".bin", ".safetensors", ".model",
}


def _sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for blob in iter(lambda: f.read(chunk), b""):
            h.update(blob)
    return h.hexdigest()


def build(models_dir: Path, version: str, required: list[str]) -> dict:
    if not models_dir.is_dir():
        raise SystemExit(f"models dir does not exist: {models_dir}")
    required_set = set(required)
    predictors: dict[str, dict] = {}
    for p in sorted(models_dir.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in _PREDICTOR_EXTS:
            continue
        rel = str(p.relative_to(models_dir))
        sha = _sha256_file(p)
        predictors[rel] = {
            "sha256": sha,
            "version": sha[:12],
            "size": p.stat().st_size,
            "required": (rel in required_set) if required_set else True,
        }
    return {
        "version": version,
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "predictors": predictors,
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models-dir", required=True, type=Path)
    ap.add_argument("--version", required=True)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument(
        "--required",
        action="append",
        default=[],
        help="Relative path of a predictor that MUST be present (repeatable). "
             "If omitted, every scanned predictor is marked required.",
    )
    args = ap.parse_args(argv)
    manifest = build(args.models_dir, args.version, args.required)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote {args.out} with {len(manifest['predictors'])} predictor(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
