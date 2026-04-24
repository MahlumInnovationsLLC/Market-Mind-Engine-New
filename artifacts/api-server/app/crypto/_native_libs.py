"""Best-effort runtime preload for libgomp (the OpenMP runtime).

LightGBM and XGBoost both link against ``libgomp.so.1``, which is not
on the default loader path under NixOS.  Installing the gcc-lib via the
package manager is fiddly, so we instead resolve the canonical path
through ``gcc -print-file-name=libgomp.so.1`` (an idiomatic gcc query
that works on any Linux toolchain) and ``ctypes.CDLL`` it before the
ML modules are imported.

Failures here are intentionally non-fatal: if libgomp is already on the
loader path (any other distro, a Docker base image with it preinstalled,
etc.) the up-front ``ctypes.CDLL("libgomp.so.1")`` succeeds and we're
done.  If neither path resolves, the eventual ``import lightgbm`` will
raise the underlying loader error, which is the right failure mode for
a server whose model files cannot be deserialised.
"""
from __future__ import annotations

import ctypes
import shutil
import subprocess


_loaded: bool = False


def ensure_libgomp() -> bool:
    """Load libgomp.so.1 into the process. Returns True on success."""
    global _loaded
    if _loaded:
        return True
    try:
        ctypes.CDLL("libgomp.so.1", mode=ctypes.RTLD_GLOBAL)
        _loaded = True
        return True
    except OSError:
        pass
    gcc = shutil.which("gcc")
    if not gcc:
        return False
    try:
        out = subprocess.run(
            [gcc, "-print-file-name=libgomp.so.1"],
            check=True, capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return False
    if not out or out == "libgomp.so.1":
        return False
    try:
        ctypes.CDLL(out, mode=ctypes.RTLD_GLOBAL)
    except OSError:
        return False
    _loaded = True
    return True
