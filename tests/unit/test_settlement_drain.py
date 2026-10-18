"""`SettlementQueue.drain()` while the PEP keeps spending — gap 38, APICTA-09.

`LeasePool._release` awaits `drain()` so that every settlement owed against a lease reaches the
ledger before `RELEASE` returns `granted - settled` (ADR-049). `drain()` used to judge progress
by queue *length*: a flush that did not shorten the queue meant the ledger had stopped
answering. But the PEP does not stop serving while a top-up drains. If it enqueues as many
settlements during one flush as the flush moved, the queue does not shorten, `drain()` returns,
and the old lease is released with its settlements still queued — they are then rejected, and
the money they record goes back to the pool. Measured by `make bench-compare`: ৳4,920 recorded
of ৳9,000 spent.

The sink here is a fake whose `commit` awaits, and the "PEP" enqueues new-lease settlements
during that await, which is the interleaving the deployed event loop produces.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from agentiam_pep.lease import CommitOutcome
from agentiam_pep.settlement import SettlementQueue, SettlementSettings

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from agentiam_pep.settlement import PendingSettlement

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
OLD = uuid.UUID("00000000-0000-4000-8000-0000000000a1")
NEW = uuid.UUID("00000000-0000-4000-8000-0000000000b2")


def outcome(lease_id: uuid.UUID) -> CommitOutcome:
    return CommitOutcome(
        reservation_id=uuid.uuid4(), lease_id=lease_id, amount=Decimal(10), escalated=False
    )


class BusySink:
    """Applies every batch; during each commit the PEP enqueues `per_flush` more settlements."""

    def __init__(self, per_flush: int, *, fail: bool = False) -> None:
        """Enqueue `per_flush` new-lease settlements during every commit."""
        self.per_flush = per_flush
        self.fail = fail
        self.committed: list[uuid.UUID] = []
        self.on_commit: Callable[[], None] = lambda: None

    async def commit(self, batch: Sequence[PendingSettlement]) -> Sequence[bool]:
        self.on_commit()
        if self.fail:
            raise ConnectionRefusedError("ledger down")
        self.committed.extend(item.lease_id for item in batch)
        return [True] * len(batch)


def a_queue(sink: BusySink) -> SettlementQueue:
    queue = SettlementQueue(sink, SettlementSettings(batch_max=64), now=lambda: NOW)

    def spend() -> None:
        for _ in range(sink.per_flush):
            queue.enqueue(outcome(NEW))

    sink.on_commit = spend
    return queue


class TestDrainWhileSpending:
    async def test_every_old_lease_settlement_reaches_the_ledger(self) -> None:
        """The defect: 200 owed, a busy PEP, and drain() used to return after one batch."""
        sink = BusySink(per_flush=64)
        queue = a_queue(sink)
        for _ in range(200):
            queue.enqueue(outcome(OLD))

        await queue.drain()

        assert sink.committed.count(OLD) == 200
        assert all(item.lease_id != OLD for item in queue._pending)

    async def test_it_does_not_chase_settlements_enqueued_after_it_began(self) -> None:
        """A PEP that never stops spending must not keep a top-up's release waiting forever."""
        sink = BusySink(per_flush=64)
        queue = a_queue(sink)
        for _ in range(130):
            queue.enqueue(outcome(OLD))

        await queue.drain()

        # 130 owed takes three batches of at most 64; each adds 64 more — those can wait.
        assert sink.committed.count(OLD) == 130
        assert queue.pending > 0

    async def test_a_quiet_pep_still_drains_to_empty(self) -> None:
        sink = BusySink(per_flush=0)
        queue = a_queue(sink)
        for _ in range(150):
            queue.enqueue(outcome(OLD))

        await queue.drain()

        assert queue.pending == 0

    async def test_an_unreachable_ledger_still_ends_the_drain(self) -> None:
        """Shutdown must end: no progress is still a reason to stop."""
        sink = BusySink(per_flush=0, fail=True)
        queue = a_queue(sink)
        for _ in range(10):
            queue.enqueue(outcome(OLD))

        await queue.drain()

        assert queue.pending == 10
        assert queue.failed_attempts == 1

    @pytest.mark.parametrize("per_flush", [1, 63, 64, 200])
    async def test_whatever_the_enqueue_rate(self, per_flush: int) -> None:
        sink = BusySink(per_flush=per_flush)
        queue = a_queue(sink)
        for _ in range(500):
            queue.enqueue(outcome(OLD))

        await queue.drain()

        assert sink.committed.count(OLD) == 500
