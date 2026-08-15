"""The PEP-local lease pool — spec 04 §4.1, §4.2, §4.5, T-021.

`reserve()` is **synchronous and touches nothing but memory.** That is the whole design.
`PLAN.md` §3.2 requires the hot path never to block on the network to reach a verdict, and
NFR-1's 1 ms budget exists because a ledger round trip in the tool-call critical path would
be worth tens of milliseconds. Everything expensive — acquiring, topping up, releasing —
happens off that path.

The three things worth knowing before changing this module:

1. **A top-up is scheduled, never awaited, from `reserve()`.** Measured: `get_running_loop()`
   raises outside a coroutine, so the hot path tolerates having no loop and simply does not
   schedule. It must never fail for want of one — the pool has to work from a worker thread.
2. **Top-ups are single-flight per dimension.** A burst of ten requests crossing the low-water
   mark must produce one `ACQUIRE`, not ten; ten would strand most of the pool in leases this
   PEP cannot spend fast enough (spec 04 §14 limitation 1).
3. **`check()` does not reserve.** `decide()` step 7 asks a question; the PEP reserves after
   the whole pipeline allows. The gap between them is a within-process race that fails closed:
   a concurrent request can empty the lease, and `reserve()` then refuses.

Losing this state costs the PEP its ability to spend, never the ledger's accounting
(spec 04 §2.3). The ledger is authoritative; this is a cache with a deadline.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

from agentiam_core.decision import BudgetVerdict
from agentiam_core.models import BudgetDimension, LeaseState
from agentiam_pep.errors import ReservationInsufficientError
from agentiam_pep.lease import CommitOutcome, LocalLease, Reservation, commit, reserve

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import datetime

__all__ = [
    "LeaseGrant",
    "LeasePool",
    "LedgerClient",
    "PoolSettings",
]


@dataclass(frozen=True, slots=True)
class LeaseGrant:
    """What `ACQUIRE` returns — the ledger-issued, immutable facts (spec 04 §2.2)."""

    id: uuid.UUID
    granted: Decimal
    expires_at: datetime


class LedgerClient(Protocol):
    """The control-plane operations the pool needs.

    A protocol rather than a concrete client because the transport does not exist yet: the
    control plane's HTTP surface is `PLAN.md` §8 and is not built. Everything here is
    testable against a fake, and the real client drops in without touching this module.
    """

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
        """Grant `min(requested, available)`, or None when the pool has nothing left."""
        ...

    async def release(self, *, lease_id: uuid.UUID) -> None:
        """Return a lease's outstanding budget to the pool (spec 04 §4.5)."""
        ...


@dataclass(frozen=True, slots=True)
class PoolSettings:
    """Spec 04 §12's parameters. All configurable, as the spec requires.

    `lease_size` is fixed: adaptive sizing is T-015, deferred, and its formula is specified
    in spec 04 §12 so the deferral is a scheduling choice rather than an open question.
    """

    pep_id: str
    lease_size: Decimal = Decimal(100)
    ttl: timedelta = timedelta(seconds=60)
    skew: timedelta = timedelta(seconds=5)
    low_water: Decimal = Decimal("0.25")

    def __post_init__(self) -> None:
        """Reject a configuration that cannot be safe (spec 04 §9.2)."""
        if self.lease_size <= 0:
            raise ValueError("lease_size must be positive")
        if not (0 <= self.low_water < 1):
            raise ValueError("low_water must be in [0, 1)")
        if self.ttl <= 2 * self.skew:
            raise ValueError(
                f"ttl ({self.ttl}) must exceed 2 x skew ({self.skew}); otherwise the PEP's "
                f"early expiry and the reaper's late reclaim overlap and the same budget can "
                f"be issued twice (spec 04 §9.2, TM-22)"
            )


@dataclass
class _Held:
    """One dimension's lease and the bookkeeping around it."""

    lease: LocalLease
    topping_up: bool = False
    mandate_exhausted: bool = False
    tasks: set[asyncio.Task[None]] = field(default_factory=set)


class LeasePool:
    """Leases held by this PEP for one mandate, keyed by dimension.

    One pool per mandate, because `BudgetOracle.check()` is handed only the requested
    amounts — the mandate is fixed by whoever built the pool.
    """

    def __init__(
        self,
        client: LedgerClient,
        settings: PoolSettings,
        *,
        mandate_id: uuid.UUID,
        now: Callable[[], datetime],
    ) -> None:
        """Build an empty pool. Nothing is acquired until `prime()`."""
        self._client = client
        self._settings = settings
        self._mandate_id = mandate_id
        self._now = now
        self._held: dict[BudgetDimension, _Held] = {}
        self._closed = False

    # -- acquisition ----------------------------------------------------------------

    async def prime(self, dimension: BudgetDimension) -> bool:
        """Acquire the first lease for `dimension`.

        Idempotent: priming a dimension that already holds a lease acquires nothing, because
        a second lease would strand budget this PEP has no plan to spend.

        Returns:
            True if a lease is held afterwards.
        """
        if dimension in self._held:
            return True
        grant = await self._acquire(dimension)
        return grant is not None

    async def _acquire(self, dimension: BudgetDimension) -> LeaseGrant | None:
        now = self._now()
        grant = await self._client.acquire(
            mandate_id=self._mandate_id,
            dimension=dimension,
            requested=self._settings.lease_size,
            pep_id=self._settings.pep_id,
            ttl=self._settings.ttl,
            now=now,
        )
        held = self._held.get(dimension)
        if grant is None:
            # The ledger has nothing left. Distinct from "our lease is empty" — spec 09
            # names them differently because the operator's fix differs.
            if held is not None:
                held.mandate_exhausted = True
            return None

        if held is None:
            self._held[dimension] = _Held(
                lease=LocalLease(
                    id=grant.id,
                    granted=grant.granted,
                    remaining_local=grant.granted,
                    expires_at=grant.expires_at,
                    state=LeaseState.ACTIVE,
                )
            )
            return grant

        # A top-up replaces the lease rather than adding to it. Holding two leases for one
        # dimension would mean two expiries and two RELEASEs to keep straight, for no gain:
        # the old lease's unspent remainder is returned by the RELEASE below.
        old = held.lease
        held.lease = LocalLease(
            id=grant.id,
            granted=grant.granted,
            remaining_local=grant.granted,
            expires_at=grant.expires_at,
            state=LeaseState.ACTIVE,
        )
        held.mandate_exhausted = False
        # The old lease is always active here: nothing in this module moves a lease out of
        # ACTIVE except aclose(), and aclose() stops top-ups before it does. When T-038's
        # revocation gossip starts marking leases from outside, this is where an already
        # retired lease must be skipped — releasing one twice is TM-21's shape (ADR-009).
        old.state = LeaseState.RELEASED
        await self._client.release(lease_id=old.id)
        return grant

    # -- the hot path ---------------------------------------------------------------

    def reserve(self, dimension: BudgetDimension, amount: Decimal) -> Reservation:
        """Hold `amount` locally. Synchronous, and never touches the network.

        Raises:
            ReservationInsufficientError: No lease, the lease is inside its skew margin, or
                the local remainder is short. All three are spec 04 §4.2's `Insufficient`.
        """
        held = self._held.get(dimension)
        if held is None or self._closed:
            raise ReservationInsufficientError(f"no active lease for {dimension.value} on this PEP")
        reservation = reserve(held.lease, amount, now=self._now(), skew=self._settings.skew)
        self._maybe_schedule_topup(dimension, held)
        return reservation

    def commit(
        self, dimension: BudgetDimension, reservation: Reservation, actual: Decimal
    ) -> CommitOutcome:
        """Settle a reservation against the real amount — spec 04 §4.3.

        Raises:
            ReservationInsufficientError: If the dimension holds no lease.
        """
        held = self._held.get(dimension)
        if held is None:
            raise ReservationInsufficientError(
                f"no lease for {dimension.value}; cannot settle {reservation.id}"
            )
        return commit(held.lease, reservation, actual, now=self._now(), skew=self._settings.skew)

    def check(self, requested: Mapping[BudgetDimension, Decimal]) -> BudgetVerdict:
        """`BudgetOracle` — whether the held leases cover `requested`. Reserves nothing.

        A `RequestContext` carries every dimension, most of them zero (ADR-007), so a zero
        request against a dimension this PEP holds no lease for is not a refusal.
        """
        for dimension, amount in requested.items():
            if amount <= 0:
                continue
            held = self._held.get(dimension)
            if held is None or self._closed:
                return BudgetVerdict(ok=False, exhausted_dimension=dimension)
            lease = held.lease
            if (
                lease.state is not LeaseState.ACTIVE
                or self._now() >= lease.expires_at - self._settings.skew
                or lease.remaining_local < amount
            ):
                return BudgetVerdict(
                    ok=False,
                    exhausted_dimension=dimension,
                    mandate_exhausted=held.mandate_exhausted,
                )
        return BudgetVerdict(ok=True)

    def remaining(self, dimension: BudgetDimension) -> Decimal:
        """This PEP's cached view of what it can still spend. Never authoritative."""
        held = self._held.get(dimension)
        return held.lease.remaining_local if held is not None else Decimal(0)

    # -- top-up ---------------------------------------------------------------------

    def _maybe_schedule_topup(self, dimension: BudgetDimension, held: _Held) -> None:
        """Start an ACQUIRE if the lease has fallen below the low-water mark.

        Deliberately tolerant of having no event loop: `reserve()` is synchronous and may be
        called from a worker thread, and refusing to spend because nothing can be scheduled
        would be the network dependency this module exists to remove.
        """
        if held.topping_up or self._closed:
            return
        if held.lease.remaining_local > held.lease.granted * self._settings.low_water:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        held.topping_up = True
        task = loop.create_task(self._topup(dimension, held))
        held.tasks.add(task)
        task.add_done_callback(held.tasks.discard)

    async def _topup(self, dimension: BudgetDimension, held: _Held) -> None:
        try:
            await self._acquire(dimension)
        finally:
            held.topping_up = False

    async def drain(self) -> None:
        """Await every scheduled top-up. For tests and for orderly shutdown."""
        while True:
            pending = [t for held in self._held.values() for t in held.tasks if not t.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    # -- shutdown -------------------------------------------------------------------

    async def aclose(self) -> None:
        """Release every held lease — spec 04 §4.5's graceful shutdown.

        Idempotent, and the lease-state check below is what makes it so — verified by
        removing it and watching `test_aclose_is_idempotent` go red. A second `RELEASE`
        would decrement `leased` twice for budget already returned, which is TM-21's shape;
        the ledger rejects it, but not sending it is better than relying on that.

        In-flight top-ups are awaited first: releasing while an `ACQUIRE` is outstanding
        would strand the lease it is about to grant for the full TTL.
        """
        await self.drain()
        self._closed = True
        for held in self._held.values():
            if held.lease.state is LeaseState.ACTIVE:
                held.lease.state = LeaseState.RELEASED
                await self._client.release(lease_id=held.lease.id)
