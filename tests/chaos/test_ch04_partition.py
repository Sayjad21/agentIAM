"""CH-4 — the PEP is partitioned from the ledger (T-052, `PLAN.md` §13.2).

*Expected: bounded spend, then fail closed.*

`PLAN.md` names toxiproxy. This uses `tests/chaos/faultproxy.py` instead, for the reasons
recorded there and in ADR-049, and it uses the **black hole** mode rather than the reset
mode on purpose. A partition does not send a FIN. If the ledger merely refused connections,
every doomed call would fail in a millisecond, and the single most valuable thing this
scenario can check — *does anything on the hot path wait for the ledger?* — would be
unobservable, because there would be nothing to wait for.

So the ledger here is infinitely slow rather than absent, and the assertion is that request
latency does not notice. That is the design claim `pool.py` opens with: `reserve()` is
synchronous and touches nothing but memory, and a top-up is *scheduled, never awaited*. A
black hole is what turns that sentence into a measurement.

The invariant sidecar keeps a **direct** engine while the PEP gets the proxied one, so the
checker can still watch the ledger the PEP has lost. CH-1 could not do that — there the
database really was gone — and the contrast is why both scenarios exist.

One thing this scenario found rather than confirmed is in
`test_a_partitioned_pep_cannot_shut_down_gracefully`.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import pytest

from agentiam_core.errors import ReasonCode
from tests.chaos.faultproxy import FaultMode, FaultProxy
from tests.chaos.harness import chaos_run, drive_load
from tests.chaos.pepstack import PepStack, a_mandate, available, build_stack, make_pool_budget

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.chaos

POOL_TOTAL = Decimal("2000.0000")
LEASE_SIZE = Decimal("200.0000")
PAYMENT = Decimal("20.0000")

#: The hot path must stay this fast with an infinitely slow ledger behind it. Generous
#: against an in-process ASGI transport that answers in single-digit milliseconds — the
#: bound is here to catch an *await on the network*, which would cost seconds, not to
#: police jitter.
HOT_PATH_CEILING_MS = 100.0


@pytest.fixture
async def partitioned(
    migrated_engine: AsyncEngine, postgres_url: str
) -> AsyncIterator[tuple[PepStack, FaultProxy]]:
    """A PEP whose ledger connection runs through a proxy this test can cut."""
    parsed = urlparse(postgres_url)
    assert parsed.hostname and parsed.port
    proxy = await FaultProxy(parsed.hostname, parsed.port).start()

    mandate = a_mandate(uuid.uuid4(), total=POOL_TOTAL)
    await make_pool_budget(migrated_engine, mandate_id=mandate.mandate_id, total=POOL_TOTAL)
    stack = await build_stack(
        ledger_url=proxy.rewrite(postgres_url),
        mandate=mandate,
        pep_id="pep-ch04",
        lease_size=LEASE_SIZE,
    )
    try:
        yield stack, proxy
    finally:
        # Heal first, so a pool still waiting on a top-up gets an answer rather than
        # nothing; `PepStack.aclose` bounds every step regardless, because a partitioned
        # pool genuinely cannot drain (see `test_a_partitioned_pep_cannot_shut_down_
        # gracefully`) and a hung teardown discards a result that was already produced.
        proxy.heal()
        await stack.aclose(graceful=True, timeout=10)
        await proxy.aclose()


class TestPartition:
    async def test_spend_is_bounded_by_the_held_lease_then_fails_closed(
        self, partitioned: tuple[PepStack, FaultProxy], migrated_engine: AsyncEngine
    ) -> None:
        stack, proxy = partitioned

        async with chaos_run(
            "CH-04",
            title="Partition PEP <-> ledger",
            expected="bounded spend, then fail closed",
            engine=migrated_engine,
        ) as run:
            healthy = await drive_load(
                lambda _i: stack.pay(PAYMENT), label="healthy", total=4, concurrency=2
            )
            run.load(healthy)
            assert healthy.ok == 4

            committed_before, leased_before, _ = await available(
                migrated_engine, stack.mandate.mandate_id
            )
            held_at_cut = stack.remaining()
            run.measure("leased_at_cut", leased_before)
            run.measure("committed_at_cut", committed_before)
            run.measure("local_remaining_at_cut", held_at_cut)

            # --- the partition ---------------------------------------------------------
            run.event("cutting PEP -> ledger (blackhole)")
            proxy.cut(FaultMode.BLACKHOLE)

            attempts = int(held_at_cut / PAYMENT) + 5
            during = await drive_load(
                lambda _i: stack.pay(PAYMENT),
                label="during partition",
                total=attempts,
                concurrency=1,
            )
            run.load(during)

            spent = PAYMENT * during.ok
            run.measure("served_during_partition", during.ok)
            run.measure("spent_during_partition", spent)
            run.measure("hot_path_p99_ms_partitioned", during.percentile(99))
            run.measure("hot_path_p99_ms_healthy", healthy.percentile(99))

            assert during.ok > 0, "nothing was served during the partition"
            assert during.dropped == 0, f"requests were dropped: {during.errors}"
            assert spent <= held_at_cut, (
                f"spent {spent} against {held_at_cut} held: the bound is this PEP's lease"
            )
            assert during.by_reason.get(ReasonCode.LEASE_UNAVAILABLE.value, 0) > 0, (
                f"it never failed closed: {during.by_reason}"
            )

            # The claim `pool.py` is built around, measured against an infinitely slow ledger.
            assert during.percentile(99) < HOT_PATH_CEILING_MS, (
                f"p99 was {during.percentile(99)} ms while the ledger was black-holed — "
                f"something on the hot path is awaiting the network, which is exactly what "
                f"`reserve()` promises not to do"
            )

            # --- the ledger did not move -----------------------------------------------
            committed_during, leased_during, _ = await available(
                migrated_engine, stack.mandate.mandate_id
            )
            run.measure("committed_during_partition", committed_during)
            assert (committed_during, leased_during) == (committed_before, leased_before), (
                "the ledger moved while the PEP could not reach it — a partitioned PEP must "
                "not be able to change the books"
            )

            held = await run.sidecar.sweep()
            run.measure("invariant_held_during_partition", held)
            assert held, "the invariant must hold while a PEP is partitioned"

            # --- heal ------------------------------------------------------------------
            run.event("healing the partition")
            proxy.heal()
            # `heal()` drops the connections the black hole accepted, so the top-up that
            # hung during the partition fails rather than waiting forever — which is what
            # clears `LeasePool`'s single-flight flag and lets the next one be scheduled.
            # Every drain here is bounded: an unbounded one turned the first run of this
            # scenario into a hang instead of a failure.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stack.pool.drain(), timeout=30)
            await stack.pay(PAYMENT)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stack.pool.drain(), timeout=30)

            after = await drive_load(
                lambda _i: stack.pay(PAYMENT), label="after heal", total=4, concurrency=1
            )
            run.load(after)
            run.measure("blackholed_connections", proxy.connections_blackholed)
            assert proxy.connections_blackholed > 0, (
                "no connection was ever black-holed, so the partition was not exercised — "
                "this is the failure mode the proxy's first draft actually had"
            )
            assert after.ok == 4, f"the PEP never recovered its lease: {after.as_dict()}"

        assert run.sidecar.clean, f"invariant violated: {run.sidecar.violations}"

    async def test_a_partitioned_pep_cannot_shut_down_gracefully(
        self, partitioned: tuple[PepStack, FaultProxy], migrated_engine: AsyncEngine
    ) -> None:
        """A finding, not a confirmation — and it turned out to be worse than expected.

        `LeasePool.aclose()` drains in-flight top-ups and then `RELEASE`s every held lease.
        Both halves need the ledger, so a PEP asked to stop while partitioned does not stop.

        That much was the expected finding. What the first run of this test added is that
        **the timeout meant to bound it does not bound it.** `asyncio.wait_for` cancels the
        coroutine; the cancellation arrives inside SQLAlchemy's greenlet bridge while
        asyncpg is blocked writing to a black-holed socket; and the driver's own cleanup
        path — rollback, then close — needs that same socket. A five-second `wait_for`
        around `aclose()` was still stuck when the run was killed five minutes later. So
        this test starts the shutdown as a task and *waits* on it without cancelling: the
        only thing that ends it is the partition healing.

        The money is safe either way. The lease expires and `REAP` reclaims it, which is
        CH-3's bound and precisely what the protocol is designed to survive. What is lost
        is availability on restart: an orchestrator's `SIGTERM` grace period elapses, the
        PEP is killed anyway, and a graceful shutdown becomes CH-3.

        Pinned here so a future change to `aclose()` shows up in this file rather than in a
        deployment.
        """
        stack, proxy = partitioned

        async with chaos_run(
            "CH-04-shutdown",
            title="Partition PEP <-> ledger — graceful shutdown",
            expected="a partitioned PEP cannot complete `LeasePool.aclose()`",
            engine=migrated_engine,
        ) as run:
            await stack.pay(PAYMENT)

            # Drive the lease below the low-water mark so a top-up is actually scheduled;
            # without one in flight, `drain()` has nothing to wait on and closes cleanly.
            below_water = int(LEASE_SIZE * Decimal("0.8") / PAYMENT)
            await drive_load(
                lambda _i: stack.pay(PAYMENT),
                label="drive below low-water",
                total=below_water,
                concurrency=1,
            )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stack.pool.drain(), timeout=30)

            run.event("cutting, then asking the pool to close")
            proxy.cut(FaultMode.BLACKHOLE)
            # One more reserve past the low-water mark schedules the top-up that will hang.
            await stack.pay(PAYMENT)

            # Started, then waited on *without* cancelling — see the docstring. Cancelling
            # is what does not work, and the point of the test is that nothing the caller
            # does short of restoring the network ends this.
            shutdown = asyncio.ensure_future(stack.pool.aclose())
            done, _pending = await asyncio.wait({shutdown}, timeout=5.0)
            closed = bool(done)
            run.measure("graceful_close_completed_within_5s", closed)
            assert not closed, (
                "the pool closed cleanly while partitioned — `aclose()` no longer needs the "
                "ledger to shut down. Good news: rewrite this test and close STATUS.md gap 21"
            )

            run.event("healing; the pending shutdown should now complete")
            proxy.heal()
            released = bool((await asyncio.wait({shutdown}, timeout=30.0))[0])
            run.measure("graceful_close_completed_after_heal", released)
            run.note(
                "A partitioned PEP cannot complete a graceful shutdown: `aclose()` drains "
                "in-flight top-ups and then RELEASEs, and both need the ledger. Worse, the "
                "timeout meant to bound it does not: `asyncio.wait_for` cancels into "
                "SQLAlchemy's greenlet bridge while asyncpg is blocked on the dead socket, "
                "and the driver's own rollback/close needs that same socket — measured stuck "
                "for 5 minutes against a 5 s bound. Healing the partition is what releases "
                "it. The stranded lease is bounded by TTL + S (CH-3), so this costs "
                "availability on restart, not correctness. See STATUS.md gap 21."
            )

        assert run.sidecar.clean, f"invariant violated: {run.sidecar.violations}"
