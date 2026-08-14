"""Integration tests for the `leases` table (T-013, spec 04 §2.2, §3).

Mirrors `test_budget_schema.py`'s pattern: real Postgres via testcontainers, never sqlite.
Covers the schema half of T-013's acceptance bar — the operation behavior (`FOR UPDATE`
serialization, the skew margin, idempotent RELEASE/REAP) is `test_ledger.py`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
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
    # And it must be repeatable, and step through 0001 -> 0002 -> base cleanly, not just
    # jump straight to head/base.
    command.upgrade(cfg, "0001")
    command.upgrade(cfg, "0002")
    command.downgrade(cfg, "0001")
    command.downgrade(cfg, "base")


async def _insert_lease(engine: AsyncEngine, *, budget_id: uuid.UUID, **overrides: object) -> None:
    row = {
        "id": uuid.uuid4(),
        "budget_id": budget_id,
        "pep_id": "pep-1",
        "granted": Decimal("10.0000"),
        "settled": Decimal("0"),
        "granted_at": _NOW,
        "expires_at": _NOW + timedelta(seconds=60),
        "state": "active",
        **overrides,
    }
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO leases "
                "(id, budget_id, pep_id, granted, settled, granted_at, expires_at, state) "
                "VALUES "
                "(:id, :budget_id, :pep_id, :granted, :settled, :granted_at, :expires_at, :state)"
            ),
            row,
        )


async def test_valid_lease_is_accepted(migrated_engine: AsyncEngine) -> None:
    budget_id = await make_budget(migrated_engine, mandate_id=uuid.uuid4())
    await _insert_lease(migrated_engine, budget_id=budget_id)


async def test_negative_granted_is_rejected(migrated_engine: AsyncEngine) -> None:
    budget_id = await make_budget(migrated_engine, mandate_id=uuid.uuid4())
    with pytest.raises(IntegrityError):
        await _insert_lease(migrated_engine, budget_id=budget_id, granted=Decimal("-1"))


async def test_negative_settled_is_rejected(migrated_engine: AsyncEngine) -> None:
    budget_id = await make_budget(migrated_engine, mandate_id=uuid.uuid4())
    with pytest.raises(IntegrityError):
        await _insert_lease(migrated_engine, budget_id=budget_id, settled=Decimal("-1"))


async def test_settled_exceeding_granted_is_rejected(migrated_engine: AsyncEngine) -> None:
    budget_id = await make_budget(migrated_engine, mandate_id=uuid.uuid4())
    with pytest.raises(IntegrityError):
        await _insert_lease(
            migrated_engine, budget_id=budget_id, granted=Decimal("5"), settled=Decimal("5.0001")
        )


async def test_settled_equal_to_granted_is_accepted(migrated_engine: AsyncEngine) -> None:
    """`outstanding == 0` is a valid, fully-settled lease — not the same as violating it."""
    budget_id = await make_budget(migrated_engine, mandate_id=uuid.uuid4())
    await _insert_lease(
        migrated_engine, budget_id=budget_id, granted=Decimal("5"), settled=Decimal("5")
    )


async def test_invalid_state_is_rejected(migrated_engine: AsyncEngine) -> None:
    budget_id = await make_budget(migrated_engine, mandate_id=uuid.uuid4())
    with pytest.raises(IntegrityError):
        await _insert_lease(migrated_engine, budget_id=budget_id, state="crashed")


async def test_nonexistent_budget_id_is_rejected(migrated_engine: AsyncEngine) -> None:
    """The real foreign key (unlike `budgets.mandate_id`, ADR-014) actually fires."""
    with pytest.raises(IntegrityError):
        await _insert_lease(migrated_engine, budget_id=uuid.uuid4())
