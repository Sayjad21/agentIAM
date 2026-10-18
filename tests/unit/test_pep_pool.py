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
from typing import TYPE_CHECKING, Any

import pytest

from agentiam_core.models import BudgetDimension
from agentiam_pep.errors import ReservationInsufficientError, UnknownMandateError
from agentiam_pep.pool import LeaseGrant, LeasePool, LeasePoolRegistry, PoolSettings, PoolStatus

if TYPE_CHECKING:
    from collections.abc import Callable

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
        #: Raise on acquire, as the deployed client does when the ledger cannot be reached.
        self.unreachable = False
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
        if self.unreachable:
            raise ConnectionError("ledger unreachable")
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


def a_pool(ledger: FakeLedger, **over: Any) -> LeasePool:
    # Most tests here reserve without ever committing, so a replaced lease would wait the
    # full production `retire_wait` for a settlement that never comes (APICTA-09). The
    # tests about that wait set it themselves.
    over.setdefault("retire_wait", timedelta(milliseconds=20))
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

    async def test_an_unreachable_ledger_is_not_mandate_exhaustion(self) -> None:
        """An outage is not a spent budget — CH-01 in CI, 2026-10-16.

        `None` from `acquire` means *the pool has nothing left*; an unreachable ledger raises.
        The chaos harness's client returned `None` for both, so a top-up that failed during
        the Postgres outage marked the mandate exhausted and the PEP told agents their budget
        was gone (`BUDGET_EXHAUSTED_MANDATE`) when the database was merely down — the retryable
        `LEASE_UNAVAILABLE` is what that is. Whether the failed top-up landed first was timing,
        which is why it passed locally and failed on CI's runner.
        """
        ledger = FakeLedger(available=Decimal(1000))
        pool = a_pool(ledger)
        await pool.prime(SPEND)
        pool.reserve(SPEND, Decimal(80))
        ledger.unreachable = True
        await pool.drain()

        verdict = pool.check({SPEND: Decimal(50)})
        assert not verdict.ok
        assert not verdict.mandate_exhausted
        assert pool.acquire_failures == 1

    async def test_an_unreachable_ledger_does_not_escape_the_top_up_task(self) -> None:
        # The deployed client raised here and nothing caught it: one unretrieved task
        # exception per failed top-up for the length of an outage.
        ledger = FakeLedger(available=Decimal(1000))
        pool = a_pool(ledger)
        await pool.prime(SPEND)
        pool.reserve(SPEND, Decimal(80))
        ledger.unreachable = True
        pool.check({SPEND: Decimal(50)})  # schedules the top-up as it refuses
        await pool.drain()
        assert pool.acquire_failures >= 1

    async def test_priming_against_an_unreachable_ledger_reports_false(self) -> None:
        ledger = FakeLedger()
        ledger.unreachable = True
        pool = a_pool(ledger)
        assert await pool.prime(SPEND) is False

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

    async def test_a_refusal_schedules_a_top_up_so_the_pool_can_recover(self) -> None:
        """The closed loop CH-1 found, and the reason `check()` has a side effect.

        Top-ups are otherwise scheduled only from `reserve()`, and `reserve()` runs only
        after the pipeline has allowed — which it cannot do while `check()` refuses. So a
        lease that reaches empty could never be refilled: the only thing that refills it is
        the spend path it is blocking.

        The low-water mark normally tops up long before empty, so the loop closes only when
        a top-up has already failed. That is CH-1 exactly — Postgres goes away, the held
        lease drains, and the PEP stays dead after the database comes back.
        """
        ledger = FakeLedger()
        pool = a_pool(ledger)
        await pool.prime(SPEND)

        # The outage. The lease drains to empty and the top-up it schedules on the way
        # down fails, which is what leaves the pool with no route back.
        ledger.fail = True
        pool.reserve(SPEND, Decimal(100))
        await pool.drain()
        assert pool.remaining(SPEND) == Decimal(0)

        ledger.fail = False
        asked_before = len(ledger.acquired)

        verdict = pool.check({SPEND: Decimal(10)})
        assert not verdict.ok, "an empty lease must still refuse the request that found it"
        await pool.drain()

        assert len(ledger.acquired) > asked_before, (
            "a refusal scheduled no top-up, so nothing will ever refill this lease"
        )
        assert pool.check({SPEND: Decimal(10)}).ok, "the pool never recovered"

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


class TestBackgroundRenewal:
    """A lease is replaced on a timer, not only when a request notices it aged out.

    Every top-up used to be request-triggered, and the scheduling is asynchronous — so the
    request that *notices* an expired lease is refused anyway, because `check()` has already
    decided by the time the replacement lands. Item 24 (ADR-049's addendum) fixed the case
    where nothing was ever scheduled at all; what was left is that something has to be
    refused to trigger the renewal that would have prevented it.

    Invisible on a PEP that spends steadily — the low-water mark fires long before anything
    expires. On an idle one it is the whole experience: the deployed PEP primes one lease at
    boot with a 60 s TTL, so the first payment after that minute is `LEASE_UNAVAILABLE` and
    the next one succeeds. Measured on the demo stack, 80 s after `up --wait`.
    """

    @staticmethod
    def _clocked(
        ledger: FakeLedger, ttl: timedelta = timedelta(seconds=60)
    ) -> tuple[LeasePool, dict[str, datetime]]:
        # Skew scales with the TTL: `PoolSettings` refuses `ttl <= 2 * skew`, and these tests
        # run on a 4 s TTL so a sweep at `ttl / 4` completes inside a test. At ttl/8 the
        # half-life margin (2 s) is comfortably wider than the skew (0.5 s), which is the
        # relationship the renewal depends on and the production defaults also have.
        settings = PoolSettings(
            pep_id="pep-1",
            lease_size=Decimal(100),
            ttl=ttl,
            skew=ttl / 8,
            low_water=Decimal("0.25"),
        )
        clock = {"t": NOW}
        return LeasePool(ledger, settings, mandate_id=MANDATE, now=lambda: clock["t"]), clock

    async def test_a_lease_past_its_half_life_is_replaced_without_a_request(self) -> None:
        """The defect itself: no `reserve()`, no `check()`, and the lease still renews."""
        ledger = FakeLedger()
        pool, clock = self._clocked(ledger, ttl=timedelta(seconds=4))
        await pool.prime(SPEND)
        assert ledger.acquired == [Decimal(100)]

        # Past the 2 s half-life, and still 0.5 s clear of the skew margin at 3.5 s — so the
        # lease being replaced is one `check()` would still have accepted.
        clock["t"] = NOW + timedelta(seconds=3)
        await pool.start()
        await asyncio.sleep(1.5)  # one sweep at ttl/4 = 1 s
        await pool.drain()

        assert len(ledger.acquired) == 2
        # The replacement releases the lease it replaced, rather than holding both.
        assert len(ledger.released) == 1
        await pool.aclose()

    async def test_the_renewed_lease_is_usable_where_the_old_one_would_not_be(self) -> None:
        """The point of renewing at the half-life rather than at expiry.

        Replacing a lease only once it is stale would put the replacement *after* `check()`
        had already begun refusing against it — the margin has to be wider than the skew, and
        `PoolSettings` refusing `ttl <= 2 * skew` is what guarantees half the TTL is.
        """
        ledger = FakeLedger()
        pool, clock = self._clocked(ledger, ttl=timedelta(seconds=4))
        await pool.prime(SPEND)

        clock["t"] = NOW + timedelta(seconds=3)
        await pool.start()
        await asyncio.sleep(1.5)
        await pool.drain()

        # Now step past where the *original* lease would have died. It expired at NOW+4 and
        # `check()` refuses from NOW+3.5 (the skew); the replacement was taken at NOW+3 and
        # runs to NOW+7. Asserting at NOW+3 would have passed with renewal switched off
        # entirely, which is the whole reason the clock moves here.
        clock["t"] = NOW + timedelta(seconds=5)
        assert pool.check({SPEND: Decimal(1)}).ok
        await pool.aclose()

    async def test_a_fresh_lease_is_left_alone(self) -> None:
        """Renewing early wastes an ACQUIRE/RELEASE pair; the sweep must be a no-op."""
        ledger = FakeLedger()
        pool, _clock = self._clocked(ledger, ttl=timedelta(seconds=4))
        await pool.prime(SPEND)

        await pool.start()
        await asyncio.sleep(1.5)
        await pool.drain()

        assert ledger.acquired == [Decimal(100)]
        await pool.aclose()

    async def test_start_is_idempotent(self) -> None:
        """Two sweeps for one pool would race each other into duplicate ACQUIREs."""
        ledger = FakeLedger()
        pool, _clock = self._clocked(ledger)
        await pool.start()
        await pool.start()

        assert pool._renewer is not None
        await pool.aclose()

    async def test_a_pool_with_nothing_held_sweeps_harmlessly(self) -> None:
        """`start()` runs before `prime()` in the deployed lifespan, deliberately."""
        ledger = FakeLedger()
        pool, _clock = self._clocked(ledger, ttl=timedelta(seconds=4))
        await pool.start()
        await asyncio.sleep(1.5)

        assert ledger.acquired == []
        await pool.aclose()

    async def test_aclose_stops_the_sweep(self) -> None:
        """A sweep firing mid-shutdown would strand a fresh lease for the full TTL.

        The same failure `aclose()`'s own docstring gives for not draining first.
        """
        ledger = FakeLedger()
        pool, clock = self._clocked(ledger, ttl=timedelta(seconds=4))
        await pool.prime(SPEND)
        await pool.start()
        await pool.aclose()

        clock["t"] = NOW + timedelta(seconds=3)
        acquired_at_close = len(ledger.acquired)
        await asyncio.sleep(1.5)

        assert len(ledger.acquired) == acquired_at_close
        assert pool._renewer is None

    async def test_renewal_settles_before_it_releases(self) -> None:
        """Renewal goes through `_acquire`, so ADR-049's ordering is not a second code path.

        A lease released while settlements against it are still queued declines every one of
        them (CH-10: 6,678 of 6,992). `_release` awaits `before_release` first, and renewal
        must not be a route around that.
        """
        ledger = FakeLedger()
        pool, clock = self._clocked(ledger, ttl=timedelta(seconds=4))
        order: list[str] = []

        async def drain_first() -> None:
            order.append("settled")

        pool._before_release = drain_first
        original_release = ledger.release

        async def record_release(*, lease_id: uuid.UUID) -> None:
            order.append("released")
            await original_release(lease_id=lease_id)

        ledger.release = record_release  # type: ignore[method-assign]

        await pool.prime(SPEND)
        clock["t"] = NOW + timedelta(seconds=3)
        await pool.start()
        await asyncio.sleep(1.5)
        await pool.drain()

        assert order == ["settled", "released"]
        await pool.aclose()


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


class TestALeaseThatExpiresUnspentIsReplaced:
    """TODO item 24 — the PEP used to stop authorizing anything that costs money.

    A lease leaves service two ways: it drains, or it ages out. `_maybe_schedule_topup` only
    tested the first, so a lease that expired **unspent** — `remaining_local` still the full
    grant — never triggered an ACQUIRE. `check()` then refused it on every later request,
    forever.

    Not a corner case. The deployed PEP primes one lease at boot with a 60 s TTL, so any
    stack idle for a minute after start-up fell into it. Found by leaving the demo stack up
    and reading the console before sending traffic: eight payments over sixteen seconds, all
    `LEASE_UNAVAILABLE`, with `select … from leases` showing one row, `granted 5000.0000`,
    `settled 0.0000`, `state 'expired'`.
    """

    @staticmethod
    def _clocked(ledger: FakeLedger) -> tuple[LeasePool, dict[str, datetime]]:
        clock = {"t": NOW}
        settings = PoolSettings(
            pep_id="pep-1",
            lease_size=Decimal(100),
            ttl=timedelta(seconds=60),
            skew=timedelta(seconds=5),
            low_water=Decimal("0.25"),
        )
        return LeasePool(ledger, settings, mandate_id=MANDATE, now=lambda: clock["t"]), clock

    async def test_an_expired_unspent_lease_schedules_a_replacement(self) -> None:
        """The defect itself: full lease, past its TTL, and no ACQUIRE was ever issued."""
        ledger = FakeLedger()
        pool, clock = self._clocked(ledger)
        await pool.prime(SPEND)
        assert len(ledger.acquired) == 1

        clock["t"] = NOW + timedelta(seconds=61)
        assert not pool.check({SPEND: Decimal(1)}).ok, "an expired lease must refuse"
        await pool.drain()

        assert len(ledger.acquired) == 2, "the refusal must have asked for a new lease"

    async def test_the_next_request_after_the_refusal_is_covered(self) -> None:
        """Recovery is the point: refusing once is correct, refusing forever is not."""
        ledger = FakeLedger()
        pool, clock = self._clocked(ledger)
        await pool.prime(SPEND)

        clock["t"] = NOW + timedelta(seconds=61)
        pool.check({SPEND: Decimal(1)})
        await pool.drain()

        assert pool.check({SPEND: Decimal(1)}).ok

    async def test_a_healthy_lease_is_not_replaced(self) -> None:
        """The guard still has to say no, or every request would top up.

        Both halves matter: a lease with time left *and* budget left is the common case, and
        acquiring a second one would strand budget this PEP has no plan to spend.
        """
        ledger = FakeLedger()
        pool, clock = self._clocked(ledger)
        await pool.prime(SPEND)

        clock["t"] = NOW + timedelta(seconds=10)
        assert pool.check({SPEND: Decimal(1)}).ok
        await pool.drain()

        assert len(ledger.acquired) == 1

    async def test_the_replacement_releases_the_lease_it_replaces(self) -> None:
        """The old lease is handed back rather than left to the reaper.

        Safe even though it has expired: the ledger's `release()` is a no-op for a lease
        already in a terminal state (spec 04 §3), so this cannot double-decrement `leased`.
        """
        ledger = FakeLedger()
        pool, clock = self._clocked(ledger)
        await pool.prime(SPEND)
        first = pool._held[SPEND].lease.id

        clock["t"] = NOW + timedelta(seconds=61)
        pool.check({SPEND: Decimal(1)})
        await pool.drain()

        assert ledger.released == [first]
        assert pool._held[SPEND].lease.id != first

    async def test_expiry_inside_the_skew_margin_also_replaces(self) -> None:
        """`check()` refuses at `expires_at - skew`, so the top-up must use the same line.

        A top-up that waited for the true expiry would leave a five-second window in which
        every request is refused and nothing is being done about it.
        """
        ledger = FakeLedger()
        pool, clock = self._clocked(ledger)
        await pool.prime(SPEND)

        clock["t"] = NOW + timedelta(seconds=56)  # past 60 - 5, before 60
        assert not pool.check({SPEND: Decimal(1)}).ok
        await pool.drain()

        assert len(ledger.acquired) == 2


class TestARequestLargerThanALease:
    """APICTA-03 — a single request the standing lease size can never cover.

    Every ACQUIRE asked for exactly `lease_size`, so no request larger than one lease could
    ever be authorized: `check()` refused it, scheduled a top-up for another `lease_size`,
    and refused it again. Found by the escalation beat on the demo stack — a CFO approves
    ৳50,000, the agent collects the elevated token and retries, and a PEP leasing ৳5,000 at
    a time answers `LEASE_UNAVAILABLE` on every retry. Ordinary traffic fits inside a lease,
    which is why nothing had hit it before. Spec 04 §4.1a.
    """

    async def test_the_refusal_asks_for_enough_to_cover_the_request(self) -> None:
        ledger = FakeLedger()
        pool = a_pool(ledger)
        await pool.prime(SPEND)

        assert not pool.check({SPEND: Decimal(500)}).ok, "the first request still waits"
        await pool.drain()

        assert ledger.acquired[-1] == Decimal(500)
        assert pool.check({SPEND: Decimal(500)}).ok, "the retry is covered"
        pool.reserve(SPEND, Decimal(500))

    async def test_once_served_the_next_top_up_is_back_to_lease_size(self) -> None:
        ledger = FakeLedger()
        pool = a_pool(ledger)
        await pool.prime(SPEND)
        pool.check({SPEND: Decimal(500)})
        await pool.drain()

        pool.reserve(SPEND, Decimal(500))  # drains the large lease, scheduling a top-up
        await pool.drain()

        assert ledger.acquired == [Decimal(100), Decimal(500), Decimal(100)]

    async def test_a_request_inside_the_lease_size_asks_for_the_lease_size(self) -> None:
        ledger = FakeLedger()
        pool = a_pool(ledger)
        await pool.prime(SPEND)
        pool.reserve(SPEND, Decimal(95))
        await pool.drain()
        ledger.acquired.clear()

        pool.reserve(SPEND, Decimal(100))
        pool.check({SPEND: Decimal(60)})
        await pool.drain()

        assert ledger.acquired == [Decimal(100)]

    async def test_demand_arriving_during_a_smaller_top_up_is_not_lost(self) -> None:
        """A top-up already in flight was sized before the large request arrived.

        Single flight means the refusal cannot start a second ACQUIRE, so the first retry
        finds a lease-size grant. What matters is that the *retry's* refusal then asks for
        enough — the pool converges in one more round trip rather than never.
        """
        ledger = FakeLedger()
        pool = a_pool(ledger)
        await pool.prime(SPEND)

        ledger.gate = asyncio.Event()
        pool.reserve(SPEND, Decimal(80))  # schedules a lease-size top-up, held at the gate
        await asyncio.sleep(0)
        assert not pool.check({SPEND: Decimal(500)}).ok
        ledger.gate.set()
        await pool.drain()
        assert ledger.acquired[-1] == Decimal(100)

        assert not pool.check({SPEND: Decimal(500)}).ok
        await pool.drain()
        assert ledger.acquired[-1] == Decimal(500)
        assert pool.check({SPEND: Decimal(500)}).ok

    async def test_a_short_pool_grants_what_it_has_and_the_request_still_waits(self) -> None:
        """Asking for more is safe: the ledger grants `min(requested, available)`."""
        ledger = FakeLedger(available=Decimal(400))
        pool = a_pool(ledger)
        await pool.prime(SPEND)  # 100 of the 400

        pool.check({SPEND: Decimal(500)})
        await pool.drain()

        assert ledger.acquired[-1] == Decimal(500)
        assert pool.remaining(SPEND) == Decimal(300)
        assert not pool.check({SPEND: Decimal(500)}).ok


class MandateLedger(FakeLedger):
    """A `FakeLedger` that knows which mandates have a budget, and records who asked."""

    def __init__(self, *, unknown: frozenset[uuid.UUID] = frozenset()) -> None:
        """Every mandate has a budget except those in `unknown`."""
        super().__init__()
        self.unknown = set(unknown)
        self.asked: list[uuid.UUID] = []
        self.leases: dict[uuid.UUID, uuid.UUID] = {}

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
        self.asked.append(mandate_id)
        if mandate_id in self.unknown:
            raise UnknownMandateError(f"no budget for mandate {mandate_id}")
        grant = await super().acquire(
            mandate_id=mandate_id,
            dimension=dimension,
            requested=requested,
            pep_id=pep_id,
            ttl=ttl,
            now=now,
        )
        if grant is not None:
            self.leases[grant.id] = mandate_id
        return grant


A = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
B = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")


def a_registry(
    ledger: FakeLedger,
    *,
    clock: Callable[[], datetime] | None = None,
    **over: Any,
) -> LeasePoolRegistry:
    return LeasePoolRegistry(
        ledger,
        # Short for the reason `a_pool` gives: these tests reserve without committing.
        PoolSettings(
            pep_id="pep-1", lease_size=Decimal(100), retire_wait=timedelta(milliseconds=20)
        ),
        now=clock or (lambda: NOW),
        **over,
    )


class TestLeasePoolRegistry:
    """APICTA-02: one PEP, a pool per mandate, each pool its own `LeasePool`.

    The pool code is unchanged; the registry decides which pool a request draws from, and
    creates one — off the request path — for a mandate it has not seen.
    """

    async def test_ensure_creates_and_primes_a_pool_for_the_mandate(self) -> None:
        ledger = MandateLedger()
        registry = a_registry(ledger)
        pool = await registry.ensure(A)
        assert pool is not None
        assert pool.mandate_id == A
        assert pool.remaining(SPEND) == Decimal(100)
        assert ledger.asked == [A]

    async def test_ensure_is_single_flight_per_mandate(self) -> None:
        ledger = MandateLedger()
        ledger.gate = asyncio.Event()
        registry = a_registry(ledger)
        first = asyncio.create_task(registry.ensure(A))
        second = asyncio.create_task(registry.ensure(A))
        await asyncio.sleep(0)
        ledger.gate.set()
        assert await first is await second
        assert ledger.asked == [A]

    async def test_an_unseen_mandate_is_pending_then_served(self) -> None:
        ledger = MandateLedger()
        registry = a_registry(ledger)
        assert registry.lookup(A) is PoolStatus.PENDING
        await registry.drain()
        found = registry.lookup(A)
        assert isinstance(found, LeasePool)
        assert found.mandate_id == A

    async def test_a_mandate_the_ledger_has_no_budget_for_is_unknown(self) -> None:
        ledger = MandateLedger(unknown=frozenset({A}))
        registry = a_registry(ledger)
        registry.lookup(A)
        await registry.drain()
        assert registry.lookup(A) is PoolStatus.UNKNOWN
        assert registry.get(A) is None

    async def test_an_unknown_mandate_is_not_asked_about_again_until_its_ttl(self) -> None:
        """Otherwise a stream of tokens for a mandate nobody funded is a stream of ACQUIREs."""
        clock = {"t": NOW}
        ledger = MandateLedger(unknown=frozenset({A}))
        registry = a_registry(ledger, clock=lambda: clock["t"], unknown_ttl=timedelta(seconds=30))
        registry.lookup(A)
        await registry.drain()
        for _ in range(5):
            registry.lookup(A)
        await registry.drain()
        assert ledger.asked == [A]

        clock["t"] = NOW + timedelta(seconds=31)
        ledger.unknown.clear()  # the mandate has been funded since
        assert registry.lookup(A) is PoolStatus.PENDING
        await registry.drain()
        assert isinstance(registry.lookup(A), LeasePool)

    async def test_an_unreachable_ledger_leaves_the_mandate_pending_not_unknown(self) -> None:
        """An outage says nothing about whether the mandate exists (the `None`-vs-raise rule)."""
        ledger = MandateLedger()
        ledger.unreachable = True
        registry = a_registry(ledger)
        registry.lookup(A)
        await registry.drain()
        assert registry.lookup(A) is PoolStatus.PENDING
        await registry.drain()
        assert ledger.asked.count(A) == 2, "the next request must try again"

    async def test_a_top_up_for_one_mandate_never_releases_the_other_s_lease(self) -> None:
        ledger = MandateLedger()
        registry = a_registry(ledger)
        pool_a = await registry.ensure(A)
        await registry.ensure(B)
        assert pool_a is not None

        pool_a.reserve(SPEND, Decimal(90))  # past A's low-water mark: A tops up
        await registry.drain()

        released = {ledger.leases[lease_id] for lease_id in ledger.released}
        assert released == {A}

    async def test_aclose_releases_every_pool_s_lease(self) -> None:
        ledger = MandateLedger()
        registry = a_registry(ledger)
        await registry.ensure(A)
        await registry.ensure(B)
        await registry.aclose()
        assert {ledger.leases[lease_id] for lease_id in ledger.released} == {A, B}

    async def test_every_pool_gets_the_before_release_hook(self) -> None:
        """ADR-049's settle-before-release, per pool, not only for the boot mandate."""
        calls: list[str] = []

        async def settle() -> None:
            calls.append("settle")

        registry = a_registry(MandateLedger(), before_release=settle)
        await registry.ensure(A)
        await registry.ensure(B)
        await registry.aclose()
        assert calls == ["settle", "settle"]

    async def test_a_fixed_registry_serves_its_pool_and_nothing_else(self) -> None:
        ledger = MandateLedger()
        pool = LeasePool(ledger, PoolSettings(pep_id="pep-1"), mandate_id=A, now=lambda: NOW)
        await pool.prime(SPEND)
        registry = LeasePoolRegistry.fixed(pool)
        assert registry.lookup(A) is pool
        assert registry.lookup(B) is PoolStatus.UNKNOWN
        await registry.drain()
        assert ledger.asked == [A], "a fixed registry must never create a pool"

    async def test_a_spent_mandate_is_exhausted_not_pending(self) -> None:
        """A pool the ledger has emptied must not sit unprimed answering `LEASE_UNAVAILABLE`."""
        ledger = MandateLedger()
        ledger.available = Decimal(0)
        registry = a_registry(ledger)
        registry.lookup(A)
        await registry.drain()
        assert registry.lookup(A) is PoolStatus.EXHAUSTED
        await registry.drain()
        assert ledger.asked == [A]

    async def test_status_reports_each_mandate_s_pool_and_each_refusal(self) -> None:
        """APICTA-02 Part C: what `/readyz` and the metrics show, per mandate."""
        ledger = MandateLedger(unknown=frozenset({B}))
        registry = a_registry(ledger)
        pool = await registry.ensure(A)
        assert pool is not None
        pool.reserve(SPEND, Decimal(30))
        registry.lookup(B)
        await registry.drain()

        status = registry.status()
        assert status["pools"][str(A)]["spend_bdt_remaining"] == "70"
        assert status["pools"][str(A)]["lease_state"] == "active"
        assert status["refused"][str(B)] == "unknown"
        assert str(B) not in status["pools"]


class TestALeaseIsNotReleasedWhileOwed:
    """Gap 38's second cause, APICTA-09: a request in flight across a top-up.

    The request reserves on lease A and waits for its upstream; a top-up swaps the pool to B
    and used to release A at once. `RELEASE` returned A's reserved money to the pool, and the
    late settlement was charged to *B* — whose ledger clamp then dropped it once B was spent.
    Measured against a real ledger: ৳190 spent, ৳140 committed, the ৳50 in flight lost.
    """

    @staticmethod
    async def swapped(pool: LeasePool) -> tuple[Any, uuid.UUID]:
        """Hold a reservation on the first lease, then drive a top-up to a second."""
        await pool.prime(SPEND)
        inflight = pool.reserve(SPEND, Decimal(50))
        spent = pool.reserve(SPEND, Decimal(40))  # 10 left: under the low-water mark
        pool.commit(SPEND, spent, Decimal(40))  # answered at once; only `inflight` is open
        for _ in range(100):
            await asyncio.sleep(0)
            if pool._held[SPEND].lease.id != inflight.lease_id:
                break
        return inflight, pool._held[SPEND].lease.id

    async def test_a_late_settlement_is_charged_to_the_lease_it_was_reserved_on(self) -> None:
        pool = a_pool(FakeLedger())
        inflight, current = await self.swapped(pool)
        assert current != inflight.lease_id

        outcome = pool.commit(SPEND, inflight, Decimal(50))

        assert outcome.lease_id == inflight.lease_id
        await pool.drain()

    async def test_the_old_lease_is_not_released_while_a_reservation_on_it_is_open(
        self,
    ) -> None:
        ledger = FakeLedger()
        pool = a_pool(ledger, retire_wait=timedelta(seconds=30))
        inflight, _ = await self.swapped(pool)
        for _ in range(20):
            await asyncio.sleep(0)

        assert inflight.lease_id not in ledger.released

        pool.commit(SPEND, inflight, Decimal(50))
        await pool.drain()
        assert inflight.lease_id in ledger.released

    async def test_settlement_drains_after_the_last_commit_and_before_the_release(self) -> None:
        """`before_release` must see the late settlement, or ADR-049's ordering is moot."""
        order: list[str] = []
        ledger = FakeLedger()

        async def before_release() -> None:
            order.append("drain")

        settings = PoolSettings(
            pep_id="pep-1",
            lease_size=Decimal(100),
            ttl=timedelta(seconds=60),
            skew=timedelta(seconds=5),
            retire_wait=timedelta(seconds=30),
        )
        pool = LeasePool(
            ledger, settings, mandate_id=MANDATE, now=lambda: NOW, before_release=before_release
        )
        inflight, _ = await self.swapped(pool)
        await asyncio.sleep(0)
        order.append("commit")
        pool.commit(SPEND, inflight, Decimal(50))
        await pool.drain()

        assert order[:2] == ["commit", "drain"]

    async def test_a_reservation_that_never_settles_holds_the_release_only_so_long(
        self,
    ) -> None:
        """An upstream that timed out never settles; the old lease must still go back."""
        ledger = FakeLedger()
        pool = a_pool(ledger, retire_wait=timedelta(milliseconds=50))
        inflight, _ = await self.swapped(pool)

        await pool.drain()

        assert inflight.lease_id in ledger.released
        assert pool.released_unsettled == 1

    async def test_shutdown_waits_for_the_held_lease_s_open_reservations(self) -> None:
        ledger = FakeLedger()
        pool = a_pool(ledger, retire_wait=timedelta(seconds=30))
        await pool.prime(SPEND)
        inflight = pool.reserve(SPEND, Decimal(10))

        closing = asyncio.create_task(pool.aclose())
        for _ in range(20):
            await asyncio.sleep(0)
        assert inflight.lease_id not in ledger.released

        pool.commit(SPEND, inflight, Decimal(10))
        await closing
        assert inflight.lease_id in ledger.released

    async def test_a_reservation_whose_lease_is_gone_keeps_its_own_lease_id(self) -> None:
        """Late beyond the bound, the settlement keeps the lease that funded it.

        The ledger rejects it and records an anomaly (spec 04 §11), rather than it being
        charged to a lease that never funded it.
        """
        pool = a_pool(FakeLedger(), retire_wait=timedelta(milliseconds=10))
        inflight, current = await self.swapped(pool)
        await pool.drain()

        outcome = pool.commit(SPEND, inflight, Decimal(50))

        assert outcome.lease_id == inflight.lease_id != current
