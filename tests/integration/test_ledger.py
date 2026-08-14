"""Integration tests for `ACQUIRE`, `RELEASE`, `REAP` (T-013, spec 04 §4.1, §4.5, §4.6).

Real Postgres via testcontainers — row-level locking is exactly the thing sqlite or a
mock session cannot prove. Two guards are demonstrated load-bearing here the way
`docs/JOURNAL.md`'s recurring lesson asks: `SELECT ... FOR UPDATE` in `acquire()` and the
clock-skew margin in `reap()` were each temporarily removed, the relevant test run to
watch it fail, then restored — see the T-013 commit message for the measurements.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import NullPool

from agentiam_controlplane.db.base import make_engine, make_session_factory
from agentiam_controlplane.db.ledger import acquire, reap, release
from agentiam_controlplane.db.models import BudgetRow, LeaseRow
from agentiam_controlplane.errors import LeaseUnavailableError
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


# ---------------------------------------------------------------------------
# ACQUIRE
# ---------------------------------------------------------------------------


async def test_acquire_grants_the_full_request_when_available(migrated_engine: AsyncEngine) -> None:
    mandate_id = uuid.uuid4()
    budget_id = await make_budget(migrated_engine, mandate_id=mandate_id, total=Decimal("100"))
    factory = make_session_factory(migrated_engine)
    async with factory() as s:
        lease = await acquire(
            s,
            mandate_id=mandate_id,
            dimension="spend_bdt",
            requested=Decimal("40"),
            pep_id="pep-1",
            ttl=_TTL,
            now=_NOW,
        )
    assert lease.granted == Decimal("40.0000")
    budget = await _budget(migrated_engine, budget_id)
    assert budget.leased == Decimal("40.0000")


async def test_acquire_grants_only_what_remains_when_request_exceeds_available(
    migrated_engine: AsyncEngine,
) -> None:
    """`grant = min(requested, available)` (spec 04 §4.1) — a partial grant, not a denial."""
    mandate_id = uuid.uuid4()
    await make_budget(migrated_engine, mandate_id=mandate_id, total=Decimal("10"))
    factory = make_session_factory(migrated_engine)
    async with factory() as s:
        lease = await acquire(
            s,
            mandate_id=mandate_id,
            dimension="spend_bdt",
            requested=Decimal("15"),
            pep_id="pep-1",
            ttl=_TTL,
            now=_NOW,
        )
    assert lease.granted == Decimal("10.0000")


async def test_acquire_raises_when_nothing_remains(migrated_engine: AsyncEngine) -> None:
    mandate_id = uuid.uuid4()
    await make_budget(migrated_engine, mandate_id=mandate_id, total=Decimal("5"))
    factory = make_session_factory(migrated_engine)
    async with factory() as s:
        await acquire(
            s,
            mandate_id=mandate_id,
            dimension="spend_bdt",
            requested=Decimal("5"),
            pep_id="pep-1",
            ttl=_TTL,
            now=_NOW,
        )
    async with factory() as s:
        with pytest.raises(LeaseUnavailableError):
            await acquire(
                s,
                mandate_id=mandate_id,
                dimension="spend_bdt",
                requested=Decimal("1"),
                pep_id="pep-2",
                ttl=_TTL,
                now=_NOW,
            )


async def test_50_concurrent_acquires_against_a_budget_that_fits_10(
    postgres_url: str, migrated_engine: AsyncEngine
) -> None:
    """The T-013 acceptance test, verbatim: exactly 10 succeed, 40 get `Insufficient`.

    Uses its own engine with `NullPool` — the default pool (5 + 10 overflow) can't hold
    50 concurrent connections, and this needs 50 truly separate sessions: a single
    `AsyncSession` is not safe for concurrent use, so "50 concurrent acquires" only tests
    anything real if each one is a distinct connection racing on the same budget row.
    """
    mandate_id = uuid.uuid4()
    budget_id = await make_budget(migrated_engine, mandate_id=mandate_id, total=Decimal("10"))

    concurrent_engine = make_engine(postgres_url, poolclass=NullPool)
    factory = make_session_factory(concurrent_engine)

    async def one_acquire(i: int) -> bool:
        async with factory() as s:
            try:
                await acquire(
                    s,
                    mandate_id=mandate_id,
                    dimension="spend_bdt",
                    requested=Decimal("1"),
                    pep_id=f"pep-{i}",
                    ttl=_TTL,
                    now=_NOW,
                )
                return True
            except LeaseUnavailableError:
                return False

    results = await asyncio.gather(*(one_acquire(i) for i in range(50)))
    await concurrent_engine.dispose()

    assert sum(results) == 10
    assert sum(not r for r in results) == 40
    budget = await _budget(migrated_engine, budget_id)
    assert budget.leased == Decimal("10.0000")
    assert budget.committed + budget.leased <= budget.total


# ---------------------------------------------------------------------------
# RELEASE
# ---------------------------------------------------------------------------


async def test_release_decrements_leased_by_outstanding_and_terminates_the_lease(
    migrated_engine: AsyncEngine,
) -> None:
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
            ttl=_TTL,
            now=_NOW,
        )
    lease_id = lease.id

    async with factory() as s:
        performed = await release(s, lease_id=lease_id)
    assert performed is True

    budget = await _budget(migrated_engine, budget_id)
    assert budget.leased == Decimal("0.0000")
    released_lease = await _lease(migrated_engine, lease_id)
    assert released_lease.state == "released"


async def test_release_is_idempotent(migrated_engine: AsyncEngine) -> None:
    """Spec 04 §3: each exit decrements `leased` by `outstanding` exactly once."""
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
            ttl=_TTL,
            now=_NOW,
        )
    lease_id = lease.id

    async with factory() as s:
        assert await release(s, lease_id=lease_id) is True
    async with factory() as s:
        assert await release(s, lease_id=lease_id) is False  # already terminal — no-op

    budget = await _budget(migrated_engine, budget_id)
    assert budget.leased == Decimal("0.0000")  # not decremented a second time


async def test_release_of_unknown_lease_raises(migrated_engine: AsyncEngine) -> None:
    factory = make_session_factory(migrated_engine)
    async with factory() as s:
        with pytest.raises(NoResultFound):
            await release(s, lease_id=uuid.uuid4())


# ---------------------------------------------------------------------------
# REAP
# ---------------------------------------------------------------------------


async def test_reap_reclaims_a_lease_past_the_skew_margin(migrated_engine: AsyncEngine) -> None:
    mandate_id = uuid.uuid4()
    budget_id = await make_budget(migrated_engine, mandate_id=mandate_id, total=Decimal("10"))
    factory = make_session_factory(migrated_engine)
    # ttl chosen so expires_at is 10s before "now" — well past the 5s skew margin.
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
    lease_id = lease.id

    async with factory() as s:
        reclaimed = await reap(s, now=_NOW)
    assert lease_id in reclaimed

    budget = await _budget(migrated_engine, budget_id)
    assert budget.leased == Decimal("0.0000")
    reaped_lease = await _lease(migrated_engine, lease_id)
    assert reaped_lease.state == "expired"


async def test_reap_does_not_reclaim_within_the_skew_margin(migrated_engine: AsyncEngine) -> None:
    """expires_at 3s in the past, margin is 5s — REAP must leave it alone (spec 04 §9)."""
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
            ttl=timedelta(seconds=-3),
            now=_NOW,
        )
    lease_id = lease.id

    async with factory() as s:
        reclaimed = await reap(s, now=_NOW)
    assert lease_id not in reclaimed

    budget = await _budget(migrated_engine, budget_id)
    assert budget.leased == Decimal("4.0000")
    still_active = await _lease(migrated_engine, lease_id)
    assert still_active.state == "active"


async def test_reap_ignores_already_released_leases(migrated_engine: AsyncEngine) -> None:
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
    lease_id = lease.id
    async with factory() as s:
        assert await release(s, lease_id=lease_id) is True

    async with factory() as s:
        reclaimed = await reap(s, now=_NOW)
    assert lease_id not in reclaimed  # released, not active — REAP must not touch it

    budget = await _budget(migrated_engine, budget_id)
    assert budget.leased == Decimal("0.0000")  # unchanged, not double-decremented


async def test_reap_reclaims_only_expired_leases_from_a_mixed_batch(
    migrated_engine: AsyncEngine,
) -> None:
    mandate_id = uuid.uuid4()
    await make_budget(migrated_engine, mandate_id=mandate_id, total=Decimal("10"))
    factory = make_session_factory(migrated_engine)
    async with factory() as s:
        expired_lease = await acquire(
            s,
            mandate_id=mandate_id,
            dimension="spend_bdt",
            requested=Decimal("3"),
            pep_id="pep-1",
            ttl=timedelta(seconds=-10),
            now=_NOW,
        )
    async with factory() as s:
        live_lease = await acquire(
            s,
            mandate_id=mandate_id,
            dimension="spend_bdt",
            requested=Decimal("3"),
            pep_id="pep-2",
            ttl=timedelta(seconds=60),
            now=_NOW,
        )

    async with factory() as s:
        reclaimed = await reap(s, now=_NOW)
    assert reclaimed == [expired_lease.id]

    still_active = await _lease(migrated_engine, live_lease.id)
    assert still_active.state == "active"
