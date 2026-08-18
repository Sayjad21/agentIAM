"""The sink that connects a settled reservation to `LEDGER_COMMIT` — spec 04 §4.4.

Structurally satisfies `agentiam_pep.settlement.SettlementSink` — a `Protocol`, so nothing
here imports from `agentiam_pep`, the same arrangement `audit_sink.py` and
`escalation_sink.py` already use. It lives in the control plane because it owns a database
session, and the PEP's contract is that its hot path never touches one.

**The return value is a retry decision, and getting it right is the whole job.**
`ledger_commit` distinguishes three outcomes and this maps them onto the two the queue
understands:

* applied, or a no-op (a replayed `reservation_id`, or an amount that clamped to zero) —
  the ledger has dealt with it. Nothing to retry.
* `LeaseNotActiveError` — a settlement arriving against a lease that was already released,
  reaped or revoked (G3, spec 04 §11, TM-21). `ledger_commit` has already written a
  `ReconciliationAnomalyRow` for it, so the divergence is on the record; retrying would
  write a second anomaly for the same event and would never succeed. Declined, not raised.
* anything else — the database could not be reached. Raised, so the queue keeps the item
  and tries again. Safe to replay: `LEDGER_COMMIT` dedups on `reservation_id` (G4).

That last distinction is the one that matters. Treating an unreachable ledger as a
permanent refusal is exactly the bug the audit emitter has, where a transient outage is
indistinguishable from a poison batch and the records are discarded.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

from agentiam_controlplane.db.ledger import ledger_commit_batch
from agentiam_controlplane.errors import LeaseNotActiveError

if TYPE_CHECKING:
    import uuid
    from collections.abc import Sequence
    from datetime import datetime
    from decimal import Decimal

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

__all__ = ["LedgerSettlementSink", "PendingSettlement"]


class PendingSettlement(Protocol):
    """What this sink reads off each queued settlement.

    Declared structurally rather than imported: `agentiam_pep.settlement` owns the concrete
    dataclass, and the whole point of these sink protocols is that neither package imports
    the other. Anything carrying these four attributes satisfies it, which is exactly what
    the PEP's `PendingSettlement` does.
    """

    @property
    def lease_id(self) -> uuid.UUID:
        """The lease this settlement applies to."""
        ...

    @property
    def reservation_id(self) -> uuid.UUID:
        """The idempotency key `LEDGER_COMMIT` dedups on (G4, spec 04 §10)."""
        ...

    @property
    def amount(self) -> Decimal:
        """What the PEP reported spending. Clamped by the ledger, never trusted (G2)."""
        ...

    @property
    def now(self) -> datetime:
        """The clock reading the settlement was made at."""
        ...


class LedgerSettlementSink:
    """Applies a batch of settlements per call, on its own session."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Hold the factory; a session is taken per batch and released after."""
        self._session_factory = session_factory
        self.rejected_leases = 0

    async def commit(self, batch: Sequence[PendingSettlement]) -> Sequence[bool]:
        """Apply a batch of settlements, all against one lease, in a single transaction.

        One `FOR UPDATE` pair for the whole batch instead of one per item — see
        `ledger_commit_batch` for why that matters and what CH-10 measured without it.

        Returns:
            One verdict per item, in order: `True` if the ledger applied it, `False` if it
            declined for good — a replay, a zero clamp, or a lease no longer active.

        Raises:
            Exception: The database could not be reached. The queue keeps the batch.
        """
        if not batch:
            return []
        lease_id = batch[0].lease_id
        async with self._session_factory() as session:
            try:
                return await ledger_commit_batch(
                    session,
                    lease_id=lease_id,
                    items=[(item.reservation_id, item.amount, item.now) for item in batch],
                )
            except LeaseNotActiveError:
                # Already recorded as reconciliation anomalies by `ledger_commit_batch`,
                # one per item. Retrying would add a second set and never apply.
                self.rejected_leases += 1
                logger.warning(
                    "%d settlement(s) rejected: lease %s is no longer active (spec 04 §11)",
                    len(batch),
                    lease_id,
                )
                return [False] * len(batch)
