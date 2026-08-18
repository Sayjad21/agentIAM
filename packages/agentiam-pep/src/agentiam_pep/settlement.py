"""Getting a settled reservation to `LEDGER_COMMIT` — spec 04 §4.3, §4.4.

**This closes a hole, and the hole is worth describing because nothing caught it for nine
tickets.** `Pipeline.settle()` called `LeasePool.commit()`, which is `agentiam_pep.lease.
commit` — pure, local, in-memory. It returns a `CommitOutcome` whose own docstring says it
is *"for the caller to enqueue as LEDGER_COMMIT"*, and the caller threw it away. Grepping
the tree for `ledger_commit` found the function, its unit tests and its race tests, and no
production caller at all.

So `budgets.committed` never moved. `RELEASE` returns `granted - settled` to the pool,
`leases.settled` is only ever written by `LEDGER_COMMIT`, and therefore a PEP that spent
most of its lease handed the **whole grant** back on shutdown — the same budget spendable
twice. T-052's CH-10 measured it: 992 requests spent 4,960, and the ledger recorded
`committed = 0`.

The invariant checker could not see it, and that is the instructive part. It compares
`committed` against the sum of settled reservations (0 == 0) and `leased` against the
outstanding total of active leases. Both held. The books were perfectly consistent about a
number that had stopped describing reality — which is the one class of failure a checker
over a single system's own records is structurally unable to catch.

**Why a queue rather than an await.** `settle()` runs on the request path, after the
upstream call and before the response is returned, and `LEDGER_COMMIT` is a locking database
round trip. Awaiting it there would put the ledger back in the tool-call critical path,
which is the single thing `pool.py` exists to prevent. So `enqueue()` is synchronous, takes
a slot on a bounded deque and returns; a background task drains it.

**Why retries are unbounded, unlike the audit emitter's.** `LEDGER_COMMIT` is idempotent on
`reservation_id` (G4, spec 04 §10) — a replay finds the reservation row already there and
applies nothing. A retry is therefore free and can never double-count, so there is no reason
to give up on one while the ledger might still come back. The audit emitter's `max_retries`
exists because a *poison record* can wedge its queue forever; a poison settlement cannot,
because the sink resolves the only permanent failure — a lease that is no longer active —
itself, by recording a reconciliation anomaly and returning `False`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import uuid
    from collections.abc import Callable, Sequence
    from datetime import datetime
    from decimal import Decimal
    from types import TracebackType

    from agentiam_pep.lease import CommitOutcome

logger = logging.getLogger(__name__)

__all__ = [
    "PendingSettlement",
    "SettlementQueue",
    "SettlementSettings",
    "SettlementSink",
]

#: The restart that a settlement backlog makes dangerous is `RELEASE`, which returns
#: `granted - settled`. Anything still queued when a lease is released is budget the pool
#: gets back after it was spent, so every shutdown path must drain before it releases.


class SettlementSink(Protocol):
    """Where a settled reservation goes. `agentiam_controlplane` supplies the real one.

    A `Protocol`, so nothing in `agentiam-pep` imports the control plane and nothing in the
    control plane imports the PEP — the same arrangement `RecordSink` and `EscalationSink`
    already use.
    """

    async def commit(self, batch: Sequence[PendingSettlement]) -> Sequence[bool]:
        """Apply a batch of settlements, **all against the same lease**, to the ledger.

        A batch rather than one at a time because `LEDGER_COMMIT` takes `FOR UPDATE` on the
        lease row and then on the *shared* budget row, so every settlement serialises every
        PEP leasing from that mandate against every other. T-052's CH-10 measured the
        consequence; ADR-049 deferred the fix and T-053 is it.

        The queue guarantees one lease per batch, so the sink can take one pair of locks.

        Returns:
            One verdict per item, in order. Each carries the retry decision, and that is
            the whole contract:

            * `True` — the ledger applied it.
            * `False` — the ledger **definitively declined** it and a retry would be
              pointless. A replayed `reservation_id`, an amount that clamped to zero, or a
              lease that is no longer active all land here; the last is recorded as a
              reconciliation anomaly by the ledger before it returns.

        Raises:
            Exception: The ledger could not be reached. The queue keeps the whole batch and
                retries indefinitely, which is safe because `LEDGER_COMMIT` dedups on
                `reservation_id` — a replayed batch applies only what it had not already.
        """
        ...


@dataclass(frozen=True, slots=True)
class SettlementSettings:
    """Buffer size and drain cadence."""

    #: Generous, because a full queue loses accounting rather than refusing a request: by
    #: the time `settle()` runs the tool call has already happened, so there is nothing
    #: left to deny. Back-pressure belongs upstream — a ledger this far behind will have
    #: saturated the audit emitter first, and *that* one denies (ADR-026).
    capacity: int = 8192
    flush_interval_s: float = 0.25
    batch_max: int = 64
    #: Pause after a failed drain, so a dead ledger is retried at a sane rate rather than
    #: spun on. The emitter learned this the hard way; see its `flush()` docstring.
    retry_pause_s: float = 0.5

    def __post_init__(self) -> None:
        """Reject a configuration that cannot work."""
        if self.capacity <= 0:
            raise ValueError("capacity must be positive")
        if self.batch_max <= 0:
            raise ValueError("batch_max must be positive")
        if self.flush_interval_s <= 0:
            raise ValueError("flush_interval_s must be positive")
        if self.retry_pause_s < 0:
            raise ValueError("retry_pause_s must not be negative")


@dataclass(frozen=True, slots=True)
class PendingSettlement:
    """One `CommitOutcome` waiting to reach the ledger, with the clock reading it was made at."""

    lease_id: uuid.UUID
    reservation_id: uuid.UUID
    amount: Decimal
    now: datetime


class SettlementQueue:
    """A bounded deque of settlements and a task that drains it into the ledger."""

    def __init__(
        self,
        sink: SettlementSink,
        settings: SettlementSettings | None = None,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        """Build a queue. Nothing drains until `start()`."""
        self._sink = sink
        self._settings = settings or SettlementSettings()
        self._now = now
        self._pending: deque[PendingSettlement] = deque()
        self._drain: asyncio.Task[None] | None = None
        # The drain task and an explicit `flush()` both walk the head of the deque, and
        # without this they race: both read `_pending[0]`, both `popleft()`, and the second
        # raises `IndexError` on an empty deque — or, worse, pops an item the first had not
        # finished with. `DecisionEmitter` carries the identical lock for the identical
        # reason, found the identical way (wiring it into a slice with a background drain).
        self._draining = asyncio.Lock()
        self._closed = False
        self.applied = 0
        self.declined = 0
        self.dropped = 0
        self.failed_attempts = 0

    # -- lifecycle ------------------------------------------------------------------

    async def start(self) -> None:
        """Begin draining. Idempotent."""
        if self._drain is None:
            self._drain = asyncio.get_running_loop().create_task(self._run())

    async def aclose(self) -> None:
        """Drain everything queued, then stop. Idempotent.

        `drain()` rather than a single `flush()`, and the difference is not cosmetic. A
        flush moves at most `batch_max` settlements, and under load the queue runs a backlog
        — T-052's CH-10 measured thousands pending when its traffic stopped. Closing on one
        batch would have discarded the rest, which is the very loss this module exists to
        prevent, and it would land at exactly the wrong moment: `RELEASE` runs next, and it
        returns `granted - settled`.

        `drain()` still gives up if the ledger stops answering, because a shutdown must end.
        What is left over then is reclaimed by `REAP` at the lease's expiry — the protocol's
        own answer to a PEP that stopped mid-settlement.
        """
        if self._closed:
            return
        self._closed = True
        await self.drain()
        if self._drain is not None:
            self._drain.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._drain
            self._drain = None

    async def __aenter__(self) -> SettlementQueue:
        """Start draining."""
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Drain and stop."""
        await self.aclose()

    # -- the hot path ---------------------------------------------------------------

    @property
    def pending(self) -> int:
        """How many settlements have not reached the ledger yet."""
        return len(self._pending)

    def enqueue(self, outcome: CommitOutcome, *, now: datetime | None = None) -> None:
        """Queue one settled reservation. Synchronous, and never raises.

        Never raising is deliberate and is the opposite of `DecisionEmitter.emit()`. That one
        refuses the request when its buffer is full, because the request has not happened
        yet. This runs *after* the upstream call, so there is no request left to refuse and
        raising here would turn a bookkeeping backlog into a 500 for work that succeeded.
        A dropped settlement is counted and logged at `error`, never silent.
        """
        if self._closed:
            self.dropped += 1
            logger.error(
                "settlement queue is closed; reservation %s (%s) will not reach the ledger",
                outcome.reservation_id,
                outcome.amount,
            )
            return
        if len(self._pending) >= self._settings.capacity:
            self.dropped += 1
            logger.error(
                "settlement queue is full (%d); reservation %s (%s) dropped and the ledger "
                "will over-report available budget by that amount until the lease expires",
                self._settings.capacity,
                outcome.reservation_id,
                outcome.amount,
            )
            return
        stamped = now if now is not None else (self._now() if self._now is not None else None)
        if stamped is None:  # pragma: no cover - guarded by construction
            raise ValueError("SettlementQueue needs a clock: pass `now=` to it or to enqueue()")
        self._pending.append(
            PendingSettlement(
                lease_id=outcome.lease_id,
                reservation_id=outcome.reservation_id,
                amount=outcome.amount,
                now=stamped,
            )
        )

    # -- draining -------------------------------------------------------------------

    async def flush(self) -> None:
        """Push one batch to the ledger, one attempt.

        A batch the ledger could not be told about is **left at the head** for the next
        tick, so ordering within a lease is preserved and nothing is skipped past. Returns
        as soon as it fails rather than hammering a sink that has had no time to recover.

        Serialized against the drain task: only one caller walks the deque at a time.
        """
        async with self._draining:
            await self._flush_locked()

    async def drain(self) -> None:
        """Flush repeatedly until the queue is empty or the ledger stops accepting.

        `flush()` moves one batch; this moves all of them. Stops on the first batch that
        makes no progress, so an unreachable ledger ends the drain instead of spinning —
        the caller (shutdown, or a test taking a reading) gets control back either way, and
        `pending` says what is left.
        """
        while self._pending:
            before = len(self._pending)
            await self.flush()
            if len(self._pending) >= before:
                return

    def _next_batch(self) -> list[PendingSettlement]:
        """The longest run of queued settlements sharing one lease, up to `batch_max`.

        **Consecutive, not grouped.** Scanning the whole deque for every item belonging to
        a lease would reorder settlements, and order within a lease is what makes the
        cumulative `outstanding` clamp mean anything. In practice a PEP holds one lease per
        dimension at a time, so the run is nearly always the whole queue anyway — and when
        a top-up has just swapped the lease, the boundary falls exactly where it should.
        """
        if not self._pending:
            return []
        lease_id = self._pending[0].lease_id
        batch: list[PendingSettlement] = []
        for item in self._pending:
            if item.lease_id != lease_id or len(batch) >= self._settings.batch_max:
                break
            batch.append(item)
        return batch

    async def _flush_locked(self) -> None:
        batch = self._next_batch()
        if not batch:
            return
        try:
            verdicts = await self._sink.commit(batch)
        except Exception:
            # The ledger is unreachable. Keep the whole batch — `LEDGER_COMMIT` dedups on
            # `reservation_id`, so replaying it later cannot double-count, and a partially
            # applied batch replays to exactly the part that did not apply.
            self.failed_attempts += 1
            logger.warning(
                "settlement batch of %d for lease %s could not reach the ledger; will retry",
                len(batch),
                batch[0].lease_id,
                exc_info=True,
            )
            return
        # Popped only after the sink has answered, and only under the lock, so nothing can
        # be lost if this coroutine is cancelled mid-commit — a replay is free.
        for applied in verdicts:
            self._pending.popleft()
            if applied:
                self.applied += 1
            else:
                self.declined += 1

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._settings.flush_interval_s)
            failed_before = self.failed_attempts
            await self.flush()
            if self.failed_attempts != failed_before:
                await asyncio.sleep(self._settings.retry_pause_s)
