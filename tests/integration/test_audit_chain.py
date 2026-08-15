"""The audit hash chain against real Postgres — spec 08, NFR-6, TM-12.

*Chain of custody* is half the project's one-sentence pitch, and a decision record that can be
edited afterwards answers nothing. These tests are what make that sentence checkable.

Three of them matter more than the rest:

* `test_tampering_with_a_record_is_detected` — NFR-6 itself.
* `test_concurrent_appends_serialize` and its companion that **removes the lock and watches
  the chain break**, because a guard never seen to fire is not a guard (spec 08 §4).
* `test_head_truncation_is_detected` — the failure a hash chain cannot see on its own, and the
  reason `audit_chain_head` exists at all.
"""

from __future__ import annotations

import asyncio
import itertools
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import delete, select, text, update

from agentiam_controlplane.db.audit import append, custody, verify_chain
from agentiam_controlplane.db.base import make_session_factory
from agentiam_controlplane.db.models import AuditChainHeadRow, AuditRecordRow
from agentiam_core.errors import ReasonCode
from agentiam_core.models import Budget, DecisionRecord, Outcome

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


def a_record(*, task_id: uuid.UUID | None = None, scope: str = "invoice:read") -> DecisionRecord:
    return DecisionRecord(
        decision_id=uuid.uuid4(),
        trace_id="trace-1",
        timestamp=NOW,
        pep_id="pep-1",
        token_chain_ids=["blk_root"],
        principal_id="kc:alice",
        task_id=task_id or uuid.uuid4(),
        agent_id="agt-1",
        depth=1,
        scope=scope,
        tool_id="invoice_api",
        arg_digest="a" * 64,
        outcome=Outcome.ALLOW,
        reason_code=ReasonCode.OK,
        policy_version="bundle-1",
        budget_before=Budget(spend_bdt=Decimal(100)),
        budget_after=Budget(spend_bdt=Decimal(90)),
        latency_us=12,
    )


class TestAppend:
    async def test_a_batch_is_chained_in_order(self, session: AsyncSession) -> None:
        records = [a_record() for _ in range(5)]
        hashes = await append(session, records, now=NOW)

        assert len(hashes) == 5
        rows = (
            (await session.execute(select(AuditRecordRow).order_by(AuditRecordRow.seq)))
            .scalars()
            .all()
        )
        assert [row.seq for row in rows] == [1, 2, 3, 4, 5]
        assert rows[0].prev_hash is None, "genesis has no predecessor"
        for earlier, later in itertools.pairwise(rows):
            assert later.prev_hash == earlier.record_hash

    async def test_the_head_tracks_the_chain(self, session: AsyncSession) -> None:
        await append(session, [a_record() for _ in range(3)], now=NOW)
        head = (
            await session.execute(select(AuditChainHeadRow).where(AuditChainHeadRow.id == 1))
        ).scalar_one()
        last = (
            await session.execute(
                select(AuditRecordRow).order_by(AuditRecordRow.seq.desc()).limit(1)
            )
        ).scalar_one()
        assert head.last_seq == last.seq == 3
        assert head.last_hash == last.record_hash

    async def test_appending_nothing_is_a_no_op(self, session: AsyncSession) -> None:
        assert await append(session, [], now=NOW) == []

    async def test_a_repeated_decision_id_is_not_appended_twice(
        self, session: AsyncSession
    ) -> None:
        """T-022 retries failed batches rather than dropping them, so this happens."""
        record = a_record()
        await append(session, [record], now=NOW)
        second = await append(session, [record], now=NOW)

        assert second == [], "a duplicate must append nothing"
        count = (await session.execute(select(AuditRecordRow))).scalars().all()
        assert len(count) == 1

    async def test_a_partly_duplicate_batch_appends_only_the_new_records(
        self, session: AsyncSession
    ) -> None:
        first, second = a_record(), a_record()
        await append(session, [first], now=NOW)
        appended = await append(session, [first, second], now=NOW)

        assert len(appended) == 1
        assert (await verify_chain(session)).ok, "the chain must survive a partial retry"

    async def test_records_carry_no_argument_values(self, session: AsyncSession) -> None:
        """NFR-5 and TM-13 — the audit table must not become a PII store."""
        await append(session, [a_record()], now=NOW)
        row = (await session.execute(select(AuditRecordRow))).scalar_one()
        assert row.record["arg_digest"] == "a" * 64
        assert "args" not in row.record


class TestVerification:
    async def test_an_intact_chain_verifies(self, session: AsyncSession) -> None:
        await append(session, [a_record() for _ in range(4)], now=NOW)
        result = await verify_chain(session)
        assert result.ok
        assert result.checked == 4

    async def test_an_empty_chain_verifies(self, session: AsyncSession) -> None:
        result = await verify_chain(session)
        assert result.ok
        assert result.checked == 0

    async def test_tampering_with_a_record_is_detected(self, session: AsyncSession) -> None:
        """NFR-6. The single most load-bearing test in this module."""
        await append(session, [a_record() for _ in range(5)], now=NOW)

        async with session.begin():
            await session.execute(
                update(AuditRecordRow)
                .where(AuditRecordRow.seq == 3)
                .values(record=text("jsonb_set(record, '{scope}', '\"payment:initiate\"')"))
            )

        result = await verify_chain(session)
        assert not result.ok
        assert result.first_bad_seq == 3, "NFR-6 requires the *first* inconsistent seq"
        assert "altered" in result.detail

    async def test_deleting_a_record_from_the_middle_is_detected(
        self, session: AsyncSession
    ) -> None:
        await append(session, [a_record() for _ in range(5)], now=NOW)
        async with session.begin():
            await session.execute(delete(AuditRecordRow).where(AuditRecordRow.seq == 3))

        result = await verify_chain(session)
        assert not result.ok
        assert result.first_bad_seq == 4

    async def test_head_truncation_is_detected(self, session: AsyncSession) -> None:
        """The failure a hash chain cannot see on its own — spec 08 §5.

        Deleting the newest records leaves a chain that verifies perfectly from genesis. Only
        the head row remembers they were there, which is the whole reason it is not simply
        `max(seq)`.
        """
        await append(session, [a_record() for _ in range(5)], now=NOW)
        async with session.begin():
            await session.execute(delete(AuditRecordRow).where(AuditRecordRow.seq > 3))

        result = await verify_chain(session)
        assert not result.ok
        assert "truncated" in result.detail
        assert result.first_bad_seq == 4

    async def test_a_reordered_prev_hash_is_detected(self, session: AsyncSession) -> None:
        await append(session, [a_record() for _ in range(4)], now=NOW)
        async with session.begin():
            await session.execute(
                update(AuditRecordRow).where(AuditRecordRow.seq == 3).values(prev_hash="f" * 64)
            )

        result = await verify_chain(session)
        assert not result.ok
        assert result.first_bad_seq == 3


class TestConcurrentAppendSerializes:
    """Spec 08 §4 — and the guard is shown to fire, per the T-013 precedent."""

    async def test_concurrent_appends_produce_one_intact_chain(
        self, migrated_engine: AsyncEngine
    ) -> None:
        factory = make_session_factory(migrated_engine)

        async def writer() -> None:
            async with factory() as own:
                await append(own, [a_record() for _ in range(3)], now=NOW)

        await asyncio.gather(*(writer() for _ in range(8)))

        async with factory() as session:
            result = await verify_chain(session)
            rows = (await session.execute(select(AuditRecordRow))).scalars().all()

        assert result.ok, f"the chain broke under concurrency: {result}"
        assert len(rows) == 24, "every record must be present, not merely consistent"

    async def test_without_the_lock_the_chain_breaks(self, migrated_engine: AsyncEngine) -> None:
        """Remove the serialization and watch it fail, so the lock is not taken on faith.

        This is the same demonstration T-013 made for `ACQUIRE`'s `FOR UPDATE`: the guard is
        only worth something if its absence is observable.

        Reading the head *without* `FOR UPDATE` lets two writers see the same `last_seq`, and
        they then collide on the primary key. The surviving evidence is a chain with fewer
        records than were submitted — an absence, which no hash can detect (spec 08 §4).
        """
        factory = make_session_factory(migrated_engine)
        submitted = 8

        async def unlocked_writer() -> bool:
            async with factory() as own:
                try:
                    async with own.begin():
                        head = (
                            await own.execute(
                                select(AuditChainHeadRow).where(AuditChainHeadRow.id == 1)
                            )
                        ).scalar_one()  # deliberately no .with_for_update()
                        await asyncio.sleep(0.02)  # widen the window the lock would close
                        record = a_record()
                        body = record.model_dump(mode="json")
                        own.add(
                            AuditRecordRow(
                                seq=head.last_seq + 1,
                                decision_id=record.decision_id,
                                record=body,
                                prev_hash=head.last_hash,
                                record_hash="0" * 64,
                                created_at=NOW,
                            )
                        )
                        head.last_seq += 1
                except Exception:  # a collision is the expected outcome here
                    return False
                return True

        results = await asyncio.gather(*(unlocked_writer() for _ in range(submitted)))

        async with factory() as session:
            rows = (await session.execute(select(AuditRecordRow))).scalars().all()

        assert sum(results) < submitted, (
            "without the lock, concurrent appends must collide — if they all succeeded the "
            "window was too narrow and this test proves nothing"
        )
        assert len(rows) < submitted, (
            f"{len(rows)} of {submitted} records survived; the lost ones are exactly the "
            f"absence a hash chain cannot detect"
        )


class TestCustody:
    async def test_custody_returns_one_task_in_order(self, session: AsyncSession) -> None:
        task = uuid.uuid4()
        other = uuid.uuid4()
        await append(
            session,
            [
                a_record(task_id=task, scope="invoice:read"),
                a_record(task_id=other, scope="vendor:read"),
                a_record(task_id=task, scope="payment:initiate"),
            ],
            now=NOW,
        )

        entries = await custody(session, task_id=task)

        assert [e.scope for e in entries] == ["invoice:read", "payment:initiate"]
        assert [e.seq for e in entries] == [1, 3]

    async def test_custody_of_an_unknown_task_is_empty(self, session: AsyncSession) -> None:
        await append(session, [a_record()], now=NOW)
        assert await custody(session, task_id=uuid.uuid4()) == []

    async def test_custody_projects_the_fields_the_console_reads(
        self, session: AsyncSession
    ) -> None:
        task = uuid.uuid4()
        await append(session, [a_record(task_id=task)], now=NOW)
        entry = (await custody(session, task_id=task))[0]
        assert entry.agent_id == "agt-1"
        assert entry.depth == 1
        assert entry.outcome == Outcome.ALLOW.value
        assert entry.reason_code == ReasonCode.OK.value


class TestEmitterToLedger:
    """T-022's emitter against the real chain — the seam T-023 depends on."""

    async def test_emitted_records_reach_the_chain(self, migrated_engine: AsyncEngine) -> None:
        from agentiam_controlplane.db.audit_sink import LedgerAuditSink
        from agentiam_pep.emitter import DecisionEmitter, EmitterSettings

        sink = LedgerAuditSink(make_session_factory(migrated_engine))
        emitter = DecisionEmitter(sink, EmitterSettings(capacity=64, flush_interval_s=0.01))

        async with emitter:
            for _ in range(3):
                emitter.emit(a_record())
            await emitter.flush()

        factory = make_session_factory(migrated_engine)
        async with factory() as session:
            result = await verify_chain(session)
            rows = (await session.execute(select(AuditRecordRow))).scalars().all()

        assert len(rows) == 3
        assert result.ok

    async def test_a_failing_ledger_is_retried_not_dropped(
        self, migrated_engine: AsyncEngine
    ) -> None:
        """ADR-026's claim, end to end: a broken sink must not silently lose records."""
        from agentiam_controlplane.db.audit_sink import LedgerAuditSink
        from agentiam_pep.emitter import DecisionEmitter, EmitterSettings

        real = LedgerAuditSink(make_session_factory(migrated_engine))
        attempts = {"n": 0}

        class FlakySink:
            async def write(self, batch: object) -> None:
                attempts["n"] += 1
                if attempts["n"] == 1:
                    raise RuntimeError("ledger is down")
                await real.write(batch)  # type: ignore[arg-type]

        emitter = DecisionEmitter(FlakySink(), EmitterSettings(capacity=64, flush_interval_s=0.01))
        async with emitter:
            emitter.emit(a_record())
            await emitter.flush()  # fails
            await emitter.flush()  # retries the same batch

        factory = make_session_factory(migrated_engine)
        async with factory() as session:
            rows = (await session.execute(select(AuditRecordRow))).scalars().all()

        assert len(rows) == 1, "the record survived a transient ledger failure"
        assert emitter.failed_batches == 1
        assert emitter.lost_records == 0
