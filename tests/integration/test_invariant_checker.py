"""The invariant checker against a real ledger (`agentiam_controlplane.db.invariants`).

`ROADMAP.md` states this ticket's bar plainly: *a checker never tested against a real
violation is decoration*. So the tests here are organised around what it must **catch**,
and each violation is injected the way the acceptance criteria describe — by writing to
the database behind the ledger's back.

The three invariants are not equally protected, and that asymmetry is the point.

* **The pool invariant** `committed + leased <= total` is already a `CHECK` constraint
  (T-012). Measured: a plain `UPDATE` that would break it is refused outright with an
  `IntegrityError`. Injecting it at all takes a `DROP CONSTRAINT`, which is what a
  half-applied migration or a hand-repaired production row looks like.
* **The books invariants** — `committed` against settled reservations, and `leased`
  against active leases' outstanding — have no schema backing at all. Measured: bumping
  `committed` from 40 to 50 while `SUM(reservations.amount)` stays at 40 is accepted
  without complaint, because the `CHECK` only compares three columns of one row.

That second case is the checker's whole reason to exist, and it is exactly the drift
ADR-010 predicts: idempotency protects the books, not the pool.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from agentiam_controlplane.db.base import make_session_factory
from agentiam_controlplane.db.invariants import (
    InvariantKind,
    check_invariants,
)
from agentiam_controlplane.db.ledger import acquire, ledger_commit, release
from agentiam_controlplane.db.models import BudgetRow

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
TTL = timedelta(seconds=60)


async def seed_budget(
    engine: AsyncEngine,
    *,
    total: Decimal = Decimal("1000.0000"),
    dimension: str = "spend_bdt",
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create one budget. Returns `(mandate_id, budget_id)`."""
    mandate_id = uuid.uuid4()
    factory = make_session_factory(engine)
    async with factory() as s, s.begin():
        row = BudgetRow(mandate_id=mandate_id, dimension=dimension, total=total)
        s.add(row)
        await s.flush()
        budget_id = row.id
    return mandate_id, budget_id


async def busy_ledger(engine: AsyncEngine) -> tuple[uuid.UUID, uuid.UUID]:
    """A budget with a realistic history: three leases, one commit, one release."""
    mandate_id, budget_id = await seed_budget(engine)
    factory = make_session_factory(engine)

    lease_ids = []
    for _ in range(3):
        async with factory() as s:
            lease = await acquire(
                s,
                mandate_id=mandate_id,
                dimension="spend_bdt",
                pep_id="pep-1",
                requested=Decimal("100.0000"),
                ttl=TTL,
                now=NOW,
            )
            lease_ids.append(lease.id)

    async with factory() as s:
        await ledger_commit(
            s,
            lease_id=lease_ids[0],
            reservation_id=uuid.uuid4(),
            amount=Decimal("40.0000"),
            now=NOW,
        )
    async with factory() as s:
        await release(s, lease_id=lease_ids[1])

    return mandate_id, budget_id


async def sql(engine: AsyncEngine, statement: str, **params: object) -> None:
    """Write to the ledger behind its back — the injection the acceptance criteria ask for."""
    factory = make_session_factory(engine)
    async with factory() as s, s.begin():
        await s.execute(text(statement), params)


@pytest.fixture
async def unconstrained_budgets(migrated_engine: AsyncEngine) -> AsyncIterator[None]:
    """Drop the pool `CHECK`, and put both it and the data back afterwards.

    Some violations cannot be injected while the constraint stands — measured, a plain
    `UPDATE` breaking the pool invariant is refused outright. Dropping it is the honest way
    to simulate a half-applied migration or a hand-repaired row.

    The repair matters as much as the drop. A test that leaves rows the schema forbids
    makes the *migration* fail on the way down: `0004_budget_split.downgrade` re-creates
    the pre-split `CHECK`, and Postgres rightly refuses to add a constraint some existing
    row violates. That surfaced as five failures and eight teardown errors across this
    module when T-017 landed — none of them in the code under test.
    """
    await sql(migrated_engine, "ALTER TABLE budgets DROP CONSTRAINT ck_budgets_invariant")
    try:
        yield
    finally:
        await sql(
            migrated_engine,
            "UPDATE budgets SET committed = 0, leased = 0, allocated = 0",
        )
        await sql(
            migrated_engine,
            "ALTER TABLE budgets ADD CONSTRAINT ck_budgets_invariant CHECK ("
            "committed >= 0 AND leased >= 0 AND allocated >= 0 "
            "AND committed + leased + allocated <= total)",
        )


class TestCleanLedger:
    async def test_an_empty_ledger_holds(self, migrated_engine: AsyncEngine) -> None:
        report = await check_invariants(migrated_engine)
        assert report.holds
        assert report.violations == ()
        assert report.budgets_checked == 0

    async def test_a_freshly_seeded_budget_holds(self, migrated_engine: AsyncEngine) -> None:
        await seed_budget(migrated_engine)
        report = await check_invariants(migrated_engine)
        assert report.holds
        assert report.budgets_checked == 1

    async def test_a_busy_ledger_holds(self, migrated_engine: AsyncEngine) -> None:
        """Acquire, commit, release — the ledger's own operations never break its books."""
        await busy_ledger(migrated_engine)
        report = await check_invariants(migrated_engine)
        assert report.holds, report.violations

    async def test_a_fully_settled_and_released_budget_holds(
        self, migrated_engine: AsyncEngine
    ) -> None:
        mandate_id, _ = await seed_budget(migrated_engine)
        factory = make_session_factory(migrated_engine)
        async with factory() as s:
            lease = await acquire(
                s,
                mandate_id=mandate_id,
                dimension="spend_bdt",
                pep_id="pep-1",
                requested=Decimal("50.0000"),
                ttl=TTL,
                now=NOW,
            )
        async with factory() as s:
            await ledger_commit(
                s,
                lease_id=lease.id,
                reservation_id=uuid.uuid4(),
                amount=Decimal("50.0000"),
                now=NOW,
            )
        async with factory() as s:
            await release(s, lease_id=lease.id)

        report = await check_invariants(migrated_engine)
        assert report.holds, report.violations


class TestBooksDrift:
    """The violations no `CHECK` constraint can see. This is why the tool exists."""

    async def test_inflated_committed_is_detected(self, migrated_engine: AsyncEngine) -> None:
        _, budget_id = await busy_ledger(migrated_engine)
        await sql(
            migrated_engine,
            "UPDATE budgets SET committed = committed + 10 WHERE id = :i",
            i=budget_id,
        )

        report = await check_invariants(migrated_engine)

        assert not report.holds
        violation = next(
            v for v in report.violations if v.kind is InvariantKind.COMMITTED_VS_RESERVATIONS
        )
        assert violation.budget_id == budget_id
        assert violation.actual == Decimal("50.0000")
        assert violation.expected == Decimal("40.0000")

    async def test_the_injection_is_accepted_by_the_schema(
        self, migrated_engine: AsyncEngine
    ) -> None:
        """Proof the previous test guards something the database cannot.

        If the `CHECK` caught this, the checker would be redundant for it.
        """
        _, injected = await busy_ledger(migrated_engine)
        await sql(
            migrated_engine,
            "UPDATE budgets SET committed = committed + 10 WHERE id = :i",
            i=injected,
        )
        # No IntegrityError. The assertion is the absence of one: if the schema caught
        # this, the checker would be redundant for it and the test above would be proving
        # nothing.
        assert (await check_invariants(migrated_engine)).holds is False

    async def test_deflated_committed_is_detected(self, migrated_engine: AsyncEngine) -> None:
        """Under-counting is as wrong as over-counting, and hides real spend."""
        _, budget_id = await busy_ledger(migrated_engine)
        await sql(
            migrated_engine,
            "UPDATE budgets SET committed = 0 WHERE id = :i",
            i=budget_id,
        )

        report = await check_invariants(migrated_engine)
        assert not report.holds
        kinds = {v.kind for v in report.violations}
        assert InvariantKind.COMMITTED_VS_RESERVATIONS in kinds

    async def test_leased_drift_is_detected(self, migrated_engine: AsyncEngine) -> None:
        """`leased` must equal the outstanding total of *active* leases only."""
        _, budget_id = await busy_ledger(migrated_engine)
        await sql(
            migrated_engine,
            "UPDATE budgets SET leased = leased - 25 WHERE id = :i",
            i=budget_id,
        )

        report = await check_invariants(migrated_engine)
        violation = next(
            v for v in report.violations if v.kind is InvariantKind.LEASED_VS_ACTIVE_LEASES
        )
        assert violation.budget_id == budget_id
        assert violation.expected - violation.actual == Decimal("25.0000")

    async def test_a_lease_retired_without_returning_its_outstanding_is_detected(
        self, migrated_engine: AsyncEngine
    ) -> None:
        """The ADR-009 failure shape: a lease leaves `active` and `leased` is not decremented.

        Closer to a real bug than an arbitrary number: it is what a missed decrement in
        `_retire` would look like the morning after.
        """
        mandate_id, _ = await seed_budget(migrated_engine)
        factory = make_session_factory(migrated_engine)
        async with factory() as s:
            lease = await acquire(
                s,
                mandate_id=mandate_id,
                dimension="spend_bdt",
                pep_id="pep-1",
                requested=Decimal("100.0000"),
                ttl=TTL,
                now=NOW,
            )
        await sql(
            migrated_engine,
            "UPDATE leases SET state = 'released' WHERE id = :i",
            i=lease.id,
        )

        report = await check_invariants(migrated_engine)
        violation = next(
            v for v in report.violations if v.kind is InvariantKind.LEASED_VS_ACTIVE_LEASES
        )
        assert violation.actual == Decimal("100.0000")
        assert violation.expected == Decimal("0.0000")


class TestPoolViolation:
    async def test_the_schema_refuses_a_plain_pool_violation(
        self, migrated_engine: AsyncEngine
    ) -> None:
        """Measured in the T-016 probe, pinned here.

        The pool invariant cannot be injected without removing the constraint first, and
        knowing that is what makes the next test's `DROP CONSTRAINT` honest rather than
        theatrical.
        """
        _, budget_id = await seed_budget(migrated_engine, total=Decimal("100.0000"))
        with pytest.raises(IntegrityError):
            await sql(
                migrated_engine,
                "UPDATE budgets SET committed = total + 1 WHERE id = :i",
                i=budget_id,
            )

    async def test_a_pool_violation_is_detected_once_the_constraint_is_gone(
        self, migrated_engine: AsyncEngine, unconstrained_budgets: None
    ) -> None:
        """A half-applied migration, or a row repaired by hand on a bad night."""
        _, budget_id = await seed_budget(migrated_engine, total=Decimal("100.0000"))
        await sql(
            migrated_engine,
            "UPDATE budgets SET committed = 150 WHERE id = :i",
            i=budget_id,
        )

        report = await check_invariants(migrated_engine)
        violation = next(v for v in report.violations if v.kind is InvariantKind.POOL)
        assert violation.budget_id == budget_id
        assert violation.actual == Decimal("150.0000")
        assert violation.expected == Decimal("100.0000")

    async def test_a_negative_balance_is_detected(
        self, migrated_engine: AsyncEngine, unconstrained_budgets: None
    ) -> None:
        """`leased` going negative is the TM-21 signature (ADR-009)."""
        _, budget_id = await seed_budget(migrated_engine)
        await sql(
            migrated_engine,
            "UPDATE budgets SET leased = -5 WHERE id = :i",
            i=budget_id,
        )

        report = await check_invariants(migrated_engine)
        assert InvariantKind.NEGATIVE_BALANCE in {v.kind for v in report.violations}


class TestReportingQuality:
    async def test_only_the_corrupted_budget_is_named(self, migrated_engine: AsyncEngine) -> None:
        """A checker that reports the whole table on one bad row is unusable on screen."""
        _, corrupted = await busy_ledger(migrated_engine)
        for _ in range(4):
            await busy_ledger(migrated_engine)

        await sql(
            migrated_engine,
            "UPDATE budgets SET committed = committed + 1 WHERE id = :i",
            i=corrupted,
        )

        report = await check_invariants(migrated_engine)
        assert report.budgets_checked == 5
        assert {v.budget_id for v in report.violations} == {corrupted}

    async def test_every_violation_on_one_budget_is_reported(
        self, migrated_engine: AsyncEngine, unconstrained_budgets: None
    ) -> None:
        """Not just the first: a partial diagnosis sends the operator down one hole."""
        _, budget_id = await busy_ledger(migrated_engine)
        await sql(
            migrated_engine,
            "UPDATE budgets SET committed = 9999, leased = 8888 WHERE id = :i",
            i=budget_id,
        )

        report = await check_invariants(migrated_engine)
        kinds = {v.kind for v in report.violations}
        assert InvariantKind.POOL in kinds
        assert InvariantKind.COMMITTED_VS_RESERVATIONS in kinds
        assert InvariantKind.LEASED_VS_ACTIVE_LEASES in kinds

    async def test_a_violation_renders_as_one_readable_line(
        self, migrated_engine: AsyncEngine
    ) -> None:
        """It goes on screen during Beat 4 and into every chaos run's log."""
        mandate_id, budget_id = await busy_ledger(migrated_engine)
        await sql(
            migrated_engine,
            "UPDATE budgets SET committed = committed + 10 WHERE id = :i",
            i=budget_id,
        )

        report = await check_invariants(migrated_engine)
        line = str(report.violations[0])

        assert "\n" not in line
        assert str(mandate_id) in line
        assert "spend_bdt" in line
        assert "40.0000" in line and "50.0000" in line

    async def test_the_report_carries_its_own_duration(self, migrated_engine: AsyncEngine) -> None:
        await busy_ledger(migrated_engine)
        report = await check_invariants(migrated_engine)
        assert report.duration_ms > 0


class TestNoFalsePositives:
    async def test_a_checker_that_cries_wolf_is_as_useless_as_one_that_never_fires(
        self, migrated_engine: AsyncEngine
    ) -> None:
        """Sweep repeatedly while the ledger is actively being mutated.

        The three sums are read in one statement precisely so they share a snapshot. Read
        them in three statements and a concurrent `ACQUIRE` landing between two of them
        produces a violation that is not real — and a checker that reports those gets
        muted, which is the same outcome as one that misses everything.
        """
        mandate_id, _ = await seed_budget(migrated_engine, total=Decimal("100000.0000"))
        factory = make_session_factory(migrated_engine)
        stop = asyncio.Event()

        async def churn() -> None:
            while not stop.is_set():
                async with factory() as s:
                    lease = await acquire(
                        s,
                        mandate_id=mandate_id,
                        dimension="spend_bdt",
                        pep_id="pep-churn",
                        requested=Decimal("10.0000"),
                        ttl=TTL,
                        now=NOW,
                    )
                async with factory() as s:
                    await ledger_commit(
                        s,
                        lease_id=lease.id,
                        reservation_id=uuid.uuid4(),
                        amount=Decimal("3.0000"),
                        now=NOW,
                    )
                async with factory() as s:
                    await release(s, lease_id=lease.id)

        workers = [asyncio.create_task(churn()) for _ in range(4)]
        try:
            false_positives: list[object] = []
            for _ in range(25):
                report = await check_invariants(migrated_engine)
                if not report.holds:
                    false_positives.extend(report.violations)
                await asyncio.sleep(0)
        finally:
            stop.set()
            await asyncio.gather(*workers, return_exceptions=True)

        assert false_positives == [], f"reported {len(false_positives)} phantom violations"

        # And the ledger really is consistent once the churn stops.
        assert (await check_invariants(migrated_engine)).holds


class TestDetectionLatency:
    async def test_a_violation_is_detected_within_one_second(
        self, migrated_engine: AsyncEngine
    ) -> None:
        """`PLAN.md` §9's acceptance bar for T-016, measured rather than assumed."""
        for _ in range(50):
            await busy_ledger(migrated_engine)
        _, budget_id = await busy_ledger(migrated_engine)

        await sql(
            migrated_engine,
            "UPDATE budgets SET committed = committed + 1 WHERE id = :i",
            i=budget_id,
        )

        started = time.perf_counter()
        report = await check_invariants(migrated_engine)
        elapsed = time.perf_counter() - started

        assert not report.holds
        assert elapsed < 1.0, f"one sweep took {elapsed * 1000:.0f} ms over 51 budgets"
