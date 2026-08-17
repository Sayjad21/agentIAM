"""The live decision stream against real Postgres — T-046.

Filtering happens in JSONB operators, so a fake session would prove nothing about the part
most likely to be wrong. The cursor behaviour matters just as much: a stream that loses
records when a burst exceeds its page size is worse than no stream, because it looks like it
is working.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from agentiam_controlplane import decisions_api
from agentiam_controlplane.app import create_app
from agentiam_controlplane.db.audit import append
from agentiam_controlplane.db.base import make_session_factory
from agentiam_controlplane.db.decisions import DecisionFilter, read_since
from agentiam_core.errors import ReasonCode
from agentiam_core.models import Budget, CaveatKind, CaveatRef, DecisionRecord, Outcome

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


def a_record(**over: Any) -> DecisionRecord:
    """One decision, built through the real model.

    Hand-inserting `AuditRecordRow`s was the first attempt and the chain rejected it:
    `ck_audit_records_genesis_only_seq_one` requires `prev_hash` to be NULL at seq 1 and
    non-NULL after, so rows fabricated with arbitrary hashes violate the invariant that
    stops a later record posing as a fresh genesis (spec 08). Going through `append()`
    means `seq` and `prev_hash` are the chain's own, and the fixture exercises the write
    path the PEP actually uses.
    """
    base: dict[str, Any] = {
        "decision_id": uuid.uuid4(),
        "trace_id": "trace-1",
        "timestamp": NOW,
        "pep_id": "pep-1",
        "token_chain_ids": ["b1"],
        "principal_id": "kc:alice",
        "task_id": uuid.uuid4(),
        "agent_id": "agt-1",
        "depth": 1,
        "scope": "invoice:read",
        "tool_id": "invoice_api",
        "arg_digest": "0" * 64,
        "outcome": Outcome.ALLOW,
        "reason_code": ReasonCode.OK,
        "reason_detail": "",
        "policy_version": "bundle-1",
        "budget_before": Budget(),
        "budget_after": Budget(),
        "latency_us": 4200,
    }
    merged = base | over

    # `DecisionRecord` enforces P-18: every deny names a cause, every allow claims none.
    # A fixture that says `outcome="deny"` and leaves `reason_code` at OK is rejected, and
    # rightly — so supply a plausible cause rather than making every caller repeat one.
    if str(merged["outcome"]) != Outcome.ALLOW and merged["reason_code"] is ReasonCode.OK:
        merged["reason_code"] = ReasonCode.POLICY_DENIED

    return DecisionRecord(**merged)


async def seed(engine: AsyncEngine, records: list[DecisionRecord]) -> None:
    """Append records to the chain, one batch, so `seq` starts at 1 and is contiguous."""
    async with make_session_factory(engine)() as session:
        await append(session, records)


class TestCursor:
    async def test_records_come_back_oldest_first(self, migrated_engine: AsyncEngine) -> None:
        # Oldest first so a reader can advance to the last element and lose nothing.
        # Newest-first reads better in a table and silently drops bursts.
        await seed(migrated_engine, [a_record(), a_record(), a_record()])
        async with make_session_factory(migrated_engine)() as session:
            events = await read_since(session)
        assert [e.seq for e in events] == [1, 2, 3]

    async def test_after_seq_excludes_what_was_already_seen(
        self, migrated_engine: AsyncEngine
    ) -> None:
        await seed(migrated_engine, [a_record(), a_record(), a_record()])
        async with make_session_factory(migrated_engine)() as session:
            events = await read_since(session, after_seq=2)
        assert [e.seq for e in events] == [3]

    async def test_a_burst_larger_than_the_limit_is_truncated_from_the_front(
        self, migrated_engine: AsyncEngine
    ) -> None:
        # The reason ordering matters: with oldest-first the caller keeps the earliest
        # unseen records and its cursor still advances, so the rest arrive next poll.
        await seed(migrated_engine, [a_record() for _ in range(5)])
        async with make_session_factory(migrated_engine)() as session:
            events = await read_since(session, limit=2)
        assert [e.seq for e in events] == [1, 2]


class TestFiltering:
    async def test_filtering_by_outcome(self, migrated_engine: AsyncEngine) -> None:
        await seed(
            migrated_engine,
            [a_record(), a_record(outcome="deny", reason_code="POLICY_DENIED"), a_record()],
        )
        async with make_session_factory(migrated_engine)() as session:
            events = await read_since(session, filters=DecisionFilter(outcome="deny"))
        assert [e.seq for e in events] == [2]

    async def test_filtering_by_agent(self, migrated_engine: AsyncEngine) -> None:
        await seed(migrated_engine, [a_record(), a_record(agent_id="agt-2")])
        async with make_session_factory(migrated_engine)() as session:
            events = await read_since(session, filters=DecisionFilter(agent_id="agt-2"))
        assert [e.seq for e in events] == [2]

    async def test_filtering_by_scope(self, migrated_engine: AsyncEngine) -> None:
        await seed(migrated_engine, [a_record(), a_record(scope="payment:initiate")])
        async with make_session_factory(migrated_engine)() as session:
            events = await read_since(session, filters=DecisionFilter(scope="payment:initiate"))
        assert [e.seq for e in events] == [2]

    async def test_filters_combine(self, migrated_engine: AsyncEngine) -> None:
        await seed(
            migrated_engine,
            [
                a_record(outcome="deny", agent_id="agt-1"),
                a_record(outcome="deny", agent_id="agt-2"),
                a_record(outcome="allow", agent_id="agt-2"),
            ],
        )
        async with make_session_factory(migrated_engine)() as session:
            events = await read_since(
                session, filters=DecisionFilter(outcome="deny", agent_id="agt-2")
            )
        assert [e.seq for e in events] == [2]

    async def test_an_empty_filter_value_matches_nothing(
        self, migrated_engine: AsyncEngine
    ) -> None:
        await seed(migrated_engine, [a_record()])
        async with make_session_factory(migrated_engine)() as session:
            events = await read_since(session, filters=DecisionFilter(agent_id=""))
        assert events == []


class TestTheFailingCaveatSurvivesTheRoundTrip:
    async def test_a_caveat_refusal_is_projected_with_its_kind_and_block(
        self, migrated_engine: AsyncEngine
    ) -> None:
        await seed(
            migrated_engine,
            [
                a_record(
                    outcome="deny",
                    reason_code=ReasonCode.BUDGET_EXHAUSTED_CAVEAT,
                    reason_detail="over ceiling",
                    failing_caveat=CaveatRef(
                        kind=CaveatKind.BUDGET_CEILING,
                        block_index=2,
                        detail="spend_bdt 60000 exceeds 50000",
                    ),
                )
            ],
        )
        async with make_session_factory(migrated_engine)() as session:
            (event,) = await read_since(session)

        assert event.failing_caveat_kind == "budget_ceiling"
        assert event.failing_caveat_block == 2
        assert "budget_ceiling" in event.explanation
        assert "block 2" in event.explanation
        assert "60000" in event.explanation


class TestTheEndpoints:
    @pytest.fixture
    async def client(self, migrated_engine: AsyncEngine) -> AsyncClient:
        app = create_app(session_factory=make_session_factory(migrated_engine))
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    async def test_the_page_returns_decisions_and_a_cursor(
        self, client: AsyncClient, migrated_engine: AsyncEngine
    ) -> None:
        await seed(migrated_engine, [a_record(), a_record()])
        async with client:
            resp = await client.get("/v1/decisions")
        assert resp.status_code == 200
        body = resp.json()
        assert [d["seq"] for d in body["decisions"]] == [1, 2]
        assert body["next_seq"] == 2

    async def test_the_cursor_is_echoed_when_nothing_matched(
        self, client: AsyncClient, migrated_engine: AsyncEngine
    ) -> None:
        # So a client polling a quiet feed does not rewind to zero and replay.
        await seed(migrated_engine, [a_record()])
        async with client:
            resp = await client.get("/v1/decisions", params={"after_seq": 9})
        assert resp.json() == {"decisions": [], "next_seq": 9}

    async def test_the_page_honours_filters(
        self, client: AsyncClient, migrated_engine: AsyncEngine
    ) -> None:
        await seed(migrated_engine, [a_record(), a_record(outcome="deny")])
        async with client:
            resp = await client.get("/v1/decisions", params={"outcome": "deny"})
        assert [d["seq"] for d in resp.json()["decisions"]] == [2]

    async def test_the_console_page_renders(self, client: AsyncClient) -> None:
        async with client:
            resp = await client.get("/decisions")
        assert resp.status_code == 200
        assert "Live decisions" in resp.text

    async def test_without_a_database_both_routes_report_503(self) -> None:
        # Same shape as the tree and the PEP's `enforcing` flag: unwired is visible rather
        # than a boot failure.
        app = create_app(session_factory=None)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/v1/decisions")).status_code == 503
            assert (await client.get("/v1/decisions/stream")).status_code == 503

    async def test_the_stream_sends_a_snapshot_then_recycles(
        self, client: AsyncClient, migrated_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A client connecting mid-demo is not blank, and the stream terminates.

        `MAX_STREAM_S` is squeezed to zero so the generator recycles immediately. That is
        what makes this assertable at all: reading one frame and breaking out leaves the
        generator suspended forever, and the whole test session hangs at teardown —
        `request.is_disconnected()` never fires under an in-process ASGI transport. That
        hang is exactly why T-045's SSE test is a bare `pass`.
        """
        monkeypatch.setattr(decisions_api, "MAX_STREAM_S", 0.0)
        await seed(migrated_engine, [a_record(outcome="deny", reason_code="POLICY_DENIED")])

        async with client, client.stream("GET", "/v1/decisions/stream") as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            body = "".join([chunk async for chunk in resp.aiter_text()])

        assert body.startswith("event: snapshot")
        assert "POLICY_DENIED" in body
        # And the reason it ended is stated rather than looking like a dropped connection.
        assert "event: recycle" in body
