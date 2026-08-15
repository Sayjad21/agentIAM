"""The PEP-local lease pool — spec 04 §4.1/§4.2/§4.5, T-021.

The pool is what makes NFR-1 possible: `reserve()` is synchronous and touches nothing but
memory, so a tool call never waits on the ledger. Everything expensive — acquiring a lease,
topping it up, releasing it — happens off the hot path.

Three of the four acceptance criteria live here. The fourth (a crash strands budget for at
most the TTL) needs a real process and a real ledger, so it is
`tests/integration/test_lease_pool_crash.py`.

Note how the zero-network test patches `socket` **inside** a running loop rather than around
`asyncio.run`. Measured on Windows: `ProactorEventLoop.__init__` calls `socket.socketpair()`
for its self-pipe, so patching first makes the loop itself fail to construct and the test
proves nothing about `reserve()`.
"""

from __future__ import annotations

import asyncio
import socket
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from agentiam_core.models import BudgetDimension
from agentiam_pep.errors import ReservationInsufficientError
from agentiam_pep.pool import LeaseGrant, LeasePool, PoolSettings

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
MANDATE = uuid.uuid4()
SPEND = BudgetDimension.SPEND_BDT


class FakeLedger:
    """A ledger client that hands out fixed grants and records what it was asked for."""

    def __init__(self, *, available: Decimal | None = None, fail: bool = False) -> None:
        """Build a ledger that grants from `available`, or refuses outright when `fail`."""
        self.acquired: list[Decimal] = []
        self.released: list[uuid.UUID] = []
        self.available = available
        self.fail = fail
        self.gate: asyncio.Event | None = None

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
        if self.gate is not None:
            await self.gate.wait()
        self.acquired.append(requested)
        if self.fail:
            return None
        granted = requested if self.available is None else min(requested, self.available)
        if granted <= 0:
            return None
        if self.available is not None:
            self.available -= granted
        return LeaseGrant(id=uuid.uuid4(), granted=granted, expires_at=now + ttl)

    async def release(self, *, lease_id: uuid.UUID) -> None:
        self.released.append(lease_id)


def a_pool(ledger: FakeLedger, **over: object) -> LeasePool:
    settings = PoolSettings(
        pep_id="pep-1",
        lease_size=Decimal(100),
        ttl=timedelta(seconds=60),
        skew=timedelta(seconds=5),
        low_water=Decimal("0.25"),
        **over,
    )
    return LeasePool(ledger, settings, mandate_id=MANDATE, now=lambda: NOW)


class TestPriming:
    async def test_prime_acquires_a_lease(self) -> None:
        ledger = FakeLedger()
        pool = a_pool(ledger)
        assert await pool.prime(SPEND)
        assert ledger.acquired == [Decimal(100)]

    async def test_prime_reports_failure_when_the_pool_is_empty(self) -> None:
        ledger = FakeLedger(fail=True)
        assert not await a_pool(ledger).prime(SPEND)

    async def test_prime_is_idempotent(self) -> None:
        """Priming twice must not acquire twice — that would strand a second lease."""
        ledger = FakeLedger()
        pool = a_pool(ledger)
        await pool.prime(SPEND)
        await pool.prime(SPEND)
        assert len(ledger.acquired) == 1


class TestReserveIsLocal:
    async def test_reserve_makes_no_network_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The whole reason the pool exists (spec 04 §4.2, NFR-1).

        The patch goes on **inside** the test body, by which point pytest-asyncio has already
        built the loop. Patching before that breaks the loop itself on Windows, where
        `ProactorEventLoop.__init__` calls `socket.socketpair()` for its self-pipe — the test
        would then fail while proving nothing about `reserve()`.
        """
        ledger = FakeLedger()
        pool = a_pool(ledger)
        await pool.prime(SPEND)

        def explode(*args: object, **kw: object) -> None:
            raise AssertionError("reserve() attempted a network call")

        for name in ("socket", "getaddrinfo", "create_connection"):
            monkeypatch.setattr(socket, name, explode)

        assert pool.reserve(SPEND, Decimal(10)).amount == Decimal(10)

    async def test_the_socket_guard_can_actually_fire(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Otherwise the test above passes for the wrong reason — a guard never seen to fire."""

        def explode(*args: object, **kw: object) -> None:
            raise AssertionError("network call attempted")

        monkeypatch.setattr(socket, "socket", explode)
        with pytest.raises(AssertionError, match="network call attempted"):
            socket.socket()

    async def test_reserve_without_a_lease_is_insufficient(self) -> None:
        with pytest.raises(ReservationInsufficientError):
            a_pool(FakeLedger()).reserve(SPEND, Decimal(1))

    async def test_reserve_draws_down_the_local_remainder(self) -> None:
        pool = a_pool(FakeLedger())
        await pool.prime(SPEND)
        pool.reserve(SPEND, Decimal(30))
        pool.reserve(SPEND, Decimal(20))
        assert pool.remaining(SPEND) == Decimal(50)

    async def test_reserve_beyond_the_lease_is_refused(self) -> None:
        pool = a_pool(FakeLedger())
        await pool.prime(SPEND)
        with pytest.raises(ReservationInsufficientError):
            pool.reserve(SPEND, Decimal(101))
        assert pool.remaining(SPEND) == Decimal(100), "a failed reserve must not draw down"

    async def test_reserve_works_with_no_running_loop(self) -> None:
        """Measured: `get_running_loop()` raises outside a coroutine.

        A top-up cannot be scheduled there, but the hot path must still work — otherwise the
        pool is unusable from a worker thread and every synchronous test of it is a lie.
        """
        ledger = FakeLedger()
        pool = a_pool(ledger)
        await pool.prime(SPEND)

        def sync_caller() -> Decimal:
            pool.reserve(SPEND, Decimal(90))  # crosses the low-water mark
            return pool.remaining(SPEND)

        assert await asyncio.to_thread(sync_caller) == Decimal(10)


class TestTopUp:
    async def test_crossing_the_low_water_mark_schedules_a_top_up(self) -> None:
        ledger = FakeLedger()
        pool = a_pool(ledger)
        await pool.prime(SPEND)

        pool.reserve(SPEND, Decimal(80))  # 20 left, under 25% of 100
        await pool.drain()

        assert len(ledger.acquired) == 2
        assert pool.remaining(SPEND) > Decimal(20)

    async def test_staying_above_the_mark_schedules_nothing(self) -> None:
        ledger = FakeLedger()
        pool = a_pool(ledger)
        await pool.prime(SPEND)

        pool.reserve(SPEND, Decimal(50))
        await pool.drain()

        assert len(ledger.acquired) == 1

    async def test_top_up_is_single_flight(self) -> None:
        """A burst below the mark must produce one ACQUIRE, not one per request.

        Ten would strand most of the pool in leases this PEP cannot spend fast enough —
        spec 04 §14 limitation 1, and the reason `max_fraction` exists at all.

        The gate is what makes this a real test: the first top-up is still in flight while
        the next five reserves happen, so without the single-flight guard each of them
        schedules another ACQUIRE.
        """
        ledger = FakeLedger()
        pool = a_pool(ledger)
        await pool.prime(SPEND)

        ledger.gate = asyncio.Event()
        pool.reserve(SPEND, Decimal(80))  # crosses the mark; the top-up blocks on the gate
        await asyncio.sleep(0)
        for _ in range(5):
            pool.reserve(SPEND, Decimal(1))  # still below the mark, top-up still in flight
        ledger.gate.set()
        await pool.drain()

        assert len(ledger.acquired) == 2, (
            f"one prime + one top-up expected, got {len(ledger.acquired)} acquires"
        )

    async def test_a_refused_top_up_marks_the_mandate_exhausted(self) -> None:
        ledger = FakeLedger(available=Decimal(100))
        pool = a_pool(ledger)
        await pool.prime(SPEND)
        pool.reserve(SPEND, Decimal(80))
        await pool.drain()

        verdict = pool.check({SPEND: Decimal(50)})
        assert not verdict.ok
        assert verdict.mandate_exhausted

    async def test_a_top_up_failure_does_not_lose_the_existing_lease(self) -> None:
        ledger = FakeLedger(available=Decimal(100))
        pool = a_pool(ledger)
        await pool.prime(SPEND)
        pool.reserve(SPEND, Decimal(80))
        await pool.drain()

        assert pool.remaining(SPEND) == Decimal(20)
        assert pool.reserve(SPEND, Decimal(20)).amount == Decimal(20)


class TestBudgetOracle:
    """`check()` is what `decision.decide()` calls at step 7."""

    async def test_a_covered_request_is_ok(self) -> None:
        pool = a_pool(FakeLedger())
        await pool.prime(SPEND)
        assert pool.check({SPEND: Decimal(10)}).ok

    async def test_check_does_not_reserve(self) -> None:
        """Step 7 asks a question; the PEP reserves after the whole pipeline allows."""
        pool = a_pool(FakeLedger())
        await pool.prime(SPEND)
        pool.check({SPEND: Decimal(10)})
        assert pool.remaining(SPEND) == Decimal(100)

    async def test_an_empty_lease_is_not_mandate_exhaustion(self) -> None:
        """Two different pages: top-up has not arrived vs the mandate is spent (spec 09)."""
        pool = a_pool(FakeLedger())
        await pool.prime(SPEND)
        verdict = pool.check({SPEND: Decimal(500)})
        assert not verdict.ok
        assert verdict.exhausted_dimension is SPEND
        assert not verdict.mandate_exhausted

    async def test_a_dimension_with_no_lease_is_refused(self) -> None:
        pool = a_pool(FakeLedger())
        await pool.prime(SPEND)
        verdict = pool.check({BudgetDimension.TOOL_CALLS: Decimal(1)})
        assert not verdict.ok
        assert verdict.exhausted_dimension is BudgetDimension.TOOL_CALLS

    async def test_a_zero_request_needs_no_lease(self) -> None:
        """Every dimension is present in a `RequestContext`, most of them zero (ADR-007)."""
        pool = a_pool(FakeLedger())
        await pool.prime(SPEND)
        assert pool.check(dict.fromkeys(BudgetDimension, Decimal(0))).ok

    async def test_the_first_exhausted_dimension_is_named(self) -> None:
        pool = a_pool(FakeLedger())
        await pool.prime(SPEND)
        verdict = pool.check({SPEND: Decimal(500), BudgetDimension.ROWS_READ: Decimal(1)})
        assert not verdict.ok
        assert verdict.exhausted_dimension is not None


class TestExpiry:
    async def test_a_lease_inside_the_skew_margin_will_not_reserve(self) -> None:
        """Expire early at `expires_at - S` — spec 04 §9, the PEP's half of the margin."""
        ledger = FakeLedger()
        settings = PoolSettings(
            pep_id="pep-1",
            lease_size=Decimal(100),
            ttl=timedelta(seconds=60),
            skew=timedelta(seconds=5),
            low_water=Decimal("0.25"),
        )
        clock = {"t": NOW}
        pool = LeasePool(ledger, settings, mandate_id=MANDATE, now=lambda: clock["t"])
        await pool.prime(SPEND)

        clock["t"] = NOW + timedelta(seconds=56)  # past 60 - 5
        with pytest.raises(ReservationInsufficientError):
            pool.reserve(SPEND, Decimal(1))

    async def test_check_refuses_inside_the_skew_margin(self) -> None:
        ledger = FakeLedger()
        settings = PoolSettings(
            pep_id="pep-1",
            lease_size=Decimal(100),
            ttl=timedelta(seconds=60),
            skew=timedelta(seconds=5),
            low_water=Decimal("0.25"),
        )
        clock = {"t": NOW}
        pool = LeasePool(ledger, settings, mandate_id=MANDATE, now=lambda: clock["t"])
        await pool.prime(SPEND)

        clock["t"] = NOW + timedelta(seconds=56)
        assert not pool.check({SPEND: Decimal(1)}).ok


class TestGracefulShutdown:
    async def test_aclose_releases_every_held_lease(self) -> None:
        ledger = FakeLedger()
        pool = a_pool(ledger)
        await pool.prime(SPEND)
        await pool.prime(BudgetDimension.TOOL_CALLS)

        await pool.aclose()

        assert len(ledger.released) == 2

    async def test_aclose_is_idempotent(self) -> None:
        """A second RELEASE would decrement `leased` twice — TM-21's shape (ADR-009)."""
        ledger = FakeLedger()
        pool = a_pool(ledger)
        await pool.prime(SPEND)

        await pool.aclose()
        await pool.aclose()

        assert len(ledger.released) == 1

    async def test_reserve_after_close_is_refused(self) -> None:
        ledger = FakeLedger()
        pool = a_pool(ledger)
        await pool.prime(SPEND)
        await pool.aclose()

        with pytest.raises(ReservationInsufficientError):
            pool.reserve(SPEND, Decimal(1))

    async def test_aclose_waits_for_an_in_flight_top_up(self) -> None:
        """Releasing while an ACQUIRE is in flight strands the lease it is about to grant.

        The stranded lease would sit until the reaper takes it — spec 04 §7's 80 seconds —
        which is precisely the cost graceful shutdown exists to avoid.
        """
        ledger = FakeLedger()
        pool = a_pool(ledger)
        await pool.prime(SPEND)

        ledger.gate = asyncio.Event()
        pool.reserve(SPEND, Decimal(80))
        await asyncio.sleep(0)

        closing = asyncio.create_task(pool.aclose())
        await asyncio.sleep(0)
        assert not closing.done(), "aclose() must not finish while an ACQUIRE is outstanding"

        ledger.gate.set()
        await closing

        assert len(ledger.released) == len(ledger.acquired), (
            f"the ledger granted {len(ledger.acquired)} leases and got back {len(ledger.released)}"
        )

    async def test_aclose_with_nothing_held_is_fine(self) -> None:
        ledger = FakeLedger()
        await a_pool(ledger).aclose()
        assert ledger.released == []


class TestSettingsRefuseUnsafeConfiguration:
    """Spec 04 §9.2 — the constraint that keeps the two halves of the skew margin apart.

    The PEP expires a lease early at `expires_at - S` and the reaper reclaims late at
    `expires_at + S`. If `ttl <= 2S` those windows overlap and the same budget can be issued
    to two holders at once, which is TM-22. Nothing else in the system checks this, so the
    constructor is the only place it can be caught.
    """

    def _settings(self, **over: object) -> PoolSettings:
        base: dict[str, object] = {
            "pep_id": "pep-1",
            "lease_size": Decimal(100),
            "ttl": timedelta(seconds=60),
            "skew": timedelta(seconds=5),
            "low_water": Decimal("0.25"),
        }
        return PoolSettings(**(base | over))  # type: ignore[arg-type]

    def test_the_defaults_are_safe(self) -> None:
        assert self._settings().ttl > 2 * self._settings().skew

    @pytest.mark.parametrize(
        ("ttl", "skew"),
        [
            (timedelta(seconds=10), timedelta(seconds=5)),
            (timedelta(seconds=8), timedelta(seconds=5)),
        ],
        ids=["ttl-equals-2S", "ttl-under-2S"],
    )
    def test_ttl_must_exceed_twice_the_skew(self, ttl: timedelta, skew: timedelta) -> None:
        with pytest.raises(ValueError, match="TM-22"):
            self._settings(ttl=ttl, skew=skew)

    @pytest.mark.parametrize("size", [Decimal(0), Decimal(-1)])
    def test_lease_size_must_be_positive(self, size: Decimal) -> None:
        with pytest.raises(ValueError, match="lease_size"):
            self._settings(lease_size=size)

    @pytest.mark.parametrize("mark", [Decimal(-1), Decimal(1), Decimal("1.5")])
    def test_low_water_must_be_a_fraction_below_one(self, mark: Decimal) -> None:
        """At 1.0 the lease is always under the mark, so every reserve schedules a top-up."""
        with pytest.raises(ValueError, match="low_water"):
            self._settings(low_water=mark)


class TestCommit:
    """Settling a reservation against what was really spent — spec 04 §4.3."""

    async def test_an_exact_settlement_changes_nothing(self) -> None:
        pool = a_pool(FakeLedger())
        await pool.prime(SPEND)
        reservation = pool.reserve(SPEND, Decimal(30))

        outcome = pool.commit(SPEND, reservation, Decimal(30))

        assert outcome.amount == Decimal(30)
        assert not outcome.escalated
        assert pool.remaining(SPEND) == Decimal(70)

    async def test_an_over_estimate_is_refunded(self) -> None:
        pool = a_pool(FakeLedger())
        await pool.prime(SPEND)
        reservation = pool.reserve(SPEND, Decimal(30))

        outcome = pool.commit(SPEND, reservation, Decimal(10))

        assert outcome.amount == Decimal(10), "the ledger is told what was really spent"
        assert pool.remaining(SPEND) == Decimal(90)

    async def test_an_under_estimate_covers_the_shortfall(self) -> None:
        pool = a_pool(FakeLedger())
        await pool.prime(SPEND)
        reservation = pool.reserve(SPEND, Decimal(30))

        outcome = pool.commit(SPEND, reservation, Decimal(40))

        assert not outcome.escalated
        assert pool.remaining(SPEND) == Decimal(60)

    async def test_a_shortfall_beyond_the_lease_escalates_rather_than_raising(self) -> None:
        """The spend already happened, so it must still reach LEDGER_COMMIT (spec 04 §4.3)."""
        pool = a_pool(FakeLedger())
        await pool.prime(SPEND)
        reservation = pool.reserve(SPEND, Decimal(30))

        outcome = pool.commit(SPEND, reservation, Decimal(500))

        assert outcome.escalated
        assert outcome.amount == Decimal(500), "an escalation must not hide the real amount"

    async def test_committing_against_a_dimension_with_no_lease_is_refused(self) -> None:
        pool = a_pool(FakeLedger())
        await pool.prime(SPEND)
        reservation = pool.reserve(SPEND, Decimal(1))

        with pytest.raises(ReservationInsufficientError):
            pool.commit(BudgetDimension.TOOL_CALLS, reservation, Decimal(1))
