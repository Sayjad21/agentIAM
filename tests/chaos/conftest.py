"""Fixtures for the chaos suite — T-052.

Almost the same as `tests/integration/conftest.py`, with one difference that is the whole
reason this file exists: **the container's host port is pinned.**

Measured, not assumed. `testcontainers` publishes on a random host port, and stopping and
starting the same container reassigns it — one probe went from `:54423` to `:54429` across
a `stop()`/`start()` pair. CH-1 stops Postgres and then asserts that recovery is clean, so
against a moving port the PEP's DSN would be dead forever and the recovery half of the
scenario would assert nothing. `with_bind_ports` pins it, and a second probe confirmed the
same URL reconnects two attempts after the restart.

The port is taken by binding a socket to `:0` and reading what the OS gave out. That is a
race in principle — the port is free when asked for and claimed a moment later — and it is
the standard one, small enough to prefer over a hard-coded number that collides with
whatever else is running on a developer's machine.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Generator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.community.postgres import PostgresContainer

from agentiam_controlplane.db.base import make_engine

_PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "packages" / "agentiam-controlplane"
_MIGRATIONS_DIR = _PACKAGE_ROOT / "src" / "agentiam_controlplane" / "db" / "migrations"

#: Not a secret — a throwaway credential for a container that lives for one module.
_TEST_DB_PASSWORD = "agentiam"  # noqa: S105


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def alembic_config(database_url: str) -> Config:
    """An Alembic config pointed at this package's migrations and `database_url`."""
    cfg = Config(str(_PACKAGE_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


@pytest.fixture(scope="module")
def pg_container() -> Generator[PostgresContainer]:
    """A Postgres container on a **pinned** host port, so it survives a restart."""
    container = PostgresContainer(
        image="postgres:16-alpine",
        driver="asyncpg",
        username="agentiam",
        password=_TEST_DB_PASSWORD,
        dbname="agentiam",
    ).with_bind_ports(5432, _free_port())
    with container:
        yield container


@pytest.fixture(scope="module")
def postgres_url(pg_container: PostgresContainer) -> str:
    """The pinned connection URL, `postgresql+asyncpg://...`."""
    return str(pg_container.get_connection_url())


@pytest.fixture
def migrated_engine(postgres_url: str) -> Iterator[AsyncEngine]:
    """An engine against a migrated database, direct — never through a fault proxy.

    Scenarios that partition the PEP hand *it* a proxied URL and keep this one for the
    invariant sidecar, so the checker can still see the ledger the PEP has lost.

    Sync, for the reason `tests/integration/conftest.py` records: `alembic.command` calls
    `asyncio.run()` internally and raises inside a running loop.
    """
    cfg = alembic_config(postgres_url)
    command.upgrade(cfg, "head")
    engine = make_engine(postgres_url)
    yield engine
    asyncio.run(engine.dispose())
    command.downgrade(cfg, "base")
