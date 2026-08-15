"""The `RecordSink` that connects T-022's emitter to the audit chain — spec 08.

The emitter (`agentiam_pep.emitter`) is deliberately ignorant of where records go: it buffers,
batches, retries and refuses, and hands a `Sequence[DecisionRecord]` to whatever satisfies
`RecordSink`. This is the production one.

It lives in the control plane rather than the PEP because it owns a database session, and the
PEP's contract is that its hot path never touches one. The emitter's drain task is off that
path, which is exactly why the sink can be here.

A failed write **raises**, which is what the emitter needs: T-022 retries a failed batch rather
than dropping it, so a sink that swallowed its own errors would turn a broken ledger into
silently lost audit records (ADR-026).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentiam_controlplane.db.audit import append

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from agentiam_core.models import DecisionRecord

__all__ = ["LedgerAuditSink"]


class LedgerAuditSink:
    """Appends batches to the audit chain, one session per batch."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Hold the factory; a session is taken per write and released after."""
        self._session_factory = session_factory

    async def write(self, batch: Sequence[DecisionRecord]) -> None:
        """Append `batch` to the chain under one lock on the head row.

        Raises:
            Exception: Whatever the database raised. The emitter counts the failure and
                retries the batch; swallowing it here would lose the records.
        """
        async with self._session_factory() as session:
            await append(session, batch)
