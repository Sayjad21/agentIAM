"""Integration tests for the `budgets` table (T-012, spec 04 §2.1).

Runs against a real Postgres in a testcontainers container, never sqlite and never the
dev compose instance — a `CHECK` constraint is a database-specific guarantee, and sqlite
would prove nothing about whether Postgres actually enforces it.

Covers the ticket's acceptance bar: Alembic up/down clean, the invariant `CHECK` fires in
the database (not only in application code), `NUMERIC(20,4)` is the real column type, and
`UNIQUE(mandate_id, dimension)` is enforced.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Generator
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.community.postgres import PostgresContainer

from agentiam_controlplane.db.base import make_engine

pytestmark = pytest.mark.integration

_PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "packages" / "agentiam-controlplane"


_MIGRATIONS_DIR = _PACKAGE_ROOT / "src" / "agentiam_controlplane" / "db" / "migrations"

#: Not a secret — a throwaway credential for a container that exists only for one test run.
_TEST_DB_PASSWORD = "agentiam"  # noqa: S105


def _alembic_config(database_url: str) -> Config:
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
def migrated_engine(postgres_url: str) -> Generator[AsyncEngine]:
    """Engine bound to a container with `alembic upgrade head` applied.

    Downgrades back to `base` on teardown — this doubles as half of the up/down test for
    every test that uses this fixture, and the explicit test below exercises the same pair
    of commands directly so the acceptance criterion has a test asserting it, not just a
    fixture that happens to exercise it.

    Deliberately a *sync* fixture: `alembic.command.upgrade` calls `asyncio.run()`
    internally (spec 04's env.py runs migrations through the async driver), and
    `asyncio.run()` raises if called from inside an already-running loop. An async fixture
    here would put it inside the test's loop; a sync fixture runs before that loop exists.
    """
    cfg = _alembic_config(postgres_url)
    command.upgrade(cfg, "head")
    engine = make_engine(postgres_url)
    yield engine
    asyncio.run(engine.dispose())
    command.downgrade(cfg, "base")


def test_alembic_upgrade_then_downgrade_is_clean(postgres_url: str) -> None:
    cfg = _alembic_config(postgres_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    # And it must be repeatable: a migration that only works once is not clean.
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")


async def _insert(engine: AsyncEngine, **values: object) -> None:
    row = {
        "id": uuid.uuid4(),
        "mandate_id": uuid.uuid4(),
        "dimension": "spend_bdt",
        "total": Decimal("100.0000"),
        "committed": Decimal("0"),
        "leased": Decimal("0"),
        "version": 0,
        **values,
    }
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO budgets "
                "(id, mandate_id, dimension, total, committed, leased, version) "
                "VALUES "
                "(:id, :mandate_id, :dimension, :total, :committed, :leased, :version)"
            ),
            row,
        )


async def test_valid_row_is_accepted(migrated_engine: AsyncEngine) -> None:
    await _insert(
        migrated_engine,
        total=Decimal("100.0000"),
        committed=Decimal("40.0000"),
        leased=Decimal("60.0000"),
    )


async def test_negative_committed_is_rejected_by_the_database(migrated_engine: AsyncEngine) -> None:
    with pytest.raises(IntegrityError):
        await _insert(migrated_engine, committed=Decimal("-0.0001"))


async def test_negative_leased_is_rejected_by_the_database(migrated_engine: AsyncEngine) -> None:
    with pytest.raises(IntegrityError):
        await _insert(migrated_engine, leased=Decimal("-0.0001"))


async def test_committed_plus_leased_exceeding_total_is_rejected(
    migrated_engine: AsyncEngine,
) -> None:
    with pytest.raises(IntegrityError):
        await _insert(
            migrated_engine,
            total=Decimal("100.0000"),
            committed=Decimal("60.0000"),
            leased=Decimal("60.0001"),
        )


async def test_committed_plus_leased_exactly_at_total_is_accepted(
    migrated_engine: AsyncEngine,
) -> None:
    await _insert(
        migrated_engine,
        total=Decimal("100.0000"),
        committed=Decimal("60.0000"),
        leased=Decimal("40.0000"),
    )


async def test_duplicate_mandate_dimension_is_rejected(migrated_engine: AsyncEngine) -> None:
    mandate_id = uuid.uuid4()
    await _insert(migrated_engine, mandate_id=mandate_id, dimension="spend_bdt")
    with pytest.raises(IntegrityError):
        await _insert(migrated_engine, mandate_id=mandate_id, dimension="spend_bdt")


async def test_same_mandate_different_dimension_is_accepted(migrated_engine: AsyncEngine) -> None:
    mandate_id = uuid.uuid4()
    await _insert(migrated_engine, mandate_id=mandate_id, dimension="spend_bdt")
    await _insert(migrated_engine, mandate_id=mandate_id, dimension="tool_calls")


async def test_fourth_decimal_place_survives_the_round_trip(migrated_engine: AsyncEngine) -> None:
    """`NUMERIC(20,4)` — a fifth decimal place would either round or truncate silently."""
    row_id = uuid.uuid4()
    await _insert(
        migrated_engine,
        id=row_id,
        total=Decimal("999999.9999"),
        committed=Decimal("0.0001"),
    )
    async with migrated_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT total, committed FROM budgets WHERE id = :id"), {"id": row_id}
        )
        total, committed = result.one()
    assert total == Decimal("999999.9999")
    assert committed == Decimal("0.0001")


async def test_money_columns_are_numeric_20_4(migrated_engine: AsyncEngine) -> None:
    async with migrated_engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT column_name, numeric_precision, numeric_scale "
                "FROM information_schema.columns "
                "WHERE table_name = 'budgets' AND column_name IN ('total', 'committed', 'leased')"
            )
        )
        rows = {name: (precision, scale) for name, precision, scale in result.all()}
    assert rows == {
        "total": (20, 4),
        "committed": (20, 4),
        "leased": (20, 4),
    }
