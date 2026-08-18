"""CH-1 — Postgres killed for 30 s (T-052, `PLAN.md` §13.2).

*Expected: PEPs spend leases, then fail closed; recovery is clean; invariant holds.*

The container is genuinely stopped, not a proxy pretending. That matters here in a way it
does not for CH-4: CH-1's second claim is about **recovery**, and a recovery you simulate is
not one you have observed. It is also why `conftest.py` pins the host port — a restarted
container is handed a new random port otherwise, and the PEP's DSN would be dead forever.

Three separate things are being watched, and only the first is the headline:

1. **The hot path is genuinely zero-network.** Every payment served while Postgres is down
   is served out of the lease this PEP already holds. If any part of `reserve()` reached
   the ledger, the success count during the outage would be zero.
2. **The spend is bounded by the lease, and then it fails closed.** Not bounded by the
   pool — by the slice of it this instance was granted before the lights went out.
3. **The audit path.** This is where the scenario earns its keep: see
   `test_the_audit_path_is_the_weak_link` below, which measures a real defect rather than
   asserting a comfortable one.

The clock is frozen (`pepstack.NOW`), so nothing here expires. That is deliberate: with
wall-clock time also running, a refusal thirty seconds in would have two candidate causes
and the scenario would establish neither.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text

from agentiam_controlplane.db.base import make_engine
from agentiam_core.errors import ReasonCode
from tests.chaos.harness import chaos_run, drive_load
from tests.chaos.pepstack import PepStack, a_mandate, available, build_stack, make_pool_budget

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine
    from testcontainers.community.postgres import PostgresContainer

pytestmark = pytest.mark.chaos

#: `PLAN.md` §13.2 says thirty seconds. Kept literal rather than shortened for suite speed —
#: the number in the results table has to be the number that was run.
OUTAGE_S = 30.0

POOL_TOTAL = Decimal("2000.0000")
LEASE_SIZE = Decimal("300.0000")
PAYMENT = Decimal("25.0000")

#: How long to wait for Postgres to accept connections again after `docker start`.
RECOVERY_TIMEOUT_S = 60.0


async def _stop(container: PostgresContainer) -> None:
    await asyncio.to_thread(container.get_wrapped_container().stop, timeout=10)


async def _start(container: PostgresContainer) -> None:
    await asyncio.to_thread(container.get_wrapped_container().start)


async def _wait_ready(url: str, *, timeout: float = RECOVERY_TIMEOUT_S) -> float:
    """Block until `url` answers a trivial query. Returns how long that took."""
    started = time.perf_counter()
    deadline = started + timeout
    while time.perf_counter() < deadline:
        engine = make_engine(url)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return time.perf_counter() - started
        except Exception:
            await asyncio.sleep(0.25)
        finally:
            await engine.dispose()
    raise AssertionError(f"Postgres never came back within {timeout} s")


@pytest.fixture
async def stack(migrated_engine: AsyncEngine, postgres_url: str) -> AsyncIterator[PepStack]:
    """One PEP holding a real lease, with the emitter at its production cadence."""
    mandate = a_mandate(uuid.uuid4(), total=POOL_TOTAL)
    await make_pool_budget(migrated_engine, mandate_id=mandate.mandate_id, total=POOL_TOTAL)
    built = await build_stack(
        ledger_url=postgres_url,
        mandate=mandate,
        pep_id="pep-ch01",
        lease_size=LEASE_SIZE,
        # `EmitterSettings`' own defaults, so what this measures is what production does.
        flush_interval_s=0.5,
        emitter_capacity=1024,
    )
    yield built
    # The ledger may or may not be reachable by now; a failed RELEASE is counted, not raised.
    await built.aclose(graceful=True)


class TestPostgresDown:
    async def test_the_pep_spends_its_lease_then_fails_closed_and_recovers(
        self,
        stack: PepStack,
        migrated_engine: AsyncEngine,
        pg_container: PostgresContainer,
        postgres_url: str,
    ) -> None:
        async with chaos_run(
            "CH-01",
            title="Kill Postgres for 30 s",
            expected=("PEPs spend leases, then fail closed; recovery is clean; invariant holds"),
            engine=migrated_engine,
        ) as run:
            # --- healthy ---------------------------------------------------------------
            healthy = await drive_load(
                lambda _i: stack.pay(PAYMENT), label="healthy", total=4, concurrency=2
            )
            run.load(healthy)
            assert healthy.ok == 4, f"the baseline must be clean: {healthy.as_dict()}"

            committed, leased, _ = await available(migrated_engine, stack.mandate.mandate_id)
            run.measure("leased_before_outage", leased)
            assert leased == LEASE_SIZE, "the ledger must show this PEP holding its lease"

            held_at_cut = stack.remaining()
            run.measure("local_remaining_at_cut", held_at_cut)
            run.measure("committed_before_outage", committed)

            # --- the outage ------------------------------------------------------------
            run.event("stopping postgres")
            await _stop(pg_container)
            run.event("postgres stopped")
            outage_started = time.perf_counter()

            # Spend past the held lease on purpose: the interesting moment is the request
            # *after* the local remainder runs out, with no ledger to top up from.
            attempts = int(held_at_cut / PAYMENT) + 6
            during = await drive_load(
                lambda _i: stack.pay(PAYMENT),
                label="during outage",
                total=attempts,
                concurrency=1,
            )
            run.load(during)

            # Hold the outage open for the full thirty seconds, still serving traffic, so
            # the sidecar records the blind window at its real length.
            while time.perf_counter() - outage_started < OUTAGE_S:
                await stack.read_invoice()
                await asyncio.sleep(0.5)
            run.measure("outage_s", round(time.perf_counter() - outage_started, 2))

            spent_during = PAYMENT * during.ok
            run.measure("spent_during_outage", spent_during)
            run.measure("served_during_outage", during.ok)
            run.measure("refused_during_outage", during.sent - during.ok)

            assert during.ok > 0, (
                "no request was served while Postgres was down — the hot path is not "
                "zero-network, which is the claim this scenario exists to check"
            )
            assert during.dropped == 0, f"the gateway dropped requests: {during.errors}"
            assert spent_during <= held_at_cut, (
                f"spent {spent_during} against a lease of {held_at_cut}: the bound is the "
                f"lease this PEP already held, not the pool"
            )
            assert during.by_reason.get(ReasonCode.LEASE_UNAVAILABLE.value, 0) > 0, (
                f"it never failed closed: {during.by_reason}"
            )

            # --- recovery --------------------------------------------------------------
            run.event("starting postgres")
            await _start(pg_container)
            ready_after = await _wait_ready(postgres_url)
            run.measure("recovery_s", round(ready_after, 2))
            run.event(f"postgres accepting connections after {ready_after:.1f} s")

            # A top-up is scheduled by `reserve()`, never awaited, so the first request
            # after recovery is expected to fail and the one after it to succeed. Draining
            # makes that deterministic instead of a race with the event loop.
            await stack.pay(PAYMENT)
            await stack.pool.drain()

            after = await drive_load(
                lambda _i: stack.pay(PAYMENT), label="after recovery", total=4, concurrency=1
            )
            run.load(after)
            assert after.ok == 4, (
                f"recovery is not clean — the PEP never took a new lease: {after.as_dict()}"
            )

            _, leased_after, _ = await available(migrated_engine, stack.mandate.mandate_id)
            run.measure("leased_after_recovery", leased_after)
            run.measure("acquire_failures", stack.ledger.acquire_failures)
            assert stack.ledger.acquire_failures > 0, (
                "no ACQUIRE failed during a 30 s outage, so the PEP never tried to top up "
                "while the ledger was gone and the scenario proved nothing"
            )

            # --- the books -------------------------------------------------------------
            run.note(
                "The clock is frozen, so no lease expired during the outage. CH-1 is about "
                "an unreachable ledger; expiry is CH-3's subject."
            )

        assert run.sidecar.clean, f"invariant violated: {run.sidecar.violations}"
        assert run.sidecar.samples_unavailable > 0, (
            "the sidecar never lost sight of the database, so Postgres was not actually down"
        )
        assert run.sidecar.samples_held > 0, "no sweep ever completed"

    async def test_the_audit_path_is_the_weak_link(
        self,
        stack: PepStack,
        migrated_engine: AsyncEngine,
        pg_container: PostgresContainer,
        postgres_url: str,
    ) -> None:
        """What a 30 s outage does to the audit chain — measured, not assumed.

        ADR-026 is unambiguous: *a system that cannot record what it authorized should not
        authorize.* The emitter implements that as deny-on-full back-pressure, and the
        reasoning holds only if a failing sink drives the buffer to capacity.

        **When this test was first written, it did not.** `_write_batch_locked` gave a batch
        `max_retries` attempts and then discarded it, because that path was written for a
        *poison* batch — one record the sink will never accept. A stopped database is
        indistinguishable from a poison batch at that layer, so records were dropped roughly
        every `(max_retries + 1) x flush_interval_s`, the queue never filled, back-pressure
        never fired, and the PEP kept authorizing requests it could no longer record.

        Fixed: a transient failure is retried without limit, and only a sink raising
        `SinkRejectedRecord` drops a batch. `capacity` is what bounds an outage now — the
        buffer fills and `DENY` refuses, which is the chain ADR-026 actually describes.

        So this asserts the strong property rather than the weak one: after a thirty-second
        outage, **every record written during it is still in the chain**. The weak property
        — that any loss is at least *counted* — is asserted too, because it is what holds if
        the buffer ever does fill.
        """
        async with chaos_run(
            "CH-01-audit",
            title="Kill Postgres for 30 s — the audit path",
            expected="every record buffered during the outage reaches the chain afterwards",
            engine=migrated_engine,
        ) as run:
            await stack.read_invoice()
            await stack.emitter.flush()

            async with make_engine(postgres_url).connect() as conn:
                before = (await conn.execute(text("SELECT count(*) FROM audit_records"))).scalar()
            run.measure("records_before_outage", before)

            run.event("stopping postgres")
            await _stop(pg_container)

            emitted = 0
            deadline = time.perf_counter() + OUTAGE_S
            while time.perf_counter() < deadline:
                status, _ = await stack.read_invoice()
                emitted += 1 if status == 200 else 0
                await asyncio.sleep(0.2)
            run.measure("allowed_while_unrecordable", emitted)

            run.event("starting postgres")
            await _start(pg_container)
            await _wait_ready(postgres_url)
            await stack.emitter.flush()

            async with make_engine(postgres_url).connect() as conn:
                after = (await conn.execute(text("SELECT count(*) FROM audit_records"))).scalar()

            assert before is not None and after is not None
            landed = after - before
            run.measure("records_landed", landed)
            run.measure("emitter_failed_batches", stack.emitter.failed_batches)
            run.measure("emitter_lost_records", stack.emitter.lost_records)
            run.measure("emitter_dropped", stack.emitter.dropped)

            assert stack.emitter.failed_batches > 0, (
                "no write failed during a 30 s database outage — the emitter was not "
                "actually writing to the stopped database"
            )
            # The strong property, and the reason the fix was worth making.
            assert stack.emitter.lost_records == 0, (
                f"{stack.emitter.lost_records} audit records were discarded during a "
                f"transient outage. A stopped database is not a poison batch: it must be "
                f"retried until it returns, or the buffer must fill and DENY (ADR-026)"
            )
            # The weak one, which is what holds if the buffer ever does fill.
            assert landed + stack.emitter.lost_records >= emitted, (
                f"{emitted} requests were authorized, {landed} records landed and only "
                f"{stack.emitter.lost_records} were counted lost — the difference is "
                f"unaccounted-for audit loss, which is the one outcome ADR-026 forbids"
            )
            run.note(
                f"Every one of the {emitted} records written while Postgres was stopped "
                f"reached the chain after it came back ({landed} landed, 0 lost, "
                f"{stack.emitter.failed_batches} failed write attempts along the way). "
                f"Before the fix this scenario measured the opposite: the emitter bounded "
                f"every failure by max_retries and discarded the batch, so an outage was "
                f"indistinguishable from a poison batch and the PEP kept authorizing "
                f"requests it could no longer record. STATUS.md gap 20, now closed."
            )
