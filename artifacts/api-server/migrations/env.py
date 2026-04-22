"""Alembic environment.

Uses a synchronous psycopg connection so `alembic upgrade head` can be
invoked from a normal blocking call without spinning up an asyncio loop.

The DSN is sourced from `app.config.get_settings().supabase_db_dsn` and
re-shaped for psycopg (psycopg accepts the same `postgresql://...` URL
plus `?sslmode=require`, so no rewriting is required).
"""
from __future__ import annotations

import logging
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make `app.*` importable when alembic is invoked from this directory.
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app.config import get_settings  # noqa: E402


config = context.config

if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name)
    except Exception:
        # The .ini logging config is convenience-only; never fail the
        # migration run because of it.
        logging.getLogger().setLevel(logging.INFO)

# Inject the runtime DSN. We use psycopg (v3) as the sync driver; the
# Supabase DSN already carries `?sslmode=require`.
_dsn = get_settings().supabase_db_dsn
if _dsn.startswith("postgresql://"):
    _dsn = "postgresql+psycopg://" + _dsn[len("postgresql://") :]
config.set_main_option("sqlalchemy.url", _dsn)


# We don't use SQLAlchemy ORM models — every migration is hand-written
# raw SQL via op.execute, so target_metadata stays None.
target_metadata = None


def run_migrations_offline() -> None:
    """Render SQL to stdout without a live DB connection."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
