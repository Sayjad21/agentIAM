"""The `EscalationSink` that connects a PEP's `ESCALATE` outcome to the queue — T-037.

Lives here, not in the PEP, for the same reason `LedgerAuditSink` does (`audit_sink.py`): it
owns a database session, and the PEP's contract is that its hot decision path never touches
one. Structurally satisfies `agentiam_pep.escalation_sink.EscalationSink` — a `Protocol`, so
nothing here imports from `agentiam_pep`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentiam_controlplane.db.escalations import create

if TYPE_CHECKING:
    from datetime import datetime, timedelta
    from decimal import Decimal
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from agentiam_core.escalation import Escalation

__all__ = ["LedgerEscalationSink"]


class LedgerEscalationSink:
    """Opens a pending escalation, one session per call."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Hold the factory; a session is taken per write and released after."""
        self._session_factory = session_factory

    async def create(
        self,
        *,
        decision_id: UUID,
        task_id: UUID,
        agent_id: str,
        principal_id: str,
        intent_hash: str,
        requested_scopes: frozenset[str],
        requested_amount: Decimal,
        reason: str,
        now: datetime,
        ttl: timedelta,
    ) -> Escalation:
        """Persist a new pending escalation and return it, id included."""
        async with self._session_factory() as session:
            return await create(
                session,
                decision_id=decision_id,
                task_id=task_id,
                agent_id=agent_id,
                principal_id=principal_id,
                intent_hash=intent_hash,
                requested_scopes=requested_scopes,
                requested_amount=requested_amount,
                reason=reason,
                now=now,
                ttl=ttl,
            )
