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
import logging
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol

from agentiam_core.decision import BudgetVerdict
from agentiam_core.models import BudgetDimension, LeaseState
from agentiam_pep.errors import ReservationInsufficientError, UnknownMandateError
from agentiam_pep.lease import CommitOutcome, LocalLease, Reservation, commit, reserve

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Mapping
    from datetime import datetime

__all__ = [
    "LeaseGrant",
    "LeasePool",
    "LeasePoolRegistry",
    "LedgerClient",
    "PoolSettings",
    "PoolStatus",
]

logger = logging.getLogger(__name__)

#: How often a retiring lease checks whether its open reservations have settled.
_SETTLE_POLL_S = 0.005


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
        """Grant `min(requested, available)`, or None when the pool has nothing left.

        **`None` means exactly one thing: the mandate's pool is empty.** A ledger that cannot
        be reached must *raise*. The pool reports the two differently — `None` marks the
        mandate exhausted (`BUDGET_EXHAUSTED_MANDATE`, the human-approved budget is spent),
        an exception leaves a retryable `LEASE_UNAVAILABLE` — and a client that returned
        `None` for an outage told agents their budget was gone while the database was down
        (CH-01 in CI, 2026-10-16).
        """
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
    #: How long a replaced lease waits for reservations still open on it before it is
    #: released (APICTA-09, gap 38). A reservation settles when the upstream answers, so this
    #: covers the gateway's 30 s read timeout; it is also capped at the lease's own expiry
    #: minus `skew`, past which the reaper may reclaim it anyway.
    retire_wait: timedelta = timedelta(seconds=30)

    def __post_init__(self) -> None:
        """Reject a configuration that cannot be safe (spec 04 §9.2)."""
        if self.lease_size <= 0:
            raise ValueError("lease_size must be positive")
        if self.retire_wait < timedelta(0):
            raise ValueError("retire_wait must not be negative")
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
    #: The largest request `check()` has refused for want of budget and that no ACQUIRE
    #: has yet been sized for. Zero when there is none. Spec 04 §4.1a.
    wanted: Decimal = Decimal(0)
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
        before_release: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Build an empty pool. Nothing is acquired until `prime()`.

        `before_release` is awaited immediately before every `RELEASE` — see `_release`.
        `None` keeps the pre-T-052 behaviour and is what the pure unit tests use.
        """
        self._client = client
        self._settings = settings
        self._mandate_id = mandate_id
        self._now = now
        self._before_release = before_release
        self._held: dict[BudgetDimension, _Held] = {}
        #: Every lease not yet released — the held ones and any being retired — so a
        #: reservation settles against the lease that funded it (gap 38).
        self._leases: dict[uuid.UUID, LocalLease] = {}
        #: Reservations taken and not yet committed, by the lease they were taken on.
        self._open: dict[uuid.UUID, set[uuid.UUID]] = {}
        self._closed = False
        #: Leases released with reservations still open after `retire_wait`. Their late
        #: settlements reach a released lease, are rejected (spec 04 §11) and recorded as
        #: anomalies; this counts how often the bound was what ended the wait.
        self.released_unsettled = 0
        self._renewer: asyncio.Task[None] | None = None
        #: `ACQUIRE`s that could not reach the ledger. Each leaves the mandate's status as it
        #: was: an outage says nothing about whether the budget is spent.
        self.acquire_failures = 0
        #: The ledger answered that it holds no budget for this mandate (APICTA-02).
        self.mandate_unknown = False

    @property
    def mandate_id(self) -> uuid.UUID:
        """The mandate every lease in this pool is drawn from."""
        return self._mandate_id

    def _track(self, held: _Held, work: Coroutine[Any, Any, None]) -> None:
        """Run `work` as one of `held`'s tasks, so `drain()` and `aclose()` await it."""
        task = asyncio.get_running_loop().create_task(work)
        held.tasks.add(task)
        task.add_done_callback(held.tasks.discard)

    async def _settled(self, lease: LocalLease) -> None:
        """Wait, bounded, until no reservation taken on `lease` is still open."""
        loop = asyncio.get_running_loop()
        until_expiry = (lease.expires_at - self._settings.skew - self._now()).total_seconds()
        deadline = loop.time() + max(
            0.0, min(self._settings.retire_wait.total_seconds(), until_expiry)
        )
        while self._open.get(lease.id) and loop.time() < deadline:
            await asyncio.sleep(_SETTLE_POLL_S)
        still_open = len(self._open.get(lease.id, ()))
        if still_open:
            self.released_unsettled += still_open
            logger.warning(
                "releasing lease %s with %d reservation(s) still open after %.1f s; their "
                "settlements will be rejected and recorded as anomalies",
                lease.id,
                still_open,
                self._settings.retire_wait.total_seconds(),
            )

    async def _retire(self, lease: LocalLease) -> None:
        """Release a replaced lease once what it funded has settled — gap 38."""
        await self._settled(lease)
        self._leases.pop(lease.id, None)
        self._open.pop(lease.id, None)
        await self._release(lease.id)

    async def _release(self, lease_id: uuid.UUID) -> None:
        """`RELEASE` one lease, after letting anything owed against it settle first.

        **The order is the whole point, and getting it wrong is a double-spend.** `RELEASE`
        returns `granted - settled` to the pool, and `leases.settled` only moves when
        `LEDGER_COMMIT` applies a settlement. So a lease released while settlements against
        it are still queued gives back budget that was already spent — and then those
        settlements arrive at a lease that is no longer active, are rejected under spec 04
        §11 (G3, TM-21), and never apply at all.

        That is not hypothetical. T-052's CH-10 measured it after asynchronous settlement
        was introduced: **6,678 of 6,992 settlements were declined**, because a top-up
        replaces the lease and releases the old one, and every settlement still in flight
        against the old lease was orphaned by it. The visible symptom was the same one that
        motivated the settlement queue in the first place — `committed` far below what had
        been spent — arriving by a completely different route.

        The hook is awaited on *both* release paths, top-up and shutdown, because both
        retire a lease that may still owe the ledger something.
        """
        if self._before_release is not None:
            await self._before_release()
        await self._client.release(lease_id=lease_id)

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
        # **Sized for the request that is waiting, if it is larger** (spec 04 §4.1a). A fixed
        # `lease_size` meant no single request above it could ever pass: each refusal topped
        # up another `lease_size` and was refused again. The ledger grants
        # `min(requested, available)`, so asking for more cannot overdraw the pool.
        pending = self._held.get(dimension)
        requested = max(self._settings.lease_size, pending.wanted if pending else Decimal(0))
        try:
            grant = await self._client.acquire(
                mandate_id=self._mandate_id,
                dimension=dimension,
                requested=requested,
                pep_id=self._settings.pep_id,
                ttl=self._settings.ttl,
                now=now,
            )
        except asyncio.CancelledError:
            raise
        except UnknownMandateError:
            # Neither empty nor unreachable: there is no pool to lease from. Not counted as an
            # acquire failure, which would read as an outage and be retried every request.
            self.mandate_unknown = True
            return None
        except Exception as exc:
            # Unreachable, not empty: count it and leave `mandate_exhausted` alone, so the
            # request path keeps answering the retryable `LEASE_UNAVAILABLE`. Caught here
            # because every caller but `prime()` is a background task whose exception nobody
            # reads — the deployed client's raise used to escape each failed top-up unseen.
            self.acquire_failures += 1
            logger.warning("lease ACQUIRE for %s could not reach the ledger: %r", dimension, exc)
            return None
        held = self._held.get(dimension)
        if grant is None:
            # The ledger has nothing left. Distinct from "our lease is empty" — spec 09
            # names them differently because the operator's fix differs.
            if held is not None:
                held.mandate_exhausted = True
            return None

        if held is None:
            lease = LocalLease(
                id=grant.id,
                granted=grant.granted,
                remaining_local=grant.granted,
                expires_at=grant.expires_at,
                state=LeaseState.ACTIVE,
            )
            self._held[dimension] = _Held(lease=lease)
            self._leases[lease.id] = lease
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
        self._leases[held.lease.id] = held.lease
        held.mandate_exhausted = False
        # Served, or superseded: a request still too large is refused again on its retry,
        # and that refusal records it afresh — keeping it here was measured to change nothing.
        held.wanted = Decimal(0)
        # The old lease is always active here: nothing in this module moves a lease out of
        # ACTIVE except aclose(), and aclose() stops top-ups before it does. When T-038's
        # revocation gossip starts marking leases from outside, this is where an already
        # retired lease must be skipped — releasing one twice is TM-21's shape (ADR-009).
        old.state = LeaseState.RELEASED
        # Retired off this task, so the next top-up is not held up behind the wait: a request
        # still in flight on `old` settles against it, and only then is it drained and
        # released (gap 38). Releasing it here returned the in-flight money to the pool.
        self._track(held, self._retire(old))
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
        self._open.setdefault(reservation.lease_id, set()).add(reservation.id)
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
        self._open.get(reservation.lease_id, set()).discard(reservation.id)
        # Against the lease that funded it, which is not the held one if a top-up swapped it
        # meanwhile (gap 38). Charged to the held lease instead, the settlement was clamped
        # once that lease was spent, and the money it recorded was lost.
        lease = self._leases.get(reservation.lease_id)
        if lease is None:
            # Retired past `retire_wait`. The ledger rejects this and records an anomaly
            # (spec 04 §11) — the honest outcome — rather than it landing on a lease that
            # never funded it.
            return CommitOutcome(
                reservation_id=reservation.id,
                lease_id=reservation.lease_id,
                amount=actual,
                escalated=False,
            )
        return commit(lease, reservation, actual, now=self._now(), skew=self._settings.skew)

    def check(self, requested: Mapping[BudgetDimension, Decimal]) -> BudgetVerdict:
        """`BudgetOracle` — whether the held leases cover `requested`. Reserves nothing.

        A `RequestContext` carries every dimension, most of them zero (ADR-007), so a zero
        request against a dimension this PEP holds no lease for is not a refusal.

        Reserves nothing, but it *does* schedule a top-up when it is about to refuse — see
        the comment on that branch. A dimension holding no lease at all is not covered: there
        is no `_Held` to top up from, and re-priming from a read is a larger change than this
        one. That case is a PEP that never primed the dimension, not one that ran dry.
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
                # **A refusal is the last chance to ask for more, so it asks.** Top-ups are
                # otherwise scheduled only from `reserve()`, which runs only after the
                # pipeline has allowed — and it cannot allow while this verdict is `ok=False`.
                # That is a closed loop: a lease that reaches empty can never be refilled,
                # because the only thing that refills it is the spend path it is now
                # blocking. The low-water mark normally tops up long before empty, so the
                # loop only closes when a top-up has already *failed* — which is exactly
                # CH-1: Postgres goes away, the held lease drains, and every later ACQUIRE
                # would have been scheduled by a `reserve()` that never runs again. The PEP
                # stayed dead after the database came back. Measured, then fixed here.
                #
                # A request larger than what is left is remembered, so the top-up is sized
                # to cover it (spec 04 §4.1a) — not only refilled to `lease_size`.
                if lease.remaining_local < amount:
                    held.wanted = max(held.wanted, amount)
                    self._schedule_topup(dimension, held)
                self._maybe_schedule_topup(dimension, held)
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

    # -- renewal ---------------------------------------------------------------------

    async def start(self) -> None:
        """Begin renewing held leases in the background. Idempotent.

        **Why a timer and not just the request path.** Every top-up was request-triggered:
        `reserve()` and `check()` call `_maybe_schedule_topup`, and the scheduling is
        asynchronous, so the request that *notices* an aged-out lease is still refused —
        `check()` has already decided by the time the replacement lands. On a PEP that is
        spending steadily that is invisible, because the low-water mark fires long before
        anything expires. On an idle one it is the whole experience: the deployed PEP primes a
        single lease at boot with a 60 s TTL, and the first payment after that minute gets
        `LEASE_UNAVAILABLE`, with the one after it succeeding.

        Measured on the demo stack, 80 s after `up --wait`, same tokens both times:

            payer settles a small invoice           429 LEASE_UNAVAILABLE
            settlement agent pays within its slice  429 LEASE_UNAVAILABLE
            -- immediately again --
            payer settles a small invoice           200 OK
            settlement agent pays within its slice  200 OK

        ADR-049's item 24 fix is what makes the second attempt work — before it the pool
        never scheduled anything for a lease that expired *unspent*, and the PEP stayed dead.
        This closes the remaining half: nothing should have to be refused to trigger the
        renewal that would have prevented it.

        Renewal replaces the lease through `_acquire`, the same path a top-up already takes,
        so ADR-049's settle-before-release ordering is unchanged — `_release` still awaits
        `before_release` first. Nothing here is a new route to the ledger.
        """
        if self._renewer is None and not self._closed:
            self._renewer = asyncio.create_task(self._renew_forever())

    @property
    def _renew_margin(self) -> timedelta:
        """How long before expiry a lease is replaced.

        Half the TTL, which is strictly greater than `skew` because `PoolSettings` refuses a
        `ttl <= 2 * skew`. That matters: renewing *inside* the skew margin would replace the
        lease only after `check()` had already begun refusing against it, which is the
        behaviour this exists to remove.
        """
        return self._settings.ttl / 2

    async def _renew_forever(self) -> None:
        """Sweep every `ttl / 4` — spec 04 §4.6's cadence for `REAP`, for the same reason.

        A period shorter than the margin guarantees at least one sweep sees a lease past its
        half-life while it is still usable. The sweep schedules and never awaits, so a top-up
        that cannot reach the ledger cannot stop it: an unreachable ledger is the fail-closed
        case the request path already handles per request, and a renewer that died on one
        outage would leave the PEP unable to renew until a restart.

        Cancellation is how this ends; `_closed` is re-checked after the sleep because
        `aclose()` can set it while the sweep is waiting.
        """
        interval = self._settings.ttl.total_seconds() / 4
        while True:
            await asyncio.sleep(interval)
            if self._closed:
                return
            for dimension, held in list(self._held.items()):
                if self._renew_due(held):
                    self._schedule_topup(dimension, held)

    def _renew_due(self, held: _Held) -> bool:
        """Whether `held`'s lease is past its half-life, or is no longer active at all."""
        lease = held.lease
        return (
            lease.state is not LeaseState.ACTIVE
            or self._now() >= lease.expires_at - self._renew_margin
        )

    # -- top-up ---------------------------------------------------------------------

    def _maybe_schedule_topup(self, dimension: BudgetDimension, held: _Held) -> None:
        """Start an ACQUIRE if the held lease is running out — of budget **or** of time.

        Deliberately tolerant of having no event loop: `reserve()` is synchronous and may be
        called from a worker thread, and refusing to spend because nothing can be scheduled
        would be the network dependency this module exists to remove.

        **Both conditions are needed, and only the first used to be here.** A lease leaves
        service two ways: it drains, or it ages out. The low-water test answers *"has this
        lease drained?"*, and a lease that expires **unspent** answers "no" — `remaining_local`
        is still the full grant. So no ACQUIRE was ever scheduled, `check()` refused the
        expired lease on every later request, and the PEP stopped authorizing anything that
        costs money until it was restarted.

        Not a corner case: the deployed PEP primes one lease at boot with a 60 s TTL, so any
        stack left idle for a minute after start-up fell into it, permanently. Found in the
        second manual pass (TODO item 24) — `make demo-seed` runs inside the first TTL, which
        is why the demo looked healthy. The comment on `check()`'s refusal branch describes
        this closed loop and the fix there only ever covered the drained half.

        Replacing an expired lease is safe: `_acquire` RELEASEs the old one, and the ledger's
        `release()` is a no-op for a lease already in a terminal state (spec 04 §3), which an
        expired lease is once `reap()` has run.
        """
        if held.topping_up or self._closed:
            return
        lease = held.lease
        drained = lease.remaining_local <= lease.granted * self._settings.low_water
        # The same "no longer usable" test `check()` refuses on, so the two cannot disagree
        # about whether a lease is still worth holding.
        stale = (
            lease.state is not LeaseState.ACTIVE
            or self._now() >= lease.expires_at - self._settings.skew
        )
        if not (drained or stale):
            return
        self._schedule_topup(dimension, held)

    def _schedule_topup(self, dimension: BudgetDimension, held: _Held) -> None:
        """Start one ACQUIRE, unless one is already running. The predicate is the caller's.

        Split out so the renewal sweep and the request path share one piece of task
        bookkeeping — `topping_up` is what keeps them from racing each other into two
        concurrent ACQUIREs for one dimension, and two copies of it would not.
        """
        if held.topping_up or self._closed:
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

    async def _stop_renewer(self) -> None:
        """Cancel the renewal sweep and wait for it to finish.

        Before `drain()`, not after: a sweep that fires mid-shutdown would schedule an
        ACQUIRE for a lease `aclose()` is about to RELEASE, stranding the new one for the
        full TTL — the same failure `aclose()`'s own docstring gives for not draining.
        """
        renewer, self._renewer = self._renewer, None
        if renewer is None:
            return
        renewer.cancel()
        with suppress(asyncio.CancelledError):
            await renewer

    async def aclose(self) -> None:
        """Release every held lease — spec 04 §4.5's graceful shutdown.

        Idempotent, and the lease-state check below is what makes it so — verified by
        removing it and watching `test_aclose_is_idempotent` go red. A second `RELEASE`
        would decrement `leased` twice for budget already returned, which is TM-21's shape;
        the ledger rejects it, but not sending it is better than relying on that.

        In-flight top-ups are awaited first: releasing while an `ACQUIRE` is outstanding
        would strand the lease it is about to grant for the full TTL.
        """
        await self._stop_renewer()
        await self.drain()
        self._closed = True
        for held in self._held.values():
            if held.lease.state is LeaseState.ACTIVE:
                # A request still in flight settles first, for the reason a top-up waits.
                await self._settled(held.lease)
                held.lease.state = LeaseState.RELEASED
                self._leases.pop(held.lease.id, None)
                await self._release(held.lease.id)


class PoolStatus(Enum):
    """Why `LeasePoolRegistry.lookup` has no pool to hand back."""

    #: Being created, or the ledger could not be reached to create it. Retryable.
    PENDING = "pending"
    #: The ledger holds no budget for this mandate. Not retried until `unknown_ttl` passes.
    UNKNOWN = "unknown"
    #: The ledger holds a budget and has nothing left in it. Not retried until `unknown_ttl`.
    EXHAUSTED = "exhausted"


class LeasePoolRegistry:
    """A `LeasePool` per mandate, created off the request path — APICTA-02, spec 04 §4.8.

    A PEP used to hold exactly one pool, bound to `AGENTIAM_PEP_MANDATE_ID` at boot, and the
    pipeline never compared a token's mandate with it: a token minted under any mandate spent
    from that one pool, so the ceiling a human set on one task was enforced against another
    task's money. The registry gives each mandate its own pool and the pipeline asks it for
    the token's.

    **The pools are unchanged.** Each is an ordinary `LeasePool` with its own leases, renewal
    and `before_release` hook (ADR-049's settle-before-release holds per pool). The registry
    only decides which one a request draws from.

    **`lookup()` is the hot path and never awaits.** A mandate it has not seen is created in a
    background task and the request is told to retry (`PENDING` → `LEASE_UNAVAILABLE`); the
    next one finds the pool. That is the same shape as a top-up — a refusal that schedules what
    would have prevented it — and it means a PEP that missed the control plane's
    `mandate:created` event (spec 04 §4.8) still converges: the token itself is the signal, and
    it is signed by the root key, so only a minted mandate can make a PEP ask.

    **What the first `ACQUIRE` said decides what happens next.** A ledger that could not be
    reached leaves nothing behind, so the next request asks again. A ledger with no budget row
    (`UnknownMandateError`) or an empty one is remembered for `unknown_ttl`, so a stream of
    requests for a mandate nobody funded is not a stream of `ACQUIRE`s; after it, the ledger is
    asked again, because a mandate can be funded or refunded later.
    """

    def __init__(
        self,
        client: LedgerClient | None,
        settings: PoolSettings | None,
        *,
        now: Callable[[], datetime],
        before_release: Callable[[], Awaitable[None]] | None = None,
        unknown_ttl: timedelta = timedelta(seconds=30),
        dimension: BudgetDimension = BudgetDimension.SPEND_BDT,
    ) -> None:
        """A registry that creates pools with `client` and `settings`.

        `client=None` makes a fixed registry: it serves the pools added to it and creates
        none — `fixed()` builds one around a single pool.
        """
        self._client = client
        self._settings = settings
        self._now = now
        self._before_release = before_release
        self._unknown_ttl = unknown_ttl
        self._dimension = dimension
        self._pools: dict[uuid.UUID, LeasePool] = {}
        self._creating: dict[uuid.UUID, asyncio.Task[LeasePool | None]] = {}
        self._refused_until: dict[uuid.UUID, tuple[PoolStatus, datetime]] = {}
        self._started = False
        self._closed = False

    @classmethod
    def fixed(cls, pool: LeasePool) -> LeasePoolRegistry:
        """A registry serving exactly `pool` — the pre-APICTA-02 single-mandate PEP."""
        registry = cls(None, None, now=pool._now)
        registry._pools[pool.mandate_id] = pool
        return registry

    @property
    def pools(self) -> Mapping[uuid.UUID, LeasePool]:
        """Every pool this PEP holds, by mandate. A copy: callers cannot add to it."""
        return dict(self._pools)

    def get(self, mandate_id: uuid.UUID) -> LeasePool | None:
        """The pool for `mandate_id`, if this PEP holds one. Never creates."""
        return self._pools.get(mandate_id)

    def status(self) -> dict[str, dict[str, Any]]:
        """Each pool's state and each mandate being refused, for `/readyz` (APICTA-02 Part C).

        A PEP serving many mandates has to be able to say which, and how much each has left
        on this PEP; otherwise "is mandate X being served here" has no answer short of
        sending it a request.
        """
        pools: dict[str, Any] = {}
        for mandate_id, pool in self._pools.items():
            held = pool._held.get(self._dimension)
            pools[str(mandate_id)] = {
                f"{self._dimension.value}_remaining": str(pool.remaining(self._dimension)),
                "lease_state": held.lease.state.value if held is not None else "none",
                "mandate_exhausted": held.mandate_exhausted if held is not None else False,
            }
        now = self._now()
        refused: dict[str, Any] = {
            str(mandate_id): status.value
            for mandate_id, (status, until) in self._refused_until.items()
            if now < until
        }
        return {"pools": pools, "refused": refused}

    def lookup(self, mandate_id: uuid.UUID) -> LeasePool | PoolStatus:
        """The pool for `mandate_id`, or why there is none — scheduling one if it may exist.

        Synchronous and network-free (`PLAN.md` §3 design rule 1).
        """
        pool = self._pools.get(mandate_id)
        if pool is not None:
            return pool
        if self._client is None or self._closed:
            return PoolStatus.UNKNOWN
        refused = self._refused_until.get(mandate_id)
        if refused is not None and self._now() < refused[1]:
            return refused[0]
        self._task_for(mandate_id)
        return PoolStatus.PENDING

    def note_created(self, mandate_id: uuid.UUID) -> None:
        """Record that `mandate_id` exists now. Acquires nothing.

        What the control plane's `mandate:created` announcement is allowed to do. It used to
        call `ensure()`, which primes a pool with a real `ACQUIRE` — so every PEP on the
        channel took `lease_size` from every mandate the moment it was minted, whether or
        not it would ever serve one. On a mandate smaller than the PEPs' combined lease size
        that emptied the ledger before the first request arrived (measured 1000 / 500 / 0 of
        1500 across three PEPs), and the PEP that actually served the task was refused
        `BUDGET_EXHAUSTED_MANDATE` — *the human-approved budget is spent* — while every taka
        of it sat unspent on idle PEPs that renewed those leases indefinitely. ADR-081.

        So the announcement only forgets a cached refusal: a PEP that asked before the budget
        row existed holds `UNKNOWN` for `unknown_ttl`, and this is the control plane saying it
        exists now. The lease itself waits for a token, which is the signal (ADR-078).
        """
        self._refused_until.pop(mandate_id, None)

    async def ensure(self, mandate_id: uuid.UUID) -> LeasePool | None:
        """Create, prime and start the pool for `mandate_id`; `None` if it could not be.

        Single-flight per mandate: concurrent callers share one creation and one `ACQUIRE`.
        """
        pool = self._pools.get(mandate_id)
        if pool is not None or self._client is None or self._closed:
            return pool
        task = self._task_for(mandate_id)
        if task is None:
            return None
        return await asyncio.shield(task)

    def _task_for(self, mandate_id: uuid.UUID) -> asyncio.Task[LeasePool | None] | None:
        task = self._creating.get(mandate_id)
        if task is not None:
            return task
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        task = loop.create_task(self._create(mandate_id))
        self._creating[mandate_id] = task
        task.add_done_callback(lambda _done: self._creating.pop(mandate_id, None))
        return task

    async def _create(self, mandate_id: uuid.UUID) -> LeasePool | None:
        assert self._client is not None and self._settings is not None  # noqa: S101
        pool = LeasePool(
            self._client,
            self._settings,
            mandate_id=mandate_id,
            now=self._now,
            before_release=self._before_release,
        )
        failures = pool.acquire_failures
        try:
            primed = await pool.prime(self._dimension)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A creation scheduled from `lookup()` has nobody awaiting it, so an exception
            # here would die unread. Logged with its cause and treated as unreachable: the
            # next request asks again.
            logger.warning(
                "could not create a lease pool for mandate %s", mandate_id, exc_info=True
            )
            return None
        if primed:
            self._refused_until.pop(mandate_id, None)
            self._pools[mandate_id] = pool
            if self._started:
                await pool.start()
            logger.info("serving mandate %s", mandate_id)
            return pool
        if pool.acquire_failures > failures:
            # Unreachable: nothing is remembered, and the next request asks again.
            return None
        status = PoolStatus.UNKNOWN if pool.mandate_unknown else PoolStatus.EXHAUSTED
        self._refused_until[mandate_id] = (status, self._now() + self._unknown_ttl)
        logger.info("mandate %s has no budget to lease: %s", mandate_id, status.value)
        return None

    async def start(self) -> None:
        """Start renewal on every pool held now, and on every pool created later."""
        self._started = True
        for pool in list(self._pools.values()):
            await pool.start()

    async def drain(self) -> None:
        """Await pool creation and every pool's top-ups. For tests and orderly shutdown."""
        while True:
            creating = [t for t in self._creating.values() if not t.done()]
            if creating:
                await asyncio.gather(*creating, return_exceptions=True)
                continue
            for pool in list(self._pools.values()):
                await pool.drain()
            if not any(not t.done() for t in self._creating.values()):
                return

    async def aclose(self) -> None:
        """Release every pool's leases — spec 04 §4.5, once per mandate.

        Creation still in flight is awaited first, for the reason `LeasePool.aclose` awaits
        its top-ups: a lease granted after shutdown began would be stranded for its TTL.
        """
        await self.drain()
        self._closed = True
        for pool in list(self._pools.values()):
            await pool.aclose()
