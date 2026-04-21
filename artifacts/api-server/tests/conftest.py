"""Test bootstrap.

Sets the bare-minimum env vars required for `app.config.get_settings()` to
import without contacting Supabase, then ensures the api-server package root
is on sys.path so `import app.main` works regardless of the cwd pytest is
launched from.
"""
from __future__ import annotations

import base64
import os
import sys
from pathlib import Path


_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

# get_settings() refuses to import without these — the values are never
# dialled out to during the auth-gate tests because no protected handler runs.
os.environ.setdefault("SUPABASE_DB_PASSWORD", "test-password")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")

# Synthesise a Clerk-style publishable key whose embedded host we control,
# so the auth module derives a known issuer (`https://<TEST_CLERK_HOST>`).
TEST_CLERK_HOST = "clerk.test.axiom-predict.local"
_pk_payload = base64.b64encode((TEST_CLERK_HOST + "$").encode()).decode().rstrip("=")
os.environ["CLERK_PUBLISHABLE_KEY"] = f"pk_test_{_pk_payload}"
