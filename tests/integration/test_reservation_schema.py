"""Integration tests for `reservations`/`reconciliation_anomalies` (T-014, spec 04 §2.2/§10/§11).

Mirrors `test_lease_schema.py`'s pattern: real Postgres via testcontainers. Covers the schema
half of T-014's acceptance bar — the operation behavior (idempotency, the G2 clamp, the G3
reject-and-record path) is `test_ledger.py`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration.conftest import alembic_config, make_budget

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_alembic_upgrade_then_downgrade_is_clean(postgres_url: str) -> None:
    cfg = alembic_config(postgres_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "0001")
    command.upgrade(cfg, "0002")
    command.upgrade(cfg, "0003")
    command.downgrade(cfg, "0002")
    command.downgrade(cfg, "base")


async def _insert_lease(engine: AsyncEngine, *, budget_id: uuid.UUID) -> uuid.UUID:
    lease_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO leases "
                "(id, budget_id, pep_id, granted, settled, granted_at, expires_at, state) "
                "VALUES "
                "(:id, :budget_id, 'pep-1', 10.0000, 0, :granted_at, :expires_at, 'active')"
            ),
            {"id": lease_id, "budget_id": budget_id, "granted_at": _NOW, "expires_at": _NOW},
        )
    return lease_id


async def _insert_reservation(
    engine: AsyncEngine, *, reservation_id: uuid.UUID, lease_id: uuid.UUID, **overrides: object
) -> None:
    row = {"id": reservation_id, "lease_id": lease_id, "amount": Decimal("1.0000"), **overrides}
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO reservations (id, lease_id, amount, created_at) "
                "VALUES (:id, :lease_id, :amount, :created_at)"
            ),
            {**row, "created_at": _NOW},
        )


async def test_valid_reservation_is_accepted(migrated_engine: AsyncEngine) -> None:
    budget_id = await make_budget(migrated_engine, mandate_id=uuid.uuid4())
    lease_id = await _insert_lease(migrated_engine, budget_id=budget_id)
    await _insert_reservation(migrated_engine, reservation_id=uuid.uuid4(), lease_id=lease_id)


async def test_duplicate_reservation_id_is_rejected(migrated_engine: AsyncEngine) -> None:
    """The primary key on the client-generated id is the entire idempotency mechanism (G4)."""
    budget_id = await make_budget(migrated_engine, mandate_id=uuid.uuid4())
    lease_id = await _insert_lease(migrated_engine, budget_id=budget_id)
    reservation_id = uuid.uuid4()
    await _insert_reservation(migrated_engine, reservation_id=reservation_id, lease_id=lease_id)
    with pytest.raises(IntegrityError):
        await _insert_reservation(migrated_engine, reservation_id=reservation_id, lease_id=lease_id)


async def test_reservation_against_nonexistent_lease_is_rejected(
    migrated_engine: AsyncEngine,
) -> None:
    with pytest.raises(IntegrityError):
        await _insert_reservation(
            migrated_engine, reservation_id=uuid.uuid4(), lease_id=uuid.uuid4()
        )


async def test_valid_anomaly_is_accepted(migrated_engine: AsyncEngine) -> None:
    budget_id = await make_budget(migrated_engine, mandate_id=uuid.uuid4())
    lease_id = await _insert_lease(migrated_engine, budget_id=budget_id)
    async with migrated_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO reconciliation_anomalies "
                "(id, lease_id, reservation_id, reported_amount, lease_state, created_at) "
                "VALUES (:id, :lease_id, :reservation_id, 5.0000, 'expired', :created_at)"
            ),
            {
                "id": uuid.uuid4(),
                "lease_id": lease_id,
                "reservation_id": uuid.uuid4(),
                "created_at": _NOW,
            },
        )


async def test_anomaly_against_nonexistent_lease_is_rejected(migrated_engine: AsyncEngine) -> None:
    with pytest.raises(IntegrityError):
        async with migrated_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO reconciliation_anomalies "
                    "(id, lease_id, reservation_id, reported_amount, lease_state, created_at) "
                    "VALUES (:id, :lease_id, :reservation_id, 5.0000, 'expired', :created_at)"
                ),
                {
                    "id": uuid.uuid4(),
                    "lease_id": uuid.uuid4(),
                    "reservation_id": uuid.uuid4(),
                    "created_at": _NOW,
                },
            )


async def test_repeated_anomalies_for_the_same_reservation_id_are_all_recorded(
    migrated_engine: AsyncEngine,
) -> None:
    """Unlike `reservations`, `reconciliation_anomalies` has no uniqueness on `reservation_id`.

    Every retried late commit is its own audit row (spec 04 §11 names no dedup requirement).
    """
    budget_id = await make_budget(migrated_engine, mandate_id=uuid.uuid4())
    lease_id = await _insert_lease(migrated_engine, budget_id=budget_id)
    reservation_id = uuid.uuid4()
    for _ in range(2):
        async with migrated_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO reconciliation_anomalies "
                    "(id, lease_id, reservation_id, reported_amount, lease_state, created_at) "
                    "VALUES (:id, :lease_id, :reservation_id, 5.0000, 'expired', :created_at)"
                ),
                {
                    "id": uuid.uuid4(),
                    "lease_id": lease_id,
                    "reservation_id": reservation_id,
                    "created_at": _NOW,
                },
            )
    async with migrated_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT count(*) FROM reconciliation_anomalies WHERE reservation_id = :rid"),
            {"rid": reservation_id},
        )
        assert result.scalar_one() == 2
