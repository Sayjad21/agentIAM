"""CH-10 — rolling restart under load (T-052, `PLAN.md` §13.2).

*Expected: zero dropped requests; invariant holds.*

Three PEPs lease from one pool behind a round-robin dispatcher, traffic runs continuously,
and each instance is taken out of rotation, shut down gracefully, rebuilt under the same
identity, and put back — one at a time, never two at once.

**What is real and what is not, stated up front.** An instance here is the whole PEP object
graph — gateway, pipeline, Cedar engine, lease pool, emitter, upstream client — torn down
and constructed again, and its lease is genuinely `RELEASE`d and a new one genuinely
`ACQUIRE`d against real Postgres. What it is *not* is a new operating-system process; the
dispatcher is a list, not a load balancer. CH-3 is where a real process dies, and it uses a
real `Popen.kill()` for exactly that reason.

That boundary is the right place for it, because the risk a rolling restart carries lives
in the ledger, not in the socket. Every restart is a `RELEASE` racing an `ACQUIRE` against
the same pool row while two other PEPs keep spending, and the pool invariant is what says
whether the two agreed. A process boundary would add TCP to the picture and change nothing
about that. So the sidecar sweeps four times a second here rather than the usual once, and
the run is only interesting if it catches the churn — which is why the scenario asserts on
the number of sweeps taken *while* an instance was down.

**In-flight requests are not shielded.** The dispatcher stops sending to an instance and
then waits for what it already sent, which is what a real drain does. Skipping the wait
would make "zero dropped" trivially true by never having anything in flight to drop.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select

from agentiam_controlplane.db.base import make_session_factory
from agentiam_controlplane.db.models import BudgetRow, LeaseRow
from agentiam_core.models import LeaseState
from tests.chaos.harness import ChaosRun, chaos_run, drive_until
from tests.chaos.pepstack import PepStack, a_mandate, available, build_stack, make_pool_budget

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine

    from agentiam_core.models import Mandate

pytestmark = pytest.mark.chaos

#: Deliberately far larger than the run can spend. It was 20,000 when settlement was a
#: no-op, which was ample — the pool never moved. Once `LEDGER_COMMIT` was wired the same
#: run really did spend 17,015 of it and started refusing near the end, which made "zero
#: refusals during a restart" a statement about exhaustion rather than about restarts. The
#: fault this scenario injects is the restart; the budget must not be a second variable.
POOL_TOTAL = Decimal("200000.0000")

#: 5,000 rather than 500, for the same isolate-the-variable reason as `POOL_TOTAL`. At 500
#: the low-water mark leaves 25 payments of headroom, and under this load four workers can
#: spend that before a top-up's `ACQUIRE` completes — measured, 14 `LEASE_UNAVAILABLE` in
#: 10,434 requests. Those refusals are the PEP correctly failing closed on top-up lag, which
#: is a property of *fixed* lease sizing (T-015, deferred) and has nothing to do with
#: restarts. Leaving them in would make "zero refusals" a statement about lease tuning.
LEASE_SIZE = Decimal("5000.0000")
PAYMENT = Decimal("5.0000")
INSTANCES = 3

#: Long enough that the sidecar takes several samples with an instance missing, short
#: enough that three of them do not dominate the suite's runtime.
SETTLE_S = 0.75


@dataclass
class Fleet:
    """Three PEPs behind a round-robin dispatcher that can drain one at a time."""

    ledger_url: str
    mandate: Mandate
    stacks: list[PepStack] = field(default_factory=list)
    serving: list[bool] = field(default_factory=list)
    in_flight: list[int] = field(default_factory=list)
    restarts: int = 0
    _next: int = 0

    async def start(self) -> None:
        """Build every instance and take its first lease."""
        for index in range(INSTANCES):
            self.stacks.append(await self._build(index))
            self.serving.append(True)
            self.in_flight.append(0)

    async def _build(self, index: int) -> PepStack:
        return await build_stack(
            ledger_url=self.ledger_url,
            mandate=self.mandate,
            pep_id=f"pep-roll-{index}",
            lease_size=LEASE_SIZE,
        )

    def _pick(self) -> int | None:
        for _ in range(INSTANCES):
            candidate = self._next % INSTANCES
            self._next += 1
            if self.serving[candidate]:
                return candidate
        return None

    async def dispatch(self, _index: int) -> tuple[int, str | None]:
        """Send one payment to the next instance in rotation.

        Raises:
            RuntimeError: Every instance is out of rotation. That would be a bug in the
                restart sequence — it takes one down at a time — and it is raised rather
                than retried so `LoadReport.dropped` counts it, which is the assertion.
        """
        chosen = self._pick()
        if chosen is None:
            raise RuntimeError("no instance in rotation")
        self.in_flight[chosen] += 1
        try:
            return await self.stacks[chosen].pay(PAYMENT)
        finally:
            self.in_flight[chosen] -= 1

    async def restart(self, index: int, run: ChaosRun) -> None:
        """Drain, stop, rebuild and re-arm one instance."""
        run.event(f"draining pep-roll-{index}")
        self.serving[index] = False
        while self.in_flight[index] > 0:
            await asyncio.sleep(0.01)

        run.event(f"stopping pep-roll-{index} (RELEASE)")
        await self.stacks[index].aclose(graceful=True)
        # Down for long enough that the sidecar samples the fleet mid-restart; a restart
        # that completes between two sweeps is a restart the checker never saw.
        await asyncio.sleep(SETTLE_S)

        run.event(f"starting pep-roll-{index} (ACQUIRE)")
        self.stacks[index] = await self._build(index)
        self.serving[index] = True
        self.restarts += 1
        run.event(f"pep-roll-{index} back in rotation")

    async def aclose(self) -> None:
        """Shut every instance down."""
        for stack in self.stacks:
            await stack.aclose(graceful=True)


@pytest.fixture
async def fleet(migrated_engine: AsyncEngine, postgres_url: str) -> AsyncIterator[Fleet]:
    """Three PEPs leasing from one pool."""
    mandate = a_mandate(uuid.uuid4(), total=POOL_TOTAL)
    await make_pool_budget(migrated_engine, mandate_id=mandate.mandate_id, total=POOL_TOTAL)
    built = Fleet(ledger_url=postgres_url, mandate=mandate)
    await built.start()
    yield built
    await built.aclose()


async def _lease_counts(engine: AsyncEngine, mandate_id: uuid.UUID) -> dict[str, int]:
    """How many leases this mandate's pool has, by state.

    Joined explicitly rather than through a relationship: `LeaseRow` carries `budget_id` as
    a plain foreign key and no ORM relationship (T-013), which is also why the invariant
    checker's sweep is hand-written SQL.
    """
    factory = make_session_factory(engine)
    async with factory() as session:
        rows = (
            await session.execute(
                select(LeaseRow.state, func.count())
                .select_from(LeaseRow)
                .join(BudgetRow, BudgetRow.id == LeaseRow.budget_id)
                .where(BudgetRow.mandate_id == mandate_id)
                .group_by(LeaseRow.state)
            )
        ).all()
    return {str(state): int(count) for state, count in rows}


class TestRollingRestart:
    async def test_no_request_is_dropped_and_the_books_stay_straight(
        self, fleet: Fleet, migrated_engine: AsyncEngine
    ) -> None:
        async with chaos_run(
            "CH-10",
            title="Rolling restart under load",
            expected="zero dropped requests; invariant holds",
            engine=migrated_engine,
            # Four sweeps a second: the window this scenario cares about is the moment a
            # RELEASE and an ACQUIRE cross on the same pool row.
            interval_s=0.25,
        ) as run:
            _, leased_before, _ = await available(migrated_engine, fleet.mandate.mandate_id)
            run.measure("leased_with_three_instances", leased_before)
            assert leased_before == LEASE_SIZE * INSTANCES, (
                "all three instances must hold a lease before anything is restarted"
            )

            # Paced rather than flat out, and the reason is a real constraint rather than
            # test shyness. Settlement costs one `LEDGER_COMMIT` per reservation, and every
            # one of them takes `FOR UPDATE` on the *same* pool row, so three instances
            # serialize against each other — measured at roughly a few hundred a second.
            # `LeasePool._release` drains the queue before retiring a lease, so a backlog
            # bigger than a lease's worth of spending stalls the top-up behind it: at
            # `concurrency=4, pace_s=0.005` this run refused 7,053 of 21,006 requests, all
            # `LEASE_UNAVAILABLE`, with the fleet correctly failing closed while it waited.
            # That is a throughput property of settlement (batching is the fix, and it
            # belongs with T-053), not a property of restarts, which is what CH-10 measures.
            stop = asyncio.Event()
            load = asyncio.create_task(
                drive_until(
                    fleet.dispatch,
                    label="continuous load across the restart",
                    stop=stop,
                    concurrency=2,
                    pace_s=0.03,
                )
            )

            await asyncio.sleep(0.5)  # steady state before touching anything
            samples_before_restarts = run.sidecar.samples

            for index in range(INSTANCES):
                await fleet.restart(index, run)

            await asyncio.sleep(0.5)
            stop.set()
            report = await load
            run.load(report)

            run.measure("restarts", fleet.restarts)
            run.measure("requests_sent", report.sent)
            run.measure("requests_dropped", report.dropped)
            run.measure("sweeps_during_restarts", run.sidecar.samples - samples_before_restarts)

            assert fleet.restarts == INSTANCES
            assert report.sent > 50, (
                f"only {report.sent} requests crossed three restarts — too little load for "
                f"'under load' to mean anything"
            )
            assert report.dropped == 0, (
                f"{report.dropped} request(s) got no answer at all during the rolling "
                f"restart: {report.errors}"
            )
            assert report.ok == report.sent, (
                f"the fleet refused {report.sent - report.ok} request(s) mid-restart "
                f"({report.by_reason}) — with two instances always serving and budget to "
                f"spare, a refusal here is the restart losing work, not policy working"
            )
            assert run.sidecar.samples - samples_before_restarts >= INSTANCES, (
                "the checker did not sample often enough to have seen the restarts at all"
            )

            # --- the books after the churn ---------------------------------------------
            # Settlement is asynchronous on purpose (spec 04 §4.4 is off the hot path), so
            # the queues are behind at the moment the load stops. How far behind is worth
            # recording: under this load a queue's drain is doing one database round trip
            # per settlement while four workers keep producing them, and the backlog is the
            # honest cost of keeping `LEDGER_COMMIT` out of the tool-call critical path.
            backlog = sum(stack.settlement.pending for stack in fleet.stacks)
            run.measure("settlement_backlog_when_load_stopped", backlog)
            for stack in fleet.stacks:
                await stack.settlement.drain()
            run.event("settlement queues drained")
            run.measure(
                "settlement_counters",
                {
                    "applied": sum(s.settlement.applied for s in fleet.stacks),
                    "declined": sum(s.settlement.declined for s in fleet.stacks),
                    "dropped": sum(s.settlement.dropped for s in fleet.stacks),
                    "failed_attempts": sum(s.settlement.failed_attempts for s in fleet.stacks),
                    "still_pending": sum(s.settlement.pending for s in fleet.stacks),
                    "shutdown_timeouts": sum(s.shutdown_timeouts for s in fleet.stacks),
                },
            )

            committed, leased_after, pool_available = await available(
                migrated_engine, fleet.mandate.mandate_id
            )
            counts = await _lease_counts(migrated_engine, fleet.mandate.mandate_id)
            spent_locally = PAYMENT * report.ok
            run.measure("leased_after_restarts", leased_after)
            run.measure("committed_after_restarts", committed)
            run.measure("spent_locally", spent_locally)
            run.measure("pool_available_after_restarts", pool_available)
            run.measure("lease_rows_by_state", counts)

            assert counts.get(LeaseState.RELEASED.value, 0) >= INSTANCES, (
                f"a graceful restart must RELEASE its lease; states were {counts}"
            )
            assert leased_after > 0, "the fleet ended holding nothing"

            # --- the money -------------------------------------------------------------
            # This assertion is what CH-10 originally found missing: it read `committed == 0`
            # after 992 requests had spent 4,960, because nothing on the PEP path called
            # LEDGER_COMMIT. `agentiam_pep.settlement` closed that; this is the regression
            # guard, and it is an equality rather than a bound because settlement is flushed
            # before every RELEASE (`PepStack.aclose`), so nothing should still be in flight.
            assert committed == spent_locally, (
                f"{report.ok} requests spent {spent_locally} but the ledger recorded "
                f"committed={committed}. A settlement went missing across the restarts, and "
                f"the difference is budget that RELEASE handed back to the pool after it had "
                f"already been spent (spec 04 §4.4)"
            )
            assert pool_available == POOL_TOTAL - committed - leased_after

            run.note(
                "An instance is the whole PEP object graph rebuilt, with a real RELEASE and "
                "a real ACQUIRE against Postgres, but not a new OS process. CH-3 is the "
                "real-process scenario. The risk a rolling restart carries is in the ledger, "
                "which is fully exercised here."
            )
            run.note(
                "Two throughput observations from tuning this scenario, both unrelated to "
                "restarts and both the PEP correctly failing closed. (1) With the lease at "
                "500 and four workers, the low-water mark leaves 25 payments of headroom "
                "and the load spent it before the top-up's ACQUIRE landed — 14 "
                "LEASE_UNAVAILABLE in 10,434 requests; that is the case T-015 (adaptive "
                "lease sizing, deferred) exists for. (2) Settlement costs one LEDGER_COMMIT "
                "per reservation, each taking FOR UPDATE on the same pool row, so three "
                "instances serialize; since `LeasePool._release` drains the queue before "
                "retiring a lease, a backlog larger than a lease's spending stalls the "
                "top-up behind it — 7,053 refusals in 21,006 requests at concurrency=4, "
                "pace=5 ms. Batching settlements is the fix and belongs with T-053."
            )
            run.note(
                f"{report.ok} requests spent {spent_locally}, and the ledger agrees: "
                f"committed={committed} across {fleet.restarts} restarts. Before T-052 this "
                f"read committed=0 — no production caller reached LEDGER_COMMIT, so every "
                f"RELEASE returned spent budget to the pool. Closed by "
                f"`agentiam_pep.settlement`."
            )

        assert run.sidecar.clean, f"invariant violated: {run.sidecar.violations}"
        assert run.sidecar.samples_unavailable == 0, (
            f"the ledger was unreachable during a rolling restart, which it should never be: "
            f"{run.sidecar.unavailable_reasons}"
        )

    async def test_only_the_unspent_remainder_returns_to_the_pool_on_restart(
        self, fleet: Fleet, migrated_engine: AsyncEngine
    ) -> None:
        """The double-spend CH-10 found, now the regression guard for its fix.

        What this test found first: `Pipeline.settle()` called `LeasePool.commit()`, which
        is `agentiam_pep.lease.commit` — pure, local, in-memory. It produces a
        `CommitOutcome` whose own docstring says it is *"for the caller to enqueue as
        LEDGER_COMMIT"*, and `settle()` discarded it. Grepping the tree for `ledger_commit`
        turned up the function, its unit tests, its race tests, and **no production caller
        at all**.

        So `budgets.committed` never moved, and the consequence landed precisely on a
        restart: `RELEASE` returns `granted - settled` to the pool, `leases.settled` is
        written only by `LEDGER_COMMIT`, and a PEP that had spent most of its lease handed
        the *whole* grant back. The same budget became spendable again. Measured here at
        300 of a 500 lease.

        The invariant checker could not see it, and that is the part worth remembering. It
        compares `committed` against the sum of settled reservations (0 == 0) and `leased`
        against outstanding active leases — both held, because the books were internally
        consistent. They were consistent about a number that had stopped describing
        reality, which is the one class of failure a checker over a single system's own
        records is structurally unable to catch. It took a chaos scenario asking *where did
        the money go* to surface it.

        Closed by `agentiam_pep.settlement` + `agentiam_controlplane.db.settlement_sink`.
        """
        async with chaos_run(
            "CH-10-settlement",
            title="Rolling restart under load — where the spent budget goes",
            expected="a RELEASE returns only the unspent remainder of the lease",
            engine=migrated_engine,
        ) as run:
            _, _, available_before = await available(migrated_engine, fleet.mandate.mandate_id)
            run.measure("pool_available_before", available_before)

            spend = LEASE_SIZE * Decimal("0.6")
            requests = int(spend / PAYMENT)
            for _ in range(requests):
                status, _reason = await fleet.stacks[0].pay(PAYMENT)
                assert status == 200
            spent = PAYMENT * requests
            run.measure("spent_by_instance_0", spent)

            run.event("stopping pep-roll-0 gracefully (settle, then RELEASE)")
            await fleet.stacks[0].aclose(graceful=True)
            fleet.serving[0] = False

            committed, _leased, available_after = await available(
                migrated_engine, fleet.mandate.mandate_id
            )
            returned = available_after - available_before
            run.measure("committed_after_release", committed)
            run.measure("pool_available_after", available_after)
            run.measure("returned_to_pool", returned)

            assert committed == spent, (
                f"instance 0 spent {spent} and the ledger recorded committed={committed}; "
                f"the difference never reached LEDGER_COMMIT (spec 04 §4.4)"
            )
            assert returned == LEASE_SIZE - spent, (
                f"a RELEASE returned {returned} of a {LEASE_SIZE} lease against {spent} "
                f"spent — it must return only the unspent remainder, or that {spent} is "
                f"spendable a second time"
            )
            run.note(
                f"Instance 0 spent {spent} of a {LEASE_SIZE} lease and then released it. The "
                f"pool got back {returned} — the unspent remainder only — and `committed` "
                f"moved to {committed}. Before T-052 the pool got the whole {LEASE_SIZE} "
                f"back and `committed` stayed at 0, so the {spent} was spendable twice."
            )

        assert run.sidecar.clean, f"invariant violated: {run.sidecar.violations}"
