"""Spent budget reaches the ledger — spec 04 §4.4, closing the gap CH-10 found.

The defect these tests were written against: `Pipeline.settle()` settled the reservation
*locally* and discarded the `CommitOutcome`, so `LEDGER_COMMIT` had no production caller at
all. `budgets.committed` never moved, and since `RELEASE` returns `granted - settled` — with
`leases.settled` written only by `LEDGER_COMMIT` — a PEP handed back the whole grant on
shutdown, spent budget included. The same money became spendable twice.

Every test here runs the real `acquire`/`ledger_commit`/`release` against real Postgres
through the real `SettlementQueue`. The one that matters most is
`test_release_returns_only_the_unspent_remainder`, because that is the double-spend itself
rather than a proxy for it.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select

from agentiam_controlplane.db.base import make_session_factory
from agentiam_controlplane.db.invariants import check_invariants
from agentiam_controlplane.db.ledger import acquire, release
from agentiam_controlplane.db.models import BudgetRow, ReconciliationAnomalyRow, ReservationRow
from agentiam_controlplane.db.settlement_sink import LedgerSettlementSink
from agentiam_core.models import BudgetDimension
from agentiam_pep.lease import CommitOutcome
from agentiam_pep.pool import LeaseGrant, LeasePool, PoolSettings
from agentiam_pep.settlement import PendingSettlement, SettlementQueue, SettlementSettings
from tests.integration.conftest import make_budget

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
POOL_TOTAL = Decimal("1000.0000")
LEASE_SIZE = Decimal("400.0000")


async def _pool(engine: AsyncEngine, mandate_id: uuid.UUID) -> tuple[Decimal, Decimal, Decimal]:
    """`(committed, leased, available)` for the mandate's spend pool."""
    factory = make_session_factory(engine)
    async with factory() as session:
        row = (
            await session.execute(
                select(BudgetRow).where(
                    BudgetRow.mandate_id == mandate_id,
                    BudgetRow.dimension == "spend_bdt",
                    BudgetRow.agent_id.is_(None),
                )
            )
        ).scalar_one()
        return row.committed, row.leased, row.total - row.committed - row.leased


async def _take_lease(engine: AsyncEngine, mandate_id: uuid.UUID) -> uuid.UUID:
    factory = make_session_factory(engine)
    async with factory() as session:
        lease = await acquire(
            session,
            mandate_id=mandate_id,
            dimension="spend_bdt",
            requested=LEASE_SIZE,
            pep_id="pep-settlement",
            ttl=timedelta(seconds=600),
            now=NOW,
        )
    assert lease is not None
    return lease.id


def _outcome(lease_id: uuid.UUID, amount: Decimal) -> CommitOutcome:
    return CommitOutcome(
        reservation_id=uuid.uuid4(), lease_id=lease_id, amount=amount, escalated=False
    )


async def _queue(engine: AsyncEngine) -> SettlementQueue:
    return SettlementQueue(
        LedgerSettlementSink(make_session_factory(engine)),
        SettlementSettings(flush_interval_s=0.01),
        now=lambda: NOW,
    )


class TestSettlementReachesTheLedger:
    async def test_committed_moves_when_a_reservation_settles(
        self, migrated_engine: AsyncEngine
    ) -> None:
        mandate_id = uuid.uuid4()
        await make_budget(migrated_engine, mandate_id=mandate_id, total=POOL_TOTAL)
        lease_id = await _take_lease(migrated_engine, mandate_id)

        committed, leased, _ = await _pool(migrated_engine, mandate_id)
        assert (committed, leased) == (Decimal(0), LEASE_SIZE)

        queue = await _queue(migrated_engine)
        queue.enqueue(_outcome(lease_id, Decimal("120.0000")))
        await queue.flush()

        committed, leased, _ = await _pool(migrated_engine, mandate_id)
        assert committed == Decimal("120.0000"), "the ledger never learned what was spent"
        assert leased == LEASE_SIZE - Decimal("120.0000"), (
            "a settlement moves budget from `leased` to `committed`; it does not add to it"
        )
        assert queue.applied == 1
        assert queue.pending == 0

    async def test_release_returns_only_the_unspent_remainder(
        self, migrated_engine: AsyncEngine
    ) -> None:
        """The double-spend, stated directly.

        Spend 300 of a 400 lease, then shut down. Before the fix the pool got all 400 back
        and `committed` stayed at 0, so the 300 was spendable a second time.
        """
        mandate_id = uuid.uuid4()
        await make_budget(migrated_engine, mandate_id=mandate_id, total=POOL_TOTAL)
        lease_id = await _take_lease(migrated_engine, mandate_id)

        queue = await _queue(migrated_engine)
        for _ in range(3):
            queue.enqueue(_outcome(lease_id, Decimal("100.0000")))
        await queue.flush()

        factory = make_session_factory(migrated_engine)
        async with factory() as session:
            assert await release(session, lease_id=lease_id) is True

        committed, leased, available = await _pool(migrated_engine, mandate_id)
        assert committed == Decimal("300.0000")
        assert leased == Decimal(0), "the lease is gone; nothing may still be leased"
        assert available == POOL_TOTAL - Decimal("300.0000"), (
            f"the pool shows {available} available after 300 was spent — a RELEASE must "
            f"return only the unspent remainder, or the same budget is spendable twice"
        )

    async def test_the_invariant_holds_after_settlement(self, migrated_engine: AsyncEngine) -> None:
        """`committed == Σ settled reservations` is now a claim about something.

        Before the fix both sides were zero, so the invariant held vacuously — which is why
        the checker could not see the gap.
        """
        mandate_id = uuid.uuid4()
        await make_budget(migrated_engine, mandate_id=mandate_id, total=POOL_TOTAL)
        lease_id = await _take_lease(migrated_engine, mandate_id)

        queue = await _queue(migrated_engine)
        for amount in ("10.0000", "25.5000", "4.2500"):
            queue.enqueue(_outcome(lease_id, Decimal(amount)))
        await queue.flush()

        report = await check_invariants(migrated_engine)
        assert report.holds, f"{report.summary()}: {[str(v) for v in report.violations]}"

        factory = make_session_factory(migrated_engine)
        async with factory() as session:
            total = (
                await session.execute(select(func.coalesce(func.sum(ReservationRow.amount), 0)))
            ).scalar_one()
        committed, _, _ = await _pool(migrated_engine, mandate_id)
        assert committed == total == Decimal("39.7500")

    async def test_a_replayed_settlement_applies_once(self, migrated_engine: AsyncEngine) -> None:
        """The property that makes unbounded retries safe (G4, spec 04 §10)."""
        mandate_id = uuid.uuid4()
        await make_budget(migrated_engine, mandate_id=mandate_id, total=POOL_TOTAL)
        lease_id = await _take_lease(migrated_engine, mandate_id)

        queue = await _queue(migrated_engine)
        outcome = _outcome(lease_id, Decimal("50.0000"))
        queue.enqueue(outcome)
        queue.enqueue(outcome)
        queue.enqueue(outcome)
        await queue.flush()

        committed, _, _ = await _pool(migrated_engine, mandate_id)
        assert committed == Decimal("50.0000"), "a replayed reservation id double-counted"
        assert queue.applied == 1
        assert queue.declined == 2, "the replays must be declined, not retried forever"

    async def test_a_settlement_against_a_released_lease_is_declined_not_retried(
        self, migrated_engine: AsyncEngine
    ) -> None:
        """G3 / TM-21: the one permanent failure, and it must not wedge the queue.

        The ledger records a reconciliation anomaly and the sink reports "declined" rather
        than raising, because a raise means *retry* and this can never succeed.
        """
        mandate_id = uuid.uuid4()
        await make_budget(migrated_engine, mandate_id=mandate_id, total=POOL_TOTAL)
        lease_id = await _take_lease(migrated_engine, mandate_id)

        factory = make_session_factory(migrated_engine)
        async with factory() as session:
            await release(session, lease_id=lease_id)

        sink = LedgerSettlementSink(make_session_factory(migrated_engine))
        queue = SettlementQueue(sink, SettlementSettings(flush_interval_s=0.01), now=lambda: NOW)
        queue.enqueue(_outcome(lease_id, Decimal("10.0000")))
        await queue.flush()

        assert queue.pending == 0, "a late settlement wedged the queue instead of being declined"
        assert queue.declined == 1
        assert sink.rejected_leases == 1

        async with factory() as session:
            anomalies = (
                await session.execute(select(func.count()).select_from(ReconciliationAnomalyRow))
            ).scalar_one()
        assert anomalies == 1, "the divergence must be surfaced, not swallowed"

    async def test_a_topup_settles_the_old_lease_before_releasing_it(
        self, migrated_engine: AsyncEngine
    ) -> None:
        """The second double-spend route, found after the first was fixed.

        A top-up replaces the held lease and `RELEASE`s the old one. If settlements against
        that old lease are still queued when it is released, two things go wrong at once:
        `RELEASE` hands back `granted - settled` with a stale `settled`, so already-spent
        budget returns to the pool; and the settlements then arrive at a lease that is no
        longer active and are rejected under spec 04 §11.

        Measured before the fix, in CH-10 with asynchronous settlement wired: **6,678 of
        6,992 settlements declined**, and `committed` sat at 3,330 against 51,660 spent.

        `LeasePool._release` now drains the settlement queue before every `RELEASE`, on the
        top-up path as well as on shutdown. This drives a real top-up through a real
        `LeasePool` to check it.
        """
        mandate_id = uuid.uuid4()
        await make_budget(migrated_engine, mandate_id=mandate_id, total=POOL_TOTAL)

        queue = await _queue(migrated_engine)
        factory = make_session_factory(migrated_engine)

        class Client:
            """The `LedgerClient` the pool acquires from — the real ledger operations."""

            async def acquire(
                self,
                *,
                mandate_id: uuid.UUID,
                dimension: BudgetDimension,
                requested: Decimal,
                pep_id: str,
                ttl: timedelta,
                now: datetime,
            ) -> LeaseGrant | None:
                async with factory() as session:
                    lease = await acquire(
                        session,
                        mandate_id=mandate_id,
                        dimension=dimension.value,
                        requested=requested,
                        pep_id=pep_id,
                        ttl=ttl,
                        now=now,
                    )
                if lease is None or lease.granted <= 0:
                    return None
                return LeaseGrant(id=lease.id, granted=lease.granted, expires_at=lease.expires_at)

            async def release(self, *, lease_id: uuid.UUID) -> None:
                async with factory() as session:
                    await release(session, lease_id=lease_id)

        pool = LeasePool(
            Client(),
            PoolSettings(pep_id="pep-topup", lease_size=Decimal("100.0000")),
            mandate_id=mandate_id,
            now=lambda: NOW,
            before_release=queue.drain,
        )
        assert await pool.prime(BudgetDimension.SPEND_BDT)
        first_lease = pool._held[BudgetDimension.SPEND_BDT].lease.id

        # Spend past the low-water mark so `reserve()` schedules a top-up, queueing each
        # settlement against the lease the top-up is about to retire.
        spent = Decimal(0)
        for _ in range(8):
            reservation = pool.reserve(BudgetDimension.SPEND_BDT, Decimal("10.0000"))
            outcome = pool.commit(BudgetDimension.SPEND_BDT, reservation, Decimal("10.0000"))
            queue.enqueue(outcome)
            spent += Decimal("10.0000")

        await pool.drain()  # let the scheduled top-up complete
        second_lease = pool._held[BudgetDimension.SPEND_BDT].lease.id
        assert second_lease != first_lease, "no top-up happened, so nothing was tested"

        assert queue.declined == 0, (
            f"{queue.declined} settlement(s) were rejected because the top-up released "
            f"their lease first (spec 04 §11) — that budget is lost from the books"
        )
        committed, _, available = await _pool(migrated_engine, mandate_id)
        assert committed == spent, (
            f"spent {spent} but the ledger recorded committed={committed}: the top-up's "
            f"RELEASE returned already-spent budget to the pool"
        )
        assert available == POOL_TOTAL - committed - Decimal("100.0000")

    async def test_a_batch_takes_one_transaction_not_one_per_settlement(
        self, migrated_engine: AsyncEngine
    ) -> None:
        """T-053: the whole point of batching, asserted at the seam that proves it.

        `LEDGER_COMMIT` takes `FOR UPDATE` on the lease and then on the *shared* budget row,
        so one settlement per transaction serialises every PEP on that mandate against every
        other. CH-10 measured the cost. Counting sink calls is the honest proxy: one call is
        one session, one transaction, one pair of locks.
        """
        mandate_id = uuid.uuid4()
        await make_budget(migrated_engine, mandate_id=mandate_id, total=POOL_TOTAL)
        lease_id = await _take_lease(migrated_engine, mandate_id)

        real = LedgerSettlementSink(make_session_factory(migrated_engine))
        calls = 0
        sizes: list[int] = []

        class CountingSink:
            async def commit(self, batch: Sequence[PendingSettlement]) -> Sequence[bool]:
                nonlocal calls
                calls += 1
                sizes.append(len(batch))
                return await real.commit(batch)

        queue = SettlementQueue(
            CountingSink(), SettlementSettings(flush_interval_s=0.01), now=lambda: NOW
        )
        for _ in range(20):
            queue.enqueue(_outcome(lease_id, Decimal("5.0000")))
        await queue.drain()

        assert calls == 1, f"20 settlements took {calls} transactions, in sizes {sizes}"
        assert sizes == [20]
        assert queue.applied == 20
        assert (await _pool(migrated_engine, mandate_id))[0] == Decimal("100.0000")

    async def test_a_batch_clamps_cumulatively_and_cannot_overdraw_the_lease(
        self, migrated_engine: AsyncEngine
    ) -> None:
        """G2 across a batch — the guard a single-item test can never exercise.

        Each item is clamped against what is left *after* its predecessors, not against the
        `outstanding` read at the top of the transaction. Clamping every item against the
        opening value would let a batch drive `leased` negative, which is exactly what G2
        exists to prevent, and every existing single-commit test would still pass.
        """
        mandate_id = uuid.uuid4()
        await make_budget(migrated_engine, mandate_id=mandate_id, total=POOL_TOTAL)
        lease_id = await _take_lease(migrated_engine, mandate_id)  # 400 outstanding

        queue = await _queue(migrated_engine)
        # 5 x 100 against a 400 lease: the first four fit, the fifth must clamp to zero.
        for _ in range(5):
            queue.enqueue(_outcome(lease_id, Decimal("100.0000")))
        await queue.drain()

        committed, leased, _ = await _pool(migrated_engine, mandate_id)
        assert committed == LEASE_SIZE, (
            f"committed is {committed}; a batch must not settle more than the lease granted"
        )
        assert leased == Decimal(0), f"leased went to {leased} — G2 did not hold across the batch"
        assert queue.applied == 4
        assert queue.declined == 1, "the over-the-lease item must be declined, not retried"

    async def test_a_top_up_splits_the_batch_at_the_lease_boundary(
        self, migrated_engine: AsyncEngine
    ) -> None:
        """Batches never span two leases, because `ledger_commit_batch` locks exactly one.

        The queue takes the longest *consecutive* run sharing a lease. A top-up swaps the
        held lease mid-stream, and the boundary has to fall exactly there.
        """
        mandate_id = uuid.uuid4()
        await make_budget(migrated_engine, mandate_id=mandate_id, total=POOL_TOTAL)
        first = await _take_lease(migrated_engine, mandate_id)
        second = await _take_lease(migrated_engine, mandate_id)

        seen: list[uuid.UUID] = []
        real = LedgerSettlementSink(make_session_factory(migrated_engine))

        class RecordingSink:
            async def commit(self, batch: Sequence[PendingSettlement]) -> Sequence[bool]:
                leases = {item.lease_id for item in batch}
                assert len(leases) == 1, f"a batch spanned {len(leases)} leases"
                seen.append(batch[0].lease_id)
                return await real.commit(batch)

        queue = SettlementQueue(
            RecordingSink(), SettlementSettings(flush_interval_s=0.01), now=lambda: NOW
        )
        for _ in range(3):
            queue.enqueue(_outcome(first, Decimal("10.0000")))
        for _ in range(2):
            queue.enqueue(_outcome(second, Decimal("10.0000")))
        await queue.drain()

        assert seen == [first, second], f"expected one batch per lease, in order; got {seen}"
        assert queue.applied == 5

    async def test_an_unreachable_ledger_keeps_the_settlement_for_retry(
        self, migrated_engine: AsyncEngine
    ) -> None:
        """A transport failure must never look like a refusal — the audit emitter's bug.

        The first sink attempt raises; the settlement stays queued and the second attempt,
        against the real ledger, applies it.
        """
        mandate_id = uuid.uuid4()
        await make_budget(migrated_engine, mandate_id=mandate_id, total=POOL_TOTAL)
        lease_id = await _take_lease(migrated_engine, mandate_id)

        real = LedgerSettlementSink(make_session_factory(migrated_engine))
        attempts = 0

        class FlakySink:
            async def commit(self, batch: Sequence[PendingSettlement]) -> Sequence[bool]:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise ConnectionError("the ledger is unreachable")
                return await real.commit(batch)

        queue = SettlementQueue(
            FlakySink(), SettlementSettings(flush_interval_s=0.01), now=lambda: NOW
        )
        queue.enqueue(_outcome(lease_id, Decimal("75.0000")))

        await queue.flush()
        assert queue.pending == 1, "a failed settlement must be kept, not dropped"
        assert queue.failed_attempts == 1
        assert (await _pool(migrated_engine, mandate_id))[0] == Decimal(0)

        await queue.flush()
        assert queue.pending == 0
        assert queue.applied == 1
        assert (await _pool(migrated_engine, mandate_id))[0] == Decimal("75.0000")
