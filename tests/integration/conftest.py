"""Shared testcontainers fixtures for the ledger's integration tests (T-013).

`tests/integration/test_budget_schema.py` (T-012) predates this file and keeps its own
copy of the same fixtures — left alone rather than refactored in a ticket that isn't
T-012's. New integration modules use this one.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Generator, Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

from agentiam_controlplane.db.base import make_engine, make_session_factory
from agentiam_controlplane.db.models import BudgetRow

_PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "packages" / "agentiam-controlplane"
_MIGRATIONS_DIR = _PACKAGE_ROOT / "src" / "agentiam_controlplane" / "db" / "migrations"

#: Not a secret — a throwaway credential for a container that exists only for one test run.
_TEST_DB_PASSWORD = "agentiam"  # noqa: S105


def alembic_config(database_url: str) -> Config:
    """Build an `alembic.config.Config` pointed at this package's migrations and `database_url`."""
    cfg = Config(str(_PACKAGE_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


@pytest.fixture(scope="module")
def postgres_url() -> Generator[str]:
    """A fresh Postgres container's connection URL, `postgresql+asyncpg://...`."""
    with PostgresContainer(
        image="postgres:16-alpine",
        driver="asyncpg",
        username="agentiam",
        password=_TEST_DB_PASSWORD,
        dbname="agentiam",
    ) as container:
        yield container.get_connection_url()


@pytest.fixture
def redis_container() -> Generator[RedisContainer]:
    """A fresh Redis container, function-scoped so a test can stop it mid-test (EC-R07)."""
    with RedisContainer(image="redis:7-alpine") as container:
        yield container


@pytest.fixture
def redis_url(redis_container: RedisContainer) -> str:
    """`redis://host:port` for `redis.asyncio.Redis.from_url()`."""
    host = redis_container.get_container_host_ip()
    port = redis_container.get_exposed_port(redis_container.port)
    return f"redis://{host}:{port}"


@pytest.fixture
def migrated_engine(postgres_url: str) -> Iterator[AsyncEngine]:
    """Engine bound to a container with `alembic upgrade head` applied.

    Deliberately a *sync* fixture — see `tests/integration/test_budget_schema.py` for why:
    `alembic.command.upgrade`/`downgrade` call `asyncio.run()` internally, which raises if
    called from inside an already-running event loop.
    """
    cfg = alembic_config(postgres_url)
    command.upgrade(cfg, "head")
    engine = make_engine(postgres_url)
    yield engine
    asyncio.run(engine.dispose())
    command.downgrade(cfg, "base")


@pytest.fixture
async def session(migrated_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A single session against the migrated engine, for tests that don't need concurrency."""
    factory = make_session_factory(migrated_engine)
    async with factory() as s:
        yield s


async def make_budget(
    engine: AsyncEngine,
    *,
    mandate_id: uuid.UUID,
    dimension: str = "spend_bdt",
    total: Decimal = Decimal("100.0000"),
) -> uuid.UUID:
    """Insert a `BudgetRow` directly (bypassing any ledger op) and return its id."""
    factory = make_session_factory(engine)
    async with factory() as s, s.begin():
        budget = BudgetRow(mandate_id=mandate_id, dimension=dimension, total=total)
        s.add(budget)
        await s.flush()
        budget_id = budget.id
    return budget_id
