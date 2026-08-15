"""INV-5, both mitigations, against a real ledger — T-017.

INV-5 is the one invariant the token format deliberately does not carry, and the reason
the ledger exists. A parent may hand the same ৳50,000 ceiling to three children. Every
token is individually valid and correctly authorized; together they could spend ৳150,000.
No amount of static token inspection prevents that, because nothing about any single token
is wrong.

Two mitigations, both here:

* **Shared pool** (the default) — siblings draw from one budget row. Enforced dynamically
  by `SELECT ... FOR UPDATE` inside `ACQUIRE`.
* **Proportional split** — the parent carves its pool into per-child rows at mint time.
  Enforced statically, by arithmetic done once under a lock.

**On the acceptance test's shape.** `PLAN.md` §9 asks for three siblings holding the full
parent ceiling, spending concurrently, with total spend never exceeding the mandate — and
adds *run this with 3 PEP instances, not 1*. So each sibling here gets its own engine, its
own session factory and its own `pep_id`; a single shared session would serialize in the
client and prove nothing about the database.

Spec 04 §13 and §15 both record the outcome as "granted 100 / 50 / 0". Measured, the order
is whatever the transactions serialize in — a probe produced `50 / 100 / 0` on the first
run. Asserting the literal sequence would be asserting the scheduler. What is actually
guaranteed, and what these tests assert, is that the grants *sum* to exactly the pool and
never exceed it.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import NullPool

from agentiam_controlplane.db.base import make_engine, make_session_factory
from agentiam_controlplane.db.invariants import InvariantKind, check_invariants
from agentiam_controlplane.db.ledger import acquire, ledger_commit, split_budget
from agentiam_controlplane.db.models import BudgetRow
from agentiam_controlplane.errors import (
    AllocationError,
    LeaseUnavailableError,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
TTL = timedelta(seconds=60)
DIMENSION = "spend_bdt"


async def a_pool(
    engine: AsyncEngine, *, total: Decimal = Decimal("150.0000")
) -> tuple[uuid.UUID, uuid.UUID]:
    """One mandate-level pool row. Returns `(mandate_id, budget_id)`."""
    mandate_id = uuid.uuid4()
    async with make_session_factory(engine)() as s, s.begin():
        row = BudgetRow(mandate_id=mandate_id, dimension=DIMENSION, total=total)
        s.add(row)
        await s.flush()
        budget_id = row.id
    return mandate_id, budget_id


async def read(engine: AsyncEngine, budget_id: uuid.UUID) -> BudgetRow:
    async with make_session_factory(engine)() as s:
        return (
            await s.execute(
                text("SELECT total, committed, leased, allocated FROM budgets WHERE id = :i"),
                {"i": budget_id},
            )
        ).one()  # type: ignore[return-value]


class TestSharedPool:
    """The default. Three siblings, one row, the database arbitrates."""

    async def test_three_pep_instances_cannot_exceed_the_mandate(
        self, postgres_url: str, migrated_engine: AsyncEngine
    ) -> None:
        """The T-017 acceptance test.

        Three separate PEP instances — separate engines, not merely separate tasks — each
        holding a token good for the *full* ৳150 ceiling, each asking for ৳100 at once.
        """
        mandate_id, budget_id = await a_pool(migrated_engine, total=Decimal("150.0000"))

        async def pep(name: str) -> Decimal:
            engine = make_engine(postgres_url, poolclass=NullPool)
            try:
                async with make_session_factory(engine)() as s:
                    lease = await acquire(
                        s,
                        mandate_id=mandate_id,
                        dimension=DIMENSION,
                        requested=Decimal("100.0000"),
                        pep_id=name,
                        ttl=TTL,
                        now=NOW,
                    )
                    return lease.granted
            except LeaseUnavailableError:
                return Decimal("0.0000")
            finally:
                await engine.dispose()

        grants = await asyncio.gather(*(pep(f"pep-{i}") for i in range(3)))

        assert sum(grants) == Decimal("150.0000"), f"grants were {grants}"
        assert all(g <= Decimal("100.0000") for g in grants)
        assert max(grants) == Decimal("100.0000"), "the pool is not split evenly out of fairness"

        pool = await read(migrated_engine, budget_id)
        assert pool.leased == Decimal("150.0000")
        assert pool.committed + pool.leased + pool.allocated <= pool.total

    async def test_the_invariant_checker_agrees(
        self, postgres_url: str, migrated_engine: AsyncEngine
    ) -> None:
        """The tool that runs on screen during Beat 4, over the scenario Beat 4 shows."""
        mandate_id, _ = await a_pool(migrated_engine, total=Decimal("150.0000"))

        async def spend(name: str) -> None:
            engine = make_engine(postgres_url, poolclass=NullPool)
            try:
                async with make_session_factory(engine)() as s:
                    lease = await acquire(
                        s,
                        mandate_id=mandate_id,
                        dimension=DIMENSION,
                        requested=Decimal("100.0000"),
                        pep_id=name,
                        ttl=TTL,
                        now=NOW,
                    )
                async with make_session_factory(engine)() as s:
                    await ledger_commit(
                        s,
                        lease_id=lease.id,
                        reservation_id=uuid.uuid4(),
                        amount=lease.granted,
                        now=NOW,
                    )
            except LeaseUnavailableError:
                pass
            finally:
                await engine.dispose()

        await asyncio.gather(*(spend(f"pep-{i}") for i in range(3)))

        report = await check_invariants(migrated_engine)
        assert report.holds, report.violations

    async def test_total_committed_never_exceeds_the_mandate(
        self, postgres_url: str, migrated_engine: AsyncEngine
    ) -> None:
        """INV-5 stated as spend rather than as leases: `Σ committed ≤ total`."""
        mandate_id, budget_id = await a_pool(migrated_engine, total=Decimal("150.0000"))

        async def spend_everything(name: str) -> None:
            engine = make_engine(postgres_url, poolclass=NullPool)
            try:
                for _ in range(5):
                    try:
                        async with make_session_factory(engine)() as s:
                            lease = await acquire(
                                s,
                                mandate_id=mandate_id,
                                dimension=DIMENSION,
                                requested=Decimal("50.0000"),
                                pep_id=name,
                                ttl=TTL,
                                now=NOW,
                            )
                    except LeaseUnavailableError:
                        return
                    async with make_session_factory(engine)() as s:
                        await ledger_commit(
                            s,
                            lease_id=lease.id,
                            reservation_id=uuid.uuid4(),
                            amount=lease.granted,
                            now=NOW,
                        )
            finally:
                await engine.dispose()

        await asyncio.gather(*(spend_everything(f"pep-{i}") for i in range(3)))

        pool = await read(migrated_engine, budget_id)
        assert pool.committed <= pool.total
        assert pool.committed == Decimal("150.0000"), "and the pool is fully spent, not stuck"


class TestProportionalSplit:
    """The parent divides the pool explicitly. Arithmetic, done once, under a lock."""

    async def test_a_split_creates_one_row_per_child(self, migrated_engine: AsyncEngine) -> None:
        _, budget_id = await a_pool(migrated_engine, total=Decimal("150.0000"))

        async with make_session_factory(migrated_engine)() as s:
            children = await split_budget(
                s,
                parent_budget_id=budget_id,
                dimension=DIMENSION,
                allocations={
                    "agent:a": Decimal("60.0000"),
                    "agent:b": Decimal("50.0000"),
                    "agent:c": Decimal("40.0000"),
                },
            )

        assert set(children) == {"agent:a", "agent:b", "agent:c"}
        assert (await read(migrated_engine, children["agent:a"])).total == Decimal("60.0000")

    async def test_the_parent_records_what_it_gave_away(self, migrated_engine: AsyncEngine) -> None:
        """Allocated budget is spoken for; the parent may not lease it out again."""
        _, budget_id = await a_pool(migrated_engine, total=Decimal("150.0000"))

        async with make_session_factory(migrated_engine)() as s:
            await split_budget(
                s,
                parent_budget_id=budget_id,
                dimension=DIMENSION,
                allocations={"agent:a": Decimal("100.0000")},
            )

        parent = await read(migrated_engine, budget_id)
        assert parent.allocated == Decimal("100.0000")
        assert parent.total == Decimal("150.0000"), "the parent's ceiling is unchanged"

    async def test_a_split_larger_than_the_pool_is_refused(
        self, migrated_engine: AsyncEngine
    ) -> None:
        """The static half of INV-5: `Σ children ≤ parent`, checked before any row exists."""
        _, budget_id = await a_pool(migrated_engine, total=Decimal("150.0000"))

        with pytest.raises(AllocationError, match="150"):
            async with make_session_factory(migrated_engine)() as s:
                await split_budget(
                    s,
                    parent_budget_id=budget_id,
                    dimension=DIMENSION,
                    allocations={
                        "agent:a": Decimal("100.0000"),
                        "agent:b": Decimal("100.0000"),
                    },
                )

    async def test_nothing_is_created_when_a_split_is_refused(
        self, migrated_engine: AsyncEngine
    ) -> None:
        """All or nothing: a partly-applied split leaves the parent's books wrong."""
        _, budget_id = await a_pool(migrated_engine, total=Decimal("150.0000"))

        with pytest.raises(AllocationError):
            async with make_session_factory(migrated_engine)() as s:
                await split_budget(
                    s,
                    parent_budget_id=budget_id,
                    dimension=DIMENSION,
                    allocations={
                        "agent:a": Decimal("100.0000"),
                        "agent:b": Decimal("100.0000"),
                    },
                )

        parent = await read(migrated_engine, budget_id)
        assert parent.allocated == Decimal("0.0000")
        async with make_session_factory(migrated_engine)() as s:
            count = (
                await s.execute(
                    text("SELECT count(*) FROM budgets WHERE parent_budget_id = :i"),
                    {"i": budget_id},
                )
            ).scalar_one()
        assert count == 0

    async def test_a_split_cannot_reach_past_what_is_already_leased(
        self, migrated_engine: AsyncEngine
    ) -> None:
        """Available means `total - committed - leased - allocated`, not `total`."""
        mandate_id, budget_id = await a_pool(migrated_engine, total=Decimal("150.0000"))
        async with make_session_factory(migrated_engine)() as s:
            await acquire(
                s,
                mandate_id=mandate_id,
                dimension=DIMENSION,
                requested=Decimal("100.0000"),
                pep_id="pep-1",
                ttl=TTL,
                now=NOW,
            )

        with pytest.raises(AllocationError):
            async with make_session_factory(migrated_engine)() as s:
                await split_budget(
                    s,
                    parent_budget_id=budget_id,
                    dimension=DIMENSION,
                    allocations={"agent:a": Decimal("100.0000")},
                )

    async def test_splitting_twice_for_the_same_agent_is_refused(
        self, migrated_engine: AsyncEngine
    ) -> None:
        """A second allocation to one child is a bug, not a top-up."""
        _, budget_id = await a_pool(migrated_engine, total=Decimal("150.0000"))
        async with make_session_factory(migrated_engine)() as s:
            await split_budget(
                s,
                parent_budget_id=budget_id,
                dimension=DIMENSION,
                allocations={"agent:a": Decimal("50.0000")},
            )

        with pytest.raises(IntegrityError):
            async with make_session_factory(migrated_engine)() as s:
                await split_budget(
                    s,
                    parent_budget_id=budget_id,
                    dimension=DIMENSION,
                    allocations={"agent:a": Decimal("10.0000")},
                )

    async def test_an_empty_split_is_a_programming_error(
        self, migrated_engine: AsyncEngine
    ) -> None:
        _, budget_id = await a_pool(migrated_engine)
        with pytest.raises(ValueError, match="allocation"):
            async with make_session_factory(migrated_engine)() as s:
                await split_budget(
                    s, parent_budget_id=budget_id, dimension=DIMENSION, allocations={}
                )

    async def test_a_non_positive_allocation_is_refused(self, migrated_engine: AsyncEngine) -> None:
        """A zero-budget child is expressed by a `BudgetCeiling` caveat, not a ledger row."""
        _, budget_id = await a_pool(migrated_engine)
        with pytest.raises(ValueError, match="positive"):
            async with make_session_factory(migrated_engine)() as s:
                await split_budget(
                    s,
                    parent_budget_id=budget_id,
                    dimension=DIMENSION,
                    allocations={"agent:a": Decimal("0")},
                )


class TestSpendingAgainstAnAllocation:
    async def test_a_child_draws_from_its_own_row(self, migrated_engine: AsyncEngine) -> None:
        mandate_id, budget_id = await a_pool(migrated_engine, total=Decimal("150.0000"))
        async with make_session_factory(migrated_engine)() as s:
            children = await split_budget(
                s,
                parent_budget_id=budget_id,
                dimension=DIMENSION,
                allocations={"agent:a": Decimal("50.0000"), "agent:b": Decimal("50.0000")},
            )

        async with make_session_factory(migrated_engine)() as s:
            lease = await acquire(
                s,
                mandate_id=mandate_id,
                dimension=DIMENSION,
                requested=Decimal("50.0000"),
                pep_id="pep-1",
                agent_id="agent:a",
                ttl=TTL,
                now=NOW,
            )

        assert lease.granted == Decimal("50.0000")
        assert (await read(migrated_engine, children["agent:a"])).leased == Decimal("50.0000")
        assert (await read(migrated_engine, children["agent:b"])).leased == Decimal("0.0000")

    async def test_a_child_cannot_spend_past_its_allocation(
        self, migrated_engine: AsyncEngine
    ) -> None:
        """The static guarantee: a ৳50 child cannot reach the parent's remaining ৳100."""
        mandate_id, budget_id = await a_pool(migrated_engine, total=Decimal("150.0000"))
        async with make_session_factory(migrated_engine)() as s:
            await split_budget(
                s,
                parent_budget_id=budget_id,
                dimension=DIMENSION,
                allocations={"agent:a": Decimal("50.0000")},
            )

        async with make_session_factory(migrated_engine)() as s:
            lease = await acquire(
                s,
                mandate_id=mandate_id,
                dimension=DIMENSION,
                requested=Decimal("500.0000"),
                pep_id="pep-1",
                agent_id="agent:a",
                ttl=TTL,
                now=NOW,
            )
        assert lease.granted == Decimal("50.0000"), "clamped to the allocation, not the pool"

    async def test_the_pool_row_is_still_reachable_without_an_agent_id(
        self, migrated_engine: AsyncEngine
    ) -> None:
        """`acquire` used to `scalar_one()` on `(mandate_id, dimension)`.

        A split adds rows sharing that pair, so the unqualified lookup would raise
        `MultipleResultsFound` — every T-013 and T-014 call site depends on it not doing
        so.
        """
        mandate_id, budget_id = await a_pool(migrated_engine, total=Decimal("150.0000"))
        async with make_session_factory(migrated_engine)() as s:
            await split_budget(
                s,
                parent_budget_id=budget_id,
                dimension=DIMENSION,
                allocations={"agent:a": Decimal("50.0000")},
            )

        async with make_session_factory(migrated_engine)() as s:
            lease = await acquire(
                s,
                mandate_id=mandate_id,
                dimension=DIMENSION,
                requested=Decimal("500.0000"),
                pep_id="pep-1",
                ttl=TTL,
                now=NOW,
            )
        assert lease.granted == Decimal("100.0000"), "total 150 minus 50 allocated away"

    async def test_siblings_with_split_budgets_cannot_exceed_the_parent(
        self, postgres_url: str, migrated_engine: AsyncEngine
    ) -> None:
        """INV-5 under the static mitigation, spent concurrently by three PEP instances."""
        mandate_id, budget_id = await a_pool(migrated_engine, total=Decimal("150.0000"))
        async with make_session_factory(migrated_engine)() as s:
            await split_budget(
                s,
                parent_budget_id=budget_id,
                dimension=DIMENSION,
                allocations={
                    "agent:a": Decimal("50.0000"),
                    "agent:b": Decimal("50.0000"),
                    "agent:c": Decimal("50.0000"),
                },
            )

        async def child_spends(agent: str, index: int) -> Decimal:
            engine = make_engine(postgres_url, poolclass=NullPool)
            spent = Decimal("0.0000")
            try:
                for _ in range(4):
                    try:
                        async with make_session_factory(engine)() as s:
                            lease = await acquire(
                                s,
                                mandate_id=mandate_id,
                                dimension=DIMENSION,
                                requested=Decimal("40.0000"),
                                pep_id=f"pep-{index}",
                                agent_id=agent,
                                ttl=TTL,
                                now=NOW,
                            )
                    except LeaseUnavailableError:
                        break
                    async with make_session_factory(engine)() as s:
                        await ledger_commit(
                            s,
                            lease_id=lease.id,
                            reservation_id=uuid.uuid4(),
                            amount=lease.granted,
                            now=NOW,
                        )
                    spent += lease.granted
            finally:
                await engine.dispose()
            return spent

        spends = await asyncio.gather(
            *(child_spends(a, i) for i, a in enumerate(("agent:a", "agent:b", "agent:c")))
        )

        assert sum(spends) == Decimal("150.0000")
        assert all(s <= Decimal("50.0000") for s in spends), f"a child overspent: {spends}"
        assert (await check_invariants(migrated_engine)).holds


class TestTheCheckerSeesAllocations:
    async def test_a_parent_whose_allocated_disagrees_with_its_children_is_caught(
        self, migrated_engine: AsyncEngine
    ) -> None:
        """The books invariant for the new column — no `CHECK` can express it."""
        _, budget_id = await a_pool(migrated_engine, total=Decimal("150.0000"))
        async with make_session_factory(migrated_engine)() as s:
            await split_budget(
                s,
                parent_budget_id=budget_id,
                dimension=DIMENSION,
                allocations={"agent:a": Decimal("50.0000")},
            )

        async with make_session_factory(migrated_engine)() as s, s.begin():
            await s.execute(
                text("UPDATE budgets SET allocated = allocated + 10 WHERE id = :i"),
                {"i": budget_id},
            )

        report = await check_invariants(migrated_engine)
        violation = next(
            v for v in report.violations if v.kind is InvariantKind.ALLOCATED_VS_CHILDREN
        )
        assert violation.expected == Decimal("50.0000")
        assert violation.actual == Decimal("60.0000")

    async def test_a_clean_split_holds(self, migrated_engine: AsyncEngine) -> None:
        _, budget_id = await a_pool(migrated_engine, total=Decimal("150.0000"))
        async with make_session_factory(migrated_engine)() as s:
            await split_budget(
                s,
                parent_budget_id=budget_id,
                dimension=DIMENSION,
                allocations={"agent:a": Decimal("50.0000"), "agent:b": Decimal("25.0000")},
            )

        report = await check_invariants(migrated_engine)
        assert report.holds, report.violations
        assert report.budgets_checked == 3, "the parent and both children are all checked"

    async def test_allocating_past_the_ceiling_is_refused_by_the_schema_too(
        self, migrated_engine: AsyncEngine
    ) -> None:
        """Belt and braces: `split_budget` checks it, and so does the `CHECK`."""
        _, budget_id = await a_pool(migrated_engine, total=Decimal("150.0000"))
        with pytest.raises(IntegrityError):
            async with make_session_factory(migrated_engine)() as s, s.begin():
                await s.execute(
                    text("UPDATE budgets SET allocated = 200 WHERE id = :i"),
                    {"i": budget_id},
                )


class TestMigrationDowngrade:
    async def test_a_spent_split_can_still_be_downgraded(
        self, migrated_engine: AsyncEngine
    ) -> None:
        """0004's downgrade has to clear dependents in foreign-key order.

        The obvious `DELETE FROM budgets WHERE parent_budget_id IS NOT NULL` fails on
        `leases_budget_id_fkey` as soon as a split has been spent against — measured, as
        a teardown error the first time these tests ran. This builds exactly that state so
        the ordering stays load-bearing rather than incidental.

        The fixture downgrades to `base` at teardown, so the assertion is that teardown
        itself does not raise.
        """
        mandate_id, budget_id = await a_pool(migrated_engine, total=Decimal("150.0000"))
        async with make_session_factory(migrated_engine)() as s:
            await split_budget(
                s,
                parent_budget_id=budget_id,
                dimension=DIMENSION,
                allocations={"agent:a": Decimal("50.0000")},
            )
        async with make_session_factory(migrated_engine)() as s:
            lease = await acquire(
                s,
                mandate_id=mandate_id,
                dimension=DIMENSION,
                requested=Decimal("30.0000"),
                pep_id="pep-1",
                agent_id="agent:a",
                ttl=TTL,
                now=NOW,
            )
        async with make_session_factory(migrated_engine)() as s:
            await ledger_commit(
                s,
                lease_id=lease.id,
                reservation_id=uuid.uuid4(),
                amount=Decimal("30.0000"),
                now=NOW,
            )

        # A child budget with a lease and a settled reservation hanging off it — the exact
        # shape the naive downgrade could not delete.
        async with make_session_factory(migrated_engine)() as s:
            hanging = (
                await s.execute(
                    text(
                        "SELECT count(*) FROM reservations r JOIN leases l ON l.id = r.lease_id"
                        " JOIN budgets b ON b.id = l.budget_id WHERE b.parent_budget_id IS NOT NULL"
                    )
                )
            ).scalar_one()
        assert hanging == 1
