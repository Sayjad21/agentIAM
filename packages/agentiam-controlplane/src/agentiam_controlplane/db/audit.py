"""The audit hash chain — spec 08, NFR-6, TM-12.

Implements [`docs/specs/08-audit-chain.md`](../../../../../docs/specs/08-audit-chain.md).

*The credit limit and chain of custody for AI agents.* This module is the second half of that
sentence: a decision record that can be edited afterwards answers nothing.

The link is `hashing.chain_hash()` from T-005 — the previous hash is bound **inside** the
hashed structure, so altering record *n* changes every hash after it.

**Appending is serialized on a single head row**, and that is load-bearing rather than
tidiness. Appending is a read-modify-write: two concurrent appends that both read head *h*
both claim `prev_hash = h`, and one of them loses its `seq` to a primary-key collision. A
record that was never written breaks no hash — nothing in a chain detects an absence
(spec 08 §4). `tests/integration/test_audit_chain.py` removes the lock and watches it happen.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from agentiam_controlplane.db.models import AuditChainHeadRow, AuditRecordRow
from agentiam_core.hashing import chain_hash

if TYPE_CHECKING:
    import uuid
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from agentiam_core.models import DecisionRecord

__all__ = ["ChainVerification", "CustodyEntry", "append", "custody", "verify_chain"]


class ChainVerification:
    """What `verify_chain()` found."""

    __slots__ = ("checked", "detail", "first_bad_seq", "ok")

    def __init__(
        self,
        *,
        ok: bool,
        checked: int,
        first_bad_seq: int | None = None,
        detail: str = "",
    ) -> None:
        """Record the outcome and, when broken, where."""
        self.ok = ok
        self.checked = checked
        self.first_bad_seq = first_bad_seq
        self.detail = detail

    def __repr__(self) -> str:
        """Readable in a test failure and in the CLI's output."""
        if self.ok:
            return f"ChainVerification(ok=True, checked={self.checked})"
        return (
            f"ChainVerification(ok=False, checked={self.checked}, "
            f"first_bad_seq={self.first_bad_seq}, detail={self.detail!r})"
        )


class CustodyEntry:
    """One decision in a task's chain of custody — spec 08 §6."""

    __slots__ = ("agent_id", "depth", "outcome", "reason_code", "record", "scope", "seq")

    def __init__(self, *, seq: int, record: dict[str, Any]) -> None:
        """Project the fields the console and the demo actually read."""
        self.seq = seq
        self.record = record
        self.agent_id = str(record.get("agent_id", ""))
        self.depth = int(record.get("depth", 0))
        self.scope = str(record.get("scope", ""))
        self.outcome = str(record.get("outcome", ""))
        self.reason_code = str(record.get("reason_code", ""))


async def _head(session: AsyncSession, *, lock: bool = True) -> AuditChainHeadRow:
    """Fetch the singleton head row, locking it unless a caller only wants to read.

    Creates it if absent, which makes the first append on a fresh database work without a
    seeding step in the migration.
    """
    statement = select(AuditChainHeadRow).where(AuditChainHeadRow.id == 1)
    if lock:
        statement = statement.with_for_update()
    head = (await session.execute(statement)).scalar_one_or_none()
    if head is None:
        head = AuditChainHeadRow(id=1, last_seq=0, last_hash=None)
        session.add(head)
        await session.flush()
    return head


async def append(
    session: AsyncSession,
    records: Sequence[DecisionRecord],
    *,
    now: datetime | None = None,
) -> list[str]:
    """Append a batch of decision records to the chain.

    The whole batch goes in under one lock on the head row, which is what makes `seq`
    monotonic and `prev_hash` correct under concurrency (spec 08 §4). Batching amortises that
    lock: T-022 delivers up to 64 records per write, so it is taken once per batch rather than
    once per decision.

    Idempotent by `decision_id`: a record already in the chain is skipped rather than appended
    twice, because T-022 retries failed batches rather than dropping them.

    Args:
        session: A session with no transaction already open — this opens its own.
        records: The batch, in the order they should be chained.
        now: The ledger's clock, for `created_at`. Defaults to the real one.

    Returns:
        The `record_hash` of each record appended, in order. Skipped duplicates are omitted.
    """
    if not records:
        return []

    stamped = now or datetime.now(UTC)
    hashes: list[str] = []

    async with session.begin():
        head = await _head(session)

        existing = set(
            (
                await session.execute(
                    select(AuditRecordRow.decision_id).where(
                        AuditRecordRow.decision_id.in_([r.decision_id for r in records])
                    )
                )
            )
            .scalars()
            .all()
        )

        last_seq = head.last_seq
        last_hash = head.last_hash
        for record in records:
            if record.decision_id in existing:
                continue
            body = record.model_dump(mode="json")
            record_hash = chain_hash(last_hash, body)
            last_seq += 1
            session.add(
                AuditRecordRow(
                    seq=last_seq,
                    decision_id=record.decision_id,
                    record=body,
                    prev_hash=last_hash,
                    record_hash=record_hash,
                    created_at=stamped,
                )
            )
            last_hash = record_hash
            hashes.append(record_hash)

        head.last_seq = last_seq
        head.last_hash = last_hash

    return hashes


async def verify_chain(session: AsyncSession) -> ChainVerification:
    """Walk the chain in `seq` order and recompute every link — spec 08 §5, NFR-6.

    Names the **first** inconsistent seq rather than reporting "invalid": everything after a
    break is untrustworthy by construction, so listing further breaks is noise, and an
    operator holding a broken chain needs somewhere to look.

    Also compares `max(seq)` with the head row, which is the only evidence of head truncation
    — deleting the newest records leaves a chain that verifies perfectly (spec 08 §5).
    """
    rows = (
        (await session.execute(select(AuditRecordRow).order_by(AuditRecordRow.seq))).scalars().all()
    )

    previous_hash: str | None = None
    previous_seq = 0
    for row in rows:
        if row.seq != previous_seq + 1:
            return ChainVerification(
                ok=False,
                checked=previous_seq,
                first_bad_seq=row.seq,
                detail=f"sequence jumps from {previous_seq} to {row.seq}; a record is missing",
            )
        if row.prev_hash != previous_hash:
            return ChainVerification(
                ok=False,
                checked=previous_seq,
                first_bad_seq=row.seq,
                detail="prev_hash does not match the preceding record's hash",
            )
        recomputed = chain_hash(previous_hash, row.record)
        if recomputed != row.record_hash:
            return ChainVerification(
                ok=False,
                checked=previous_seq,
                first_bad_seq=row.seq,
                detail="the record does not hash to its stored record_hash; it was altered",
            )
        previous_hash = row.record_hash
        previous_seq = row.seq

    head = await _head(session, lock=False)
    highest = (await session.execute(select(func.max(AuditRecordRow.seq)))).scalar() or 0
    if head.last_seq != highest:
        return ChainVerification(
            ok=False,
            checked=previous_seq,
            first_bad_seq=highest + 1,
            detail=(
                f"the head records {head.last_seq} records but only {highest} are present; "
                f"the chain has been truncated"
            ),
        )

    return ChainVerification(ok=True, checked=previous_seq)


async def custody(session: AsyncSession, *, task_id: uuid.UUID) -> list[CustodyEntry]:
    """Every decision recorded for one task, oldest first — spec 08 §6.

    The query behind *who authorized this payment?*. It is an ordinary read; the chain is what
    makes the answer trustworthy, not a separate structure.
    """
    rows = (
        (
            await session.execute(
                select(AuditRecordRow)
                .where(AuditRecordRow.record["task_id"].astext == str(task_id))
                .order_by(AuditRecordRow.seq)
            )
        )
        .scalars()
        .all()
    )
    return [CustodyEntry(seq=row.seq, record=row.record) for row in rows]
