"""End-to-end verification that the v155 model artifacts in the
``axiom-predict-artifacts`` Supabase bucket are reachable through
``app.models_loader`` exactly the way the trading loop will reach for
them at request time.

What this script does (in order):

1. **Bucket inventory.** Lists ``exports/`` and prints every part file
   plus the manifest. A bucket inventory mismatch is the single most
   common cause of a silent ``models_loader`` failure, so we surface
   it first before we even try to download.
2. **End-to-end ``ensure_artifacts()``.** Calls the same entry point
   the FastAPI app calls during ``lifespan``. This walks the full
   pipeline: signature verification on the manifest, multi-part
   reassembly of ``models.tar`` and ``source.tar``, safe extraction
   into ``.cache/axiom-models/{models,source}``, predictor scan,
   sha256 verification against the manifest, and the canonical
   smoke-load of every entry in ``CANONICAL_PREDICTORS``.
3. **Per-predictor report.** Prints one line per ``CANONICAL_PREDICTORS``
   entry: present? sha matches manifest? deserializes? type? load
   time? Plus a summary of any mismatch / missing / unexpected
   findings from the manifest check.
4. **Exit code.** ``0`` if every required predictor verified and the
   manifest check passed; ``1`` otherwise. The script never modifies
   the bucket — fixes for any mismatch must land in
   ``app/models_loader.py`` (filename mapping, manifest parsing,
   version selection), per the explicit constraint in task #729.

Run with::

    /home/runner/workspace/.pythonlibs/bin/python3 \\
        artifacts/api-server/scripts/verify_v155_artifacts.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import models_loader, storage  # noqa: E402
from app.config import get_settings  # noqa: E402


# Canonical predictors whose absence is a documented, accepted condition
# rather than an artifact-pipeline bug. Each entry is paired with a short
# reason that surfaces in the verifier output so an operator reading a
# FAIL line can immediately tell "ship-blocking" apart from "expected".
#
# Today this list covers the weather GRU checkpoints. v155 does not
# bundle them; ``app/weather/predictor.py::_resolve_gru_weights`` and
# ``app/weather/precip_predictor.py`` both wrap
# ``models_loader.load_predictor`` in try/except and fall back to
# deterministic baseline weights with a logged ``[temperature] gru
# weight load failed: …`` warning. The trading loop continues to score
# weather markets — just with the baseline curve instead of a trained
# GRU — so missing GRU artifacts do not block the READY gate.
FAIL_SOFT_PREDICTORS: dict[str, str] = {
    "models/saved/temperature_gru.pkl": (
        "v155 does not ship a trained temperature GRU; "
        "weather/predictor.py falls back to gru.baseline_weights()"
    ),
    "models/saved/precip_gru.pkl": (
        "v155 does not ship a trained precipitation GRU; "
        "weather/precip_predictor.py falls back to baseline weights"
    ),
}


def _fmt_bytes(n: int | None) -> str:
    if n is None:
        return "?"
    n = int(n)
    if n < 1 << 10:
        return f"{n} B"
    if n < 1 << 20:
        return f"{n / (1 << 10):.1f} KiB"
    if n < 1 << 30:
        return f"{n / (1 << 20):.1f} MiB"
    return f"{n / (1 << 30):.2f} GiB"


async def _bucket_inventory() -> tuple[list[storage.StorageEntry], list[str]]:
    """Return (entries, problems) for the exports/ prefix.

    ``problems`` is a list of human-readable strings describing
    anything that would block ``ensure_artifacts`` — missing
    manifest, missing required parts, etc.
    """
    settings = get_settings()
    print(f"  bucket  = {settings.supabase_bucket}")
    print(f"  prefix  = exports/")
    entries = await storage.list_path("exports", limit=500)
    if len(entries) >= 500:
        print("  WARNING: hit list_path cap of 500 — paging not implemented here")
    files = sorted([e for e in entries if not e.is_folder], key=lambda e: e.name)
    print(f"  found   = {len(files)} object(s)")
    for e in files:
        print(f"    {e.name:48s}  {_fmt_bytes(e.size)}")

    names = {e.name for e in files}
    problems: list[str] = []
    if "models_manifest.json" not in names:
        problems.append("missing required object: models_manifest.json")
    # Check that at least models_part_aa and source_part_aa are present;
    # the loader's reassembly will validate full contiguity itself.
    if not any(n.startswith("models_part_") for n in names):
        problems.append("no models_part_* objects found (loader will fail)")
    if not any(n.startswith("source_part_") for n in names):
        problems.append("no source_part_* objects found (loader will fail)")
    return files, problems


async def main() -> int:
    print("=" * 78)
    print(" v155 artifact verification — end to end")
    print("=" * 78)

    print("\n[1/4] bucket inventory")
    print("-" * 78)
    try:
        _files, problems = await _bucket_inventory()
    except Exception as e:
        print(f"  FAIL: bucket inventory failed: {type(e).__name__}: {e}")
        return 1
    if problems:
        print("  PROBLEMS detected before download:")
        for p in problems:
            print(f"    - {p}")

    print("\n[2/4] ensure_artifacts() — same path the API server uses")
    print("-" * 78)
    print(f"  cache_root = {models_loader.CACHE_ROOT}")
    # Force a clean run so we don't get a confusingly-cached PASS when
    # the on-disk cache was populated during a previous boot.
    state = await models_loader.ensure_artifacts(force=True)
    if state.error:
        print(f"  loader.error = {state.error}")
    print(f"  loader.ready = {state.ready}")
    if state.manifest:
        print(
            f"  manifest:    version={state.manifest.get('version')!r} "
            f"predictor_count={state.manifest.get('predictor_count')} "
            f"signature={state.manifest.get('signature')}"
        )
    else:
        print("  manifest:    (none)")
    if state.archives:
        for name, info in state.archives.items():
            print(
                f"  archive[{name}]: extracted={_fmt_bytes(info.extracted_bytes)} "
                f"files={info.file_count} cached={info.cached}"
            )
    check = state.manifest_check or {}
    if check:
        print(
            f"  manifest_check: ok={check.get('ok')} "
            f"matched={check.get('matched_count')}/{check.get('manifest_predictor_count')} "
            f"signature={check.get('signature')} reason={check.get('reason') or '(ok)'}"
        )
        for label in ("missing", "mismatch"):
            items = check.get(label) or []
            if items:
                print(f"  {label}:")
                for it in items[:10]:
                    print(f"    - {it.get('name')}")
                if len(items) > 10:
                    print(f"    ... +{len(items) - 10} more")
        unexpected = check.get("unexpected") or []
        if unexpected:
            print(f"  unexpected files in extract (informational): {len(unexpected)}")
            for u in unexpected[:5]:
                print(f"    - {u}")
            if len(unexpected) > 5:
                print(f"    ... +{len(unexpected) - 5} more")

    print("\n[3/4] CANONICAL_PREDICTORS smoke-load")
    print("-" * 78)
    canonical = models_loader.CANONICAL_PREDICTORS
    loaded = state.loaded or {}
    print(f"  canonical predictor count = {len(canonical)}")
    ok_count = 0
    hard_fail: list[str] = []
    soft_fail: list[str] = []
    for name in canonical:
        result = loaded.get(name) or {}
        present = name in state.predictors
        sha = (state.predictors.get(name) or {}).get("version")
        if result.get("ok"):
            ok_count += 1
            print(
                f"  OK   {name}\n"
                f"        present={present} sha12={sha} "
                f"type={result.get('module')}.{result.get('type')} "
                f"load={result.get('load_seconds')}s"
            )
        elif name in FAIL_SOFT_PREDICTORS:
            soft_fail.append(name)
            print(
                f"  SOFT {name}\n"
                f"        present={present} reason={result.get('error') or '(absent)'}\n"
                f"        accepted because: {FAIL_SOFT_PREDICTORS[name]}"
            )
        else:
            hard_fail.append(name)
            print(
                f"  FAIL {name}\n"
                f"        present={present} sha12={sha} "
                f"reason={result.get('error') or '(no smoke result)'}"
            )

    print("\n[4/4] verdict")
    print("-" * 78)
    # PASS criteria: loader is ready, manifest sha-check is fully ok,
    # AND every canonical predictor either loaded successfully OR is in
    # the documented FAIL_SOFT_PREDICTORS set. A canonical predictor
    # that's absent without a documented fail-soft path is a hard fail
    # — that's exactly the kind of silent regression #729 was meant to
    # catch.
    required_ok = (
        state.ready
        and check.get("ok") is True
        and not hard_fail
    )
    print(f"  ready                 = {state.ready}")
    print(f"  manifest_check.ok     = {check.get('ok')}")
    print(f"  signature             = {check.get('signature')}")
    print(
        f"  canonical loaded      = {ok_count}/{len(canonical)} "
        f"(soft-fail accepted: {len(soft_fail)}, hard-fail: {len(hard_fail)})"
    )
    print(f"  predictor catalog     = {len(state.predictors)} files")
    print(f"  bucket problems       = {len(problems)}")
    if required_ok:
        print("\n  VERDICT: PASS — v155 artifacts are reachable through models_loader.")
        if soft_fail:
            print(
                f"  Note: {len(soft_fail)} canonical predictor(s) absent under documented"
            )
            print("  fail-soft paths (see SOFT lines above).")
        return 0
    print("\n  VERDICT: FAIL — see above for the specific failure.")
    print(
        "  Per task #729: any fix lands in app/models_loader.py "
        "(naming / manifest / version selection). DO NOT modify the bucket."
    )
    return 1


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc)
