"""Programmatic Alembic migration runner.

Replaces the previous family of lazy ``ensure_schema`` / ``ensure_table``
helpers that ran ``CREATE TABLE IF NOT EXISTS`` and ``ALTER TABLE
... ADD COLUMN IF NOT EXISTS`` from inside request handlers. Now we run
``alembic upgrade head`` once on startup before serving traffic.

The runner is invoked from ``app.main.lifespan`` via
``asyncio.to_thread`` so the synchronous psycopg engine doesn't block
the event loop.
"""
from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config


log = logging.getLogger(__name__)


_API_SERVER_ROOT = Path(__file__).resolve().parent.parent
_ALEMBIC_INI = _API_SERVER_ROOT / "alembic.ini"


def _build_config() -> Config:
    cfg = Config(str(_ALEMBIC_INI))
    # Alembic resolves `script_location` relative to the .ini, but only
    # if its value is itself relative; we store it relative ("migrations")
    # so this just works. We still pin the absolute prepend_sys_path so
    # `import app.config` works regardless of cwd.
    cfg.set_main_option("script_location", str(_API_SERVER_ROOT / "migrations"))
    cfg.set_main_option("prepend_sys_path", str(_API_SERVER_ROOT))
    return cfg


def run_migrations_sync() -> None:
    """Run ``alembic upgrade head`` against the configured DB.

    Synchronous on purpose so it can be called from
    ``asyncio.to_thread`` inside the FastAPI lifespan. Raises on
    failure; the caller is expected to mark the readiness check failed
    and surface the error via ``/api/health/ready``.
    """
    cfg = _build_config()
    log.info("running alembic upgrade head")
    command.upgrade(cfg, "head")
    log.info("alembic migrations applied")


def current_revision() -> str | None:
    """Return the head revision known to the script directory.

    Cheap, does not touch the DB. Used by the readiness probe to surface
    "we expect to be at <rev>" alongside whatever check status.
    """
    try:
        from alembic.script import ScriptDirectory

        cfg = _build_config()
        script = ScriptDirectory.from_config(cfg)
        head = script.get_current_head()
        return head
    except Exception as e:
        log.debug("unable to read alembic head revision: %s", e)
        return None
