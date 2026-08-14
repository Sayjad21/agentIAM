"""Alembic environment: async engine over `asyncpg`, matching the application's driver.

The project's `DATABASE_URL` is `postgresql+asyncpg://...` (`.env.example`), so migrations
run through the same async driver rather than pulling in a second, sync-only one just for
Alembic. The URL comes from the `DATABASE_URL` environment variable, or from
`sqlalchemy.url` in `alembic.ini` if a caller has set it via `Config.set_main_option` — the
integration test uses the latter to point at its testcontainers instance.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import AsyncEngine, async_engine_from_config

from agentiam_controlplane.db.base import Base
from agentiam_controlplane.db.models import (  # noqa: F401  (registers on Base.metadata)
    BudgetRow,
    LeaseRow,
    ReconciliationAnomalyRow,
    ReservationRow,
)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL") or config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError(
            "No database URL: set DATABASE_URL or pass sqlalchemy.url via Config.set_main_option"
        )
    return url


def run_migrations_offline() -> None:
    """Emit SQL without a live connection (`alembic upgrade --sql`)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _database_url()
    connectable: AsyncEngine = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations against a live async connection."""
    asyncio.run(_run_migrations_online())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
