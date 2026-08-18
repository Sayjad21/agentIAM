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
from typing import TYPE_CHECKING

from agentiam_controlplane.db.ledger import ledger_commit
from agentiam_controlplane.errors import LeaseNotActiveError

if TYPE_CHECKING:
    import uuid
    from datetime import datetime
    from decimal import Decimal

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

__all__ = ["LedgerSettlementSink"]


class LedgerSettlementSink:
    """Applies one settlement per call, on its own session."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Hold the factory; a session is taken per settlement and released after."""
        self._session_factory = session_factory
        self.rejected_leases = 0

    async def commit(
        self,
        *,
        lease_id: uuid.UUID,
        reservation_id: uuid.UUID,
        amount: Decimal,
        now: datetime,
    ) -> bool:
        """Apply one settlement.

        Returns:
            `True` if the ledger applied it, `False` if the ledger declined it for good —
            a replay, a zero clamp, or a lease that is no longer active.

        Raises:
            Exception: The database could not be reached. The queue retries.
        """
        async with self._session_factory() as session:
            try:
                return await ledger_commit(
                    session,
                    lease_id=lease_id,
                    reservation_id=reservation_id,
                    amount=amount,
                    now=now,
                )
            except LeaseNotActiveError:
                # Already recorded as a reconciliation anomaly by `ledger_commit` itself.
                # Retrying would add a second anomaly for one event and never apply.
                self.rejected_leases += 1
                logger.warning(
                    "settlement %s rejected: lease %s is no longer active (spec 04 §11)",
                    reservation_id,
                    lease_id,
                )
                return False
