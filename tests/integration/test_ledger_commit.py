"""Integration tests for `LEDGER_COMMIT` (T-014, spec 04 §4.4, §10, §11).

Real Postgres via testcontainers — the same reason `test_ledger.py` uses it: row-level
locking and the PK-based idempotency mechanism are exactly what sqlite or a mock session
cannot prove. Three guards are demonstrated load-bearing here the way `docs/JOURNAL.md`'s
recurring lesson asks:

- G2 (clamp `amount` to `lease.outstanding`) — temporarily removed, watch `leased` go
  negative, restored. See the T-014 commit message for the measurement.
- G3 (reject commits against a non-`active` lease) — temporarily removed, watch a commit
  against an already-reaped lease double-decrement `leased`, restored.
- The duplicate-check-before-the-lease-lock ordering that spec 04 §4.4's own pseudocode
  writes literally is a TOCTOU race: two concurrent commits carrying the same
  `reservation_id` can both pass the "already settled?" check before either takes the
  lease's `FOR UPDATE` lock, and both then apply. `ledger_commit()` checks *after* acquiring
  the lock instead — same postcondition, race closed. Demonstrated by temporarily reverting
  to the literal order and rerunning the concurrent-duplicate test.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import NullPool

from agentiam_controlplane.db.base import make_engine, make_session_factory
from agentiam_controlplane.db.ledger import acquire, ledger_commit, reap, release
from agentiam_controlplane.db.models import BudgetRow, LeaseRow, ReconciliationAnomalyRow
from agentiam_controlplane.errors import LeaseNotActiveError
from tests.integration.conftest import make_budget

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_TTL = timedelta(seconds=60)


async def _budget(engine: AsyncEngine, budget_id: uuid.UUID) -> BudgetRow:
    factory = make_session_factory(engine)
    async with factory() as s:
        result = await s.execute(select(BudgetRow).where(BudgetRow.id == budget_id))
        return result.scalar_one()


async def _lease(engine: AsyncEngine, lease_id: uuid.UUID) -> LeaseRow:
    factory = make_session_factory(engine)
    async with factory() as s:
        result = await s.execute(select(LeaseRow).where(LeaseRow.id == lease_id))
        return result.scalar_one()


async def _acquire(engine: AsyncEngine, *, mandate_id: uuid.UUID, requested: Decimal) -> LeaseRow:
    factory = make_session_factory(engine)
    async with factory() as s:
        return await acquire(
            s,
            mandate_id=mandate_id,
            dimension="spend_bdt",
            requested=requested,
            pep_id="pep-1",
            ttl=_TTL,
            now=_NOW,
        )


# ---------------------------------------------------------------------------
# Basic apply
# ---------------------------------------------------------------------------


async def test_ledger_commit_applies_the_full_amount_when_within_outstanding(
    migrated_engine: AsyncEngine,
) -> None:
    mandate_id = uuid.uuid4()
    budget_id = await make_budget(migrated_engine, mandate_id=mandate_id, total=Decimal("100"))
    lease = await _acquire(migrated_engine, mandate_id=mandate_id, requested=Decimal("40"))

    factory = make_session_factory(migrated_engine)
    async with factory() as s:
        applied = await ledger_commit(
            s, lease_id=lease.id, reservation_id=uuid.uuid4(), amount=Decimal("30"), now=_NOW
        )
    assert applied is True

    budget = await _budget(migrated_engine, budget_id)
    assert budget.committed == Decimal("30.0000")
    assert budget.leased == Decimal("10.0000")
    settled_lease = await _lease(migrated_engine, lease.id)
    assert settled_lease.settled == Decimal("30.0000")
    assert settled_lease.outstanding == Decimal("10.0000")


async def test_ledger_commit_decimal_precision_is_exact_to_four_places(
    migrated_engine: AsyncEngine,
) -> None:
    """PLAN.md §9's T-014 acceptance bar names these two values explicitly."""
    mandate_id = uuid.uuid4()
    await make_budget(migrated_engine, mandate_id=mandate_id, total=Decimal("1000000"))
    lease = await _acquire(migrated_engine, mandate_id=mandate_id, requested=Decimal("999999.9999"))

    factory = make_session_factory(migrated_engine)
    async with factory() as s:
        await ledger_commit(
            s,
            lease_id=lease.id,
            reservation_id=uuid.uuid4(),
            amount=Decimal("0.0001"),
            now=_NOW,
        )
    async with factory() as s:
        await ledger_commit(
            s,
            lease_id=lease.id,
            reservation_id=uuid.uuid4(),
            amount=Decimal("999999.9998"),
            now=_NOW,
        )

    settled_lease = await _lease(migrated_engine, lease.id)
    assert settled_lease.settled == Decimal("999999.9999")
    assert settled_lease.outstanding == Decimal("0.0000")


# ---------------------------------------------------------------------------
# G2 — clamp to outstanding
# ---------------------------------------------------------------------------


async def test_ledger_commit_clamps_amount_to_outstanding(migrated_engine: AsyncEngine) -> None:
    """A PEP reporting more than the lease actually holds is clamped, not trusted (G2)."""
    mandate_id = uuid.uuid4()
    budget_id = await make_budget(migrated_engine, mandate_id=mandate_id, total=Decimal("100"))
    lease = await _acquire(migrated_engine, mandate_id=mandate_id, requested=Decimal("10"))

    factory = make_session_factory(migrated_engine)
    async with factory() as s:
        applied = await ledger_commit(
            s, lease_id=lease.id, reservation_id=uuid.uuid4(), amount=Decimal("999"), now=_NOW
        )
    assert applied is True

    budget = await _budget(migrated_engine, budget_id)
    assert budget.committed == Decimal("10.0000")
    assert budget.leased == Decimal("0.0000")  # not negative
    settled_lease = await _lease(migrated_engine, lease.id)
    assert settled_lease.outstanding == Decimal("0.0000")


async def test_ledger_commit_is_a_no_op_once_the_lease_is_fully_settled(
    migrated_engine: AsyncEngine,
) -> None:
    """A second, distinct reservation against an already-fully-settled active lease.

    Clamps to zero and changes nothing — not an error, just nothing left to apply.
    """
    mandate_id = uuid.uuid4()
    await make_budget(migrated_engine, mandate_id=mandate_id, total=Decimal("100"))
    lease = await _acquire(migrated_engine, mandate_id=mandate_id, requested=Decimal("10"))
    factory = make_session_factory(migrated_engine)
    async with factory() as s:
        await ledger_commit(
            s, lease_id=lease.id, reservation_id=uuid.uuid4(), amount=Decimal("10"), now=_NOW
        )
    async with factory() as s:
        applied = await ledger_commit(
            s, lease_id=lease.id, reservation_id=uuid.uuid4(), amount=Decimal("5"), now=_NOW
        )
    assert applied is False
    settled_lease = await _lease(migrated_engine, lease.id)
    assert settled_lease.settled == Decimal("10.0000")


# ---------------------------------------------------------------------------
# G4 — idempotency by reservation_id
# ---------------------------------------------------------------------------


async def test_ledger_commit_duplicate_reservation_id_is_idempotent(
    migrated_engine: AsyncEngine,
) -> None:
    """P-12 (ADR-010): assert accounting equality, not just that the pool stays safe."""
    mandate_id = uuid.uuid4()
    budget_id = await make_budget(migrated_engine, mandate_id=mandate_id, total=Decimal("100"))
    lease = await _acquire(migrated_engine, mandate_id=mandate_id, requested=Decimal("40"))
    reservation_id = uuid.uuid4()

    factory = make_session_factory(migrated_engine)
    for _ in range(3):
        async with factory() as s:
            await ledger_commit(
                s, lease_id=lease.id, reservation_id=reservation_id, amount=Decimal("30"), now=_NOW
            )

    budget = await _budget(migrated_engine, budget_id)
    assert budget.committed == Decimal("30.0000")  # not 90
    settled_lease = await _lease(migrated_engine, lease.id)
    assert settled_lease.settled == Decimal("30.0000")
    assert settled_lease.outstanding == Decimal("10.0000")  # not 10 - 60


async def test_concurrent_duplicate_ledger_commits_apply_exactly_once(
    postgres_url: str, migrated_engine: AsyncEngine
) -> None:
    """G4 under real concurrency, not just sequential replay.

    A retried batch send racing itself is the scenario idempotency exists for.
    """
    mandate_id = uuid.uuid4()
    budget_id = await make_budget(migrated_engine, mandate_id=mandate_id, total=Decimal("100"))
    lease = await _acquire(migrated_engine, mandate_id=mandate_id, requested=Decimal("40"))
    reservation_id = uuid.uuid4()

    concurrent_engine = make_engine(postgres_url, poolclass=NullPool)
    factory = make_session_factory(concurrent_engine)

    async def one_commit() -> bool:
        async with factory() as s:
            return await ledger_commit(
                s, lease_id=lease.id, reservation_id=reservation_id, amount=Decimal("30"), now=_NOW
            )

    results = await asyncio.gather(*(one_commit() for _ in range(10)))
    await concurrent_engine.dispose()

    assert sum(results) == 1
    budget = await _budget(migrated_engine, budget_id)
    assert budget.committed == Decimal("30.0000")


# ---------------------------------------------------------------------------
# G3 — reject commits against a non-active lease
# ---------------------------------------------------------------------------


async def test_ledger_commit_rejects_a_released_lease_and_records_an_anomaly(
    migrated_engine: AsyncEngine,
) -> None:
    mandate_id = uuid.uuid4()
    budget_id = await make_budget(migrated_engine, mandate_id=mandate_id, total=Decimal("10"))
    lease = await _acquire(migrated_engine, mandate_id=mandate_id, requested=Decimal("4"))
    factory = make_session_factory(migrated_engine)
    async with factory() as s:
        assert await release(s, lease_id=lease.id) is True

    reservation_id = uuid.uuid4()
    async with factory() as s:
        with pytest.raises(LeaseNotActiveError) as exc_info:
            await ledger_commit(
                s, lease_id=lease.id, reservation_id=reservation_id, amount=Decimal("4"), now=_NOW
            )
    assert exc_info.value.reason_code is not None
    assert exc_info.value.reason_code.value == "LEASE_NOT_ACTIVE"

    budget = await _budget(migrated_engine, budget_id)
    assert budget.leased == Decimal("0.0000")  # unchanged by the rejected commit
    assert budget.committed == Decimal("0.0000")

    async with factory() as s:
        result = await s.execute(
            select(ReconciliationAnomalyRow).where(
                ReconciliationAnomalyRow.reservation_id == reservation_id
            )
        )
        anomaly = result.scalar_one()
    assert anomaly.lease_id == lease.id
    assert anomaly.reported_amount == Decimal("4.0000")
    assert anomaly.lease_state == "released"


async def test_ledger_commit_rejects_a_reaped_lease_TM21(migrated_engine: AsyncEngine) -> None:
    """TM-21: a commit for budget already returned by REAP must not double-decrement `leased`."""
    mandate_id = uuid.uuid4()
    budget_id = await make_budget(migrated_engine, mandate_id=mandate_id, total=Decimal("10"))
    factory = make_session_factory(migrated_engine)
    async with factory() as s:
        lease = await acquire(
            s,
            mandate_id=mandate_id,
            dimension="spend_bdt",
            requested=Decimal("4"),
            pep_id="pep-1",
            ttl=timedelta(seconds=-10),
            now=_NOW,
        )
    async with factory() as s:
        assert lease.id in await reap(s, now=_NOW)

    async with factory() as s:
        with pytest.raises(LeaseNotActiveError):
            await ledger_commit(
                s, lease_id=lease.id, reservation_id=uuid.uuid4(), amount=Decimal("4"), now=_NOW
            )

    budget = await _budget(migrated_engine, budget_id)
    assert budget.leased == Decimal("0.0000")  # already returned once by REAP, not twice
