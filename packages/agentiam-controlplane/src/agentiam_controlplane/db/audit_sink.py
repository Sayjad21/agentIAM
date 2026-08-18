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

**But *how* it raises decides whether the batch is ever retried**, and only this module can
make that call, because only this module knows what a given database error means. The
emitter retries anything it does not recognise, indefinitely; `SinkRejectedRecord` is the
one signal that says *stop, this will never work*. So the split here is deliberate and
narrow:

* `DataError`, `IntegrityError`, `ProgrammingError` — the statement itself is wrong for this
  content. A retry produces the identical error forever, so these become
  `SinkRejectedRecord` and the batch is dropped, loudly and counted.
* everything else, `OperationalError` and `InterfaceError` above all — the database is
  unreachable, restarting, or out of connections. Propagated unchanged, so the emitter keeps
  the batch and tries again when it comes back.

Getting that split backwards is the T-052 defect in miniature: treating an outage as
permanent loses records, and treating a permanent rejection as an outage wedges the queue
until the PEP denies everything.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy.exc import DataError, IntegrityError, ProgrammingError

from agentiam_controlplane.db.audit import append
from agentiam_core.errors import SinkRejectedRecord

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from agentiam_core.models import DecisionRecord

logger = logging.getLogger(__name__)

__all__ = ["LedgerAuditSink"]

#: Errors that mean *this batch*, not *this database*. See the module docstring.
_PERMANENT = (DataError, IntegrityError, ProgrammingError)


class LedgerAuditSink:
    """Appends batches to the audit chain, one session per batch."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Hold the factory; a session is taken per write and released after."""
        self._session_factory = session_factory
        self.rejected_batches = 0

    async def write(self, batch: Sequence[DecisionRecord]) -> None:
        """Append `batch` to the chain under one lock on the head row.

        Raises:
            SinkRejectedRecord: The batch can never be written — a value the schema will
                not take, or a constraint it cannot satisfy. The emitter drops it.
            Exception: Whatever else the database raised, unchanged. The emitter keeps the
                batch and retries indefinitely; swallowing it here would lose the records.
        """
        try:
            async with self._session_factory() as session:
                await append(session, batch)
        except _PERMANENT as exc:
            self.rejected_batches += 1
            logger.error("audit batch of %d record(s) can never be written: %s", len(batch), exc)
            raise SinkRejectedRecord(
                f"the audit chain will not accept this batch of {len(batch)}: {exc}"
            ) from exc
