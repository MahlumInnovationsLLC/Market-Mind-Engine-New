"""Build `exports/models_manifest.json` from a local extracted models tree.

The API loader compares each predictor it scans on disk against this
manifest and refuses to mark itself ready if any required entry is
missing or its sha256 doesn't match. Run this script after producing a
new export, then upload the resulting JSON (and, if signed, the matching
`.sig` file) to `<bucket>/exports/` alongside the `models_part_*` chunks.

Manifest signing
----------------
Pass `--sign-key <path>` to produce a detached ed25519 signature next to
the JSON output (`<out>.sig`, base64-encoded raw 64-byte signature over
the exact JSON bytes that are written to disk). The matching public key
must be configured on the API side as `AXIOM_MANIFEST_PUBLIC_KEY` so
`models_loader._download_manifest` can verify it on boot. A leaked
storage key alone is then not enough to swap the manifest -- the
attacker would also need the offline signing key.

The signing key file may be either:
  * an ed25519 PEM (PKCS8 PrivateKeyInfo), or
  * 32 raw bytes (the seed) base64-encoded on a single line.

Generate a fresh keypair with `--gen-key <out>` (writes the private key
in PEM and prints the matching public key as base64, ready to paste
into `AXIOM_MANIFEST_PUBLIC_KEY`).

Example:
    python scripts/build_models_manifest.py \\
        --models-dir /tmp/axiom-cache/models \\
        --version 2026.04.21 \\
        --out exports/models_manifest.json \\
        --sign-key /secure/axiom_manifest_signing.pem
"""
from __future__ import annotations

import argparse
import base64
import datetime as _dt
import hashlib
import json
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

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


def _load_signing_key(path: Path) -> Ed25519PrivateKey:
    raw = path.read_bytes().strip()
    # Try PEM first.
    if raw.startswith(b"-----BEGIN"):
        key = serialization.load_pem_private_key(raw, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise SystemExit(f"signing key {path} is not ed25519")
        return key
    # Fall back to base64-encoded raw seed (32 bytes).
    try:
        seed = base64.b64decode(raw, validate=True)
    except Exception as e:
        raise SystemExit(
            f"signing key {path} is neither PEM nor base64-encoded raw bytes: {e}"
        ) from e
    if len(seed) != 32:
        raise SystemExit(
            f"signing key {path}: expected 32 raw seed bytes, got {len(seed)}"
        )
    return Ed25519PrivateKey.from_private_bytes(seed)


def _sign_manifest(payload: bytes, key: Ed25519PrivateKey) -> bytes:
    """Return a base64-encoded detached signature over `payload`."""
    sig = key.sign(payload)
    return base64.b64encode(sig)


def _gen_keypair(out: Path) -> None:
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(pem)
    try:
        out.chmod(0o600)
    except OSError:
        pass
    pub = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    print(f"wrote private key to {out} (mode 0600)")
    print("public key (set AXIOM_MANIFEST_PUBLIC_KEY to this value):")
    print(base64.b64encode(pub).decode())


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models-dir", type=Path)
    ap.add_argument("--version")
    ap.add_argument("--out", type=Path)
    ap.add_argument(
        "--required",
        action="append",
        default=[],
        help="Relative path of a predictor that MUST be present (repeatable). "
             "If omitted, every scanned predictor is marked required.",
    )
    ap.add_argument(
        "--sign-key",
        type=Path,
        default=None,
        help="Path to the ed25519 private key (PEM or base64-encoded 32-byte "
             "seed). When set, writes a detached `<out>.sig` next to the JSON.",
    )
    ap.add_argument(
        "--gen-key",
        type=Path,
        default=None,
        help="Generate a fresh ed25519 signing keypair, write the private "
             "key as PEM to this path, print the public key, and exit.",
    )
    args = ap.parse_args(argv)

    if args.gen_key is not None:
        _gen_keypair(args.gen_key)
        return 0

    if not args.models_dir or not args.version or not args.out:
        ap.error("--models-dir, --version, and --out are required")

    manifest = build(args.models_dir, args.version, args.required)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
    args.out.write_bytes(payload)
    print(f"wrote {args.out} with {len(manifest['predictors'])} predictor(s)")

    if args.sign_key is not None:
        key = _load_signing_key(args.sign_key)
        sig = _sign_manifest(payload, key)
        sig_path = args.out.with_suffix(args.out.suffix + ".sig")
        sig_path.write_bytes(sig + b"\n")
        print(f"wrote detached signature to {sig_path} ({len(sig)} bytes b64)")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
